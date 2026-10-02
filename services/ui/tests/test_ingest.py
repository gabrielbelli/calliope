"""Resolve, confirm, fetch -- and abandon, through the stand-in downloader.

Every link here says `instant`, which tests/fake_fetcher.py reads as "no
waiting"; the other words in a link choose what the stand-in does.
"""

from __future__ import annotations

import logging

from conftest import ADMIN, fetched, finished_job, wait_for

URL = "https://media.example/instant-talk"


def resolve(api, url=URL, **headers):
    return api.post("/ui/resolve", json={"url": url}, **headers)


# ------------------------------------------------------------- resolve --


def test_resolving_downloads_nothing(client):
    api, _, fetches = client()
    body = resolve(api).json()
    assert fetches.calls() == [], "a resolve started a download"
    assert body["token"] == URL
    assert body["title"] == "Probed talk instant-talk"
    assert body["uploader"] == "Example Channel"
    assert (body["duration"], body["bytes"]) == (180.0, 1_440_000)
    assert body["probed"] is True and body["probe_enabled"] is True
    assert body["is_live"] is False and body["video"] is False
    assert api.get("/ui/progress", params={"token": URL}).json()["status"] == "pending"


def test_short_obvious_media_does_not_nag(client):
    # Three minutes, 1.4 MB: inside both thresholds, so no dialog.
    api, _, _ = client()
    assert resolve(api).json()["confirm"] is False


def test_long_media_confirms(client):
    api, _, _ = client()
    assert resolve(api, "https://media.example/instant-long").json()["confirm"] is True


def test_an_unknown_duration_confirms_because_not_knowing_is_the_point(client):
    """The probe outlived UI_PROBE_TIMEOUT: the card has a title and no facts."""
    api, _, _ = client(UI_PROBE_TIMEOUT="0.5")
    body = resolve(api, "https://media.example/unprobed").json()
    assert body["probed"] is False and body["confirm"] is True
    assert body["duration"] is None and body["video"] is None
    assert body["title"] == "https://media.example/unprobed"


def test_our_guard_refuses_before_anything_runs(client):
    api, _, fetches = client()
    response = resolve(api, "http://10.0.0.5:8080/x")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "refused_url"
    assert api.get("/ui/progress", params={"token": "http://10.0.0.5:8080/x"}).status_code == 404


def test_a_live_stream_is_refused_with_the_reason(client):
    api, _, _ = client()
    response = resolve(api, "https://media.example/live-now")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "live_stream"
    assert "needs ffmpeg" in error["message"]


def test_a_playlist_is_refused(client):
    api, _, _ = client()
    response = resolve(api, "https://media.example/playlist")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "playlist"


def test_a_destination_the_child_refuses_is_a_400_about_the_link(client):
    """The child's guard saw a private address the server's first look did not."""
    api, _, _ = client()
    response = resolve(api, "https://media.example/private")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "destination_not_allowed"
    assert error["message"] == "refusing to fetch: 10.0.0.5 is a private address"


def test_a_link_no_extractor_reads_says_why(client):
    api, _, _ = client()
    response = resolve(api, "https://media.example/unsupported")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unresolvable"
    assert error["message"].startswith("Could not read that link: Unsupported URL:")


def test_resolving_your_own_running_download_is_a_409_and_it_keeps_running(client):
    api, _, fetches = client()
    url = "https://media.example/hang"
    resolve(api, url)
    assert api.post("/ui/commit", json={"token": url}).status_code == 200
    response = resolve(api, url)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "in_progress"
    assert api.get("/ui/progress", params={"token": url}).json()["status"] in (
        "pending", "downloading")


def test_resolve_is_rate_limited_so_it_is_not_a_scanner(client):
    api, _, _ = client(UI_RESOLVE_PER_MINUTE="2")
    for _ in range(2):
        resolve(api)
    response = resolve(api)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"


# -------------------------------------------------------------- commit --


def test_commit_refuses_a_token_that_was_never_resolved(client, sign):
    """The confirm gate is the server's, not only the page's: no job, no start,
    for anyone, an administrator included."""
    api, _, fetches = client()
    body = {"token": URL, "clip_start": 0, "clip_end": 600}
    assert api.post("/ui/commit", json=body).status_code == 404
    assert api.post("/ui/commit", json=body, headers=sign(scopes=ADMIN)).status_code == 404
    assert fetches.calls() == []


def test_committing_starts_one_audio_download(client):
    api, _, fetches = client()
    resolve(api)
    body = api.post("/ui/commit", json={"token": URL}).json()
    assert body == {"token": URL, "status": "started", "video": False}
    assert wait_for(api, URL)["ready"] is True
    [call] = fetches.calls()
    assert call["argv"][:2] == ["fetch", "audio"]
    assert call["argv"][-2:] == ["--", URL]


def test_the_kinds_take_precedence_captions_then_clip_then_video(client):
    api, _, fetches = client()
    cases = [({"captions": True, "for_clip": True, "video": True}, "captions"),
             ({"for_clip": True, "video": True}, "clip"),
             ({"video": True}, "video"),
             ({}, "audio")]
    for n, (options, kind) in enumerate(cases):
        url = f"https://media.example/instant-video-subs-{n}"
        resolve(api, url)
        assert api.post("/ui/commit", json={"token": url, **options}).status_code == 200
        wait_for(api, url)
        assert fetches.calls()[-1]["argv"][1] == kind, options


def test_a_captions_download_asks_for_the_language_the_probe_found(client):
    api, _, fetches = client()
    url = "https://media.example/instant-subs"
    fetched(api, url, captions=True)
    assert fetches.calls()[-1]["argv"][1:4] == ["captions", str(8 * 1024**2), "en"]


def test_a_clip_source_over_ten_minutes_or_of_unknown_length_is_refused(client):
    api, _, fetches = client(UI_PROBE_TIMEOUT="0.5")
    for url, said in (("https://media.example/instant-long", "120:00"),
                      ("https://media.example/unprobed", "of unknown length")):
        resolve(api, url)
        response = api.post("/ui/commit", json={"token": url, "for_clip": True})
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "too_long_for_clip"
        assert said in error["message"]
    assert fetches.calls() == []


def test_the_video_is_refused_where_the_site_has_no_single_file(client):
    api, _, fetches = client()
    resolve(api)
    response = api.post("/ui/commit", json={"token": URL, "video": True})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "video_unavailable"
    assert fetches.calls() == []


def test_stop_at_must_be_after_start_at(client):
    api, _, _ = client()
    resolve(api)
    response = api.post("/ui/commit", json={"token": URL, "clip_start": 30, "clip_end": 30})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_clip_range"


def test_committing_a_running_download_again_is_a_409(client):
    api, _, _ = client()
    url = "https://media.example/hang"
    resolve(api, url)
    api.post("/ui/commit", json={"token": url})
    response = api.post("/ui/commit", json={"token": url})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "in_progress"


# ------------------------------------------------- progress and abandon --


def test_progress_only_calls_a_download_ready_when_it_has_finished(client):
    api, _, _ = client()
    url = "https://media.example/talk"            # three seconds, a line a second
    resolve(api, url)
    api.post("/ui/commit", json={"token": url})
    first = api.get("/ui/progress", params={"token": url}).json()
    assert first["ready"] is False and first["where"] == "queue"
    state = wait_for(api, url)
    assert state["ready"] is True and state["where"] == "done"
    assert state["filename"] == "Probed talk talk.wav"


def test_a_failed_download_is_not_mistaken_for_ready(client):
    api, _, _ = client()
    url = "https://media.example/instant-broken"
    state = fetched(api, url)
    assert state["ready"] is False and state["status"] == "error"
    assert state["error"] == "[generic] Unable to download webpage: HTTP Error 403: Forbidden"


def test_abandon_says_reaped_and_the_link_is_gone(client):
    api, _, _ = client()
    resolve(api)
    assert api.post("/ui/abandon", json={"token": URL}).json() == {"token": URL, "reaped": True}
    assert api.get("/ui/progress", params={"token": URL}).status_code == 404


# --------------------------------------------------------------- fetch --


def test_fetch_streams_the_file_into_the_gateway_as_multipart(client):
    api, gateway, _ = client()
    fetched(api, URL)
    response = api.post("/ui/fetch", params={"response_format": "srt"}, json={"token": URL})
    assert response.status_code == 200, response.text
    [sent] = gateway.transcriptions()
    body = sent.content
    assert b"multipart/form-data" in sent.headers["content-type"].encode()
    assert b'name="response_format"\r\n\r\nsrt' in body
    # `model` is required by /v1 validation and does not choose an engine.
    assert b'name="model"\r\n\r\nparakeet' in body
    # The probe's title and the file's own suffix.
    assert b'filename="Probed talk instant-talk.wav"' in body
    assert b"RIFF" in body and b"WAVE" in body


def test_the_excerpt_reaches_stt_as_two_fields(client):
    api, gateway, _ = client()
    fetched(api, URL, clip_start=2, clip_end=7.5)
    api.post("/ui/fetch", json={"token": URL})
    [sent] = gateway.transcriptions()
    assert b'name="clip_start"\r\n\r\n2.0\r\n' in sent.content
    assert b'name="clip_end"\r\n\r\n7.5\r\n' in sent.content


def test_no_excerpt_sends_neither_field(client):
    api, gateway, _ = client()
    fetched(api, URL)
    api.post("/ui/fetch", json={"token": URL})
    [sent] = gateway.transcriptions()
    assert b"clip_start" not in sent.content and b"clip_end" not in sent.content


def test_fetch_refuses_a_download_that_has_not_finished(client):
    api, _, _ = client()
    resolve(api)
    response = api.post("/ui/fetch", json={"token": URL})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_ready"


def test_fetching_twice_downloads_once(client):
    """Transcribing a link again sends the file on again; it is not fetched again."""
    api, gateway, fetches = client()
    fetched(api, URL)
    for _ in range(2):
        assert api.post("/ui/fetch", json={"token": URL}).status_code == 200
    assert len(gateway.transcriptions()) == 2
    assert len(fetches.calls()) == 1


# ------------------------------------------------------------ captions --
#
# "This has real subtitles already" on the confirm card commits the captions
# kind, which fetches the subtitle track and no media at all.


def test_a_captions_download_is_never_handed_to_the_transcriber(client):
    api, gateway, _ = client()
    fetched(api, "https://media.example/instant-subs", captions=True)
    response = api.post("/ui/fetch", json={"token": "https://media.example/instant-subs"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_media"
    assert not gateway.seen, "a subtitle file reached the gateway; that is the whole bug"


def test_captions_come_back_as_text_and_nothing_is_transcribed(client):
    api, gateway, _ = client()
    url = "https://media.example/instant-subs"
    fetched(api, url, captions=True)
    body = api.post("/ui/captions", json={"token": url}).json()
    assert body["format"] == "vtt"
    assert body["text"].startswith("WEBVTT\n\n00:00:00.000 --> 00:00:04.000\nThe first line.")
    assert body["filename"] == "Probed talk instant-subs.vtt"
    assert not gateway.seen


def test_subrip_is_reported_as_subrip(client):
    """The page needs it for the extension the Download button writes."""
    api, _, _ = client()
    from app import main
    finished_job(main, URL, b"1\n00:00:00,000 --> 00:00:01,000\nHi.\n", ".srt")
    assert api.post("/ui/captions", json={"token": URL}).json()["format"] == "srt"


def test_captions_refuses_a_media_download(client):
    api, _, _ = client()
    fetched(api, URL)
    response = api.post("/ui/captions", json={"token": URL})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_captions"


def test_captions_on_an_unfinished_download_is_a_409(client):
    api, _, _ = client()
    resolve(api)
    assert api.post("/ui/captions", json={"token": URL}).status_code == 409


# ------------------------------------------------------------ /ui/media --
#
# WHAT THE PLAYER ACTUALLY NEEDS, which is byte ranges. A <video> seeks by
# asking for one; served by something that ignores Range and answers 200 with
# the whole file, it plays from the start and drops every scrub on the floor.

MEDIA = b"0123456789abcdefghij"


def playable(client, suffix=".mp4"):
    api, _, _ = client()
    from app import main
    finished_job(main, URL, MEDIA, suffix)
    return api


def test_a_link_is_playable_at_all_or_none_of_this_works(client):
    api = playable(client)
    response = api.get("/ui/media", params={"token": URL})
    assert response.status_code == 200
    assert response.content == MEDIA
    assert response.headers["content-type"] == "video/mp4"
    # Without this the element never sends a Range at all and the scrub bar is
    # decorative.
    assert response.headers["accept-ranges"] == "bytes"


def test_a_bounded_range_comes_back_as_a_206_and_not_as_the_whole_file(client):
    api = playable(client)
    response = api.get("/ui/media", params={"token": URL}, headers={"Range": "bytes=4-9"})
    assert response.status_code == 206
    assert response.content == MEDIA[4:10]
    assert response.headers["content-range"] == f"bytes 4-9/{len(MEDIA)}"
    # THE SLICE, not the file.
    assert response.headers["content-length"] == "6"


def test_an_open_ended_range_is_what_a_seek_sends(client):
    api = playable(client)
    response = api.get("/ui/media", params={"token": URL}, headers={"Range": "bytes=12-"})
    assert response.status_code == 206
    assert response.content == MEDIA[12:]
    assert response.headers["content-range"] == f"bytes 12-19/{len(MEDIA)}"


def test_a_range_past_the_end_is_the_416_a_browser_knows_how_to_correct(client):
    api = playable(client)
    response = api.get("/ui/media", params={"token": URL},
                       headers={"Range": "bytes=9000-9999"})
    assert response.status_code == 416


def test_a_stale_if_range_gets_the_whole_file_not_a_slice_of_another(client):
    api = playable(client)
    whole = api.get("/ui/media", params={"token": URL})
    etag = whole.headers["etag"]
    fresh = api.get("/ui/media", params={"token": URL},
                    headers={"Range": "bytes=0-3", "If-Range": etag})
    assert fresh.status_code == 206 and fresh.content == MEDIA[:4]
    stale = api.get("/ui/media", params={"token": URL},
                    headers={"Range": "bytes=0-3", "If-Range": '"gone"'})
    assert stale.status_code == 200 and stale.content == MEDIA


def test_a_stranger_s_bytes_are_played_and_never_rendered(client):
    api = playable(client)
    headers = api.get("/ui/media", params={"token": URL}).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == "sandbox"
    assert headers["cache-control"] == "private, no-store"


def test_each_suffix_is_sent_as_its_own_type(client):
    api, _, _ = client()
    from app import main
    for n, (suffix, kind) in enumerate(((".weba", "audio/webm"), (".m4a", "audio/mp4"),
                                        (".mp4", "video/mp4"), (".wav", "audio/wav"))):
        url = f"{URL}-{n}"
        finished_job(main, url, MEDIA, suffix)
        response = api.get("/ui/media", params={"token": url})
        assert response.headers["content-type"].split(";")[0] == kind, suffix


def test_a_downloaded_webm_with_no_picture_is_audio(client):
    """The child says video false, so the parent names it .weba and the page
    picks the <audio> element."""
    api, _, _ = client()
    url = "https://media.example/instant-webm"
    state = fetched(api, url)
    assert state["filename"].endswith(".weba")
    assert api.get("/ui/media", params={"token": url}).headers["content-type"] == "audio/webm"


def test_media_refuses_a_subtitle_file_however_it_is_reached(client):
    api, _, _ = client()
    from app import main
    finished_job(main, URL, b"WEBVTT\n", ".vtt")
    response = api.get("/ui/media", params={"token": URL})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_media"


def test_media_refuses_a_download_that_has_not_finished(client):
    api, _, _ = client()
    resolve(api)
    assert api.get("/ui/media", params={"token": URL}).status_code == 409


def test_media_serves_only_a_token_this_person_resolved(client):
    api, _, _ = client()
    response = api.get("/ui/media", params={"token": "https://media.example/other"})
    assert response.status_code == 404


# ------------------------------------------- timings on the link path --
#
# WHY A LINK HAD NO HIGHLIGHT. The cues come from timedFromJson(), which needs
# verbose_json; formatForUpload() asks for it but only ran on the upload path,
# and this route forwarded `model` and `response_format` and nothing else -- so
# a link was transcribed as plain text and no timing ever came back.


def test_a_link_can_ask_for_the_timings_the_highlight_is_drawn_from(client):
    api, gateway, _ = client()
    fetched(api, URL)
    response = api.post(
        "/ui/fetch",
        params=[("response_format", "verbose_json"),
                ("timestamp_granularities", "word"),
                ("timestamp_granularities", "segment")],
        json={"token": URL})
    assert response.status_code == 200

    sent = gateway.transcriptions()[-1].content
    assert b'name="response_format"\r\n\r\nverbose_json' in sent
    # BOTH, and a dict would have carried one.
    assert sent.count(b'name="timestamp_granularities[]"') == 2
    assert b'name="timestamp_granularities[]"\r\n\r\nword' in sent
    assert b'name="timestamp_granularities[]"\r\n\r\nsegment' in sent


def test_an_invented_granularity_never_reaches_the_multipart_frame(client):
    """The frame is built by hand, so an unvalidated value carrying CRLF closes
    one part and opens another -- the same hole response_format was allowlisted
    for."""
    api, gateway, _ = client()
    fetched(api, URL)
    response = api.post(
        "/ui/fetch",
        params=[("response_format", "verbose_json"),
                ("timestamp_granularities",
                 'word"\r\n\r\nx\r\n--boundary\r\nContent-Disposition: form-data; name="model')],
        json={"token": URL})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_granularity"
    assert not gateway.transcriptions()


def test_asking_for_no_granularity_still_sends_none(client):
    """The audio-only, text-only case must not start paying for timestamps it
    has nothing to draw with -- about 5% on Parakeet, measured in asr.py."""
    api, gateway, _ = client()
    fetched(api, URL)
    api.post("/ui/fetch", params={"response_format": "text"}, json={"token": URL})
    sent = gateway.transcriptions()[-1].content
    assert b"timestamp_granularities" not in sent


# ----------------------------------------------------------------- logs --


def test_no_log_line_names_the_link(client, caplog):
    """A link is what a person fetched. Codes and exit statuses only."""
    api, _, _ = client()
    with caplog.at_level(logging.DEBUG):
        for url in (URL, "https://media.example/instant-broken",
                    "https://media.example/unsupported", "https://media.example/private"):
            resolve(api, url)
            api.post("/ui/commit", json={"token": url})
            wait_for(api, url, seconds=5)
            api.post("/ui/fetch", json={"token": url})
            api.post("/ui/abandon", json={"token": url})
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "media.example" not in logged, logged
