"""The run-record sender: through the gateway, as this service, saying whose run it was.

The queue, the drop policy and the text cap are pinned by the senders' own
suites (services/stt and services/tts). What is pinned here is what this
release changed: the key comes from service.key and not from RUNLOG_KEY, it
goes to the gateway's internal listener and to no other host, a rotated key is
picked up after one 401, and `owner` and `credential` are carried only when
they have the shape the receiver will trust.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from voice_common import runlog
from voice_common.conformance import FakeGateway
from voice_common.identity import GATEWAY_INTERNAL, Credentials
from voice_common.runlog import RunLog
from voice_common.scopes import CREDENTIAL, SERVICE_SUB, USER_ID

FIXTURES = Path(__file__).parent / "fixtures" / "run_records.json"


class Receiver:
    """The gateway's POST /runs, answering each request from a script of statuses."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float | None = None):  # noqa: ANN204
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 201
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "refused",
                                         {}, io.BytesIO(b""))
        return contextlib.nullcontext()

    def authorisations(self) -> list[str]:
        return [request.get_header("Authorization") for request in self.requests]

    def bodies(self) -> list[dict]:
        return [json.loads(request.data) for request in self.requests]


def settle(log: RunLog) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log._queue.unfinished_tasks == 0 and (log.sent or log.last_error):
            return
        time.sleep(0.005)
    raise AssertionError(f"the sender never finished: {log.stats()}")


@pytest.fixture
def gateway(tmp_path: Path) -> FakeGateway:
    return FakeGateway(tmp_path)


class Host:
    """A real HTTP server on loopback that records every request and its Authorization."""

    def __init__(self, status: int, location: str | None = None) -> None:
        seen: list[tuple[str, str, str | None]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self) -> None:
                seen.append((self.command, self.path,
                             self.headers.get("Authorization")))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = answer

            def log_message(self, *args: object) -> None:
                del args

        self.seen = seen
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hosts() -> Iterator[Callable[..., Host]]:
    started: list[Host] = []

    def start(status: int, location: str | None = None) -> Host:
        started.append(Host(status, location))
        return started[-1]

    yield start
    for host in started:
        host.close()


def sender(directory: Path) -> RunLog:
    return RunLog(url=GATEWAY_INTERNAL, host="test", service="stt",
                  engine="parakeet", credentials=Credentials(directory))


def test_a_record_is_posted_to_the_gateway_with_this_services_key(
        gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = Receiver()
    monkeypatch.setattr(runlog, "urlopen", receiver)
    log = sender(gateway.directory)
    log.record(kind="transcribe", owner="u_mfrggzdfmztwq2lk", credential="session")
    settle(log)
    assert receiver.requests[0].full_url == "http://voice-gateway:8081/runs"
    assert receiver.authorisations() == [f"Bearer {gateway.service_key}"]
    assert receiver.bodies()[0]["owner"] == "u_mfrggzdfmztwq2lk"
    assert receiver.bodies()[0]["credential"] == "session"


@pytest.mark.parametrize("url", [
    "http://tts-long:8002",
    "https://voice-gateway:8081",
    "http://voice-gateway:8081/elsewhere",
    "http://someone:hunter2@voice-gateway:8081",
])
def test_runlog_url_naming_anywhere_but_the_gateway_turns_the_log_off_and_says_so(
        url: str, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """The old value posted straight to tts-long, handing the key to a peer that is not the gateway."""
    receiver = Receiver()
    monkeypatch.setattr(runlog, "urlopen", receiver)
    with caplog.at_level(logging.ERROR, logger="voice_common.runlog"):
        log = RunLog(url=url, host="test", service="stt", engine="parakeet",
                     credentials=Credentials(gateway.directory))
    log.record(kind="transcribe")
    assert not log.enabled
    assert receiver.requests == []
    assert log.stats()["last_error"] == f"RUNLOG_URL must be {GATEWAY_INTERNAL}"
    assert "RUNLOG_URL" in caplog.text
    assert "hunter2" not in caplog.text
    assert "hunter2" not in json.dumps(log.stats())


def test_a_redirect_never_carries_the_key_to_the_host_it_names(
        gateway: FakeGateway, hosts: Callable[..., Host]) -> None:
    """urllib's default opener repeats the request, Authorization and all, at the Location."""
    elsewhere = hosts(200)
    receiver = hosts(302, f"{elsewhere.url}/runs")
    log = sender(gateway.directory)
    # The address check is pinned above; a real receiver has to be on loopback.
    log.url = receiver.url
    log.record(kind="transcribe")
    settle(log)
    assert [auth for *_, auth in receiver.seen] == [f"Bearer {gateway.service_key}"]
    assert elsewhere.seen == []
    assert log.sent == 0
    assert log.stats()["last_error"] == "RuntimeError: POST /runs -> 302"


def test_a_proxy_variable_never_routes_the_key_through_the_proxy(
        gateway: FakeGateway, hosts: Callable[..., Host],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """urllib's default opener sends every request to whatever HTTP_PROXY names."""
    proxy = hosts(201)
    receiver = hosts(201)
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, proxy.url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    log = sender(gateway.directory)
    log.url = receiver.url
    log.record(kind="transcribe")
    settle(log)
    assert [path for _, path, _ in receiver.seen] == ["/runs"]
    assert proxy.seen == []
    assert log.sent == 1


def test_without_a_service_key_nothing_is_posted_and_health_says_why(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A POST with no key would only be refused; the reason belongs in /health instead."""
    receiver = Receiver()
    monkeypatch.setattr(runlog, "urlopen", receiver)
    log = sender(tmp_path)
    log.record(kind="transcribe")
    settle(log)
    assert receiver.requests == []
    assert "no service key" in log.stats()["last_error"]


def test_runlog_key_is_not_read_any_more(gateway: FakeGateway,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = Receiver()
    monkeypatch.setattr(runlog, "urlopen", receiver)
    monkeypatch.setenv("RUNLOG_URL", "http://voice-gateway:8081")
    monkeypatch.setenv("RUNLOG_KEY", "the-old-shared-key")
    monkeypatch.setenv("CALLIOPE_RUN_DIR", str(gateway.directory))
    log = RunLog.from_env("stt", "parakeet")
    log.record(kind="transcribe")
    settle(log)
    assert receiver.authorisations() == [f"Bearer {gateway.service_key}"]


def test_a_401_reads_the_key_again_and_sends_once_more(
        gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway rotated this service's key; the next read of service.key fixes it."""
    receiver = Receiver(401)
    monkeypatch.setattr(runlog, "urlopen", receiver)
    log = sender(gateway.directory)
    assert log.credentials.service_key() == gateway.service_key
    rotated = "calliope_svc_" + "R" * 36
    (gateway.directory / "service.key").write_text(rotated)
    log.record(kind="transcribe")
    settle(log)
    assert receiver.authorisations() == [f"Bearer {gateway.service_key}",
                                         f"Bearer {rotated}"]
    assert log.sent == 1


def test_a_401_with_an_unchanged_key_is_not_retried(
        gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = Receiver(401)
    monkeypatch.setattr(runlog, "urlopen", receiver)
    log = sender(gateway.directory)
    log.record(kind="transcribe")
    settle(log)
    assert len(receiver.requests) == 1
    assert log.stats()["last_error"] == "RuntimeError: POST /runs -> 401"


@pytest.mark.parametrize(("field", "value"), [
    ("owner", "admin"), ("owner", "u_../../etc"), ("owner", 7),
    ("credential", "calliope_AbCdEf"), ("credential", "session:abcdefghijklmnopq"),
])
def test_an_owner_or_credential_of_the_wrong_shape_is_dropped_not_sent(
        field: str, value: object, gateway: FakeGateway,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Dropped, the run is a system record: hidden from its user, never shown to another."""
    receiver = Receiver()
    monkeypatch.setattr(runlog, "urlopen", receiver)
    log = sender(gateway.directory)
    with caplog.at_level(logging.WARNING, logger="voice_common.runlog"):
        log.record(kind="transcribe", **{field: value})
        settle(log)
    assert field not in receiver.bodies()[0]
    assert caplog.records


def test_the_shared_fixture_records_say_whose_run_each_was() -> None:
    """The contract both halves read: tts-long keeps these, stt and tts send them."""
    records = json.loads(FIXTURES.read_text(encoding="utf-8"))
    for kind, record in records.items():
        owner, credential = record["owner"], record["credential"]
        assert USER_ID.fullmatch(owner) or SERVICE_SUB.fullmatch(owner), kind
        assert CREDENTIAL.fullmatch(credential), kind
    assert records["transcribe"]["owner"] == "svc:satellites"
