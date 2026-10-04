"""The jobs, the child and the cache, through the stand-in downloader.

tests/fake_fetcher.py is spawned exactly as app/fetcher.py would be, and a word
in each link chooses what it does. Times in the cache are moved with os.utime,
which is what touch() does too: atime is the last use, mtime never changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections import namedtuple
from pathlib import Path

import httpx
import pytest

from conftest import ALICE, BOB, fetched, wait_for

URL = "https://media.example/instant-talk"
HOUR = 3600


def job_of(sub, url):
    from app import downloads
    return downloads.JOBS[(sub, url)]


def age(path, seconds):
    """Make `path` last used `seconds` ago, leaving its mtime alone."""
    st = path.stat()
    os.utime(path, ns=(time.time_ns() - int(seconds * 1e9), st.st_mtime_ns))


def settled(job, seconds=10.0):
    """Wait for the job's task to end, so the sweep after it has run too."""
    ends = time.monotonic() + seconds
    while job.task is not None and not job.task.done() and time.monotonic() < ends:
        time.sleep(0.05)


def gone(pid, seconds=5.0):
    """Whether `pid` has exited within `seconds`. A zombie waiting for init to
    reap it counts: an orphaned grandchild is reparented, not reaped, at once."""
    ends = time.monotonic() + seconds
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].startswith("Z"):
                return True
        except (OSError, IndexError):
            pass
        if time.monotonic() > ends:
            return False
        time.sleep(0.05)


# --------------------------------------------------------------- the key --


def test_the_key_differs_per_person_and_per_kind():
    from app import downloads
    alice = downloads.Job(sub=ALICE, url=URL, facts=None, seen=0.0)
    bob = downloads.Job(sub=BOB, url=URL, facts=None, seen=0.0)
    keys = {downloads._key(alice, "audio"), downloads._key(bob, "audio"),
            downloads._key(alice, "video"), downloads._key(alice, "captions")}
    assert len(keys) == 4
    assert all(downloads.NAME.fullmatch(f"{key}.wav") for key in keys)


def test_a_cache_file_name_holds_no_title_and_no_link(client):
    api, _, fetches = client()
    fetched(api, URL)
    names = [p.name for p in fetches.dir.iterdir() if p.is_file() and p.name != "fetches.log"]
    assert len(names) == 1
    from app import downloads
    assert downloads.NAME.fullmatch(names[0])
    assert "talk" not in names[0] and "media" not in names[0]


def test_every_media_suffix_has_a_type():
    from app import downloads, ingest
    assert set(ingest.MEDIA_SUFFIXES) <= set(downloads.MEDIA_TYPES)


# ------------------------------------------------------------- the cache --


def test_a_hit_finishes_at_commit_and_starts_no_child(client):
    api, _, fetches = client()
    fetched(api, URL)
    api.post("/ui/abandon", json={"token": URL})
    api.post("/ui/resolve", json={"url": URL})
    body = api.post("/ui/commit", json={"token": URL})
    assert body.status_code == 200
    assert api.get("/ui/progress", params={"token": URL}).json()["ready"] is True
    assert len(fetches.calls()) == 1, "the second commit downloaded it again"


def test_another_kind_of_the_same_link_is_another_download(client):
    api, _, fetches = client()
    url = "https://media.example/instant-subs-video"
    fetched(api, url)
    fetched(api, url, captions=True)
    assert [c["argv"][1] for c in fetches.calls()] == ["audio", "captions"]


def test_a_hit_keeps_the_file_s_mtime_and_its_etag(client):
    api, _, _ = client()
    fetched(api, URL)
    path = job_of(ALICE, URL).path
    mtime = path.stat().st_mtime_ns
    etag = api.get("/ui/media", params={"token": URL}).headers["etag"]
    age(path, 600)
    api.post("/ui/abandon", json={"token": URL})
    api.post("/ui/resolve", json={"url": URL})
    api.post("/ui/commit", json={"token": URL})
    assert path.stat().st_mtime_ns == mtime
    assert time.time() - path.stat().st_atime < 60, "the hit was not a use"
    assert api.get("/ui/media", params={"token": URL}).headers["etag"] == etag


def test_a_big_file_goes_on_abandon_and_a_small_one_stays(client):
    api, _, _ = client()
    big, small = "https://media.example/instant-big", URL
    fetched(api, big)
    fetched(api, small)
    big_path, small_path = job_of(ALICE, big).path, job_of(ALICE, small).path
    assert big_path.stat().st_size == 129 * 2**20
    for url in (big, small):
        api.post("/ui/abandon", json={"token": url})
    assert not big_path.exists() and small_path.exists()


def test_small_files_over_the_total_go_least_recently_used_first(client):
    """And a big file is not counted towards the total at all."""
    api, _, _ = client(UI_CACHE_BYTES="1000000")      # two 384 kB tones fit, three do not
    first, second, third = (f"https://media.example/instant-{n}" for n in "abc")
    fetched(api, "https://media.example/instant-big")
    fetched(api, first)
    fetched(api, second)
    age(job_of(ALICE, first).path, 100)
    age(job_of(ALICE, second).path, 50)
    paths = [job_of(ALICE, url).path for url in (first, second)]
    fetched(api, third)
    assert not paths[0].exists(), "the least recently used small file was kept"
    assert paths[1].exists() and job_of(ALICE, third).path.exists()
    assert job_of(ALICE, "https://media.example/instant-big").path.exists()


def test_a_file_bigger_than_the_whole_cache_is_kept_as_a_big_one(client):
    """Counted as small, it would be over the total on its own, and the sweep
    after its download would delete it the moment it finished."""
    api, _, _ = client(UI_CACHE_BYTES="100000")       # under one 384 kB tone
    state = fetched(api, URL)
    settled(job_of(ALICE, URL))
    assert state["ready"] is True, state
    assert api.get("/ui/progress", params={"token": URL}).json()["ready"] is True
    assert api.get("/ui/media", params={"token": URL}).status_code == 200
    first = job_of(ALICE, URL).path
    fetched(api, "https://media.example/instant-other")
    assert not first.exists(), "a person keeps one big file, and this one is big"


def test_a_person_keeps_one_big_file_and_another_person_s_is_untouched(client, sign):
    api, _, _ = client()
    bob = sign(sub=BOB)
    one, two = "https://media.example/instant-big-1", "https://media.example/instant-big-2"
    fetched(api, one)
    fetched(api, one, headers=bob)
    first, bobs = job_of(ALICE, one).path, job_of(BOB, one).path
    fetched(api, two)
    assert not first.exists(), "Alice's first big file outlived her second"
    assert bobs.exists() and job_of(ALICE, two).path.exists()


def test_a_small_file_is_kept_a_day_after_its_last_use(client):
    api, _, _ = client()
    from app import downloads
    kept, gone = URL, "https://media.example/instant-old"
    fetched(api, kept)
    fetched(api, gone)
    age(job_of(ALICE, kept).path, 23 * HOUR)
    age(job_of(ALICE, gone).path, 25 * HOUR)
    downloads.sweep()
    assert job_of(ALICE, kept).path.exists()
    assert not job_of(ALICE, gone).path.exists()


def test_a_big_file_is_kept_an_hour_after_its_last_use(client, sign):
    api, _, _ = client()
    from app import downloads
    url = "https://media.example/instant-big"
    fetched(api, url)
    fetched(api, url, headers=sign(sub=BOB))
    age(job_of(ALICE, url).path, 59 * 60)
    age(job_of(BOB, url).path, 61 * 60)
    downloads.sweep()
    assert job_of(ALICE, url).path.exists()
    assert not job_of(BOB, url).path.exists()


def test_with_the_cache_off_nothing_is_kept_past_an_hour(client, sign):
    """Every file is big then, so each person keeps one, for an hour."""
    api, _, _ = client(UI_CACHE_BYTES="0")
    from app import downloads
    fetched(api, URL)
    fetched(api, URL, headers=sign(sub=BOB))
    age(job_of(ALICE, URL).path, 59 * 60)
    age(job_of(BOB, URL).path, 61 * 60)
    downloads.sweep()
    assert job_of(ALICE, URL).path.exists()
    assert not job_of(BOB, URL).path.exists()


def test_a_file_the_sweep_took_is_a_410_and_progress_says_so(client):
    api, _, _ = client()
    from app import downloads
    fetched(api, URL)
    age(job_of(ALICE, URL).path, 25 * HOUR)
    downloads.sweep()
    response = api.get("/ui/media", params={"token": URL})
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "expired"
    state = api.get("/ui/progress", params={"token": URL}).json()
    assert state["status"] == "error" and state["ready"] is False
    assert state["error"] == downloads.EXPIRED


def test_start_up_empties_jobs_and_deletes_only_what_it_wrote(build, sign, tmp_path):
    from fastapi.testclient import TestClient
    cache = tmp_path / "cache"
    (cache / "jobs" / "0123456789abcdef").mkdir(parents=True)
    (cache / "jobs" / "0123456789abcdef" / "media.webm.part").write_bytes(b"x")
    stale, fresh = cache / ("a" * 64 + ".wav"), cache / ("b" * 64 + ".wav")
    for path in (stale, fresh):
        path.write_bytes(b"RIFF")
    age(stale, 25 * HOUR)
    notes = cache / "notes.txt"
    notes.write_text("not ours")
    age(notes, 1000 * HOUR)
    main, _, _ = build()
    with TestClient(main.app, headers=sign()):
        assert list((cache / "jobs").iterdir()) == []
        assert not stale.exists() and fresh.exists() and notes.exists()


# ---------------------------------------------------------------- limits --


def test_a_third_running_download_for_one_person_is_a_429(client):
    api, _, _ = client()
    for n in range(3):
        url = f"https://media.example/hang-{n}"
        api.post("/ui/resolve", json={"url": url})
        response = api.post("/ui/commit", json={"token": url})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_downloads"
    assert response.headers["retry-after"] == "30"


def test_three_children_run_at_once_and_the_rest_wait_their_turn(client, sign):
    api, _, fetches = client()
    people = [sign(sub=f"u_{letter * 16}") for letter in "cde"]
    urls = [f"https://media.example/hang-{n}" for n in range(2)]
    for person in people:
        for url in urls:
            api.post("/ui/resolve", json={"url": url}, headers=person)
            assert api.post("/ui/commit", json={"token": url}, headers=person).status_code == 200
    ends = time.monotonic() + 10
    while len(fetches.calls()) < 3 and time.monotonic() < ends:
        time.sleep(0.05)
    time.sleep(0.5)
    states = [api.get("/ui/progress", params={"token": url}, headers=person).json()["status"]
              for person in people for url in urls]
    assert len(fetches.calls()) == 3
    assert sorted(states) == ["downloading"] * 3 + ["pending"] * 3
    queued = [api.get("/ui/progress", params={"token": url}, headers=person).json()["where"]
              for person in people for url in urls]
    assert queued == ["queue"] * 6


def together(main, *calls):
    """Run several requests at once through one lifespan, and their answers in order."""
    async def run():
        async with (main.lifespan(main.app),
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                      base_url="http://ui") as api):
            tasks = []
            for wait, url, headers in calls:
                await asyncio.sleep(wait)
                tasks.append(asyncio.create_task(
                    api.post("/ui/resolve", json={"url": url}, headers=headers)))
            started = time.monotonic()
            answers = [await task for task in tasks]
            return answers, time.monotonic() - started
    return asyncio.run(run())


def test_one_person_resolves_one_link_at_a_time(build, sign):
    main, _, _ = build()
    (first, second), _ = together(main, (0, "https://media.example/unprobed", sign()),
                                  (0.3, URL, sign()))
    assert first.json()["probed"] is False
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "resolve_in_progress"
    assert second.headers["retry-after"] == "5"


def test_the_time_limit_covers_the_wait_for_a_probe_slot(build, sign):
    """Both slots held by probes that will not answer: a third person's resolve
    still answers within UI_PROBE_TIMEOUT, not after waiting for a slot and then
    for a probe of its own."""
    main, _, _ = build(UI_PROBE_TIMEOUT="2")
    began = time.monotonic()
    answers, _ = together(main,
                          (0, "https://media.example/unprobed-1", sign(sub=ALICE)),
                          (0, "https://media.example/unprobed-2", sign(sub=BOB)),
                          (0.5, "https://media.example/unprobed-3", sign(sub="u_cccccccccccccccc")))
    assert [a.json()["probed"] for a in answers] == [False, False, False]
    assert time.monotonic() - began < 2.0 + 1.0, "the third probe waited for a slot and then ran"


def test_a_download_that_would_not_fit_on_the_disk_fails_and_says_so(client, monkeypatch):
    """Free space minus the caps of running downloads must leave the cap and 64 MiB."""
    api, _, _ = client(UI_MAX_DOWNLOAD_BYTES="1000000")
    from app import downloads
    usage = namedtuple("usage", "total used free")
    room = 1_000_000 + downloads.HEADROOM + 10
    monkeypatch.setattr(downloads.shutil, "disk_usage", lambda path: usage(10**12, 0, room))
    running = "https://media.example/hang"
    api.post("/ui/resolve", json={"url": running})
    api.post("/ui/commit", json={"token": running})
    ends = time.monotonic() + 10
    while (api.get("/ui/progress", params={"token": running}).json()["status"] != "downloading"
           and time.monotonic() < ends):
        time.sleep(0.05)
    state = fetched(api, URL)
    assert state["status"] == "error"
    assert state["error"] == downloads.NO_SPACE


# ------------------------------------------------------ the child's output --


def test_a_download_past_the_time_limit_is_killed_with_its_process_group(client, monkeypatch):
    """The child's own child too, which proc.kill() alone would leave running."""
    api, _, fetches = client()
    from app import downloads
    monkeypatch.setattr(downloads, "DOWNLOAD_SECONDS", 1)
    state = fetched(api, "https://media.example/grandchild-hang")
    assert state["status"] == "error" and state["error"] == downloads.TOO_LONG
    [call] = fetches.calls()
    try:
        assert gone(call["pid"]), "the child outlived its time limit"
        assert gone(call["grandchild"]), "the child's own child outlived it"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(call["grandchild"], signal.SIGKILL)


def test_a_child_is_killed_whatever_ends_its_download(client, monkeypatch):
    """An exception the parent did not foresee fails the job, and the child
    goes with it: once the task is done, nothing else holds the child, and
    shutdown() would not find it."""
    api, _, fetches = client()
    from app import downloads
    url = "https://media.example/stall"
    api.post("/ui/resolve", json={"url": url})

    def unforeseen(line):
        raise RuntimeError("a fault in the parent")

    monkeypatch.setattr(downloads, "_message", unforeseen)
    api.post("/ui/commit", json={"token": url})
    state = wait_for(api, url)
    assert state["status"] == "error"
    assert state["error"] == "The download could not be run on this server."
    [call] = fetches.calls()
    assert gone(call["pid"]), "the child outlived its download"


def test_numbers_json_cannot_carry_back_out_are_ignored(client):
    """Infinity, NaN and a 400-digit number. json.loads takes all three; int()
    of the first is an OverflowError, and json.dumps would put the rest into
    /ui/progress as words no browser parses."""
    api, _, _ = client()
    url = "https://media.example/instant-infinity"
    resolved = api.post("/ui/resolve", json={"url": url})
    assert resolved.status_code == 200
    assert "Infinity" not in resolved.text and "NaN" not in resolved.text
    assert (resolved.json()["duration"], resolved.json()["bytes"]) == (None, None)
    assert api.post("/ui/commit", json={"token": url}).status_code == 200
    state = wait_for(api, url)
    assert state["ready"] is True, state
    text = api.get("/ui/progress", params={"token": url}).text
    assert "Infinity" not in text and "NaN" not in text


def test_a_line_nested_too_deep_to_parse_is_ignored(client):
    """60,000 [ is under LINE_LIMIT and a RecursionError to json.loads."""
    api, _, _ = client()
    url = "https://media.example/instant-nested"
    resolved = api.post("/ui/resolve", json={"url": url})
    assert resolved.status_code == 200 and resolved.json()["probed"] is True
    assert api.post("/ui/commit", json={"token": url}).status_code == 200
    assert wait_for(api, url)["ready"] is True


def test_a_line_too_long_to_read_fails_the_download_cleanly(client):
    api, _, _ = client()
    from app import downloads
    state = fetched(api, "https://media.example/instant-flood")
    assert state["status"] == "error" and state["error"] == downloads.UNREADABLE


def test_a_line_too_long_to_read_is_a_probe_with_no_answer(client):
    api, _, _ = client()
    body = api.post("/ui/resolve", json={"url": "https://media.example/flood"}).json()
    assert body["probed"] is False


def test_lines_that_are_not_json_are_ignored(client):
    api, _, _ = client()
    assert fetched(api, "https://media.example/instant-junk")["ready"] is True


def test_a_child_that_says_nothing_fails_with_its_exit_status(client):
    api, _, _ = client()
    state = fetched(api, "https://media.example/instant-silent")
    assert state["status"] == "error"
    assert state["error"] == "The downloader stopped without saying why (exit 0)."


@pytest.mark.parametrize("word,said", [
    ("twofiles", "something other than one file"),
    ("symlink", "something other than one file"),
    ("badsuffix", "a .exe file"),
    ("empty", "empty or over the size limit"),
])
def test_the_one_file_rule(client, word, said):
    api, _, fetches = client()
    state = fetched(api, f"https://media.example/instant-{word}")
    assert state["status"] == "error" and said in state["error"], state
    from app import downloads
    assert list((fetches.dir / "jobs").iterdir()) == [], "the run's directory was left behind"
    assert downloads.stats()["items"] == 0


def test_a_clip_s_download_has_a_cap_of_its_own(client, monkeypatch):
    """The ten minutes a clip source is allowed are the site's word, and the
    browser decodes every byte of it, so the cap is what holds."""
    api, _, fetches = client()
    from app import config, downloads
    fetched(api, URL, for_clip=True)
    [call] = fetches.calls()
    assert call["argv"][1:3] == ["clip", str(config.CLIP_SOURCE_BYTES)]
    assert config.CLIP_SOURCE_BYTES < config.MAX_DOWNLOAD_BYTES
    monkeypatch.setattr(config, "MAX_DOWNLOAD_BYTES", 1000)
    assert downloads._cap(downloads.Job(sub=ALICE, url=URL, facts=None, seen=0.0,
                                        kind="clip")) == 1000


def test_the_child_is_isolated_and_told_nothing_but_four_variables(client):
    api, _, fetches = client()
    fetched(api, URL)
    [call] = fetches.calls()
    from app import downloads
    env = {k: v for k, v in call["env"].items()
           # macOS adds this to every process it starts; Linux does not.
           if not (sys.platform == "darwin" and k == "__CF_USER_TEXT_ENCODING")}
    assert env == downloads.ENV
    assert call["isolated"] is True
    assert os.path.realpath(call["cwd"]).startswith(os.path.realpath(fetches.dir / "jobs"))
