"""The GPU runner's HTTP surface: the guard, the two documents, clips, validation.

THE GUARD IS TESTED AS RAW ASGI WHERE THE CLAIM IS ABOUT ORDER. "The key is
checked before the body is read" cannot be seen through a client that has
already sent the body, so those cases call the guard with a `receive` that
fails the test if it is ever awaited.

Everything else goes through the real app on a TestClient, with the GPU probe
and nvidia-smi faked (runner_support.py) and no worker unless a test needs one.
Every test is named after the mistake it prevents.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import itertools
import re
import socket
import time

import pytest
from starlette.testclient import TestClient

from runner_support import AUTH, RUNNER_KEY, settle, submit

ONE_GIB = 1 << 30
FREE_ASSET = b"RIFF" + b"\x00" * 4000


def _asgi(guard, scope_headers, path="/v1/services/chatterbox/jobs",
          method="POST"):
    """Call the guard once, with a receive() that must never be awaited."""
    sent: list[dict] = []

    async def never():
        raise AssertionError("receive() was awaited before the guard decided")

    async def collect(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path,
             "headers": scope_headers, "client": ("192.0.2.9", 5000)}
    asyncio.run(guard(scope, never, collect))
    return sent[0]["status"], sent


# ---------------------------------------------------------------- the guard --


def test_a_huge_body_without_a_key_is_refused_before_a_byte_is_read(runner_modules):
    """MEASURED: a 300 MiB POST with no key grew the process by 1.5 GB.

    As a FastAPI dependency the key was checked after the body had been read
    and parsed. The guard answers from the headers alone.
    """
    guard = runner_modules.api.Guard(lambda *a: None, RUNNER_KEY.encode())
    length = [(b"content-length", str(ONE_GIB).encode())]
    status, _ = _asgi(guard, length)
    assert status == 401
    status, _ = _asgi(guard, length + [(b"authorization",
                                        f"Bearer {RUNNER_KEY}".encode())])
    assert status == 413, "with the key, the cap still answers before the body"


@pytest.mark.parametrize("credential", [None, "Bearer not-the-key",
                                        f"Basic {RUNNER_KEY}",
                                        f"Bearer {RUNNER_KEY[:-1]}"])
def test_without_the_key_every_route_answers_401(runner, credential):
    """No route is exempt: not the documents, not a 404, not a parse error."""
    client = TestClient(runner.app)
    headers = {"authorization": credential} if credential else {}
    digest = "a" * 64
    job = "0" * 32
    for method, path, kwargs in [
            ("GET", "/v1/services", {}), ("GET", "/v1/status", {}),
            ("HEAD", f"/v1/assets/{digest}", {}),
            ("POST", "/v1/assets", {"content": FREE_ASSET}),
            ("POST", "/v1/services/chatterbox/jobs", {"json": {"segments": ["a"]}}),
            ("GET", f"/v1/services/chatterbox/jobs/{job}", {}),
            ("GET", f"/v1/services/chatterbox/jobs/{job}/result?artefact=x", {}),
            ("DELETE", f"/v1/services/chatterbox/jobs/{job}", {}),
            ("GET", "/openapi.json", {}), ("GET", "/docs", {}),
            ("GET", "/nope", {}), ("PUT", "/v1/services", {}),
            ("POST", "/v1/services/chatterbox/jobs",
             {"content": b"not json", "headers": {"content-type": "application/json"}})]:
        merged = {**headers, **kwargs.pop("headers", {})}
        answer = client.request(method, path, headers=merged, **kwargs)
        assert answer.status_code == 401, (method, path, answer.status_code)
        if method != "HEAD":
            assert answer.json() == {"error": "a valid bearer key is required",
                                     "status": 401}
        assert answer.headers["www-authenticate"] == "Bearer"


def test_with_the_key_there_are_no_documentation_routes_and_no_422(runner):
    assert runner.client.get("/openapi.json").status_code == 404
    assert runner.client.get("/docs").status_code == 404
    assert runner.client.get("/nope").json() == {"error": "no such route",
                                                 "status": 404}
    assert runner.client.put("/v1/services").status_code == 405
    bad = runner.client.post("/v1/services/chatterbox/jobs", content=b"{nope",
                             headers={"content-type": "application/json"})
    assert bad.status_code == 400
    assert bad.json() == {"error": "the body is not JSON", "status": 400}
    listed = runner.client.post("/v1/services/chatterbox/jobs", json=["a"])
    assert listed.status_code == 400, "a JSON list is not a job body"


def test_transfer_encoding_and_oversized_bodies_are_refused_by_their_headers(
        runner_modules, runner):
    guard = runner_modules.api.Guard(lambda *a: None, RUNNER_KEY.encode())
    key = [(b"authorization", f"Bearer {RUNNER_KEY}".encode())]
    status, _ = _asgi(guard, key + [(b"transfer-encoding", b"chunked")])
    assert status == 411
    over = str(32 * (1 << 20) + 1).encode()
    status, _ = _asgi(guard, key + [(b"content-length", over)], path="/v1/assets")
    assert status == 413, "an asset over 32 MiB"
    status, _ = _asgi(guard, key + [(b"content-length", str((1 << 20) + 1).encode())])
    assert status == 413, "a job body over 1 MiB"
    # AND A REAL CLIP UNDER THE CAP GOES THROUGH: 12 MiB, over every other
    # route's cap and well under the clip route's.
    clip = bytes(range(256)) * (12 * 4096)
    answer = runner.client.post("/v1/assets", content=clip)
    assert answer.status_code == 201, answer.text
    assert answer.json() == {"sha256": hashlib.sha256(clip).hexdigest(),
                             "bytes": len(clip)}


def test_the_seventeenth_request_in_flight_gets_503_not_a_queue(runner_modules):
    """Sixteen at once, counted by authenticated request, never by socket."""
    release = None
    entered = 0

    async def slow(scope, receive, send):
        nonlocal entered
        entered += 1
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        guard = runner_modules.api.Guard(slow, RUNNER_KEY.encode())
        statuses: list[int] = []

        async def one():
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                if message["type"] == "http.response.start":
                    statuses.append(message["status"])

            scope = {"type": "http", "method": "GET", "path": "/v1/status",
                     "headers": [(b"authorization", f"Bearer {RUNNER_KEY}".encode())]}
            await guard(scope, receive, send)

        tasks = [asyncio.create_task(one()) for _ in range(16)]
        while entered < 16:
            await asyncio.sleep(0.01)
        await one()
        assert statuses == [503], "the seventeenth was not refused at once"
        release.set()
        await asyncio.gather(*tasks)
        return statuses

    statuses = asyncio.run(scenario())
    assert statuses.count(200) == 16


def test_the_lifespan_reaches_the_app_so_shutdown_closes_the_dispatcher(runner_modules,
                                                                        tmp_path):
    """Refusing every scope that is not HTTP would also refuse the lifespan."""
    from runner_support import build_runner

    built = build_runner(runner_modules, tmp_path / "state")
    with TestClient(built.app):
        assert built.dispatcher._thread is not None, "the lifespan never started it"
        assert built.dispatcher._thread.is_alive()
    assert not built.dispatcher._thread.is_alive(), "close() never ran"


def test_the_key_is_compared_in_constant_time_and_never_with_equals(runner_modules):
    """The same rule as test_verification_is_never_disabled_anywhere_in_this_module."""
    from pathlib import Path

    source = Path(runner_modules.api.__file__).read_text(encoding="utf-8")
    assert "hmac.compare_digest(token.strip(), self.key)" in source
    code = re.sub(r'""".*?"""|#[^\n]*', "", source, flags=re.S)
    assert not re.search(r"self\.key\s*[!=]=|[!=]=\s*self\.key", code)
    assert not re.search(r"token[^\n]*\s==\s", code)


@pytest.mark.parametrize("content", [None, "a" * 31, "a" * 20 + " " + "b" * 20,
                                     "é" * 40])
def test_serve_refuses_a_missing_short_or_malformed_key_and_never_says_it(
        runner_modules, tmp_path, monkeypatch, caplog, capsys, content):
    cli = importlib.import_module("app.runner.__main__")
    path = tmp_path / "runner-key"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("RUNNER_API_KEY_FILE", str(path))
    with pytest.raises(SystemExit) as stopped:
        cli.main(["serve"])
    said = str(stopped.value.code)
    assert isinstance(stopped.value.code, str), "a refusal must exit non-zero"
    assert str(path) in said, "the sentence must name the file"
    out = capsys.readouterr()
    for text in (said, caplog.text, out.out, out.err):
        if content:
            assert content.strip() not in text, "the key's value was printed"


def test_importing_the_cli_does_nothing(runner_modules):
    """The tests import it, and an import must start nothing."""
    from pathlib import Path

    source = Path(importlib.import_module("app.runner.__main__").__file__).read_text()
    assert source.rstrip().endswith('if __name__ == "__main__":\n    raise SystemExit(main())')


# ------------------------------------------------- the services and status --


def test_one_row_per_hosted_engine_and_each_reads_ready_to_tts_long(runner):
    from app.remote import RunnerClient, RunnerConfig

    doc = runner.client.get("/v1/services").json()
    assert [row["id"] for row in doc["services"]] == ["chatterbox", "chatterbox-turbo"]
    client = RunnerClient(RunnerConfig(host="192.0.2.10", fingerprint="0" * 64))
    for row in doc["services"]:
        assert row["manifest"]["id"] == row["id"]
        assert row["manifest"]["outputs"] == "audio/pcm-f32@24000"
        assert client._service_state(row, doc) == (True, "")
    assert doc["mode"] == "always-on" and doc["machine_state"] == "free"
    assert "limits" not in doc, "a cap of nothing would be drawn as a cap of zero"


def test_a_failed_probe_says_no_usable_gpu_everywhere(runner):
    runner.probe.state, runner.probe.reason = "failed", "no CUDA device is visible"
    services = runner.client.get("/v1/services").json()
    status = runner.client.get("/v1/status").json()
    for row in services["services"]:
        assert row["available"] is False
        assert row["unavailable_reason"] == "no usable GPU: no CUDA device is visible"
    assert services["machine_state"] == "nogpu"
    assert (status["state"], status["can_run"]) == ("no gpu", False)
    answer = submit(runner.client, segments=["Hello."])
    assert answer.status_code == 503
    assert "no usable GPU" in answer.json()["error"]


def test_a_probe_still_running_reads_checking(runner):
    runner.probe.state = "checking"
    status = runner.client.get("/v1/status").json()
    assert (status["state"], status["can_run"], status["machine_state"]) == \
        ("checking", False, "free")
    row = runner.client.get("/v1/services").json()["services"][0]
    assert (row["available"], row["unavailable_reason"]) == (False, "checking the GPU")


def test_a_full_card_closes_the_gate_only_while_nothing_of_ours_is_loaded(runner):
    """Something else on the host has the memory: refuse to load into it.

    A live worker's memory is ours to swap, so with one alive the gate is open
    whatever nvidia-smi says.
    """
    runner.sampler.reading = {"memory_free_mib": 1024, "memory_used_mib": 5000,
                              "utilisation_pct": 90, "power_watts": 80.0,
                              "pstate": "P2"}
    services = runner.client.get("/v1/services").json()
    status = runner.client.get("/v1/status").json()
    assert services["machine_state"] == "busy"
    assert all(not row["available"] for row in services["services"])
    assert services["services"][0]["unavailable_reason"].startswith(
        "the GPU has 1024 MiB free")
    assert (status["state"], status["can_run"]) == ("busy", False)
    assert status["gpu"]["memory_used_mib"] == 5000
    assert "memory_free_mib" not in status["gpu"]

    runner.sampler.reading = None
    job = submit(runner.client, segments=["Hello."]).json()["job_id"]
    settle(lambda: runner.client.get(f"/v1/services/chatterbox/jobs/{job}")
           .json()["status"] == "done")
    runner.sampler.reading = {"memory_free_mib": 1024}
    assert runner.dispatcher.worker_alive()
    row = runner.client.get("/v1/services").json()["services"][0]
    assert row["available"] is True, "our own loaded engine closed the gate"


def test_a_reading_from_before_an_idle_stop_does_not_close_the_gate(runner,
                                                                    monkeypatch):
    """Our worker's memory, read before it stopped, is not somebody else's.

    nvidia-smi is read every five seconds. Straight after an idle stop the last
    reading still counted the worker's memory as used: the gate closed, a
    submit got 503, and tts-long spoke the job on its CPU.
    """
    def done(job):
        return runner.client.get(f"/v1/services/chatterbox/jobs/{job}").json()[
            "status"] == "done"

    job = submit(runner.client, segments=["Hello."]).json()["job_id"]
    settle(lambda: done(job))
    runner.sampler.reading = {"memory_free_mib": 1024}
    assert runner.dispatcher.worker_alive()
    monkeypatch.setattr(runner.modules.jobs, "IDLE_SECONDS", 0.0)
    settle(lambda: not runner.dispatcher.worker_alive(),
           why="the idle worker was never stopped")
    assert runner.sampler.asked_again, "the card was not read again"
    assert runner.client.get("/v1/status").json()["machine_state"] == "free"
    after = submit(runner.client, segments=["Straight after."])
    assert after.status_code == 202, after.text
    settle(lambda: done(after.json()["job_id"]))

    # A reading taken once nothing of ours is loaded still closes it.
    settle(lambda: not runner.dispatcher.worker_alive(),
           why="the idle worker was never stopped")
    runner.sampler.reading = {"memory_free_mib": 1024}
    assert runner.client.get("/v1/status").json()["machine_state"] == "busy"
    assert submit(runner.client, segments=["Too full."]).status_code == 503


def test_the_sampler_drops_a_reading_from_before_and_reads_again_when_asked(
        runner_modules, monkeypatch):
    """The real Sampler's half of the test above, with nvidia-smi faked."""
    gpu = runner_modules.gpu
    count = itertools.count()
    monkeypatch.setattr(gpu.Sampler, "sample",
                        staticmethod(lambda: {"memory_free_mib": next(count)}))
    monkeypatch.setattr(gpu.shutil, "which", lambda _name: "nvidia-smi")
    monkeypatch.setattr(gpu, "SAMPLE_EVERY_S", 60.0)
    sampler = gpu.Sampler()
    sampler.start()
    try:
        first = settle(sampler.latest, why="the sampler never read the card")
        released = time.monotonic()
        assert sampler.latest(since=released) is None, "a reading from before counted"
        sampler.again()
        second = settle(lambda: sampler.latest(since=released),
                        why="again() did not read the card")
        assert second["memory_free_mib"] > first["memory_free_mib"]
    finally:
        sampler.stop()


def test_state_is_ready_when_idle_and_busy_while_a_job_runs(runner):
    idle = runner.client.get("/v1/status").json()
    assert (idle["state"], idle["can_run"], idle["job_running"]) == ("ready", True, False)
    job = submit(runner.client, segments=["__sleep:1__"]).json()["job_id"]
    busy = settle(lambda: (d := runner.client.get("/v1/status").json())["job_running"] and d)
    assert (busy["state"], busy["can_run"], busy["running_service"]) == \
        ("busy", True, "chatterbox")
    settle(lambda: runner.client.get(f"/v1/services/chatterbox/jobs/{job}")
           .json()["status"] == "done")


def test_the_documents_say_nothing_private(runner):
    """tts-long's gateway inlines /v1/status into an unauthenticated /health."""
    runner.sampler.reading = {"memory_free_mib": 9000, "memory_used_mib": 400,
                              "utilisation_pct": 3, "power_watts": 9.8,
                              "pstate": "P8"}
    text = (runner.client.get("/v1/status").text
            + runner.client.get("/v1/services").text)
    assert str(runner.store.root) not in text and "/state" not in text
    host = socket.gethostname().split(".")[0]
    assert len(host) < 4 or host not in text, "this machine's name"
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), "an address"


# ------------------------------------------------------------------- clips --


def test_a_clip_is_kept_under_its_own_digest_and_never_served_back(runner):
    digest = hashlib.sha256(FREE_ASSET).hexdigest()
    first = runner.client.post("/v1/assets", content=FREE_ASSET)
    again = runner.client.post("/v1/assets", content=FREE_ASSET)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json() == again.json() == {"sha256": digest, "bytes": len(FREE_ASSET)}
    assert (runner.store.assets / digest).read_bytes() == FREE_ASSET
    assert oct((runner.store.assets / digest).stat().st_mode & 0o777) == "0o600"
    assert runner.client.head(f"/v1/assets/{digest}").status_code == 200
    assert runner.client.head(f"/v1/assets/{'b' * 64}").status_code == 404
    assert runner.client.head("/v1/assets/NOT-HEX").status_code == 400
    assert runner.client.get(f"/v1/assets/{digest}").status_code == 405, \
        "a voice recording is readable back over the network"
    assert runner.client.post("/v1/assets", content=b"").status_code == 400


def test_a_clip_that_fails_mid_write_leaves_nothing_under_its_digest(
        runner_modules, tmp_path, monkeypatch):
    store = runner_modules.jobs.Store(tmp_path / "state")
    digest = hashlib.sha256(FREE_ASSET).hexdigest()

    def broken(fd):
        raise OSError("the disk went away")

    monkeypatch.setattr(runner_modules.jobs.os, "fsync", broken)
    with pytest.raises(OSError):
        store.put_asset(FREE_ASSET)
    monkeypatch.undo()
    assert not (store.assets / digest).exists(), "a short clip under its digest"
    assert list(store.assets.iterdir()) == []

    (store.assets / f"{digest}.leftover.tmp").write_bytes(b"half")
    runner_modules.jobs.Store(tmp_path / "state")
    assert list(store.assets.iterdir()) == [], "a leftover temporary file survived"


# -------------------------------------------------------------- validation --


@pytest.mark.parametrize("service,body,named", [
    ("chatterbox-turbo", {"segments": ["a"], "exaggeration": 0.5}, "exaggeration"),
    ("chatterbox", {"segments": ["a"], "language": "en", "speed": 1.0}, "speed"),
    ("chatterbox-turbo", {"segments": ["a"], "language": "en"}, "language"),
    ("chatterbox", {"segments": ["a"], "language": "en", "sample_rate": 22050},
     "sample_rate"),
    ("chatterbox", {"segments": ["a"], "language": "en", "sample_rate": True},
     "sample_rate"),
    ("chatterbox", {"segments": ["a"], "language": "xx"}, "language"),
    ("chatterbox", {"segments": ["a"]}, "language"),
    ("chatterbox", {"segments": ["a"], "language": "en", "temperature": 9.0},
     "temperature"),
    ("chatterbox", {"segments": ["a"], "language": "en", "cfg_weight": True},
     "cfg_weight"),
    ("chatterbox", {"segments": ["a"], "language": "en",
                    "reference_sha256": "c" * 64}, "reference_sha256"),
    ("chatterbox", {"segments": [], "language": "en"}, "segments"),
    ("chatterbox", {"segments": ["a" * 2001], "language": "en"}, "segment"),
    ("chatterbox", {"segments": [1], "language": "en"}, "segments"),
])
def test_every_field_is_honoured_or_refused_by_name(runner, service, body, named):
    answer = runner.client.post(f"/v1/services/{service}/jobs", json=body)
    assert answer.status_code == 400, answer.text
    said = answer.json()["error"]
    assert named in said, said
    assert answer.json()["status"] == 400


def test_the_refusal_says_what_the_engine_does_honour(runner):
    answer = submit(runner.client, "chatterbox-turbo", segments=["a"],
                    exaggeration=0.5)
    assert answer.json()["error"] == (
        "unsupported parameter 'exaggeration': chatterbox-turbo honours "
        "segments, sample_rate, reference_sha256, temperature")


def test_a_null_control_is_absent_and_an_unknown_service_is_404(runner):
    answer = submit(runner.client, segments=["Hello."], temperature=None,
                    exaggeration=0.3, sample_rate=24000)
    assert answer.status_code == 202, answer.text
    assert runner.client.post("/v1/services/voxtral/jobs",
                              json={"segments": ["a"]}).status_code == 404
    long_key = runner.client.post("/v1/services/chatterbox/jobs",
                                  json={"segments": ["a"], "language": "en"},
                                  headers={"Idempotency-Key": "k" * 513})
    assert long_key.status_code == 400


def test_a_known_clip_reaches_the_worker_as_a_path_inside_the_store(runner):
    digest = runner.client.post("/v1/assets", content=FREE_ASSET).json()["sha256"]
    parsed = runner.modules.api.validate(
        runner.services["chatterbox"],
        {"segments": ["a"], "language": "en", "reference_sha256": digest},
        runner.store)
    assert parsed["reference"] == str(runner.store.assets / digest)
    assert AUTH["authorization"].endswith(RUNNER_KEY)
