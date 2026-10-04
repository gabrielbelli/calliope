"""The GPU runner's job lifecycle, with a real spawned worker and a fake engine.

THE PROCESS BOUNDARY IS REAL. Each test starts app/runner/worker.py in a
process of its own through multiprocessing's `spawn`, over the real pipe,
writing the real files; tests/runner_fake.py stands in for the model and
logs every generation as `pid engine text`, which is how a test counts
generations and tells worker processes apart.

Every test is named after the mistake it prevents.
"""

from __future__ import annotations

import importlib
import random
import re
import time

import numpy as np
from starlette.testclient import TestClient

from runner_support import (RUNNER_KEY, build_runner, fake_synth_lines,
                            finished, generated, settle, submit)


def _doc(runner, job_id, service="chatterbox"):
    return runner.client.get(f"/v1/services/{service}/jobs/{job_id}").json()


def _pids(runner) -> list[str]:
    seen: list[str] = []
    for line in generated(runner.modules):
        pid = line.split(" ", 1)[0]
        if pid not in seen:
            seen.append(pid)
    return seen


def _pid_of(runner, text: str) -> str:
    return next(line.split(" ", 1)[0] for line in generated(runner.modules)
                if line.endswith(" " + text))


def _running(runner, job_id, service="chatterbox"):
    return settle(lambda: _doc(runner, job_id, service)["status"] == "running",
                  why="the job never started running")


# ------------------------------------------------------------- the audio --


def test_one_artefact_per_segment_and_the_record_adds_up(runner):
    """An empty segment is a zero-byte artefact, not a missing one.

    tts-long splices each segment's pause by its index, so a missing file
    would move every pause after it.
    """
    job = submit(runner.client, segments=["Hello there.", "", "Again."]).json()["job_id"]
    doc = finished(runner.client, "chatterbox", job)
    assert doc["status"] == "done", doc
    assert doc["artefacts"] == [f"{job}.0.f32", f"{job}.1.f32", f"{job}.2.f32"]
    sizes = []
    for name in doc["artefacts"]:
        raw = runner.client.get(f"/v1/services/chatterbox/jobs/{job}/result",
                                params={"artefact": name})
        assert raw.status_code == 200
        assert raw.headers["content-type"] == "application/octet-stream"
        audio = np.frombuffer(raw.content, dtype="<f4")
        assert np.allclose(audio, 0.1)
        sizes.append(audio.size)
    assert sizes == [1200, 0, 600]
    record = doc["record"]
    assert record["frames"] == sum(sizes)
    assert record["frame_rate"] == 24000
    assert record["input_tokens"] == 3
    assert record["segments"] == 3 and record["device"] == "cpu"
    assert record["peak_vram_mib"] is None


def test_the_same_idempotency_key_never_speaks_twice(runner):
    first = submit(runner.client, key="local-job-1", segments=["Only once."])
    assert first.status_code == 202
    job = first.json()["job_id"]
    finished(runner.client, "chatterbox", job)
    again = submit(runner.client, key="local-job-1", segments=["Only once."])
    assert again.status_code == 200
    assert again.json()["job_id"] == job and again.json()["reused"] is True
    assert again.json()["status"] == "done"
    assert [line for line in generated(runner.modules)
            if line.endswith("Only once.")].__len__() == 1
    other = submit(runner.client, "chatterbox-turbo", key="local-job-1",
                   segments=["Only once."])
    assert other.status_code == 409


def test_a_job_never_goes_from_running_back_to_queued(runner):
    job = submit(runner.client, segments=["one", "__sleep:0.5__", "three"]).json()["job_id"]
    seen: list[str] = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = _doc(runner, job)["status"]
        if not seen or seen[-1] != status:
            seen.append(status)
        if status == "done":
            break
        time.sleep(0.05)
    assert seen[-1] == "done", seen
    if "running" in seen:
        assert "queued" not in seen[seen.index("running"):], seen


# -------------------------------------------------------------- cancelling --


def test_a_queued_job_cancelled_is_finished_at_once_and_never_spoken(runner):
    first = submit(runner.client, segments=["__sleep:1.5__"]).json()["job_id"]
    _running(runner, first)
    second = submit(runner.client, segments=["Never spoken."]).json()["job_id"]
    said = runner.client.delete(f"/v1/services/chatterbox/jobs/{second}").json()
    assert said == {"job_id": second, "was": "queued", "cancelled_now": True,
                    "requested": True}
    doc = _doc(runner, second)
    assert doc["status"] == "cancelled"
    assert doc["record"] == {"cancelled_before_start": True}
    finished(runner.client, "chatterbox", first)
    assert not any(line.endswith("Never spoken.") for line in generated(runner.modules))


def test_a_running_job_cancelled_stops_between_segments_and_keeps_its_audio(runner):
    job = submit(runner.client, segments=["one", "__sleep:1__", "three", "four"]).json()["job_id"]
    settle(lambda: _doc(runner, job)["artefacts"], why="the first segment never landed")
    said = runner.client.delete(f"/v1/services/chatterbox/jobs/{job}").json()
    assert said["was"] == "running" and said["requested"] is True
    assert _doc(runner, job)["status"] == "cancelling"
    doc = finished(runner.client, "chatterbox", job)
    assert doc["status"] == "cancelled"
    done = doc["record"]["segments_done"]
    assert 1 <= done < 4
    assert doc["artefacts"] == [f"{job}.{n}.f32" for n in range(done)]
    assert not any(line.endswith(" four") for line in generated(runner.modules))
    finished_again = runner.client.delete(f"/v1/services/chatterbox/jobs/{job}").json()
    assert finished_again == {"job_id": job, "was": "cancelled",
                              "cancelled_now": False, "requested": False}


# ------------------------------------------------------------ the worker --


def test_a_worker_that_dies_fails_its_job_and_the_next_gets_a_new_one(runner):
    job = submit(runner.client, segments=["first words", "__crash__"]).json()["job_id"]
    doc = finished(runner.client, "chatterbox", job)
    assert doc["status"] == "failed"
    assert "exited with code 3" in doc["record"]["error"]
    assert runner.probe.asked_again >= 1, "a crash did not ask the probe again"
    after = submit(runner.client, segments=["after the crash"]).json()["job_id"]
    assert finished(runner.client, "chatterbox", after)["status"] == "done"
    assert _pid_of(runner, "after the crash") != _pid_of(runner, "__crash__")


def test_a_worker_killed_between_jobs_is_replaced_before_the_next_is_sent(runner):
    """The OOM killer does not wait for a job to be running."""
    import os
    import signal

    first = submit(runner.client, segments=["before the kill"]).json()["job_id"]
    finished(runner.client, "chatterbox", first)
    os.kill(int(_pid_of(runner, "before the kill")), signal.SIGKILL)
    settle(lambda: not runner.dispatcher.worker_alive(), timeout=5,
           why="a dead idle worker was never noticed")
    after = submit(runner.client, segments=["after the kill"]).json()["job_id"]
    assert finished(runner.client, "chatterbox", after)["status"] == "done"
    assert _pid_of(runner, "after the kill") != _pid_of(runner, "before the kill")
    assert runner.probe.asked_again >= 1


def test_an_ordinary_failure_keeps_the_worker_and_its_model(runner):
    job = submit(runner.client, segments=["__raise__"]).json()["job_id"]
    doc = finished(runner.client, "chatterbox", job)
    assert doc["status"] == "failed"
    assert doc["record"]["error"] == "ValueError: bad clip"
    after = submit(runner.client, segments=["still loaded"]).json()["job_id"]
    assert finished(runner.client, "chatterbox", after)["status"] == "done"
    assert _pid_of(runner, "still loaded") == _pid_of(runner, "__raise__")


def test_a_cuda_error_costs_the_worker_and_the_next_job_gets_a_fresh_one(runner):
    job = submit(runner.client, segments=["__cuda__"]).json()["job_id"]
    doc = finished(runner.client, "chatterbox", job)
    assert doc["status"] == "failed"
    assert doc["record"]["error"].startswith("RuntimeError: CUDA error")
    after = submit(runner.client, segments=["fresh context"]).json()["job_id"]
    assert finished(runner.client, "chatterbox", after)["status"] == "done"
    assert _pid_of(runner, "fresh context") != _pid_of(runner, "__cuda__")


def test_the_other_engine_unloads_the_first_before_it_loads(runner):
    """Baseline and Turbo together do not fit in 6 GB: one engine at a time."""
    one = submit(runner.client, segments=["baseline words"]).json()["job_id"]
    finished(runner.client, "chatterbox", one)
    two = submit(runner.client, "chatterbox-turbo", segments=["turbo words"]).json()["job_id"]
    finished(runner.client, "chatterbox-turbo", two)
    first, second = _pid_of(runner, "baseline words"), _pid_of(runner, "turbo words")
    assert first != second
    lines = settle(lambda: (ls := fake_synth_lines(runner.modules))
                   and f"{first} exit" in ls and ls)
    assert lines.index(f"{first} exit") < lines.index(f"{second} chatterbox-turbo turbo words")
    assert generated(runner.modules)[0].split(" ")[1] == "chatterbox"


def test_smoke_measures_each_engine_after_the_last_has_left_the_card(
        runner_modules, monkeypatch, capsys):
    """MEASURED: smoke loaded Turbo with baseline still resident.

    Every Synth's reaper thread holds it, so `del synth` freed nothing. On a
    6 GB card Turbo's load failed before its figures printed; on a larger one
    its peak included baseline's, and RUNNER_MIN_FREE_MIB is set from it.
    """
    monkeypatch.setenv("RUNNER_DEVICE", "cpu")
    cli = importlib.import_module("app.runner.__main__")
    assert cli.smoke([], factory="runner_fake:FakeSynth") == 0
    out = capsys.readouterr().out
    for service in ("chatterbox", "chatterbox-turbo"):
        assert re.search(rf"^{service}: load 0\.0 s, realtime factor \d+\.\d{{3}}x$",
                         out, re.M), out
    pids: dict[str, set[str]] = {}
    for line in generated(runner_modules):
        pid, engine, _text = line.split(" ", 2)
        pids.setdefault(engine, set()).add(pid)
    (first,), (second,) = pids["chatterbox"], pids["chatterbox-turbo"]
    assert first != second, "both engines were measured in one process"
    lines = fake_synth_lines(runner_modules)
    loaded = next(n for n, line in enumerate(lines) if line.startswith(f"{second} "))
    assert lines.index(f"{first} exit") < loaded, \
        "baseline was still resident when Turbo loaded"


def test_an_idle_worker_is_stopped_and_the_vram_is_given_back(runner, monkeypatch):
    monkeypatch.setattr(runner.modules.jobs, "IDLE_SECONDS", 1.0)
    job = submit(runner.client, segments=["then quiet"]).json()["job_id"]
    finished(runner.client, "chatterbox", job)
    settle(lambda: not runner.dispatcher.worker_alive(), timeout=10,
           why="the idle worker was never stopped")
    status = runner.client.get("/v1/status").json()
    assert not any(row["running"] for row in status["services"])
    pid = _pid_of(runner, "then quiet")
    assert f"{pid} exit" in fake_synth_lines(runner.modules)


def test_jobs_arriving_around_the_idle_limit_all_finish(runner, monkeypatch):
    """The dispatcher owns the idle stop, so no job is sent to an exiting worker."""
    monkeypatch.setattr(runner.modules.jobs, "IDLE_SECONDS", 0.5)
    rng = random.Random(7)
    ids = []
    for n in range(20):
        ids.append(submit(runner.client, segments=[f"line {n}"]).json()["job_id"])
        settle(lambda: _doc(runner, ids[-1])["status"] != "queued", timeout=15)
        time.sleep(rng.uniform(0.3, 0.7))
    statuses = [finished(runner.client, "chatterbox", job)["status"] for job in ids]
    assert statuses == ["done"] * 20, statuses


# --------------------------------------------------------------- orphans --


def test_a_job_nobody_polls_is_cancelled_and_the_card_freed(runner, monkeypatch):
    monkeypatch.setattr(runner.modules.jobs, "ORPHAN_SECONDS", 2.0)
    monkeypatch.setattr(runner.modules.jobs, "CANCEL_GRACE_S", 1.0)
    orphan = submit(runner.client, segments=["__sleep:10__"]).json()["job_id"]
    started = time.monotonic()
    job = settle(lambda: (j := runner.store.get(orphan)).finished and j, timeout=15,
                 why="the orphan was never cancelled")
    assert job.status == "cancelled"
    assert job.record.get("orphaned") is True
    next_job = submit(runner.client, segments=["next in line"]).json()["job_id"]
    assert finished(runner.client, "chatterbox", next_job)["status"] == "done"
    assert time.monotonic() - started < 2.0 + 1.0 + 6.0, "the card was held too long"


def test_a_job_that_is_polled_is_never_an_orphan(runner, monkeypatch):
    monkeypatch.setattr(runner.modules.jobs, "ORPHAN_SECONDS", 1.0)
    job = submit(runner.client, segments=["__sleep:2.5__"]).json()["job_id"]
    doc = finished(runner.client, "chatterbox", job)
    assert doc["status"] == "done"


# ----------------------------------------------------------- housekeeping --


def test_the_fifth_queued_job_is_refused(runner):
    first = submit(runner.client, segments=["__sleep:2__"]).json()["job_id"]
    _running(runner, first)
    queued = [submit(runner.client, segments=[f"q{n}"]) for n in range(4)]
    assert [a.status_code for a in queued] == [202] * 4
    fifth = submit(runner.client, segments=["one too many"])
    assert fifth.status_code == 503
    assert fifth.json() == {"error": "the queue is full", "status": 503}
    for answer in queued:
        runner.client.delete(f"/v1/services/chatterbox/jobs/{answer.json()['job_id']}")


def test_finished_jobs_are_swept_with_their_files(runner, monkeypatch):
    job = submit(runner.client, segments=["swept"]).json()["job_id"]
    finished(runner.client, "chatterbox", job)
    directory = runner.store.get(job).dir
    monkeypatch.setattr(runner.modules.jobs, "KEEP_SECONDS", 0.0)
    settle(lambda: runner.client.get(f"/v1/services/chatterbox/jobs/{job}")
           .status_code == 404, timeout=5,
           why="a job past its retention was still answered")
    assert not directory.exists()


def test_only_the_newest_finished_jobs_are_kept(runner, monkeypatch):
    monkeypatch.setattr(runner.modules.jobs, "KEEP_JOBS", 3)
    ids = []
    for n in range(4):
        ids.append(submit(runner.client, segments=[f"kept {n}"]).json()["job_id"])
        finished(runner.client, "chatterbox", ids[-1])
    settle(lambda: runner.store.get(ids[0]) is None, timeout=5,
           why="the oldest finished job was not evicted")
    assert all(runner.store.get(job) is not None for job in ids[1:])


def test_no_segment_text_stays_in_the_store_once_a_job_runs(runner):
    job = submit(runner.client, segments=["__sleep:1__", "secret words"]).json()["job_id"]
    _running(runner, job)
    assert runner.store.get(job).request is None
    assert not runner.store.has_text()
    finished(runner.client, "chatterbox", job)


def test_a_restart_forgets_jobs_and_keeps_clips(runner_modules, tmp_path):
    root = tmp_path / "state"
    store = runner_modules.jobs.Store(root)
    digest, _ = store.put_asset(b"a clip")
    (store.jobs_dir / ("f" * 32)).mkdir()
    (store.jobs_dir / ("f" * 32) / "x.f32").write_bytes(b"1234")
    again = runner_modules.jobs.Store(root)
    assert list(again.jobs_dir.iterdir()) == []
    assert again.has_asset(digest)


def test_the_result_route_serves_only_this_jobs_finished_segments(runner):
    other = submit(runner.client, segments=["other job"]).json()["job_id"]
    finished(runner.client, "chatterbox", other)
    job = submit(runner.client, segments=["__sleep:1__", "two"]).json()["job_id"]
    _running(runner, job)
    early = runner.client.get(f"/v1/services/chatterbox/jobs/{job}/result",
                              params={"artefact": f"{job}.0.f32"})
    assert early.status_code == 409
    finished(runner.client, "chatterbox", job)
    for name in ("../x", f"{other}.0.f32", f"{job}.0.wav", f"{job}.0.f32.part",
                 f"{job}.9.f32", ""):
        answer = runner.client.get(f"/v1/services/chatterbox/jobs/{job}/result",
                                   params={"artefact": name})
        assert answer.status_code == 404, name
    assert runner.client.get(f"/v1/services/chatterbox/jobs/{'0' * 32}").status_code == 404
    assert runner.client.get(f"/v1/services/chatterbox/jobs/{job.upper()}").status_code == 404
    assert runner.client.get(f"/v1/services/chatterbox-turbo/jobs/{job}").status_code == 404


def test_shutdown_cancels_the_running_job_within_the_grace(runner_modules, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(runner_modules.jobs, "SHUTDOWN_GRACE_S", 1.0)
    built = build_runner(runner_modules, tmp_path / "state")
    with TestClient(built.app) as client:
        client.headers["authorization"] = f"Bearer {RUNNER_KEY}"
        job = submit(client, segments=["__sleep:30__"]).json()["job_id"]
        settle(lambda: client.get(f"/v1/services/chatterbox/jobs/{job}")
               .json()["status"] == "running")
        started = time.monotonic()
    took = time.monotonic() - started
    assert built.store.get(job).status == "cancelled"
    assert took < 1.0 + 5.0, f"shutdown took {took:.1f} s"
    assert not built.dispatcher.worker_alive()
