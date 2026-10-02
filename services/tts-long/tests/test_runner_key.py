"""The GPU runner's key: from the secret store, cached briefly, sent only where allowed.

THE TWO FAILURES THIS FILE GUARDS ARE OPPOSITE ONES. A key that does not
follow the store means a rotation needs a restart of a 6.5 GB container, and a
cleared key keeps working. A key that follows the store too eagerly means a
gateway restart takes the runner away from jobs already promised to it. So:
cached for at most a minute, the last good value kept while the gateway cannot
answer, and a 404 obeyed at once (D42, D45).

NOTHING HERE OPENS A SOCKET. The gateway is a scripted stand-in for runlog's
opener, the one name `app.runner_key` sends through, and the runner is a
recording connection behind `RunnerClient._connect`.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from voice_common import identity
from voice_common.conformance import FakeGateway

from app import runner_key as runner_key_module
from app.remote import RunnerClient, RunnerConfig
from app.runner_key import NAME, RunnerKey, origin

TARGET = "https://runner.invalid:47600"
SECRET = "runner-key-from-the-store"
ROTATED = "runner-key-after-rotation"
OLD_FILE = "runner-key-from-the-old-file"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Gateway:
    """The internal listener's two secret routes, answering from a script.

    `answers` is consumed one per request; the last one repeats. An answer is
    a status, a status and a document, or an exception to raise.
    """

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []

    def urlopen(self, request, *, timeout):  # noqa: ANN001, ANN201
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, document = answer if isinstance(answer, tuple) else (answer, None)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "refused",
                                         {}, io.BytesIO(b""))
        body = json.dumps(document).encode() if document is not None else b""
        response = io.BytesIO(body)
        response.status = status  # type: ignore[attr-defined]
        return response


def stored(value=SECRET, hosts=(TARGET,), max_age=60):
    return 200, {"value": value, "version": 1, "allowed_hosts": list(hosts),
                 "max_age": max_age}


@pytest.fixture
def credentials(tmp_path):
    """This service's credential volume, as the gateway writes it."""
    gateway = FakeGateway(tmp_path / "credentials")
    return identity.Credentials(gateway.directory), gateway.service_key


@pytest.fixture
def make(credentials, monkeypatch):
    """A RunnerKey talking to a scripted gateway on a hand-driven clock."""
    creds, _ = credentials

    def build(gateway: Gateway, *, fallback="", fallback_from=None):
        monkeypatch.setattr(runner_key_module, "urlopen", gateway.urlopen)
        clock = Clock()
        key = RunnerKey(target=TARGET, fallback=fallback,
                        fallback_from=fallback_from, credentials=creds,
                        clock=clock)
        return key, clock

    return build


# ---------------------------------------------------------- the store ----


def test_the_key_comes_from_the_store_with_this_services_own_key(
        make, credentials):
    gateway = Gateway(stored())
    key, _ = make(gateway)
    assert key.get() == SECRET
    request = gateway.requests[0]
    assert request.full_url == f"{identity.GATEWAY_INTERNAL}/internal/secrets/{NAME}"
    assert request.get_header("Authorization") == f"Bearer {credentials[1]}"


def test_a_rotated_key_is_picked_up_within_sixty_seconds(make):
    """A rotation in Admin › Secrets used to be a restart of this container."""
    gateway = Gateway(stored(), stored(ROTATED))
    key, clock = make(gateway)
    assert key.get() == SECRET
    clock.now += 59
    assert key.get() == SECRET, "the cache was not used inside its minute"
    assert len(gateway.requests) == 1
    clock.now += 2
    assert key.get() == ROTATED


def test_the_gateways_max_age_can_shorten_the_cache_but_never_lengthen_it(make):
    gateway = Gateway(stored(max_age=3600), stored(ROTATED))
    key, clock = make(gateway)
    key.get()
    clock.now += 61
    assert key.get() == ROTATED


@pytest.mark.parametrize("failure", [
    503, 500, urllib.error.URLError("connection refused"), TimeoutError()])
def test_a_gateway_that_cannot_answer_keeps_the_last_good_key(make, failure):
    """Stale-if-error (D42): a gateway restarting or in locked mode must not
    take the runner away from jobs already promised to it."""
    gateway = Gateway(stored(), failure)
    key, clock = make(gateway, fallback=OLD_FILE, fallback_from="TTS_RUNNER_API_KEY_FILE")
    assert key.get() == SECRET
    clock.now += 61
    assert key.get() == SECRET, "the last good value was dropped"


def test_a_cleared_secret_stops_at_once_and_is_never_replaced_by_the_file(make):
    """404 is an admin's answer, not an outage: nothing older stands in for it."""
    gateway = Gateway(stored(), 404, stored(ROTATED))
    key, clock = make(gateway, fallback=OLD_FILE, fallback_from="TTS_RUNNER_API_KEY_FILE")
    key.get()
    clock.now += 61
    assert key.get() is None
    # NEVER CACHED: set again, it is used on the very next request.
    assert key.get() == ROTATED


def test_with_nothing_in_the_store_yet_and_no_gateway_the_old_file_is_used(make):
    """D45: the fallback for this release, while the gateway is away."""
    key, _ = make(Gateway(urllib.error.URLError("no route")),
                  fallback=OLD_FILE, fallback_from="TTS_RUNNER_API_KEY_FILE")
    assert key.get() == OLD_FILE


def test_a_failed_fetch_is_not_repeated_on_every_runner_request(make):
    """The probe asks every ten seconds and a job polls every two; a hanging
    gateway must not add its timeout to each of them."""
    gateway = Gateway(urllib.error.URLError("no route"))
    key, clock = make(gateway, fallback=OLD_FILE, fallback_from="TTS_RUNNER_API_KEY_FILE")
    for _ in range(5):
        key.get()
    assert len(gateway.requests) == 1
    clock.now += runner_key_module.RETRY_AFTER_ERROR
    key.get()
    assert len(gateway.requests) == 2


def test_the_key_is_not_sent_to_a_host_the_secret_does_not_name(make):
    """D41: a value goes only where its secret says, however it was cached."""
    key, _ = make(Gateway(stored(hosts=["https://somewhere-else.invalid"])))
    assert key.get() is None


@pytest.mark.parametrize("entry", [
    "https://RUNNER.invalid:47600", "runner.invalid.:47600",
    "https://user@runner.invalid:47600", "https://runner.invalid:47600/",
])
def test_allowed_hosts_are_compared_after_normalising_both_sides(make, entry):
    key, _ = make(Gateway(stored(hosts=[entry])))
    assert key.get() == SECRET


@pytest.mark.parametrize("entry", [
    "http://runner.invalid:47600", "runner.invalid", "https://runner.invalid",
    "https://runner.invalid:47601", "https://evil.invalid/runner.invalid:47600",
    # The store refuses an entry with a path, so one that arrives admits nothing.
    "https://runner.invalid:47600/path",
])
def test_a_near_miss_host_is_still_a_miss(make, entry):
    """No scheme means https on 443, and http is never https."""
    key, _ = make(Gateway(stored(hosts=[entry])))
    assert key.get() is None


def test_origin_writes_out_what_a_host_leaves_implicit():
    assert origin("HA.lan.") == "https://ha.lan:443"
    assert origin("user@ha.lan") == "https://ha.lan:443"
    assert origin("ha.lan:443") == origin("https://ha.lan")
    assert origin("http://ha.lan") == "http://ha.lan:80"
    assert origin("https://[::1]:8123") == "https://[::1]:8123"
    assert origin("https://bücher.example") == "https://xn--bcher-kva.example:443"
    assert origin("ftp://ha.lan") is None
    assert origin("https://ha.lan:99999") is None


def test_the_service_key_is_read_again_after_the_gateway_answers_401(
        make, credentials):
    """The gateway rotated this service's key under it (§2.4)."""
    creds, old = credentials
    gateway = Gateway(401, stored())
    key, _ = make(gateway)
    assert creds.service_key() == old
    rotated = "calliope_svc_" + "R" * 36
    (creds.directory / identity.SERVICE_KEY_FILE).write_text(rotated + "\n")
    assert key.get() == SECRET
    assert [r.get_header("Authorization") for r in gateway.requests] == [
        f"Bearer {old}", f"Bearer {rotated}"]


# ------------------------------------------------------------ the runner ----


class Runner:
    """A runner behind `_connect` that records every request's headers."""

    def __init__(self, *, refuse=frozenset()) -> None:
        self.seen: list[tuple[str, str, dict]] = []
        self.refuse = refuse

    def connect(self, timeout=None):  # noqa: ANN001, ANN201
        return _Connection(self)


class _Connection:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.status = 200
        self._body = b""

    def request(self, method, path, body=None, headers=None):  # noqa: ANN001
        headers = dict(headers or {})
        self.runner.seen.append((method, path, headers))
        sent = headers.get("Authorization", "")
        if sent in self.runner.refuse:
            self.status, self._body = 401, b"{}"
        elif path == "/v1/services":
            self._body = json.dumps({"gpu_available": True, "services": [
                {"id": "chatterbox", "device": "gpu", "installed": True,
                 "enabled": True, "available": True}]}).encode()
        elif method == "POST" and path.endswith("/jobs"):
            self._body = json.dumps({"job_id": "r1"}).encode()
        elif path.endswith("/jobs/r1"):
            self._body = json.dumps({"status": "done", "artefacts": ["r1.0.f32"],
                                     "record": {"input_tokens": 3}}).encode()
        elif "result" in path:
            self._body = np.zeros(2400, dtype="<f4").tobytes()
        else:
            self._body = b"{}"

    def getresponse(self):  # noqa: ANN201
        return self

    def read(self) -> bytes:
        return self._body

    @staticmethod
    def getheaders():  # noqa: ANN205
        return []

    def close(self) -> None:
        pass


def test_a_runner_401_fetches_the_key_again_once(make, monkeypatch):
    """The store may hold a rotation the cache has not seen; asked once."""
    gateway = Gateway(stored(), stored(ROTATED))
    key, _ = make(gateway)
    runner = Runner(refuse={f"Bearer {SECRET}"})
    client = RunnerClient(RunnerConfig(host="runner.invalid"), key=key)
    monkeypatch.setattr(client, "_connect", runner.connect)

    status, _, _ = client._request("GET", "/v1/status")
    assert status == 200
    assert [h["Authorization"] for _, _, h in runner.seen] == [
        f"Bearer {SECRET}", f"Bearer {ROTATED}"]


def test_a_runner_that_refuses_every_key_costs_one_gateway_fetch_per_ten_seconds(
        make, monkeypatch):
    """A wrong value in Admin › Secrets, or a runner rotated ahead of the
    store, makes the runner refuse every key. Each runner 401 used to empty
    the cache and the back-off together, so the probe, every poll and every
    /health snapshot became an audited secret read: eleven in twenty seconds."""
    gateway = Gateway(stored())
    key, clock = make(gateway)
    runner = Runner(refuse={f"Bearer {SECRET}"})
    client = RunnerClient(RunnerConfig(host="runner.invalid"), key=key)
    monkeypatch.setattr(client, "_connect", runner.connect)

    seconds = 20
    for _ in range(seconds // 2):
        status, _, _ = client._request("GET", "/v1/status")
        assert status == 401
        clock.now += 2

    # The first fetch, then at most one more per RETRY_AFTER_ERROR.
    allowed = 1 + seconds / runner_key_module.RETRY_AFTER_ERROR
    assert len(gateway.requests) <= allowed, (
        f"{len(gateway.requests)} gateway fetches in {seconds} s")


def test_a_request_to_the_runner_never_carries_an_identity_header(
        speech, gateway, monkeypatch):
    """D65, end to end: a job submitted with an assertion, a delegation token
    and a cookie reaches the runner with an explicit header set and none of
    the three."""
    import app.main as main

    runner = Runner()
    key = RunnerKey(target=TARGET, fallback=OLD_FILE,
                    fallback_from="TTS_RUNNER_API_KEY",
                    credentials=identity.Credentials(gateway.directory / "none"))
    client = RunnerClient(RunnerConfig(host="runner.invalid", poll=0.0), key=key)
    monkeypatch.setattr(client, "_connect", runner.connect)
    lane = main.dispatch.lanes["runner"]
    monkeypatch.setattr(lane, "hop", 0.0)
    main.state["runner"] = client
    lane.probe.once()
    assertion = gateway.assertion("tts-long")
    try:
        posted = speech.post(
            "/jobs", json={"text": "One short line.", "voice": "default"},
            headers={identity.ASSERTION_HEADER: assertion,
                     identity.DELEGATION_HEADER: gateway.delegation(),
                     "Cookie": "calliope_session=not-for-the-runner"})
        assert posted.status_code == 202, posted.text
        job_id = posted.json()["id"]
        for _ in range(500):
            job = speech.get(f"/jobs/{job_id}").json()
            if job["status"] == "done":
                break
            time.sleep(0.01)
        assert job["backend"] == "runner", "the job never reached the runner"
    finally:
        main.state["runner"] = None
        lane.probe.once()

    assert runner.seen, "nothing was sent to the runner"
    for method, path, headers in runner.seen:
        names = {name.lower() for name in headers}
        assert not {n for n in names if n.startswith("x-calliope-")}, (method, path)
        assert "cookie" not in names
        assert assertion not in json.dumps(headers)
        assert headers["Authorization"] == f"Bearer {OLD_FILE}"


# ------------------------------------------------------------ the import ---


def test_the_old_setting_is_imported_once_with_its_host_and_the_window_closed(
        make):
    gateway = Gateway((201, {}))
    key, _ = make(gateway, fallback=OLD_FILE,
                  fallback_from="TTS_RUNNER_API_KEY_FILE")
    assert key.import_fallback() is True
    request = gateway.requests[0]
    assert request.full_url == f"{identity.GATEWAY_INTERNAL}/internal/secrets/import"
    assert json.loads(request.data) == {
        "final": True,
        "secrets": [{"name": NAME, "value": OLD_FILE, "kind": "bearer",
                     "allowed_hosts": [TARGET],
                     "source": "file TTS_RUNNER_API_KEY_FILE"}],
        "declared": {NAME: [TARGET]}}


@pytest.mark.parametrize("fallback_from", ["TTS_RUNNER_API_KEY_FILE", "TTS_RUNNER_API_KEY"])
def test_the_import_is_one_the_gateway_s_store_accepts_and_keeps_the_runner_host(
        make, fallback_from):
    """The body is checked against the gateway's own import schema and source
    rule: the store answers 422 to a field it does not know, and with no
    `declared` hosts it keeps the value but lets it go nowhere."""
    gateway_app = Path(__file__).resolve().parents[2] / "gateway" / "app"
    if not gateway_app.is_dir():
        pytest.skip("the gateway is not beside this service")
    schema = ast.parse((gateway_app / "routes_secrets.py").read_text(encoding="utf-8"))
    fields = {cls.name: {n.target.id for n in cls.body if isinstance(n, ast.AnnAssign)}
              for cls in schema.body if isinstance(cls, ast.ClassDef)}
    store = ast.parse((gateway_app / "secret_store.py").read_text(encoding="utf-8"))
    source_rule = next(ast.literal_eval(node.value.args[0]) for node in ast.walk(store)
                       if isinstance(node, ast.Assign)
                       and getattr(node.targets[0], "id", "") == "SOURCE")

    gateway = Gateway((201, {}))
    key, _ = make(gateway, fallback=OLD_FILE, fallback_from=fallback_from)
    assert key.import_fallback() is True
    sent = json.loads(gateway.requests[0].data)

    assert set(sent) <= fields["ImportBatch"]
    assert all(set(entry) <= fields["ImportEntry"] for entry in sent["secrets"])
    assert re.fullmatch(source_rule, sent["secrets"][0]["source"])
    assert sent["declared"][NAME] == sent["secrets"][0]["allowed_hosts"] == [TARGET]


def test_with_nothing_to_import_the_window_is_still_closed(make):
    """An open window is a way to plant a key nobody chose (D66)."""
    gateway = Gateway((200, {}))
    key, _ = make(gateway)
    assert key.import_fallback() is True
    assert json.loads(gateway.requests[0].data) == {"final": True, "secrets": [],
                                                    "declared": {NAME: [TARGET]}}


@pytest.mark.parametrize("answer,done", [
    ((201, {}), True), (410, True), (400, True),
    (503, False), (500, False), (urllib.error.URLError("no route"), False),
])
def test_the_import_is_retried_only_while_the_gateway_cannot_answer_it(
        make, answer, done):
    key, _ = make(Gateway(answer), fallback=OLD_FILE,
                  fallback_from="TTS_RUNNER_API_KEY_FILE")
    assert key.import_fallback() is done


def test_the_old_setting_is_read_from_the_variable_before_the_file(tmp_path):
    path = tmp_path / "runner-key"
    path.write_text(OLD_FILE + "\n")
    from_file = RunnerKey.from_env(TARGET, {"TTS_RUNNER_API_KEY_FILE": str(path)})
    assert from_file._fallback == OLD_FILE
    assert from_file._fallback_from == "TTS_RUNNER_API_KEY_FILE"
    both = RunnerKey.from_env(TARGET, {"TTS_RUNNER_API_KEY": "direct",
                                       "TTS_RUNNER_API_KEY_FILE": str(path)})
    assert (both._fallback, both._fallback_from) == ("direct", "TTS_RUNNER_API_KEY")


def test_no_value_is_ever_logged(make, caplog):
    """Names only. Every path that warns is walked with the values in hand."""
    scripts = [
        Gateway(stored(), 503), Gateway(urllib.error.URLError("x")),
        Gateway(stored(hosts=["https://elsewhere.invalid"])), Gateway(404),
        Gateway((201, {})), Gateway(400),
    ]
    with caplog.at_level(logging.DEBUG):
        for script in scripts:
            key, clock = make(script, fallback=OLD_FILE,
                              fallback_from="TTS_RUNNER_API_KEY_FILE")
            key.get()
            clock.now += 61
            key.get()
            key.import_fallback()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert NAME in logged
    for value in (SECRET, OLD_FILE):
        assert value not in logged
