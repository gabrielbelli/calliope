"""tts-long's real RunnerClient against the real runner, over real TLS.

THE CLAIM THE WHOLE RUNNER RESTS ON is that tts-long needs no new client: the
runner speaks offpeak's protocol closely enough that `RunnerClient` and
`RemoteSynth`, unchanged, offer, upload, submit, poll, collect and cancel
against it. So nothing here is a stand-in on tts-long's side. The runner is
served by uvicorn through server.build_config on 127.0.0.1 with a certificate
minted in tmp_path, the client pins that certificate's fingerprint, and the
only fake anywhere is the engine inside the spawned worker.

Every test is named after the mistake it prevents.
"""

from __future__ import annotations

import time
import uuid

import numpy as np
import pytest
import soundfile

from runner_support import RUNNER_KEY, build_runner, settle, tls_server

SAMPLES_PER_CHAR = 100   # tests/runner_fake.py


class _Key:
    """The RunnerKey seam: the key, and no second opinion after a 401."""

    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def refused(self, _key: str) -> None:
        return None


class _Recorder:
    """Every request that got past TLS, as (method, path), in front of the guard."""

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app
        self.seen: list[tuple[str, str]] = []

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] == "http":
            self.seen.append((scope["method"], scope["path"]))
        await self.app(scope, receive, send)


@pytest.fixture
def remote(runner_modules, tmp_path, monkeypatch):
    """The runner served over TLS, and tts-long's client for it."""
    import types

    from app.remote import RunnerClient, RunnerConfig

    cert, key = runner_modules.tls.ensure(tmp_path / "tls")
    built = build_runner(runner_modules, tmp_path / "state")
    recorder = _Recorder(built.app)
    with tls_server(runner_modules, recorder, cert, key) as port:
        cfg = RunnerConfig(host="127.0.0.1", port=port,
                           fingerprint=runner_modules.tls.fingerprint(cert),
                           poll=0.05, offer_timeout=5.0, timeout=10.0)
        client = RunnerClient(cfg, key=_Key(RUNNER_KEY))
        yield types.SimpleNamespace(client=client, cfg=cfg, built=built,
                                    seen=recorder.seen, port=port,
                                    modules=runner_modules)


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "voice.wav"
    soundfile.write(str(path), np.zeros(24000, dtype=np.float32), 24000)
    return str(path)


def test_both_services_are_offered_as_ready(remote):
    offer = remote.client.offer()
    assert offer.ready and offer.why == ""
    assert offer.by_service == {"chatterbox": (True, ""),
                                "chatterbox-turbo": (True, "")}
    assert offer.device == "cpu"
    assert offer.cpu_pct is None, "the runner publishes no cap, so none is read"


def test_a_job_speaks_through_remote_synth_with_tts_longs_own_pauses(remote, clip):
    from app.engines import ENGINES
    from app.main import FRAME_TOLERANCE_S
    from app.remote import RemoteSynth

    chunks: list[np.ndarray] = []
    synth = RemoteSynth(remote.client, job_id=uuid.uuid4().hex,
                        spec=ENGINES["chatterbox"])
    segments = [("Hello there.", 0.5), ("", 0.25), ("Again.", 0.0)]
    spoken = synth.speak_segments(segments, "en", {"temperature": 0.6},
                                  reference=clip, on_chunk=chunks.append)
    generated = (len("Hello there.") + len("Again.")) * SAMPLES_PER_CHAR
    pauses = int(24000 * 0.5) + int(24000 * 0.25)
    assert spoken.audio.size == generated + pauses
    assert len(chunks) == 3, "on_chunk once per segment, the empty one included"
    assert spoken.input_tokens == 3
    claimed = synth.frames / synth.frame_rate
    assert abs(claimed - spoken.audio.size / 24000 + 0.75) <= FRAME_TOLERANCE_S
    assert synth.frames == generated and synth.frame_rate == 24000


def test_a_clip_crosses_the_network_once(remote, clip):
    from app.engines import ENGINES
    from app.remote import RemoteSynth

    for _ in range(2):
        RemoteSynth(remote.client, job_id=uuid.uuid4().hex,
                    spec=ENGINES["chatterbox"]).speak_segments(
            [("Twice.", 0.0)], "en", {}, reference=clip)
    assert remote.seen.count(("POST", "/v1/assets")) == 1


def test_turbo_is_sent_temperature_alone_and_succeeds(remote, clip):
    from app.engines import ENGINES
    from app.remote import RemoteSynth

    turbo = remote.client.for_service("chatterbox-turbo")
    synth = RemoteSynth(turbo, job_id=uuid.uuid4().hex,
                        spec=ENGINES["chatterbox-turbo"])
    spoken = synth.speak_segments([("Quickly now.", 0.0)], "en",
                                  {"temperature": 0.7, "exaggeration": 0.3,
                                   "cfg_weight": 0.3}, reference=clip)
    assert spoken.audio.size == len("Quickly now.") * SAMPLES_PER_CHAR
    assert ("POST", "/v1/services/chatterbox-turbo/jobs") in remote.seen


def test_a_script_of_more_than_2000_short_lines_is_spoken_on_the_card(remote):
    """tts-long caps characters, not segments: 2,001 lines are 2,001 segments.

    The runner refused more than 2,000 with a 400, which tts-long reads as a
    lost lane: the job went back to its CPU and the lane cooled for 30 s.
    """
    from app.engines import ENGINES
    from app.remote import RemoteSynth

    lines = [(f"Line {n}.", 0.0) for n in range(2001)]
    spoken = RemoteSynth(remote.client, job_id=uuid.uuid4().hex,
                         spec=ENGINES["chatterbox"]).speak_segments(lines, "en", {})
    assert spoken.audio.size == sum(len(t) for t, _ in lines) * SAMPLES_PER_CHAR


def test_a_wrong_key_is_a_lost_lane_not_a_failed_job(remote):
    from app.remote import RemoteUnavailable, RunnerClient

    stranger = RunnerClient(remote.cfg, key=_Key("x" * 40))
    with pytest.raises(RemoteUnavailable) as lost:
        stranger.offer()
    assert "401" in str(lost.value)


def test_a_wrong_fingerprint_sends_nothing_at_all(remote):
    from dataclasses import replace

    from app.remote import RemoteUnavailable, RunnerClient

    before = len(remote.seen)
    impostor = RunnerClient(replace(remote.cfg, fingerprint="0" * 64),
                            key=_Key(RUNNER_KEY))
    with pytest.raises(RemoteUnavailable) as lost:
        impostor.offer()
    assert "fingerprint mismatch" in str(lost.value)
    assert len(remote.seen) == before, "a request crossed before the pin was checked"


def test_the_status_panel_reads_the_runner(remote):
    snap = remote.client.snapshot(max_age=0)
    assert snap["reachable"] is True
    assert snap["state"] == "ready" and snap["mode"] == "always-on"
    assert [row["id"] for row in snap["services"]] == ["chatterbox", "chatterbox-turbo"]
    assert all(row["available"] for row in snap["services"])


def test_a_cancel_in_tts_long_cancels_the_job_on_the_runner(remote):
    from app.engines import ENGINES
    from app.remote import RemoteSynth

    stop = {"now": False}
    synth = RemoteSynth(remote.client, job_id=uuid.uuid4().hex,
                        spec=ENGINES["chatterbox"])
    spoken = synth.speak_segments(
        [("first", 0.0), ("__sleep:1__", 0.0), ("third", 0.0), ("fourth", 0.0)],
        "en", {}, on_chunk=lambda _piece: stop.update(now=True),
        cancelled=lambda: stop["now"])
    assert any(method == "DELETE" for method, _path in remote.seen), \
        "tts-long gave up on the job and never told the runner"
    assert spoken.audio.size >= len("first") * SAMPLES_PER_CHAR
    store = remote.built.store
    job = settle(lambda: next((j for j in store._jobs.values() if j.finished), None),
                 why="the runner's job never ended")
    assert job.status == "cancelled"


def test_a_client_that_stops_polling_does_not_hold_the_card(remote, monkeypatch):
    """tts-long restarted mid-job: nobody polls, and the next job must not wait."""
    monkeypatch.setattr(remote.modules.jobs, "ORPHAN_SECONDS", 2.0)
    monkeypatch.setattr(remote.modules.jobs, "CANCEL_GRACE_S", 1.0)
    from app.engines import ENGINES
    from app.remote import RemoteSynth

    abandoned = remote.client.submit({"segments": ["__sleep:20__"], "language": "en",
                                      "sample_rate": 24000}, uuid.uuid4().hex)
    started = time.monotonic()
    spoken = RemoteSynth(remote.client, job_id=uuid.uuid4().hex,
                         spec=ENGINES["chatterbox"]).speak_segments(
        [("Next in line.", 0.0)], "en", {})
    took = time.monotonic() - started
    assert spoken.audio.size == len("Next in line.") * SAMPLES_PER_CHAR
    assert took < 2.0 + 1.0 + 6.0, f"the next job waited {took:.1f} s"
    job = remote.built.store.get(abandoned)
    assert job.status == "cancelled" and job.record.get("orphaned") is True
