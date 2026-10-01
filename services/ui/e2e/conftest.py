"""The browser half of the harness: one lock, one stack, one headless browser.

WHAT THIS PROTECTS, IN ORDER. The user's Mac first: a past run left 125 Chrome
processes holding 23.8 GB, so every rule below is about there being at most one
browser, never a visible one, and nothing left behind. Then the network: tests
talk to 127.0.0.1 and nowhere else. Then the tests themselves.

    one browser, machine-wide   MachineLock (fcntl.flock on WEBUI/browser.lock)
                                is taken before the stack or the browser starts
                                and released only after both are down. A second
                                session, from any agent, waits for it.
    never visible               Playwright's chromium-headless-shell, a bare
                                binary with no .app bundle, launched headless by
                                path. Nothing here can launch another browser,
                                and PWDEBUG (which would open a headed one with
                                the inspector) is removed from the driver's
                                environment.
    never heard                 --mute-audio. The page plays what it synthesises.
    never the network           --proxy-server at a dead loopback port, and a
                                resolver that knows no names, so nothing but
                                loopback can load; every request the page makes
                                is checked, and one to anything but 127.0.0.1
                                fails the test that made it.
    nothing left behind         the browser's profile is WEBUI/profiles/<pid>/...,
                                which is on its command line, and its helpers share
                                its process group. Teardown closes it, waits,
                                kills whatever is left, and FAILS the session if
                                anything had to be killed. The stack's children
                                are found by their marker the same way. An
                                earlier run's leftovers are swept at the start,
                                under the lock. SIGTERM, SIGHUP, exit and a 20
                                minute watchdog all run the same cleanup.

One browser per session, and one context and one page per test: a context is a
fresh profile in all but name (storage, cookies, cache, permissions), which is
the isolation a test needs, at a fraction of a browser's cost.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stack as st  # noqa: E402

VIEWPORTS = {"desktop": {"width": 1440, "height": 900}, "mobile": {"width": 390, "height": 844}}
SESSION_SECONDS = st.SESSION_SECONDS
# What the teardown prints, filled in as the session closes.
SUMMARY: dict[str, Any] = {}


def headless_shell() -> Path:
    """The chromium-headless-shell binary for the installed Playwright, from the
    cache it already has. Never downloaded here: a missing binary stops the run
    with the one command that installs only this, and never the full browser."""
    import playwright

    listing = json.loads((Path(playwright.__file__).parent / "driver/package/browsers.json").read_text())
    revision = next(b["revision"] for b in listing["browsers"] if b["name"] == "chromium-headless-shell")
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / "Library/Caches/ms-playwright")
    found = sorted((root / f"chromium_headless_shell-{revision}").glob("chrome-headless-shell-*/chrome-headless-shell"))
    if not found:
        pytest.exit(f"chromium-headless-shell {revision} is not in {root}. Install only it with: "
                    f"{sys.executable} -m playwright install chromium-headless-shell", returncode=4)
    return found[0]


def browser_args() -> list[str]:
    return [
        "--mute-audio", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions",
        # NOTHING BUT LOOPBACK. Chrome sends loopback direct whatever the proxy
        # says, and everything else to a port where nothing listens; the
        # resolver rule makes every name but localhost unresolvable as well.
        "--proxy-server=http://127.0.0.1:9",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE localhost , EXCLUDE 127.0.0.1",
        "--disable-background-networking", "--disable-component-update", "--disable-sync",
        "--disable-domain-reliability", "--no-pings",
        # A synthetic microphone that asks nobody: the page records reference
        # clips and dictation with getUserMedia, and the real microphone is
        # never opened.
        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
    ]


# ---- memory --------------------------------------------------------------------------


class Monitor(threading.Thread):
    """Samples the resident size of the browser's process group and of the
    stack's processes once a second, keeping the peaks."""

    def __init__(self, session: E2ESession) -> None:
        super().__init__(name="e2e-memory", daemon=True)
        self.session = session
        self.stopping = threading.Event()
        self.peak = {"browser_mb": 0.0, "browser_processes": 0, "stack_mb": 0.0, "samples": 0}

    def run(self) -> None:
        while not self.stopping.wait(1.0):
            try:
                everything = st.processes()
            except Exception:
                continue
            groups = self.session.browser_groups()
            browser = [p for p in everything if p.pgid in groups]
            stack = [p for p in everything if st.is_stack(p, os.getpid())]
            self.peak["samples"] += 1
            self.peak["browser_mb"] = max(self.peak["browser_mb"], sum(p.rss_kb for p in browser) / 1024)
            self.peak["browser_processes"] = max(self.peak["browser_processes"], len(browser))
            self.peak["stack_mb"] = max(self.peak["stack_mb"], sum(p.rss_kb for p in stack) / 1024)


# ---- the session ---------------------------------------------------------------------


class E2ESession:
    def __init__(self) -> None:
        self.lock = st.MachineLock()
        self.stack: st.Stack | None = None
        self.profile = st.PROFILES / str(os.getpid())
        self.playwright = None
        self.browser = None
        self.monitor = Monitor(self)
        self.timer: threading.Timer | None = None
        self.leaks: list[str] = []
        self.swept: list[st.Proc] = []
        self._groups: set[int] = set()

    def open(self) -> None:
        self.lock.acquire()
        st.on_emergency(self.emergency)
        self.swept = st.sweep_orphans()
        self.timer = st.watchdog(SESSION_SECONDS, "the browser session")
        self.stack = st.Stack(deadline=SESSION_SECONDS).start()
        self.monitor.start()

    def browser_groups(self) -> set[int]:
        if self.browser is not None and not self._groups:
            _, protected = st.untouchable()
            self._groups = {p.pgid for p in st.processes() if st.is_browser(p, os.getpid())} - protected
        return set(self._groups)

    def launch(self):
        """The one browser of this session, launched on first use."""
        if self.browser is not None:
            return self.browser
        from playwright.sync_api import sync_playwright

        executable = headless_shell()
        self.profile.mkdir(parents=True, exist_ok=True)
        # THE PROFILE LIVES WHERE THE DRIVER'S TMPDIR POINTS. Playwright refuses
        # --user-data-dir on launch() and makes its own profile under the
        # driver's temporary directory, so the driver is started with TMPDIR set
        # to this session's directory: the profile, and every temporary file
        # the browser writes, land in PROFILES/<pid>/, which is the marker the
        # leak check looks for. PWDEBUG is dropped because it makes Playwright
        # launch a headed browser with its inspector.
        saved = {k: os.environ.get(k) for k in ("TMPDIR", "PWDEBUG")}
        os.environ["TMPDIR"] = str(self.profile)
        os.environ.pop("PWDEBUG", None)
        try:
            self.playwright = sync_playwright().start()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.browser = self.playwright.chromium.launch(
            executable_path=str(executable), headless=True, args=browser_args(), timeout=30_000)
        SUMMARY["browser"] = f"{executable} {self.browser.version}"
        return self.browser

    def close(self) -> None:
        """Browser, then stack, then the leak check, then the lock."""
        try:
            self._close_browser()
            if self.stack is not None:
                self.stack.stop()
                survivors = self.stack.survivors()
                if survivors:
                    self.leaks += [f"stack pid {p.pid}: {p.command[:160]}" for p in survivors]
                    st.kill_all(survivors)
                SUMMARY["violations"] = self.stack.violations()
                SUMMARY["stack"] = self.stack.describe()
            remaining = st.harness_processes(owner=os.getpid())
            if remaining:
                self.leaks += [f"pid {p.pid}: {p.command[:160]}" for p in remaining]
                st.kill_all(remaining)
        finally:
            self.monitor.stopping.set()
            if self.timer is not None:
                self.timer.cancel()
            shutil.rmtree(self.profile, ignore_errors=True)
            SUMMARY["memory"] = dict(self.monitor.peak)
            SUMMARY["leaks"] = list(self.leaks)
            SUMMARY["swept"] = [f"pid {p.pid}: {p.command[:120]}" for p in self.swept]
            if self.stack is not None:
                with open(self.stack.run / "session.json", "w") as out:
                    json.dump({k: v for k, v in SUMMARY.items() if k != "stack"}, out, indent=2)
            self.lock.release()

    def _close_browser(self) -> None:
        groups = self.browser_groups()
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception as exc:
                self.leaks.append(f"browser.close() failed: {exc!r}")
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception as exc:
                self.leaks.append(f"playwright.stop() failed: {exc!r}")
        self.browser = self.playwright = None
        ends = time.monotonic() + 5.0
        left: list[st.Proc] = []
        while time.monotonic() < ends:
            left = [p for p in st.processes() if st.is_browser(p, os.getpid()) or p.pgid in groups]
            if not left:
                break
            time.sleep(0.2)
        if left:
            self.leaks += [f"browser pid {p.pid}: {p.command[:160]}" for p in left]
            st.kill_all(left)
        drivers = self._drivers()
        if drivers:
            self.leaks += [f"playwright driver pid {p.pid}" for p in drivers]
            st.kill_all(drivers)

    @staticmethod
    def _drivers() -> list[st.Proc]:
        """Playwright's node driver: this process's child, running its cli.js."""
        return [p for p in st.processes() if p.ppid == os.getpid()
                and "/playwright/driver/" in p.command.split(" ", 1)[0]]

    def emergency(self) -> None:
        """From a signal handler, atexit or the watchdog: no Playwright calls,
        which may be what is stuck. Kill by group and marker."""
        st.kill_all([p for p in st.processes() if st.is_browser(p, os.getpid()) or p.pgid in self._groups]
                    + self._drivers())
        if self.stack is not None:
            self.stack.stop()
        shutil.rmtree(self.profile, ignore_errors=True)
        self.lock.release()


# ---- per test ------------------------------------------------------------------------


class BrowserLog:
    """What the pages of one test did: every request, every failed one, every
    console error and uncaught exception. `outside` is any request to anything
    but loopback, which fails the test."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.console: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.outside: list[str] = []
        self.allowed: list[tuple[int, str]] = []

    def attach(self, page) -> None:
        def request(r) -> None:
            url = urlparse(r.url)
            if url.scheme in ("http", "https", "ws", "wss") and url.hostname not in ("127.0.0.1", "localhost"):
                self.outside.append(r.url)
            entry = {"t": time.time(), "method": r.method, "url": r.url, "path": url.path,
                     "query": url.query, "type": r.resource_type}
            if r.method not in ("GET", "HEAD"):
                data = r.post_data_buffer
                entry["bytes"] = len(data or b"")
                if data and "json" in (r.headers.get("content-type") or ""):
                    with contextlib.suppress(ValueError):
                        entry["json"] = json.loads(data)
            self.requests.append(entry)

        page.on("request", request)
        page.on("requestfailed", lambda r: self.failed.append(
            {"method": r.method, "url": r.url, "error": r.failure}))
        page.on("response", lambda r: self.responses.append(
            {"status": r.status, "method": r.request.method, "url": r.url, "path": urlparse(r.url).path}))
        page.on("console", lambda m: self.console.append({"type": m.type, "text": m.text})
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.errors.append(str(e)))

    def sent(self, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        """Requests the page made; `path` is a regular expression searched in the path."""
        return [r for r in self.requests if (method is None or r["method"] == method.upper())
                and (path is None or re.search(path, r["path"]))]

    def bad_responses(self, statuses=(404, 405)) -> list[dict[str, Any]]:
        return [r for r in self.responses if r["status"] in statuses or r["status"] >= 500]

    def allow(self, status: int, path: str) -> None:
        """A bad answer this test caused on purpose (a link to a job that is
        gone is a 404 the page asks for): `path` is a regular expression
        searched in the path. Everything else still fails the test."""
        self.allowed.append((status, path))

    def unexpected(self) -> list[dict[str, Any]]:
        """The bad answers no allow() accounts for."""
        return [r for r in self.bad_responses()
                if not any(r["status"] == status and re.search(path, r["path"])
                           for status, path in self.allowed)]


DENY_NOTIFICATIONS = """
if (window.Notification) {
  Object.defineProperty(Notification, "permission", { get: () => "denied", configurable: true });
  Notification.requestPermission = () => Promise.resolve("denied");
}
"""


class Pages:
    """New pages, each in a context of its own, all closed when the test ends."""

    def __init__(self, session: E2ESession, log: BrowserLog) -> None:
        self.session = session
        self.log = log
        self.contexts: list = []

    def new(self, viewport: str | tuple[int, int] = "desktop", *, scheme: str = "light",
            mobile: bool | None = None, reduced_motion: str = "no-preference",
            notifications: str = "denied", **options: Any):
        browser = self.session.launch()
        size = VIEWPORTS[viewport] if isinstance(viewport, str) else {"width": viewport[0], "height": viewport[1]}
        mobile = viewport == "mobile" if mobile is None else mobile
        permissions = ["microphone", "clipboard-read", "clipboard-write"]
        if notifications == "granted":
            permissions.append("notifications")
        context = browser.new_context(
            viewport=size, color_scheme=scheme, reduced_motion=reduced_motion,
            is_mobile=mobile, has_touch=mobile, device_scale_factor=2 if mobile else 1,
            locale="en-GB", timezone_id="Europe/London", service_workers="block",
            accept_downloads=True, permissions=permissions, **options)
        # NOTIFICATIONS ARE A NO THAT NOBODY IS ASKED FOR. Queueing a job asks
        # for the permission, and an unanswered request is a prompt the test
        # cannot see; "denied" is said by the page itself, before it runs.
        if notifications != "granted":
            context.add_init_script(DENY_NOTIFICATIONS)
        context.set_default_timeout(10_000)
        context.set_default_navigation_timeout(20_000)
        self.contexts.append(context)
        page = context.new_page()
        self.log.attach(page)
        return page

    def close(self) -> None:
        for context in self.contexts:
            with contextlib.suppress(Exception):
                context.close()
        self.contexts.clear()


# ---- fixtures ------------------------------------------------------------------------


@pytest.fixture(scope="session")
def e2e():
    session = E2ESession()
    try:
        session.open()
        yield session
    finally:
        session.close()
    if session.leaks:
        pytest.fail("leaked processes had to be killed:\n  " + "\n  ".join(session.leaks), pytrace=False)


@pytest.fixture(scope="session")
def stack(e2e) -> st.Stack:
    """The running stack: .url is the gateway (where the page is served),
    .ui_direct the page server's own port, .hub the hub, .restart_hub()."""
    return e2e.stack


@pytest.fixture
def fake(stack) -> st.FakeControl:
    """The fakes' control API (see stack.FakeControl). Failures injected by a
    test are cleared after it."""
    yield stack.fake
    stack.fake.clear_failures()


@pytest.fixture
def browser_log() -> BrowserLog:
    return BrowserLog()


@pytest.fixture
def new_page(e2e, browser_log):
    """new_page(viewport="desktop"|"mobile"|(w, h), scheme="light"|"dark",
    mobile=None, reduced_motion="no-preference", notifications="denied"|"granted")
    -> a Page in a new context."""
    pages = Pages(e2e, browser_log)
    yield pages.new
    pages.close()
    if browser_log.outside:
        pytest.fail("the page reached outside loopback:\n  " + "\n  ".join(browser_log.outside), pytrace=False)


@pytest.fixture
def page(new_page):
    """A desktop page (1440 x 900, light) in a fresh context."""
    return new_page()


@pytest.fixture
def fresh_hub(stack) -> st.Stack:
    """A hub with nothing on it but the three satellites as they start, for a
    test that changes what the hub holds: an adopt, a forget, a rename, a wake
    word, firmware, telemetry, a setting. Restarted before the test, so what
    an earlier test left is gone; a test that only reads uses the hub as the
    session left it."""
    stack.restart_hub()
    return stack


class Dialogs:
    """The page's confirm() and alert() answers. Without this Playwright
    dismisses every dialog, which is a No the test never chose."""

    def __init__(self, request) -> None:
        self.request = request
        self.seen: list[tuple[str, str]] = []

    def __call__(self, target=None, answer: bool | Callable[[Any], bool] = True) -> Dialogs:
        """dialogs(target=None, answer=True): answer every dialog on `target`
        (the default page) with Yes, No, or answer(dialog) -> bool, and record
        each one's type and message in .seen."""
        page = target or self.request.getfixturevalue("page")

        def on(dialog) -> None:
            self.seen.append((dialog.type, dialog.message))
            yes = answer(dialog) if callable(answer) else answer
            if yes:
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", on)
        return self


@pytest.fixture
def dialogs(request) -> Dialogs:
    return Dialogs(request)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Each phase's report on the test item, for the checks below to read."""
    report = yield
    setattr(item, "rep_" + report.when, report)
    return report


@pytest.fixture(autouse=True)
def page_stayed_healthy(request, browser_log):
    """WHAT EVERY TEST ASSERTS WITHOUT SAYING SO, after a test that passed: no
    uncaught exception on the page, no 404, 405 or 5xx the test did not cause
    on purpose (browser_log.allow), and no request that failed outright other
    than one cut short by the page or a navigation (ERR_ABORTED)."""
    yield
    call = getattr(request.node, "rep_call", None)
    if call is None or not call.passed:
        return
    problems = []
    if browser_log.errors:
        problems.append(f"uncaught errors on the page: {browser_log.errors}")
    if browser_log.unexpected():
        problems.append(f"missing routes or server errors: {browser_log.unexpected()}")
    broken = [f for f in browser_log.failed if "ERR_ABORTED" not in (f["error"] or "")]
    if broken:
        problems.append(f"requests that failed outright: {broken}")
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)


@pytest.fixture
def goto(stack, request) -> Callable[..., Any]:
    """goto(path="/ui", target=None): load a path of the page's origin (the
    gateway) in `target`, or in the default `page`, and wait for the load
    event. Not networkidle: the Satellites stream never goes idle."""
    def go(path: str = "/ui", target=None, wait_until: str = "load"):
        return (target or request.getfixturevalue("page")).goto(stack.url + path, wait_until=wait_until)
    return go


@pytest.fixture
def screenshot(request) -> Callable[..., Path]:
    """screenshot(name, viewport=None, scheme=None, full_page=False, target=None)
    -> the PNG's path, WEBUI/shots/<name>.png, of `target` or the default
    `page`. viewport resizes the page ("desktop", "mobile" or (w, h)); for real
    mobile emulation (touch, device pixels) make the page with
    new_page("mobile") instead. scheme switches prefers-color-scheme."""
    st.SHOTS.mkdir(parents=True, exist_ok=True)

    def shoot(name: str, viewport: str | tuple[int, int] | None = None, scheme: str | None = None,
              full_page: bool = False, target=None) -> Path:
        p = target or request.getfixturevalue("page")
        if viewport is not None:
            p.set_viewport_size(VIEWPORTS[viewport] if isinstance(viewport, str)
                                else {"width": viewport[0], "height": viewport[1]})
        if scheme is not None:
            p.emulate_media(color_scheme=scheme)
        path = st.SHOTS / (re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") + ".png")
        p.screenshot(path=str(path), full_page=full_page, animations="disabled", caret="hide")
        return path

    return shoot


# ---- the session's report ------------------------------------------------------------


def pytest_configure(config) -> None:
    if os.environ.get("CI"):
        pytest.exit("the browser tests never run in CI; they are for a developer's machine", returncode=0)


def pytest_terminal_summary(terminalreporter) -> None:
    if not SUMMARY:
        return
    write = terminalreporter.write_line
    terminalreporter.section("e2e session")
    memory = SUMMARY.get("memory", {})
    write(f"browser: {SUMMARY.get('browser', 'not started')}")
    write(f"peak memory: browser {memory.get('browser_mb', 0):.0f} MB in at most "
          f"{memory.get('browser_processes', 0)} processes; stack {memory.get('stack_mb', 0):.0f} MB "
          f"({memory.get('samples', 0)} samples)")
    write("leaks: " + ("none" if not SUMMARY.get("leaks") else "; ".join(SUMMARY["leaks"])))
    if SUMMARY.get("swept"):
        write("swept from an earlier run: " + "; ".join(SUMMARY["swept"]))
    violations = SUMMARY.get("violations") or []
    write(f"network: {len(violations)} outbound attempt(s) refused by the stack's guard"
          + ("" if not violations else ": " + "; ".join(violations[:5])))
    stack_info = SUMMARY.get("stack") or {}
    write(f"logs: {stack_info.get('run', '?')}   screenshots: {st.SHOTS}")
