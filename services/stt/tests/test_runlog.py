"""The run record: what leaves this service after the transcript has.

A transcription keeps nothing. The text goes into the response, the clip was
never ours, and until this existed the question "what did I dictate this
morning, and how fast is this machine really" had no answer anywhere in the
stack — while tts-long, behind the slowest engine of the three, had kept a
record of every job since the beginning.

THE TRANSCRIPT IS THE ARTEFACT. A clone job's record points at a wav file that
can be played back; this kind has no file and never did, so the text is the
thing a reader came to get back, and it is the reason `text` is in the body at
all. RUNLOG_TEXT=0 is there for the deployment that would rather keep the
timings and lose the words.

IT MUST NOT REACH THE CALLER. Not the latency, not the failure, not the
exception: /transcribe and both /v1 routes end up in pipeline.run on a worker
thread, and a POST made inline would be added to the reply of somebody waiting
for their words back.

No socket is bound anywhere here. voice_common.runlog.urlopen is replaced with
a receiver that records what it was handed, which is honest about what is
under test: the body, the threading and the failure handling are this repo's,
and HTTP itself is not.

WHOSE RUN IT WAS goes with every record, taken from the assertion the request
carried (D31): a user's transcript is theirs, and a satellite's is the hub's,
which keeps household speech out of every user's list.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import threading
import time
import urllib.error

import pytest
from starlette.testclient import TestClient
from voice_common import runlog as sender_module
from voice_common.conformance import FakeGateway
from voice_common.identity import GATEWAY_INTERNAL
from voice_common.runlog import MAX_QUEUED, SERVER_ONLY, RunLog

from app import asr, pipeline
from app.main import app
from test_glossaries import AUDIENCE, FakeEngine
from test_windowing import wav

RUNS = f"{GATEWAY_INTERNAL}/runs"

# THE ONE THING BOTH HALVES MAY TREAT AS AUTHORITATIVE. tts-long codes its
# POST /runs against this file and so does this service; if the two disagree,
# the fixture is right and the code is wrong. Asserted to exist rather than
# skipped over — a missing contract is the failure, not a reason to pass.
FIXTURES = (pathlib.Path(__file__).resolve().parents[3]
            / "packages" / "common" / "tests" / "fixtures" / "run_records.json")

TRANSCRIPT = "I made a comet on the harmony dashboard"


def contract(kind: str) -> dict[str, object]:
    assert FIXTURES.is_file(), (
        f"{FIXTURES} is the shared record contract and it is not there; "
        "without it this service and tts-long are each guessing at the other")
    return json.loads(FIXTURES.read_text(encoding="utf-8"))[kind]


class Receiver:
    """tts-long's POST /runs, with no socket in the way.

    Records every body it is handed BEFORE it sleeps or fails, so a test can
    tell "the record never left" from "the receiver never answered" — the two
    have opposite fixes and look identical from the caller.
    """

    def __init__(self, *, delay: float = 0.0,
                 error: Exception | None = None) -> None:
        self.delay = delay
        self.error = error
        self.bodies: list[dict[str, object]] = []
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.entered = threading.Event()

    def __call__(self, request, timeout: float | None = None):  # noqa: ANN001
        self.urls.append(request.full_url)
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        self.bodies.append(json.loads(request.data.decode("utf-8")))
        self.entered.set()
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return contextlib.nullcontext()

    def only(self) -> dict[str, object]:
        assert len(self.bodies) == 1, f"expected one record, got {self.bodies}"
        return self.bodies[0]


class SpeaksDutch(FakeEngine):
    """Parakeet's profile with the one thing it does not do: report a language.

    Whisper does, and `language` is optional in the record for exactly that
    reason. A fake that could not report one would leave the field asserted
    nowhere.
    """

    reports_language = True

    def transcribe(self, samples, opts):  # noqa: ANN001, ANN201
        del samples, opts
        return asr.Recognition(text=TRANSCRIPT, words=(), language="nl")


def drained(log: RunLog, expected: int = 1, timeout: float = 5.0) -> dict:
    """Wait for the sender thread to have finished with `expected` records.

    Polls the counters /health publishes rather than the queue, because those
    are the numbers an operator reads and a test that watched a private
    attribute could pass while /health said nothing happened.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stats = log.stats()
        if stats["sent"] + stats["dropped"] >= expected:
            return stats
        time.sleep(0.005)
    raise AssertionError(f"no record was sent or dropped in {timeout}s: "
                         f"{log.stats()}")


def sender(**kwargs: object) -> RunLog:
    """A sender as RUNLOG_URL configures it: the gateway's internal listener.

    Its key is the service.key the `gateway` fixture wrote, read when a record
    is sent.
    """
    return RunLog(url=GATEWAY_INTERNAL, host="nas", service="stt",
                  engine="parakeet", **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Receiver:
    fake = Receiver()
    monkeypatch.setattr(sender_module, "urlopen", fake)
    return fake


@pytest.fixture
def log(gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> RunLog:
    """A sender pointed at the fake receiver, in place of the module's own.

    Rebound rather than reconfigured, which is why pipeline.run reads the
    module global at call time instead of holding a reference.
    """
    replacement = sender()
    monkeypatch.setattr(pipeline, "runlog", replacement)
    return replacement


@pytest.fixture
def client(gateway: FakeGateway, engine: FakeEngine) -> TestClient:
    """The app with a fake engine and no VAD, every request signed for an admin.

    TestClient WITHOUT its context manager, for the reason every other file
    here gives: entering it runs the lifespan, and the lifespan loads a real
    460 MB model over the fake.
    """
    pipeline.state.clear()
    pipeline.state["asr"] = engine
    pipeline.state["rules"] = []
    yield TestClient(app, headers=gateway.headers(AUDIENCE))
    pipeline.state.clear()


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine(TRANSCRIPT)


def openai(client: TestClient, seconds: float = 4.0,
           headers: dict[str, str] | None = None, **fields):  # noqa: ANN003, ANN201
    return client.post(
        "/v1/audio/transcriptions", headers=headers,
        files={"file": ("clip.wav", wav(seconds), "audio/wav")},
        data={"model": "whisper-1", **fields})


def native(client: TestClient, seconds: float = 4.0):  # noqa: ANN201
    return client.post(
        "/transcribe",
        files={"file": ("clip.wav", wav(seconds), "audio/wav")})


def health(client: TestClient) -> dict:
    return json.loads(client.get("/health").text)


# --- the contract ----------------------------------------------------------

def test_a_run_is_recorded_with_the_fields_the_contract_names(
        client: TestClient, receiver: Receiver, log: RunLog) -> None:
    """The key set is the fixture's, exactly, and the values mean what it says.

    Compared against the shared file rather than a list written out here: a
    list in this repo can be edited to match whatever the emitter grew into,
    which is how a contract stops being one.
    """
    assert openai(client).status_code == 200
    drained(log)

    body = receiver.only()
    assert receiver.urls == [RUNS]
    assert set(body) == set(contract("transcribe"))

    assert body["kind"] == "transcribe"
    assert body["service"] == "stt"
    assert body["engine"] == "parakeet"
    assert body["host"] == "nas"
    assert body["route"] == "/v1/audio/transcriptions"
    assert body["client"] == "openai"
    # What the client ASKED for, beside what actually ran. This service has one
    # engine and `model` chooses nothing, so a listing showing whisper-1
    # requested and parakeet used is the only place that gap is visible.
    assert body["model_requested"] == "whisper-1"
    assert body["status"] == "done"
    assert body["backend"] == "local"
    assert body["text"] == TRANSCRIPT
    assert body["chars"] == len(TRANSCRIPT)
    assert body["chunks"] == 1
    assert body["audio_seconds"] == 4.0
    assert body["realtime_factor"] > 0
    assert body["created_at"] == body["started_at"]
    assert body["finished_at"] >= body["started_at"]


def test_a_sender_never_sets_a_field_tts_long_owns(
        client: TestClient, receiver: Receiver, log: RunLog) -> None:
    """`path` in particular would be a sender choosing the argument to open().

    tts-long answers 400 to any of these rather than dropping them, so a
    service that grew one would lose every record it sent with nothing but a
    rejection count to show for it.
    """
    assert openai(client).status_code == 200
    assert native(client).status_code == 200
    drained(log, expected=2)
    assert len(receiver.bodies) == 2
    for body in receiver.bodies:
        assert SERVER_ONLY.isdisjoint(body), (
            f"{sorted(SERVER_ONLY & set(body))} is tts-long's to set")


def test_the_native_route_is_recorded_under_its_own_name(
        client: TestClient, receiver: Receiver, log: RunLog) -> None:
    """One record point covers all three routes; only `origin` differs.

    The pipeline must not learn what a route is beyond carrying this. Three
    routes share run() precisely so the compatibility layer cannot become a
    second pipeline, and a branch on the route in there would be the first
    crack in that.
    """
    assert native(client).status_code == 200
    drained(log)

    body = receiver.only()
    assert body["route"] == "/transcribe"
    # Null rather than a guess: the page, a script and a shell all send this
    # same multipart body, and a user agent read as a client would be stored
    # as a fact. `model` does not exist on this route at all.
    assert "client" not in body
    assert "model_requested" not in body


def test_the_clips_length_is_recorded_not_the_speechs(
        client: TestClient, receiver: Receiver, log: RunLog,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`audio_seconds` has to mean the same thing on all three kinds of record.

    For a clone job it is the audio produced; here it is the audio that
    ARRIVED, not what survived the VAD. Recording the post-VAD figure under
    that name would make a clip with long pauses look like a shorter clip
    transcribed slowly. `speech_seconds` carries the second number, which no
    other kind of record has.
    """
    kept = None

    def half(samples, tuning):  # noqa: ANN001, ANN202
        nonlocal kept
        from app import vad
        half_way = samples.size // 2
        kept = vad.Speech(samples=samples[:half_way],
                          spans=((0, half_way),), kept=0.5)
        return kept

    monkeypatch.setattr(pipeline, "_speech", half)
    assert openai(client, seconds=8.0).status_code == 200
    drained(log)

    body = receiver.only()
    assert body["audio_seconds"] == 8.0
    assert body["speech_seconds"] == 4.0


def test_the_window_count_reaches_the_record(
        client: TestClient, receiver: Receiver, log: RunLog,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`chunks` is how many passes the recogniser made, not a guess from length.

    A long clip is cut at the VAD's pauses and stitched, and a run that took
    three passes is a different run from one that took one — it is the number
    that says whether MAX_WINDOW_SECONDS is doing anything on this deployment.
    """
    monkeypatch.setattr(pipeline, "MAX_WINDOW_SECONDS", 10.0)
    assert openai(client, seconds=30.0).status_code == 200
    drained(log)

    assert receiver.only()["chunks"] == 3


def test_a_detected_language_reaches_the_record(
        gateway: FakeGateway, receiver: Receiver, log: RunLog) -> None:
    """Whisper reports one and Parakeet does not, so the field is optional.

    Absent means "this engine reports none", which is a real answer; a default
    of "en" written in here would be this service inventing a measurement.
    """
    pipeline.state.clear()
    pipeline.state["asr"] = SpeaksDutch()
    pipeline.state["rules"] = []
    try:
        client = TestClient(app, headers=gateway.headers(AUDIENCE))
        assert openai(client).status_code == 200
        drained(log)
        assert receiver.only()["language"] == "nl"
    finally:
        pipeline.state.clear()


# --- it must not reach the caller ------------------------------------------

def test_the_response_does_not_wait_on_the_log(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The receiver takes five seconds. The transcript must not.

    This is the whole reason for the thread and the queue. Written as a
    measurement rather than a claim about the code, because the failure it
    guards — somebody making the POST inline "just for now" — reads perfectly
    well and is invisible until tts-long is slow.
    """
    slow = Receiver(delay=5.0)
    monkeypatch.setattr(sender_module, "urlopen", slow)
    monkeypatch.setattr(pipeline, "runlog", sender())

    started = time.monotonic()
    assert openai(client).status_code == 200
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"the transcript took {elapsed:.2f}s against a receiver sleeping for "
        "5s; the record is on the caller's clock")
    # And it really was sent, so this is not passing because nothing was posted.
    assert slow.entered.wait(2.0)


def test_a_receiver_that_refuses_never_reaches_the_caller(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 is what TTS_RUNLOG_ACCEPT=0 or an older tts-long answers.

    The transcript is unaffected and the reason is visible in /health, which is
    the only shape of failure a log is allowed to have.
    """
    refusing = Receiver(error=urllib.error.HTTPError(
        RUNS, 404, "Not Found", {}, None))  # type: ignore[arg-type]
    monkeypatch.setattr(sender_module, "urlopen", refusing)
    replacement = sender()
    monkeypatch.setattr(pipeline, "runlog", replacement)

    response = openai(client)
    assert response.status_code == 200
    assert response.json()["text"] == TRANSCRIPT

    deadline = time.monotonic() + 5.0
    while replacement.last_error is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert replacement.stats()["sent"] == 0
    assert "404" in str(replacement.stats()["last_error"])
    assert health(client)["runlog"]["last_error"]


def test_an_unset_url_records_nothing_and_raises_nothing(
        client: TestClient, receiver: Receiver,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The default, and the deployment rule it exists for.

    This stack has to work completely with tts-long stopped, unplugged or
    lying about itself. Unset means no queue, no thread and no POST — not a
    connection refused once a request, which is the shape this would have if
    the URL had a default.
    """
    silent = RunLog(url=None, host="nas", service="stt", engine="parakeet")
    monkeypatch.setattr(pipeline, "runlog", silent)

    assert openai(client).status_code == 200
    assert native(client).status_code == 200
    assert receiver.bodies == []
    # THIS sender's thread, not "a thread called runlog": the senders other
    # tests in this file build are daemons that outlive them, so enumerating
    # every thread in the process would assert about somebody else's.
    assert silent._thread is None, (
        "a disabled sender started a sender thread; unset has to cost a None "
        "check and nothing else")
    body = health(client)
    assert body["runlog"]["url"] is None
    assert body["runlog"]["sent"] == 0
    assert body["host_label"] == "nas"


def test_a_full_queue_drops_and_says_so_in_health(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped record must be visible as a gap, not invisible as an absence.

    Dropping is the right trade — the alternative is holding memory for a
    receiver that may never come back — but only while somebody can see it
    happening. A row that is simply not there says nothing at all.
    """
    blocked = threading.Event()

    def stuck(request, timeout: float | None = None):  # noqa: ANN001, ARG001
        blocked.wait(10.0)
        return contextlib.nullcontext()

    monkeypatch.setattr(sender_module, "urlopen", stuck)
    replacement = sender()
    monkeypatch.setattr(pipeline, "runlog", replacement)
    try:
        for index in range(MAX_QUEUED + 20):
            replacement.record(kind="transcribe", status="done", text=f"{index}")

        assert replacement.stats()["dropped"] > 0
        assert health(client)["runlog"]["dropped"] > 0
        # The runs themselves are unaffected; it is their history that is lost.
        assert openai(client).status_code == 200
    finally:
        blocked.set()


def test_the_transcript_can_be_left_behind_without_losing_the_record(
        client: TestClient, receiver: Receiver,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """RUNLOG_TEXT=0, and this is the service the switch was written for.

    A transcript is the most sensitive thing either service handles. `chars`
    still goes, so the length survives the switch: the alternative is an
    operator choosing between sending every dictated word to a store and
    having no history at all.
    """
    quiet = sender(send_text=False)
    monkeypatch.setattr(pipeline, "runlog", quiet)

    assert openai(client).status_code == 200
    drained(quiet)
    body = receiver.only()
    assert "text" not in body
    assert body["chars"] == len(TRANSCRIPT)


# --- whose run it was --------------------------------------------------------

ALICE = "u_bbbbbbbbbbbbbbbb"


def test_a_users_run_is_recorded_as_theirs(
        client: TestClient, receiver: Receiver, log: RunLog,
        gateway: FakeGateway) -> None:
    """Owner and credential come from the assertion, on every route (D31).

    The key's ID rather than "session" is what tells the owner, in their own
    list, which of their keys spent the time.
    """
    key = gateway.headers(AUDIENCE, sub=ALICE, cred="k_bbbbbbbbbbbb",
                          scopes={"speech:transcribe"})
    assert openai(client, headers=key).status_code == 200
    assert client.post("/transcribe", headers=key, files={
        "file": ("clip.wav", wav(4.0), "audio/wav")}).status_code == 200
    drained(log, expected=2)

    assert [(body["owner"], body["credential"]) for body in receiver.bodies] == [
        (ALICE, "k_bbbbbbbbbbbb")] * 2


def test_a_satellites_run_is_recorded_as_the_hubs(
        client: TestClient, receiver: Receiver, log: RunLog,
        gateway: FakeGateway) -> None:
    """Household speech from a satellite is a system record, in no user's list."""
    hub = gateway.headers(AUDIENCE, kind="service", sub="svc:satellites")
    assert openai(client, headers=hub).status_code == 200
    drained(log)

    body = receiver.only()
    assert (body["owner"], body["credential"]) == ("svc:satellites", "svc:satellites")
    assert (body["owner"], body["credential"]) == (
        contract("transcribe")["owner"], contract("transcribe")["credential"])


def test_the_record_carries_this_services_key_and_nothing_it_was_sent(
        client: TestClient, receiver: Receiver, log: RunLog,
        gateway: FakeGateway) -> None:
    """The record goes to the gateway's internal listener as svc:stt (D6).

    And with an explicit header set (D65): the assertion this request arrived
    with is never what the record leaves with.
    """
    assert openai(client).status_code == 200
    drained(log)

    assert receiver.urls == [RUNS]
    headers = receiver.headers[0]
    assert headers["authorization"] == f"Bearer {gateway.service_key}"
    assert not [name for name in headers if name.startswith("x-calliope-")]
