"""What the runner tests share: the key, the fakes, and a runner to build.

Not conftest.py: this suite has two of those, and `from conftest import`
would reach whichever one was imported last. The fixtures that use these are
in tests/conftest.py.
"""

from __future__ import annotations

import threading
import time
import types
from contextlib import contextmanager

RUNNER_KEY = "test-runner-key-" + "0123456789abcdef" * 2
RUNNER_ENGINES = "chatterbox,chatterbox-turbo"
AUTH = {"authorization": f"Bearer {RUNNER_KEY}"}


class FakeProbe:
    """The GPU probe's verdict, set by hand. `again()` is counted."""

    def __init__(self, state: str = "ok", reason: str = "") -> None:
        self.state, self.reason = state, reason
        self.asked_again = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def again(self) -> None:
        self.asked_again += 1


class FakeSampler:
    """nvidia-smi's last reading, set by hand. None is "no nvidia-smi"."""

    def __init__(self, reading: dict | None = None) -> None:
        self.reading = reading

    def latest(self) -> dict | None:
        return self.reading

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def build_runner(modules, root, *, probe=None, sampler=None,  # noqa: ANN001
                 device: str = "cpu"):
    """A store, a dispatcher on the fake engine, and the guarded app."""
    services = {spec.facts.runner_service: spec
                for spec in modules.engines.values() if spec.local}
    store = modules.jobs.Store(root)
    probe = probe or FakeProbe()
    sampler = sampler or FakeSampler()
    dispatcher = modules.jobs.Dispatcher(
        store, services={s: spec.id for s, spec in services.items()},
        device=device, factory="runner_fake:FakeSynth", probe=probe,
        sampler=sampler)
    app = modules.api.create_app(dispatcher, services=services,
                                 key=RUNNER_KEY.encode("ascii"))
    return types.SimpleNamespace(app=app, store=store, dispatcher=dispatcher,
                                 probe=probe, sampler=sampler,
                                 services=services, modules=modules)


def fake_synth_lines(modules) -> list[str]:  # noqa: ANN001
    """What the fake engine generated, across every worker process."""
    try:
        return modules.fake_log.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def generated(modules) -> list[str]:  # noqa: ANN001
    """Only the generations, without the `pid exit` lines."""
    return [line for line in fake_synth_lines(modules)
            if not line.endswith(" exit")]


def settle(predicate, timeout: float = 15.0, why: str = "it never happened"):  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError(why)


def submit(client, service: str = "chatterbox", key: str | None = None,  # noqa: ANN001
           **body):
    """POST a job, with the language baseline requires unless one is given."""
    if service == "chatterbox":
        body.setdefault("language", "en")
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/v1/services/{service}/jobs", json=body, headers=headers)


def finished(client, service: str, job_id: str, timeout: float = 15.0) -> dict:  # noqa: ANN001
    """Poll until the job ends, as tts-long would."""
    def ended():
        doc = client.get(f"/v1/services/{service}/jobs/{job_id}").json()
        return doc if doc["status"] in {"done", "failed", "cancelled"} else None
    return settle(ended, timeout, f"job {job_id} never finished")


@contextmanager
def tls_server(modules, app, cert, key):  # noqa: ANN001
    """The app behind uvicorn over real TLS, through the runner's own config.

    127.0.0.1 and an ephemeral port; yields the port. The config is
    server.build_config's, so the TLS floor and the h11 deadline are the ones
    production serves with.
    """
    import uvicorn

    config = modules.server.build_config(app, cert, key, host="127.0.0.1",
                                         port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="tls-server")
    thread.start()
    settle(lambda: server.started, timeout=20, why="uvicorn did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(30)


def client_context(minimum=None, maximum=None):  # noqa: ANN001
    """A client TLS context that checks nothing: these tests are about the server."""
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if minimum is not None:
        context.minimum_version = minimum
    if maximum is not None:
        context.maximum_version = maximum
    return context
