"""The page behind a login: the session's refusals, its tabs, and its keys.

Two halves. The first reads ui.html against the code it restates -- the
gateway's tab table and voice_common's scopes and presets -- so neither side
can move alone: a speech account cannot read /admin/roles, so the New key form
carries its own copy of what a key may hold. The second runs the page's
session layer (the source between "the session" and "state" markers) in Node
with a fake fetch and a fake dialog, and asks what each refusal does. Without
`node` on PATH those skip, with the reason.

What they prevent:

  * a key form that offers a session-only scope, or a preset the gateway
    would refuse, or a year where the gateway caps the key at 90 days;
  * a tab drawn for a scope the gateway checks differently;
  * a password or a key built into markup, or sent by a form that could
    submit without the script;
  * a 401 that leaves the page half-working instead of sending the reader to
    sign in, or a backend's 401 that sends a live session round /login for
    ever, a step-up that is never asked for, a scope refusal that leaves the
    control live to be pressed again or greys one that did not ask, and a
    locked or silent gateway that looks like a page that works.
"""

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from voice_common import scopes

REPO = Path(__file__).resolve().parents[3]
PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html"
HTML = PAGE.read_text()
SCRIPT = HTML[HTML.index("<script>"):HTML.index("</script>")]
CODE = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.S)
NODE = shutil.which("node")


def page_object(name: str) -> dict:
    """A page `const NAME = {...};` of string keys and string values."""
    found = re.search(r"const " + name + r" = \{(.*?)\n\};", CODE, re.S)
    assert found, f"{name} is gone from the page"
    return dict(re.findall(r'"([^"]+)": "([^"]*)"', found.group(1)))


def page_list(name: str) -> list[str]:
    found = re.search(r"const " + name + r" = \[(.*?)\];", CODE, re.S)
    assert found, f"{name} is gone from the page"
    return re.findall(r'"([^"]+)"', found.group(1))


def page_function(name: str) -> str:
    found = re.search(r"\n(?:async )?function " + re.escape(name) + r"\(", CODE)
    assert found, f"{name}() is gone from the page"
    return CODE[found.start():CODE.index("\n}\n", found.start()) + 2]


# ---------------------------------------------------------------- the copies --


def test_the_key_form_offers_every_scope_a_person_may_hold_in_the_gateways_words():
    assert page_object("KEY_SCOPES") == {name: text for name, text in scopes.SCOPES.items()
                                         if name not in scopes.SERVICE_ONLY}


def test_no_session_only_scope_is_ever_offered_to_a_key():
    """D60: a key that held users:manage or keys:manage:own was a way round
    the password prompt, and a leaked one could mint keys that outlived it."""
    assert set(page_list("SESSION_ONLY_SCOPES")) == scopes.SESSION_ONLY
    draw = page_function("keyFormDraw")
    assert "holds(s) && !SESSION_ONLY_SCOPES.includes(s)" in draw


def test_the_key_presets_are_the_gateways_presets():
    found = re.search(r"const KEY_PRESETS = \{(.*?)\n\};", CODE, re.S)
    assert found
    page = {name: set(re.findall(r'"([a-z]+:[a-z]+(?::own|:all)?)"', body))
            for name, body in re.findall(r'"([a-z-]+)": (\[.*?\]|Object\.keys\(.*?\))', found.group(1), re.S)}
    # The admin preset is written as "every scope a person may hold, less the
    # session-only ones", which is how scopes.py defines it.
    assert 'Object.keys(KEY_SCOPES).filter(s => !SESSION_ONLY_SCOPES.includes(s))' in found.group(1)
    page["admin"] = (set(scopes.SCOPES) - scopes.SERVICE_ONLY) - scopes.SESSION_ONLY
    assert page == {name: set(preset.scopes) for name, preset in scopes.PRESETS.items()}


def test_a_scope_that_caps_a_key_takes_the_year_and_never_off_the_form():
    """Recheck M-5: the cap is EXPIRY_CAPPED, not "admin-only", so the
    home-assistant preset may still have a year."""
    assert set(page_list("EXPIRY_CAPPED_SCOPES")) == scopes.EXPIRY_CAPPED
    follow = page_function("keyExpiryFollow")
    assert '(capped && (option === "365" || option === "never"))' in follow
    assert 'capped ? "90" : "365"' in follow
    assert not set(scopes.PRESETS["home-assistant"].scopes) & scopes.EXPIRY_CAPPED


def test_an_admin_only_scope_takes_never_off_the_form():
    """The server refuses a never-expiring key that holds an admin-only scope."""
    assert set(page_list("ADMIN_ONLY_SCOPES")) == scopes.ADMIN_ONLY
    assert '(yearly && option === "never")' in page_function("keyExpiryFollow")


def test_each_tab_needs_the_scope_the_gateway_checks_for_its_address():
    main = ast.parse((REPO / "services" / "gateway" / "app" / "main.py").read_text())
    table = next(node.value for node in main.body if isinstance(node, ast.Assign)
                 and any(getattr(t, "id", "") == "PAGE_TABS" for t in node.targets))
    gateway = {"vocab" if tab == "vocabulary" else tab: scope
               for tab, scope in ast.literal_eval(table)}
    found = re.search(r"const TAB_SCOPE = \{(.*?)\};", CODE, re.S)
    assert found, "TAB_SCOPE is gone from the page"
    assert dict(re.findall(r'(\w+): "([^"]+)"', found.group(1))) == gateway


def test_every_row_on_the_account_and_admin_tabs_is_built_as_nodes():
    """Usernames, key names, browsers, audit targets and paths are typed by
    somebody, an attempted username by anybody on the internet (recheck L9):
    none of them goes through markup."""
    section = SCRIPT[SCRIPT.index("function make(tag, props, ...children) {"):
                     SCRIPT.index("/* ============================================================== live === */")]
    section = re.sub(r"/\*.*?\*/", "", section, flags=re.S)
    assert "innerHTML" not in section and "insertAdjacentHTML" not in section
    assert 'if (key.startsWith("on")) throw' in page_function("make")


def test_no_password_field_can_be_sent_by_a_form_without_the_script():
    """A form without its script submits by GET, and a named password field
    would land in the URL and every log on the way. None is named."""
    markup = HTML[HTML.index("<body>"):HTML.index("<script>")]
    for field in re.findall(r'<input[^>]*type="password"[^>]*>', markup):
        assert " name=" not in field, field
    for form in re.findall(r"<form[^>]*>", markup):
        assert "action=" not in form and "method=" not in form, form


def test_no_markup_the_page_writes_carries_an_inline_handler():
    """The policy names the page's one script by its hash (§4.7), and a hash
    does not cover an inline handler: an `onerror=` in the markup, or in a
    string the script turns into markup, would simply not run, and adding
    'unsafe-hashes' to let it would let an injected one run too. Handlers are
    attached by the script (addEventListener, or an element's property)."""
    markup = re.sub(r"<!--.*?-->", "", HTML[:HTML.index("<script>")], flags=re.S)
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", markup)
    strings = re.findall(r"`[^`]*`|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'", CODE)
    for text in strings:
        assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", text), text[:120]
    assert "javascript:" not in CODE


def test_storing_a_new_secret_under_a_name_already_stored_is_refused_not_merged():
    """A PUT keeps the fields it is not sent and overwrites the ones it is:
    this form on SATELLITES_HA_TOKEN with its hosts box empty replaced the
    token's hosts with none, and Home Assistant calls stopped with
    host_not_allowed. The name is looked up, fresh, before anything is sent."""
    handler = CODE[CODE.index('$("secret-new").addEventListener'):CODE.index('$("secret-rotate").addEventListener')]
    checked = handler.index("if (await secretRow(name)) {")
    assert checked < handler.index('method: "PUT"'), "the name is looked up after the PUT"
    assert "is already stored: use Replace or Bindings on its row." in handler[checked:handler.index("} else {", checked)]
    assert 'json("/admin/secrets")' in page_function("secretRow")


def test_the_page_never_keeps_a_key_or_a_password_it_was_shown():
    """A new key and a temporary password are shown once, in the DOM only,
    and Done takes them out; nothing is stored or logged."""
    once = page_function("showOnce")
    assert "host.replaceChildren(); host.hidden = true;" in once
    for name in ("showOnce", "userReset", "keyFormDraw"):
        body = page_function(name)
        assert "store.set" not in body and "console." not in body, name


# ------------------------------------------------------------ the session layer --

node = pytest.mark.skipif(NODE is None, reason="node is not on PATH; these drive the "
                          "page's own JavaScript and need a JavaScript runtime")

HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const from = html.indexOf("/* ======================================================== the session === */");
const to = html.indexOf("/* ============================================================= state === */");
if (from < 0 || to < 0 || to < from) throw new Error("the session section's markers moved");
const SECTION = html.slice(from, to);
const SCENARIO = fs.readFileSync(process.argv[3], "utf8");

const elements = new Map();
function element(id) {
  const listeners = {};
  return { id, hidden: true, value: "", textContent: "", title: "", disabled: false, open: false,
           isConnected: true, listeners,
           addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
           removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter(f => f !== fn); },
           fire(type, ev) { for (const fn of [...(listeners[type] || [])]) fn(ev || { preventDefault() {} }); },
           focus() {}, showModal() { this.open = true; }, close() { this.open = false; } };
}
const $ = id => { if (!elements.has(id)) elements.set(id, element(id)); return elements.get(id); };
class Element {}
const Event = { NONE: 0 };
const captured = [];
const document = { addEventListener: (type, fn) => captured.push(fn) };
const location = { pathname: "/ui/jobs", search: "?show=failed", assigned: [],
                   assign(url) { this.assigned.push(url); } };
const notes = [];
const note = (host, kind, text) => { notes.push([host.id, kind, String(text)]); };
const busy = () => () => {};
const reason = (p, fallback) => (p && p.error && p.error.message) || fallback;
const failure = status => "refused " + status;
const roughly = s => "about " + Math.round(s / 60) + " minutes";
const JSON_BODY = { "Content-Type": "application/json" };

// The gateway: answers queued per path, each a [status, body, headers].
const queued = {}, sent = [];
const answer = (path, status, body, headers) => (queued[path] = queued[path] || []).push([status, body, headers || {}]);
async function fetch(path, options) {
  sent.push([(options && options.method) || "GET", path, options && options.body]);
  const [status, body, headers] = (queued[path] || []).shift() || [200, {}, {}];
  const response = { ok: status < 300, status,
    headers: { get: name => headers[name.toLowerCase()] ?? null },
    json: async () => body, clone() { return response; } };
  return response;
}
// A control a scenario presses; a tab says so as the page asks it.
const control = role => ({ disabled: false, title: "", isConnected: true,
                           matches: selector => selector === "[role=tab]" && role === "tab" });
// A press on a control, as the page's capture listeners see it: `handler`
// runs while the press is being handled, and its answer is handed back.
// After that the dispatch is over, as in a browser.
function press(target, handler) {
  const ev = { target: Object.assign(new Element(), { closest: () => target }), eventPhase: 1 };
  for (const fn of captured) fn(ev);
  ev.eventPhase = 2;
  const started = handler ? handler() : undefined;
  ev.eventPhase = Event.NONE;
  return started;
}
const settle = () => new Promise(r => setImmediate(r));
// A toast's timer does not hold the process: a scenario is over when its
// last line has printed. In a `hurry` every wait is recorded and over at
// once, and then it does hold the process, since a scenario is waiting on it.
let hurry = false;
const waited = [];
const setTimeout = (fn, ms) => {
  if (hurry) { waited.push(ms); return global.setTimeout(fn, 0); }
  const timer = global.setTimeout(fn, ms); timer.unref(); return timer;
};
const clearTimeout = timer => global.clearTimeout(timer);

eval(SECTION + "\n;(async () => {\n" + SCENARIO + "\n})().catch(e => { console.error(e); process.exit(2); });");
"""


def run(tmp_path: Path, scenario: str):
    """The scenario's last line prints one JSON value; that value."""
    (tmp_path / "harness.js").write_text(HARNESS)
    (tmp_path / "scenario.js").write_text(scenario)
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(PAGE),
                           str(tmp_path / "scenario.js")],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout.strip().splitlines()[-1])


@node
def test_an_ended_session_sends_the_reader_to_sign_in_once_and_back_here(tmp_path):
    got = run(tmp_path, """
      answer("/jobs", 401, { error: { code: "unauthenticated", message: "Sign in." } });
      answer("/auth/me", 401, { error: { code: "unauthenticated", message: "Sign in." } });
      answer("/glossaries", 401, { error: { code: "unauthenticated", message: "Sign in." } });
      const first = await api("/jobs");
      await api("/glossaries");
      console.log(JSON.stringify({ status: first.status, went: location.assigned,
                                   asked: sent.filter(([, path]) => path === "/auth/me").length }));
    """)
    assert got == {"status": 401, "went": ["/login?next=%2Fui%2Fjobs%3Fshow%3Dfailed"], "asked": 1}, got


@node
def test_a_backend_refusing_the_gateways_word_keeps_a_live_session_on_the_page(tmp_path):
    """A backend that has not read a rotated identity key answers 401, and
    the gateway passes it on. Sent to /login, a live session bounced straight
    back to the same refusal, a page load at a time, in every open tab."""
    got = run(tmp_path, """
      for (const path of ["/jobs", "/glossaries"])
        answer(path, 401, { error: { code: "unauthenticated", message: "Authentication required." } });
      answer("/auth/me", 200, { user: { id: "u_aaaaaaaaaaaaaaaa" }, must_change: false });
      const [jobs] = await Promise.all([api("/jobs"), api("/glossaries")]);
      console.log(JSON.stringify({ status: jobs.status, went: location.assigned, said: $("toast").textContent,
                                   asked: sent.filter(([, path]) => path === "/auth/me").length }));
    """)
    assert got["status"] == 401 and got["went"] == [], got
    assert got["asked"] == 1, "two refusals at once asked who is signed in twice"
    assert got["said"] == ("A service behind the gateway did not accept who you are signed in as. "
                           "You are still signed in; that service's log says why."), got


@node
def test_a_session_that_must_choose_a_new_password_is_sent_to_choose_one(tmp_path):
    got = run(tmp_path, """
      answer("/jobs", 401, { error: { code: "unauthenticated", message: "Choose a new password." } });
      answer("/auth/me", 200, { user: { id: "u_aaaaaaaaaaaaaaaa" }, must_change: true });
      await api("/jobs");
      console.log(JSON.stringify({ went: location.assigned }));
    """)
    assert got == {"went": ["/login?next=%2Fui%2Fjobs%3Fshow%3Dfailed"]}, got


@node
def test_a_step_up_asks_for_the_password_and_sends_the_same_request_again(tmp_path):
    """D13: the request waits on the prompt; the password goes to
    /auth/step-up and nowhere else, and the field is emptied."""
    got = run(tmp_path, """
      answer("/admin/users", 403, { error: { code: "step_up_required", message: "Again." } });
      answer("/admin/users", 201, { user: { id: "u_aaaaaaaaaaaaaaaa" } });
      const asked = api("/admin/users", { method: "POST", headers: JSON_BODY, body: '{"username":"ana"}' });
      await settle(); await settle();
      const open = $("stepup").open;
      $("stepup-password").value = "correct horse battery staple";
      $("stepupform").fire("submit");
      const done = await asked;
      console.log(JSON.stringify({ open, status: done.status, sent, emptied: $("stepup-password").value,
                                   closed: !$("stepup").open }));
    """)
    assert got["open"] is True and got["status"] == 201 and got["closed"] is True, got
    assert got["emptied"] == ""
    assert got["sent"] == [
        ["POST", "/admin/users", '{"username":"ana"}'],
        ["POST", "/auth/step-up", '{"password":"correct horse battery staple"}'],
        ["POST", "/admin/users", '{"username":"ana"}']], got


@node
def test_a_cancelled_step_up_sends_nothing_more_and_hands_back_the_refusal(tmp_path):
    got = run(tmp_path, """
      answer("/admin/secrets/X", 403, { error: { code: "step_up_required", message: "Again." } });
      const asked = api("/admin/secrets/X", { method: "PUT", headers: JSON_BODY, body: "{}" });
      await settle(); await settle();
      $("stepup-cancel").fire("click");
      const done = await asked;
      console.log(JSON.stringify({ status: done.status, sent: sent.length, closed: !$("stepup").open }));
    """)
    assert got == {"status": 403, "sent": 1, "closed": True}, got


@node
def test_a_wrong_step_up_password_is_said_and_the_prompt_stays(tmp_path):
    got = run(tmp_path, """
      answer("/admin/users/u_aaaaaaaaaaaaaaaa", 403, { error: { code: "step_up_required", message: "Again." } });
      answer("/auth/step-up", 403, { error: { code: "wrong_password", message: "That password is not correct." } });
      api("/admin/users/u_aaaaaaaaaaaaaaaa", { method: "DELETE" });
      await settle(); await settle();
      $("stepup-password").value = "wrong";
      $("stepupform").fire("submit");
      await settle(); await settle();
      console.log(JSON.stringify({ open: $("stepup").open, notes, emptied: $("stepup-password").value }));
    """)
    assert got["open"] is True and got["emptied"] == "", got
    assert got["notes"][-1] == ["stepupnote", "bad", "That password is not correct."], got


REFUSED_SCOPE = """
const refuse = path => answer(path, 403, { error: { code: "insufficient_scope", message: "No." } },
  { "www-authenticate": 'Bearer error="insufficient_scope", scope="satellites:listen"' });
"""


@node
def test_a_missing_scope_is_said_and_the_control_that_asked_stays_off(tmp_path):
    got = run(tmp_path, REFUSED_SCOPE + """
      const button = control();
      refuse("/satellites/x/listen");
      const r = await press(button, () => api("/satellites/x/listen", { method: "POST" }));
      // busy() gives a control back after its request; this one stays off.
      button.disabled = false || DENIED.has(button);
      console.log(JSON.stringify({ status: r.status, said: $("toast").textContent, toast: !$("toast").hidden,
                                   disabled: button.disabled, title: button.title }));
    """)
    assert got == {"status": 403, "said": "Your account cannot do this (needs satellites:listen).",
                   "toast": True, "disabled": True, "title": "Needs satellites:listen"}, got


@node
def test_a_refusal_for_a_request_the_press_did_not_start_greys_nothing(tmp_path):
    """A poll, a timer or a reload started just after a click is not the
    click's: under a one-second rule a background refusal greyed the Jobs
    tab, or whatever was last pressed, for the rest of the page's life."""
    got = run(tmp_path, REFUSED_SCOPE + """
      const button = control(), tab = control("tab");
      refuse("/satellites"); refuse("/satellites/x/listen");
      press(button, () => {});
      await api("/satellites");
      const said = $("toast").textContent;
      await press(tab, () => api("/satellites/x/listen", { method: "POST" }));
      console.log(JSON.stringify({ said, button: [button.disabled, DENIED.has(button)],
                                   tab: [tab.disabled, DENIED.has(tab)] }));
    """)
    assert got["said"] == "Your account cannot do this (needs satellites:listen).", got
    assert got["button"] == [False, False], "a background refusal greyed the last control pressed"
    assert got["tab"] == [False, False], "opening a tab whose read was refused greyed the tab"


@node
def test_a_locked_gateway_covers_the_page_with_what_to_fix(tmp_path):
    got = run(tmp_path, """
      answer("/jobs", 503, { error: { code: "locked", message: "Locked." }, reason: "removed_variable",
                             variable: "GATEWAY_API_KEYS" });
      const r = await api("/jobs");
      console.log(JSON.stringify({ status: r.status, shown: !$("lockout").hidden,
                                   why: $("lockout-why").textContent, what: $("lockout-variable").textContent }));
    """)
    assert got["status"] == 503 and got["shown"] is True, got
    assert "Remove it from the gateway's configuration" in got["why"]
    assert got["what"] == "Setting: GATEWAY_API_KEYS"


@node
def test_a_gateway_that_does_not_answer_at_start_covers_the_page_and_is_asked_again(tmp_path):
    """A 502 from the proxy while the gateway restarts, or no connection at
    all, left a shell: tabs drawn, nothing read, nothing polled, and a toast
    that went after eight seconds."""
    got = run(tmp_path, page_function("whoAmI") + """
      let shown = 0;
      function sessionShow() { shown++; }
      hurry = true;
      answer("/auth/me", 502, null);
      answer("/auth/me", 500, { error: { code: "internal", message: "Something broke." } });
      answer("/auth/me", 200, { user: { id: "u_aaaaaaaaaaaaaaaa", username: "ana" }, role: "speech",
                                scopes: ["speech:speak"], must_change: false });
      const covers = [];
      const watch = global.setInterval(() => covers.push([$("lockout").hidden, $("lockout-title").textContent,
                                                          $("lockout-why").textContent]), 0);
      const going = await whoAmI();
      global.clearInterval(watch);
      console.log(JSON.stringify({ going, shown, waited, covered: covers.some(([hidden]) => !hidden),
                                   said: [...new Set(covers.filter(([hidden]) => !hidden).map(c => c[1] + " | " + c[2]))],
                                   after: $("lockout").hidden, user: ME.user.username, went: location.assigned }));
    """)
    assert got["going"] is True and got["shown"] == 1 and got["user"] == "ana", got
    assert got["waited"] == [2000, 4000], "not asked again on a back-off"
    assert got["covered"] is True and got["after"] is True, got
    assert got["said"] == ["Calliope is not answering | refused 502",
                           "Calliope is not answering | Something broke."], got
    assert got["went"] == [], got


@node
def test_a_gateway_that_locks_after_not_answering_says_it_is_locked(tmp_path):
    got = run(tmp_path, page_function("whoAmI") + """
      hurry = true;
      answer("/auth/me", 502, null);
      answer("/auth/me", 503, { error: { code: "locked", message: "Locked." }, reason: "bootstrap_required",
                                variable: "CALLIOPE_ADMIN_PASSWORD" });
      const going = await whoAmI();
      console.log(JSON.stringify({ going, shown: !$("lockout").hidden, title: $("lockout-title").textContent,
                                   what: $("lockout-variable").textContent }));
    """)
    assert got == {"going": False, "shown": True, "title": "Calliope is locked",
                   "what": "Setting: CALLIOPE_ADMIN_PASSWORD"}, got


@node
def test_a_stale_page_and_a_wait_are_said_and_nothing_else_is_taken_over(tmp_path):
    """csrf and 429 are the page's to say; a 409 or a 404 is the caller's,
    which alone knows where to say it."""
    got = run(tmp_path, """
      answer("/ui/resolve", 403, { error: { code: "csrf", message: "Cross-site." } });
      await api("/ui/resolve", { method: "POST" });
      const stale = $("toast").textContent;
      answer("/ui/resolve", 429, { error: { code: "rate_limited", message: "Slow." } }, { "retry-after": "12" });
      await api("/ui/resolve", { method: "POST" });
      const wait = $("toast").textContent;
      $("toast").textContent = "";
      answer("/ui/resolve", 409, { error: { code: "pending_for_another_user", message: "Someone else." } });
      const theirs = await api("/ui/resolve", { method: "POST" });
      console.log(JSON.stringify({ stale, wait, after: $("toast").textContent, status: theirs.status,
                                   went: location.assigned }));
    """)
    assert got["stale"] == "This page is out of date. Reload it and try again."
    assert got["wait"] == "Too many requests. Try again in 12 seconds."
    assert got["after"] == "" and got["status"] == 409 and got["went"] == [], got
