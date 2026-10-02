"""Whose job it is, whose voice it is, and how much of the queue one person holds.

THE BOUNDARY THIS FILE GUARDS. This service decides nothing about access: the
gateway does, once, and signs who is asking (D3, D4). What is decided here is
which rows that person's question is ABOUT. A listing is filtered to the
caller before it is counted or capped, another user's job answers 404 exactly
as a job that does not exist, and only an `:all` scope widens either (D31,
D32). A cloned voice is personal: it resolves in its owner's namespace and in
nobody else's, an admin's included (D35).

Every request here carries an assertion the test gateway signed for a named
person, so each test says who is asking rather than inheriting the conftest's
default admin.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from voice_common.conformance import FakeGateway
from voice_common.scopes import PRESETS, session_scopes

AUDIENCE = "tts-long"
ADMIN = FakeGateway.USER
ALICE = "u_alicealicealicea"
BOB = "u_bobbobbobbobbobb"


def as_user(gateway, sub, role="speech", cred="session"):
    """The header the gateway forwards for this person's signed-in session."""
    return gateway.headers(AUDIENCE, sub=sub, scopes=session_scopes(role),
                           cred=cred)


def as_service(gateway, name):
    return gateway.headers(AUDIENCE, kind="service", sub=f"svc:{name}")


def _wait(client, job_id, headers, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=headers).json()
        if job["status"] in {"done", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished")


def _speak(client, headers, text="One short line.", voice="default"):
    created = client.post("/jobs", json={"text": text, "voice": voice},
                          headers=headers)
    assert created.status_code == 202, created.text
    return created.json()["id"]


def _clip(directory: Path, name: str, seconds: float = 6.0) -> None:
    """A real file with a real header, as conftest's voice_dir writes them."""
    import soundfile

    directory.mkdir(parents=True, exist_ok=True)
    soundfile.write(str(directory / f"{name}.wav"),
                    np.zeros(int(24000 * seconds), dtype=np.float32), 24000)


# ----------------------------------------------------------------- jobs ---


def test_one_user_cannot_list_read_play_or_delete_another_users_job(
        speech, gateway):
    """Every route that names a job answers another user's ID with the 404 a
    missing ID gets, word for word: a 403 would confirm the job exists."""
    bob, alice = as_user(gateway, BOB), as_user(gateway, ALICE)
    job_id = _speak(speech, bob)
    _wait(speech, job_id, bob)
    missing = speech.get("/jobs/no-such-job", headers=alice)

    listed = [j["id"] for j in speech.get("/jobs", headers=alice).json()["jobs"]]
    assert job_id not in listed
    for method, path in (("GET", f"/jobs/{job_id}"),
                         ("GET", f"/jobs/{job_id}/audio"),
                         ("DELETE", f"/jobs/{job_id}/audio"),
                         ("DELETE", f"/jobs/{job_id}")):
        refused = speech.request(method, path, headers=alice)
        assert refused.status_code == 404, f"{method} {path}: {refused.text}"
        assert refused.json() == missing.json()

    kept = speech.get(f"/jobs/{job_id}", headers=bob).json()
    assert kept["audio"]["state"] == "present", "alice's DELETE reached bob's job"


def test_counts_are_per_owner(speech, gateway):
    """Counted after the owner filter, so "Everything (3)" never tells one
    person how much the rest of the household has said."""
    bob, alice = as_user(gateway, BOB), as_user(gateway, ALICE)
    ids = [_speak(speech, bob), _speak(speech, bob), _speak(speech, alice)]
    for job_id, who in zip(ids, (bob, bob, alice)):
        _wait(speech, job_id, who)

    assert speech.get("/jobs", headers=alice).json()["counts"]["all"] == 1
    assert speech.get("/jobs", headers=bob).json()["counts"]["all"] == 2
    everyone = speech.get("/jobs?owner=all").json()
    assert everyone["counts"]["all"] == 3
    assert {j["owner"] for j in everyone["jobs"]} == {ALICE, BOB}


def test_the_owner_filter_runs_before_the_limit(speech, gateway):
    """A cap applied before a filter is a cap on the wrong set: bob's newer
    jobs must not push alice's only one off the end of her own listing."""
    bob, alice = as_user(gateway, BOB), as_user(gateway, ALICE)
    _wait(speech, _speak(speech, alice), alice)
    for _ in range(3):
        _wait(speech, _speak(speech, bob), bob)

    mine = speech.get("/jobs?limit=1", headers=alice).json()
    assert [j["owner"] for j in mine["jobs"]] == [ALICE]
    assert mine["truncated"] is False


def test_a_job_takes_its_owner_from_the_assertion_and_never_from_the_body(
        speech, gateway):
    bob_key = as_user(gateway, BOB, cred="k_bobkeybobkey")
    job_id = _speak(speech, bob_key)
    row = _wait(speech, job_id, bob_key)
    assert (row["owner"], row["credential"]) == (BOB, "k_bobkeybobkey")

    forged = speech.post("/jobs", json={"text": "hi", "owner": ALICE},
                         headers=bob_key)
    assert forged.status_code == 422, "a body field named the owner"


def test_a_system_record_is_visible_only_with_jobs_read_all(speech, gateway):
    """A run the hub caused is the household's, not anybody's to read (D32)."""
    run = speech.post("/runs", json={"kind": "transcribe", "service": "stt",
                                     "engine": "parakeet", "text": "lights off",
                                     "owner": "svc:satellites",
                                     "credential": "svc:satellites"},
                      headers=as_service(gateway, "stt"))
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    alice = as_user(gateway, ALICE)

    assert speech.get(f"/jobs/{run_id}", headers=alice).status_code == 404
    assert speech.get("/jobs?owner=system", headers=alice).status_code == 403
    assert run_id not in [j["id"] for j in
                          speech.get("/jobs", headers=alice).json()["jobs"]]

    system = speech.get("/jobs?owner=system").json()["jobs"]
    assert [j["id"] for j in system] == [run_id]
    assert speech.get(f"/jobs/{run_id}?owner=system").json()["owner"] \
        == "svc:satellites"
    assert run_id not in [j["id"] for j in speech.get("/jobs").json()["jobs"]], (
        "an admin's default listing widened to the system's rows unasked")
    assert speech.get(f"/jobs/{run_id}").status_code == 404, (
        "an admin's read by ID widened to the system's rows unasked")


def test_a_record_with_no_owner_is_a_system_record(speech, gateway):
    """Every record written before there were users has no owner, and none is
    rewritten to say so: absent IS system."""
    from app import main

    (main.OUT_DIR / "runs").mkdir(parents=True, exist_ok=True)
    main._sidecar("before-users").write_text(json.dumps(
        {"status": "done", "kind": "speech", "finished_at": 1.0,
         "created_at": 1.0}))
    main._recover()

    assert speech.get("/jobs/before-users",
                      headers=as_user(gateway, ALICE)).status_code == 404
    assert [j["id"] for j in speech.get("/jobs?owner=system").json()["jobs"]] \
        == ["before-users"]
    row = speech.get("/jobs/before-users?owner=system")
    assert row.status_code == 200, row.text
    assert "owner" not in row.json()


@pytest.mark.parametrize("value", ["../x", "u_short", "svc:stt", "ALL"])
def test_an_owner_filter_that_is_not_an_owner_answers_400(speech, value):
    """Checked before it is used anywhere, so `../x` is never a comparison, a
    path or a log line."""
    refused = speech.get("/jobs", params={"owner": value})
    assert refused.status_code == 400, refused.text


def test_naming_someone_else_needs_jobs_read_all(speech, gateway):
    alice = as_user(gateway, ALICE)
    for value in ("all", "system", BOB):
        refused = speech.get(f"/jobs?owner={value}", headers=alice)
        assert refused.status_code == 403, value
        assert refused.json()["error"]["code"] == "insufficient_scope"
    assert speech.get(f"/jobs?owner={ALICE}", headers=alice).status_code == 200
    assert speech.get("/jobs?owner=me", headers=alice).status_code == 200


def test_an_admin_can_ask_for_one_users_jobs(speech, gateway):
    bob, alice = as_user(gateway, BOB), as_user(gateway, ALICE)
    bobs = _speak(speech, bob)
    _wait(speech, _speak(speech, alice), alice)
    _wait(speech, bobs, bob)
    listed = speech.get(f"/jobs?owner={BOB}").json()["jobs"]
    assert [j["id"] for j in listed] == [bobs]


def test_an_admin_reaches_another_users_job_by_id_only_by_naming_its_owner(
        speech, gateway):
    """The listing's rule, applied to one ID. Holding `:all` used to be
    enough, so an admin could read, play or delete any transcript without an
    `?owner=` on the request, and the gateway audits an `:all` access by that
    parameter alone: nothing was recorded anywhere."""
    bob = as_user(gateway, BOB)
    job_id = _wait(speech, _speak(speech, bob), bob)["id"]
    missing = speech.get("/jobs/no-such-job").json()

    for method, path in (("GET", f"/jobs/{job_id}"),
                         ("GET", f"/jobs/{job_id}/audio"),
                         ("DELETE", f"/jobs/{job_id}/audio"),
                         ("DELETE", f"/jobs/{job_id}")):
        for owner in (None, "me", "system", ALICE):
            refused = speech.request(method, path, params={"owner": owner}
                                     if owner else None)
            assert refused.status_code == 404, f"{method} {path} as {owner}"
            assert refused.json() == missing
    assert speech.get(f"/jobs/{job_id}", headers=bob).json()["audio"]["state"] \
        == "present", "a refused admin request reached bob's job"

    for owner in ("all", BOB):
        assert speech.get(f"/jobs/{job_id}", params={"owner": owner}
                          ).status_code == 200, owner
        assert speech.get(f"/jobs/{job_id}/audio", params={"owner": owner}
                          ).status_code == 200, owner
    assert speech.delete(f"/jobs/{job_id}", params={"owner": BOB}
                         ).json()["status"] == "deleted"


def test_a_monitor_key_can_read_anybodys_job_but_not_delete_it(speech, gateway):
    """`?owner=` widens a delete only for a holder of jobs:delete:all; a key
    that may read everything is refused by scope, before any job is looked
    up."""
    bob = as_user(gateway, BOB)
    job_id = _wait(speech, _speak(speech, bob), bob)["id"]
    monitor = gateway.headers(AUDIENCE, sub=ADMIN, cred="k_monitorkeymo",
                              scopes=PRESETS["monitor"].scopes)

    assert speech.get(f"/jobs/{job_id}", params={"owner": "all"},
                      headers=monitor).status_code == 200
    for path in (f"/jobs/{job_id}", f"/jobs/{job_id}/audio"):
        refused = speech.delete(path, params={"owner": "all"}, headers=monitor)
        assert refused.status_code == 403, path
        assert refused.json()["error"]["code"] == "insufficient_scope"
    assert speech.get(f"/jobs/{job_id}", headers=bob).status_code == 200


def test_the_owner_survives_a_restart_and_a_forged_one_does_not(speech, gateway):
    """The record file is in a writable volume; an owner in it that is not an
    ID is dropped on the way in, and the row falls back to system."""
    from app import main

    bob = as_user(gateway, BOB)
    job_id = _wait(speech, _speak(speech, bob), bob)["id"]
    main._sidecar("forged").write_text(json.dumps(
        {"status": "done", "kind": "speech", "owner": "../../etc",
         "credential": "Bearer abc", "created_at": 1.0}))
    main.jobs.clear()
    main._recover()

    assert speech.get(f"/jobs/{job_id}", headers=bob).json()["owner"] == BOB
    forged = speech.get("/jobs/forged?owner=system")
    assert forged.status_code == 200, forged.text
    assert "owner" not in forged.json() and "credential" not in forged.json()
    assert speech.get("/jobs/forged", headers=bob).status_code == 404


# ---------------------------------------------------------------- /runs ---


@pytest.mark.parametrize("who", ["admin", "svc:satellites"])
def test_only_a_holder_of_runs_write_may_post_a_run(speech, gateway, who):
    """The page must never be a writer of the history it reads, and neither
    may a service that has no business keeping one."""
    headers = (gateway.headers(AUDIENCE) if who == "admin"
               else as_service(gateway, "satellites"))
    refused = speech.post("/runs", json={"kind": "speech", "service": "tts"},
                          headers=headers)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "insufficient_scope"


def test_a_service_says_whose_run_it_was(speech, gateway):
    """stt and tts know whose request they answered; the record is that
    person's, in that person's list (D31)."""
    posted = speech.post("/runs", json={"kind": "speech", "service": "tts",
                                        "owner": BOB,
                                        "credential": "k_bobkeybobkey"},
                         headers=as_service(gateway, "tts"))
    assert posted.status_code == 201, posted.text
    bobs = speech.get("/jobs", headers=as_user(gateway, BOB)).json()["jobs"]
    assert [(j["id"], j["credential"]) for j in bobs] == [
        (posted.json()["id"], "k_bobkeybobkey")]


def test_a_run_that_names_no_owner_belongs_to_its_sender(speech, gateway):
    posted = speech.post("/runs", json={"kind": "speech", "service": "tts"},
                         headers=as_service(gateway, "tts"))
    row = speech.get(f"/jobs/{posted.json()['id']}?owner=system").json()
    assert (row["owner"], row["credential"]) == ("svc:tts", "svc:tts")


@pytest.mark.parametrize("field,value", [
    ("owner", "../../etc/passwd"), ("owner", "admin"), ("owner", 7),
    ("credential", "Bearer calliope_x"), ("credential", "session:x"),
])
def test_a_run_with_a_malformed_owner_is_refused_by_name(speech, gateway,
                                                         field, value):
    body = {"kind": "speech", "service": "tts", "owner": BOB, field: value}
    refused = speech.post("/runs", json=body, headers=as_service(gateway, "stt"))
    assert refused.status_code == 400, refused.text
    assert field in refused.text


def test_the_record_rate_limit_is_per_sender_and_not_per_claimed_service(
        speech, gateway, monkeypatch):
    """A sender that varied `service` used to get a fresh minute each time."""
    from app import main

    monkeypatch.setattr(main, "RUNLOG_RATE", 2)
    main._runlog_hits.clear()
    codes = [speech.post("/runs", json={"kind": "speech", "service": name},
                         headers=as_service(gateway, "stt")).status_code
             for name in ("tts", "stt", "kokoro")]
    assert codes == [201, 201, 429]


# ---------------------------------------------------------------- limits ---


@pytest.fixture
def held(speech, monkeypatch):
    """Synthesis that waits until the test lets it go, so jobs stay live."""
    from app import synth as synth_module

    release = threading.Event()
    original = synth_module.Synth._speak

    def waits(self, *args, **kwargs):
        release.wait(30)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(synth_module.Synth, "_speak", waits)
    try:
        yield release
    finally:
        release.set()


def test_one_person_may_hold_at_most_four_live_jobs(speech, gateway, held):
    """The queue of 32 is the household's, so one tab in a loop cannot fill it
    (D37). Both routes count, both say how long to wait, and another person is
    untouched by it."""
    bob = as_user(gateway, BOB)
    for _ in range(4):
        _speak(speech, bob)

    refused = speech.post("/jobs", json={"text": "fifth", "voice": "default"},
                          headers=bob)
    assert refused.status_code == 429, refused.text
    assert int(refused.headers["Retry-After"]) >= 1
    openai = speech.post("/v1/audio/speech",
                         json={"input": "fifth", "response_format": "pcm"},
                         headers=bob)
    assert openai.status_code == 429
    assert openai.json()["error"]["type"] == "rate_limit_error"
    streamed = speech.post("/v1/audio/speech",
                           json={"input": "fifth", "response_format": "pcm",
                                 "stream_format": "sse"}, headers=bob)
    assert streamed.status_code == 429
    assert streamed.json()["error"]["type"] == "rate_limit_error"

    assert speech.post("/jobs", json={"text": "hers", "voice": "default"},
                       headers=as_user(gateway, ALICE)).status_code == 202


def test_a_service_is_not_held_to_the_per_user_cap(speech, gateway, held):
    """A service's work is somebody's request that already passed the check."""
    hub = as_service(gateway, "satellites")
    codes = [speech.post("/jobs", json={"text": f"line {i}", "voice": "default"},
                         headers=hub).status_code for i in range(6)]
    assert codes == [202] * 6


def test_concurrent_posts_from_one_person_never_get_past_the_cap(
        speech, gateway, held, monkeypatch):
    """The check and the insertion are one step. They were two, with the
    voice choice and the chunking between them, and sixteen simultaneous
    posts from one person got five jobs in against a cap of four. Both of
    those are slowed here so that any gap between check and insertion shows
    up every time rather than one run in three."""
    from app import main

    for name in ("_choose", "_segments"):
        original = getattr(main, name)

        def slow(*args, _original=original, **kwargs):
            time.sleep(0.02)
            return _original(*args, **kwargs)

        monkeypatch.setattr(main, name, slow)
    bob = as_user(gateway, BOB)
    start = threading.Barrier(16)
    codes: list[int] = []

    def post(i: int) -> None:
        start.wait()
        codes.append(speech.post("/jobs", json={"text": f"line {i}",
                                                "voice": "default"},
                                 headers=bob).status_code)

    threads = [threading.Thread(target=post, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(codes) == [202] * 4 + [429] * 12


def test_cancelling_a_queued_job_frees_its_place_at_once(speech, gateway, held):
    """The 429 says "collect or cancel one first". A cancelled job that is
    still queued generates nothing when the dispatcher reaches it, so it must
    not hold one of the four places until then."""
    from app import main

    bob = as_user(gateway, BOB)
    ids = [_speak(speech, bob) for _ in range(4)]
    assert speech.post("/jobs", json={"text": "fifth", "voice": "default"},
                       headers=bob).status_code == 429

    last = ids[-1]
    assert main.jobs[last]["status"] == "queued", "the test needs a queued job"
    assert speech.delete(f"/jobs/{last}", headers=bob).json()["status"] \
        == "cancelling"
    again = speech.post("/jobs", json={"text": "fifth", "voice": "default"},
                        headers=bob)
    assert again.status_code == 202, again.text


def test_an_over_long_text_is_413(speech, monkeypatch):
    """Segments count too: splitting the same text into pieces is the same
    hours of compute."""
    from app import main

    monkeypatch.setattr(main, "MAX_TEXT", 20)
    assert speech.post("/jobs", json={"text": "x" * 21}).status_code == 413
    pieces = [{"text": "x" * 11}, {"text": "x" * 10}]
    assert speech.post("/jobs", json={"segments": pieces}).status_code == 413
    assert speech.post("/jobs", json={"text": "x" * 20,
                                      "voice": "default"}).status_code == 202


# ---------------------------------------------------------------- voices ---


def test_one_user_cannot_speak_in_another_users_voice_and_an_admin_cannot_either(
        speech, gateway):
    """Holding a scope that can delete somebody's clip is not a licence to
    make it say things (D35). The refusal does not even name it."""
    from app import main

    _clip(main.VOICES.root / "users" / BOB, "bobs-voice")
    assert _wait(speech, _speak(speech, as_user(gateway, BOB),
                                voice="bobs-voice"),
                 as_user(gateway, BOB))["status"] == "done"

    for who in (as_user(gateway, ALICE), gateway.headers(AUDIENCE)):
        refused = speech.post("/jobs", json={"text": "hi", "voice": "bobs-voice"},
                              headers=who)
        assert refused.status_code == 400, refused.text
        assert "bobs-voice" not in refused.json()["detail"].split(":", 1)[1]
        openai = speech.post("/v1/audio/speech",
                             json={"input": "hi", "voice": "bobs-voice",
                                   "response_format": "pcm"}, headers=who)
        assert openai.status_code == 400
        assert openai.json()["error"]["param"] == "voice"


def test_each_caller_lists_only_the_voices_they_may_name(speech, gateway):
    from app import main

    _clip(main.VOICES.root, "narrator")
    _clip(main.VOICES.root / "users" / BOB, "bobs-voice")
    _clip(main.VOICES.root / "users" / ALICE, "alices-voice")

    def names(headers):
        return speech.get("/voices", headers=headers).json()["voices"]

    assert names(as_user(gateway, ALICE)) == ["default", "alices-voice"]
    assert names(as_user(gateway, BOB)) == ["default", "bobs-voice"]
    # The system namespace goes with voices:write:all, and nobody's own does.
    assert names(gateway.headers(AUDIENCE)) == ["default", "narrator"]
    assert names(as_service(gateway, "satellites")) == ["default"]


def test_a_system_clip_needs_voices_write_all(speech, gateway, voice_dir):
    voice_dir("narrator", 6.0)
    refused = speech.post("/jobs", json={"text": "hi", "voice": "narrator"},
                          headers=as_user(gateway, ALICE))
    assert refused.status_code == 400
    assert speech.post("/jobs", json={"text": "hi", "voice": "narrator"}
                       ).status_code == 202


def test_a_clip_added_to_a_users_directory_is_usable_on_the_next_request(
        speech, gateway):
    """The top level's mtime does not change when a file lands in a user's
    directory, so each namespace is stamped on its own."""
    from app import main

    bob = as_user(gateway, BOB)
    assert speech.get("/voices", headers=bob).json()["voices"] == ["default"]
    _clip(main.VOICES.root / "users" / BOB, "first")
    assert "first" in speech.get("/voices", headers=bob).json()["voices"]
    _clip(main.VOICES.root / "users" / BOB, "second")
    assert "second" in speech.get("/voices", headers=bob).json()["voices"]
