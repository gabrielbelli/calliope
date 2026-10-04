"""Resolve, confirm, fetch -- and never in a different order.

    POST /ui/resolve   guard -> a probe child -> a pending job, the caller's own
    POST /ui/commit    the job starts: a cache hit, or a download child
    POST /ui/abandon   the job goes: its child killed, a big file deleted
    GET  /ui/progress  the job's state, as percent / speed / eta
    POST /ui/fetch     the finished file -> multipart -> the gateway's internal
                       listener, as the person who asked (D64) -> stt-stack
    POST /ui/captions  the finished .vtt/.srt -> the page. NO stt CALL AT ALL
    GET  /ui/media     the finished file -> the browser, byte ranges and all

THIS CONTAINER FETCHES THE LINK ITSELF. yt-dlp runs in a child process,
app/fetcher.py, which checks every connection it makes against app/guard.py's
rules; app/downloads.py holds the jobs, runs the children and keeps a small
cache of finished files. No ffmpeg: one native file per link, nothing merged,
converted or trimmed.

EVERY LINK IS ONE PERSON'S JOB, AND EVERY ROUTE BUT /ui/resolve LOOKS IT UP
FIRST (D36). The token is the URL, which anyone can guess, so jobs are keyed
by the caller's `sub` and the URL together: a stranger's token finds nothing,
and gets the same 404 a link nobody pasted gets, before anything else runs.
Two people with one link have two jobs, and neither can see, stop or play the
other's.

NOTHING IS DOWNLOADED BEFORE THE USER SAYS SO, which is the requirement the
user pressed hardest on. /ui/resolve runs a probe that writes nothing
(RLIMIT_FSIZE 0) and leaves a pending job; only /ui/commit starts a download,
and /ui/abandon drops the job.

THE TRANSCRIPTION IS THE USER'S, NOT THIS SERVICE'S. /ui/fetch arrives with a
delegation token beside the identity assertion; it is sent on, with this
service's own key, to the gateway's internal listener, which re-checks the
person's session or key live and counts the token's uses (D64). The run record
is then owned by that person, and a person signed out since cannot use it.

AN EXCERPT IS CUT BY stt, NOT HERE. Without ffmpeg nothing in this container
can trim audio, so Start at and Stop at download the whole audio once and go
to stt as clip_start and clip_end, which decodes only that window.

/ui/media IS THE ONE ROUTE HERE THAT SENDS MEDIA DOWNWARDS, and only a file
this person's own job finished, when their player asks for it. That is what
makes the caption band and the karaoke highlight work for a link at all.

THE ESTIMATES ARE NOT COMPUTED HERE, deliberately. This module returns FACTS --
title, duration, the size of the stream it would fetch, whether real subtitles
exist -- and the page does the arithmetic with the realtime factor it has
measured on this box. Two reasons. The rate is a moving number the browser
keeps an EMA of from every transcription it runs, and a server-side estimate
would be a second, staler copy of it. And the download half and the transcribe
half are NEVER blended into one figure: for long media the download is the slow
half, and one merged number hides which half to blame when it drags.

NO LINK IS LOGGED. Codes and exit statuses only: a link is what a person
fetched.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from typing import Any, AsyncIterator

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from voice_common import identity
from voice_common.errors import error_response

from . import config, downloads, guard

log = logging.getLogger("voice-ui.ingest")

# identity.pub and service.key, from this service's credential volume (D7).
# One instance, shared with identity.install in main.py, so the key that
# verifies an assertion and the key that is sent on /ui/fetch are read from
# the same place.
CREDENTIALS = identity.Credentials()

# What /v1/audio/transcriptions accepts. Kept here rather than imported from
# the gateway because this service must not depend on that one's internals,
# and kept as a set rather than a regex because the whole vocabulary is five
# words -- see the interpolation guard in fetch() for why it is checked at all.
RESPONSE_FORMATS = frozenset({"json", "text", "srt", "vtt", "verbose_json"})

# What a captions download leaves behind. yt-dlp writes WebVTT when it can and
# SubRip when the site has only that, and the suffix is how a finished job is
# told apart from a media one.
CAPTION_SUFFIXES = (".vtt", ".srt")

# WHAT A DOWNLOAD MAY BE, AND WHAT /ui/media WILL HAND TO A BROWSER, as an
# allowlist rather than "anything that is not a subtitle". The child names its
# file after whatever the site served, and an allowlist refuses a .part, a
# .json or an .exe by default where a denylist would serve it and wait to be
# corrected. downloads.MEDIA_TYPES has a type for each.
MEDIA_SUFFIXES = (".mp4", ".m4v", ".mkv", ".webm", ".mov",
                  ".m4a", ".mp3", ".opus", ".ogg", ".oga", ".wav", ".flac",
                  ".aac", ".weba")

# The containers a <video> element is worth showing rather than an <audio>.
# Read by the page from the finished filename, and repeated here so the route
# can say which of the two it is serving without the page guessing twice.
VIDEO_SUFFIXES = (".mp4", ".m4v", ".mkv", ".webm", ".mov")

# The two values /v1/audio/transcriptions accepts for timestamp_granularities[]
# (openai_api.py _values). Allowlisted for the same reason RESPONSE_FORMATS is:
# these strings are interpolated into a multipart frame this module builds by
# hand, and an unvalidated one carrying CRLF closes a part and opens another.
GRANULARITIES = frozenset({"word", "segment"})

router = APIRouter()

# Per-person budget for /ui/resolve, keyed on the assertion's `sub` (D36). That
# route spawns a process which makes an outbound request to a host the caller
# chose; unmetered, it is a port and host scanner with a nice JSON interface.
_recent: dict[str, list[float]] = {}


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    window = [t for t in _recent.get(key, ()) if now - t < 60.0]
    if len(window) >= config.RESOLVE_PER_MINUTE:
        _recent[key] = window
        return True
    window.append(now)
    _recent[key] = window
    if len(_recent) > 512:
        # Unbounded growth is the only way this dict misbehaves. Drop the
        # entries whose window is empty rather than keeping a second structure.
        for name in [k for k, v in _recent.items()
                     if not v or now - v[-1] > 120.0]:
            _recent.pop(name, None)
    return False


class ResolveRequest(BaseModel):
    url: str


class CommitRequest(BaseModel):
    token: str
    # Set only by the clone sheet: AAC first, because every browser's
    # decodeAudioData reads it, and the browser cuts the clip out.
    for_clip: bool = False
    # Promoted out of any expert panel and onto the confirm card itself,
    # because for long media this is what turns "no, too much" into "yes, but
    # only this bit". The whole audio is still downloaded; stt transcribes the
    # window.
    clip_start: float | None = Field(default=None, ge=0)
    clip_end: float | None = Field(default=None, ge=0)
    # A video with real (non-ASR) captions already has a human transcript.
    # Fetching only that track takes about two seconds, which beats
    # transcribing it however fast Parakeet is.
    captions: bool = False
    # KEEP THE PICTURE, AND IT DEFAULTS OFF ON PURPOSE. Audio-only is what makes
    # a link affordable, and that must not change because a feature was added.
    # Offered only where the site has one file with picture and sound in it.
    video: bool = False


class TokenRequest(BaseModel):
    token: str


def _json(payload: dict[str, Any], status: int = 200) -> Response:
    return Response(media_type="application/json", status_code=status,
                    content=json.dumps(payload).encode())


def _job(request: Request, token: str) -> downloads.Job | Response:
    """This caller's job for `token`, or the 404 a link nobody pasted gets."""
    job = downloads.get(identity.claims_of(request).sub, token)
    if job is None:
        return error_response(404, "No download of that link is held here.",
                              code="unknown_token", param="token")
    return job


def _not_ready(job: downloads.Job) -> Response:
    return error_response(409, f"that download is {job.state}, not finished",
                          code="not_ready", param="token")


def _expired() -> Response:
    return error_response(410, downloads.EXPIRED, code="expired", param="token")


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


# The probe's own codes, and what the page is told for each.
REFUSALS = {
    "refused": ("destination_not_allowed", None),
    "live": ("live_stream",
             "This is a live or upcoming stream. Recording one needs ffmpeg, "
             "which this server does not carry. Try again once it has ended."),
    "playlist": ("playlist",
                 "That link is a playlist or a channel. Paste the link of one video."),
}


@router.post("/ui/resolve")
async def resolve(request: Request, body: ResolveRequest) -> Response:
    if not downloads.available():
        return error_response(
            501,
            "Link ingestion is switched off on this server (UI_LINKS=0, or "
            "UI_CACHE_DIR is not writable). File upload still works.",
            code="ingestion_not_configured")

    who = identity.claims_of(request).sub
    if _rate_limited(who):
        return error_response(
            429, f"Too many links resolved; the limit is "
                 f"{config.RESOLVE_PER_MINUTE} a minute.",
            code="rate_limited", headers={"Retry-After": "30"})

    # The first layer, in this process: a URL it hates never reaches a child.
    # The child applies the same rules again at every connection it makes.
    try:
        url = guard.check(body.url)
    except guard.GuardError as exc:
        return error_response(400, str(exc), code="refused_url", param="url")

    # Resolving again must not kill a download that is running.
    held = downloads.get(who, url)
    if held is not None and held.active:
        return error_response(409, "That link is already downloading.",
                              code="in_progress", param="url")

    try:
        facts = await downloads.probe(who, url)
    except downloads.Busy:
        return error_response(429, "Wait for your last link to finish resolving.",
                              code="resolve_in_progress", headers={"Retry-After": "5"})
    except downloads.Refused as exc:
        code, message = REFUSALS.get(exc.code, ("unresolvable", None))
        if code == "unresolvable":
            message = f"Could not read that link: {exc.message}"
        return error_response(400, message or exc.message, code=code, param="url")

    downloads.replace(who, url, facts)
    duration = (facts or {}).get("duration")
    size = (facts or {}).get("bytes")
    # WHEN TO NAG. Below both thresholds the page skips the dialog entirely --
    # see config.CONFIRM_SECONDS for the defence of the numbers. An unknown
    # duration always confirms: not knowing is exactly the case the dialog
    # exists for.
    confirm = (duration is None or duration > config.CONFIRM_SECONDS
               or (size or 0) > config.CONFIRM_BYTES)
    response = _json({
        "token": url,
        "title": (facts or {}).get("title") or url,
        "uploader": (facts or {}).get("uploader"),
        "duration": duration,
        "bytes": size,
        # Always false: a live stream is refused above.
        "is_live": False,
        "has_subtitles": bool((facts or {}).get("has_subtitles")),
        # Whether "Keep the video" can be offered: one file with picture and
        # sound. None when the probe gave no answer.
        "video": None if facts is None else bool(facts.get("video")),
        "probed": facts is not None,
        "confirm": confirm,
        "probe_enabled": True,
    })
    downloads.sweep()
    return response


@router.post("/ui/commit")
async def commit(request: Request, body: CommitRequest) -> Response:
    job = _job(request, body.token)
    if isinstance(job, Response):
        return job
    try:
        guard.check(body.token)
    except guard.GuardError as exc:
        return error_response(400, str(exc), code="refused_url", param="token")
    if job.active:
        return error_response(409, "That link is already downloading.",
                              code="in_progress", param="token")

    # WHICH OF THE FOUR KINDS OF DOWNLOAD THIS IS, decided once. They are
    # mutually exclusive and the precedence is not arbitrary:
    #
    #   captions  wins over everything. It fetches no media at all, so asking
    #             for a video AND for no media is a contradiction.
    #   clip      wins over video. The clone sheet decodes it in the browser,
    #             and AAC is what every browser decodes.
    #   video     the only one the user ticks.
    if body.captions:
        kind = "captions"
    elif body.for_clip:
        kind = "clip"
    elif body.video:
        kind = "video"
    else:
        kind = "audio"

    facts = job.facts
    duration = (facts or {}).get("duration")
    if kind == "clip" and (duration is None or duration > config.CLIP_SOURCE_SECONDS):
        length = _clock(duration) if duration is not None else "of unknown length"
        return error_response(
            400, f"Cloning from a link takes recordings up to "
                 f"{int(config.CLIP_SOURCE_SECONDS // 60)} minutes long, and this "
                 f"one is {length}. Your browser cuts the clip out and has to "
                 f"hold the whole recording to do it. Download it and upload "
                 f"the part you want.",
            code="too_long_for_clip", param="token")
    if kind == "video" and facts is not None and not facts.get("video"):
        return error_response(
            400, "This site sends picture and sound as separate streams, and "
                 "joining them needs ffmpeg, which this server does not carry. "
                 "Fetch the audio only.",
            code="video_unavailable", param="video")
    if (body.clip_start is not None and body.clip_end is not None
            and body.clip_end <= body.clip_start):
        return error_response(400, "Stop at must be after Start at.",
                              code="invalid_clip_range", param="clip_end")

    job.clip = ((body.clip_start, body.clip_end) if kind in ("audio", "video")
                else (None, None))
    try:
        downloads.start(job, kind)
    except downloads.Busy:
        return error_response(
            429, f"You already have {downloads.PER_PERSON} downloads running. "
                 f"Wait for one to finish.",
            code="too_many_downloads", headers={"Retry-After": "30"})
    return _json({"token": body.token, "status": "started",
                  # Echoed rather than assumed by the page: it decides between
                  # the <video> and the <audio> element from this and from the
                  # finished filename.
                  "video": kind == "video"})


@router.post("/ui/abandon")
async def abandon(request: Request, body: TokenRequest) -> Response:
    job = _job(request, body.token)
    if isinstance(job, Response):
        return job
    downloads.drop(job.sub, job.url)
    return _json({"token": body.token, "reaped": True})


# What the page reads off each state.
WHERE = {"pending": ("pending", "pending"), "queued": ("queue", "pending"),
         "downloading": ("queue", "downloading"), "finished": ("done", "finished"),
         "error": ("done", "error")}


@router.get("/ui/progress")
async def progress(request: Request, token: str) -> Response:
    job = _job(request, token)
    if isinstance(job, Response):
        return job
    # A finished job whose file has gone is an error from here on.
    downloads.file_of(job, use=False)
    where, status = WHERE[job.state]
    ready = job.state == "finished"
    return _json({
        "token": token,
        "where": where,
        "status": status,
        "ready": ready,
        # The child's own numbers, which are real, unlike anything we could
        # predict about someone else's bandwidth.
        "percent": round(100 * job.done / job.total, 1) if job.total else None,
        "speed": job.speed,
        "eta": job.eta,
        "filename": downloads.filename(job) if ready else None,
        "error": job.error,
    })


# What may not appear in a value written into a multipart header or field:
# a quote or backslash ends the quoted filename, and CR or LF ends the line.
_FRAME_BREAKING = re.compile(r'["\\\x00-\x1f\x7f]')


def _multipart(boundary: str, fields: list[tuple[str, str]], *, filename: str,
               chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """A multipart body streamed from a file, never buffered.

    A LIST OF PAIRS AND NOT A DICT, because `timestamp_granularities[]` is sent
    TWICE -- once for `word` and once for `segment` -- and a dict can hold one
    of them. That is not a detail: `word` is what the highlight follows and
    `segment` is what the caption band and the .srt sidecar are built from, and
    a mapping would have silently dropped whichever came second.

    Written by hand rather than handed to httpx's `files=`, which wants a
    file-like object it can size. The alternative was buffering the whole
    download -- 131 MB for a 2h14m podcast -- into a container with a 512 MB
    limit. Multipart is four lines of framing; that is not worth it.

    THE FILENAME IS A VIDEO'S TITLE, chosen by whoever uploaded it, so what
    would end the quoted parameter is replaced here, where the frame is built.
    stt keeps the rest of it for the run record.
    """
    filename = _FRAME_BREAKING.sub("_", filename)

    async def body() -> AsyncIterator[bytes]:
        for name, value in fields:
            yield (f"--{boundary}\r\n"
                   f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                   f"{value}\r\n").encode()
        yield (f"--{boundary}\r\n"
               f'Content-Disposition: form-data; name="file"; '
               f'filename="{filename}"\r\n'
               f"Content-Type: application/octet-stream\r\n\r\n").encode()
        async for chunk in chunks:
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()
    return body()


@router.post("/ui/captions")
async def captions(request: Request, body: TokenRequest) -> Response:
    """Hand a finished captions download to the page as text.

    THE SIBLING OF /ui/fetch, AND IT CALLS NOTHING. That route exists to move
    media it must never keep; this one returns a file that is already the
    answer. A video with real, human-written subtitles has a transcript
    attached to it, the captions kind fetches only that track, and it costs
    about two seconds and no transcription at all.

    IT IS RETURNED VERBATIM AND PARSED IN THE BROWSER. The page already has a
    SubRip/WebVTT parser for the karaoke highlight -- CUE_LINE and
    parseSubtitles in ui.html -- and a second one here would be two parsers
    that must agree about a cue, in two languages, with only one of them
    tested. The file is at most MAX_CAPTION_BYTES, which the child was held to.
    """
    job = _job(request, body.token)
    if isinstance(job, Response):
        return job
    if job.state != "finished" or job.path is None:
        return _not_ready(job)
    # The mirror of the guard in fetch(): media here would mean the page asked
    # the wrong route, and returning a few megabytes of opus as if it were text
    # is a worse answer than saying so.
    if job.path.suffix not in CAPTION_SUFFIXES:
        return error_response(
            409,
            "That download is media, not subtitles. Transcribe it with POST "
            "/ui/fetch.",
            code="not_captions", param="token")
    path = downloads.file_of(job)
    if path is None:
        return _expired()
    data = await anyio.Path(path).read_bytes()
    return _json({
        "token": body.token,
        "filename": downloads.filename(job),
        # Which of the two the page is holding. It decides the extension the
        # Download button writes; the parser reads both from one pattern,
        # because stt's own _clock() writes both from one function.
        "format": "srt" if path.suffix == ".srt" else "vtt",
        # errors="replace", not "strict". yt-dlp writes UTF-8, but one bad byte
        # in a forty-minute subtitle track would otherwise be a 500 for a file
        # that is 99.99% readable, and a lozenge in one word is the better
        # failure by a wide margin.
        "text": data.decode("utf-8", "replace"),
    })


@router.post("/ui/fetch")
async def fetch(request: Request, body: TokenRequest) -> Response:
    """Stream a finished download into the transcription route, as the person who asked.

    SERVER-SIDE, and that is the point: the browser never downloads the media
    to have it transcribed. The 131 MB of a two-hour podcast goes from this
    container's cache to the gateway to stt-stack, and the laptop sees only
    the transcript.

    AS THE PERSON, NOT AS THIS SERVICE (D64). The gateway hands this request a
    delegation token for the signed-in user; it goes on, unread, to the
    gateway's internal listener beside this service's own key, and the gateway
    checks there that the person's session or key is still live. This service
    holds no scope that can transcribe on its own, so a request without a
    token stops here, before anything is read.
    """
    job = _job(request, body.token)
    if isinstance(job, Response):
        return job
    delegation = identity.delegation_of(request)
    if delegation is None:
        return error_response(
            403, "This request carries no delegation for the person who sent "
                 "it, so nothing can be transcribed on their behalf.",
            code="delegation_refused")
    if CREDENTIALS.service_key() is None:
        return error_response(
            503, "This service's key has not been written to its credential "
                 "volume yet, so it cannot reach the gateway. It appears "
                 "within seconds of the gateway starting.",
            type_="server_error", code="not_ready", headers={"Retry-After": "5"})
    if job.state != "finished" or job.path is None:
        return _not_ready(job)

    # A captions download is ALREADY a transcript. It is read by /ui/captions
    # and never transcribed, which is the entire point of offering it: about
    # two seconds and no compute at all. Streamed into stt, a subtitle file
    # would come back as a decode error from two services away.
    if job.path.suffix in CAPTION_SUFFIXES:
        return error_response(
            409,
            "That download is subtitles, not media. Read it with POST "
            "/ui/captions -- it is already a transcript.",
            code="not_media", param="token")
    path = downloads.file_of(job)
    if path is None:
        return _expired()

    http: httpx.AsyncClient = request.app.state.client
    query = dict(request.query_params)
    # ALLOWLISTED, because this string is interpolated into a multipart frame
    # this module builds by hand. Unvalidated, a value carrying CRLF and a
    # boundary marker closes the part and opens another: a crafted
    # ?response_format= put a SECOND `name="model"` part on the wire, and the
    # gateway received model=parakeet followed by model=whisper. No privilege
    # is gained -- the same caller can POST arbitrary multipart straight at
    # /v1/audio/transcriptions -- but a hand-built protocol frame must not take
    # an unvalidated string, and the set of valid values is this short.
    wanted = query.get("response_format", "json")
    if wanted not in RESPONSE_FORMATS:
        return error_response(
            400, f"response_format must be one of "
                 f"{', '.join(sorted(RESPONSE_FORMATS))}, not {wanted!r}",
            code="invalid_response_format", param="response_format")

    # THE REASON A LINK HAD NO KARAOKE HIGHLIGHT AND NO CAPTION BAND, and it
    # was never about the player. The cues come from timedFromJson(), which
    # reads verbose_json's `words[]` and `segments[]`; formatForUpload() asks
    # for verbose_json but only ever ran on the upload path, and this route
    # forwarded `model` and `response_format` and nothing else -- so a link was
    # transcribed as plain text and no timing ever came back to draw with. Both
    # halves had to move: the page asks, and this route has to be able to carry
    # the ask.
    #
    # REPEATED QUERY PARAMETER, one multipart field each, because that is the
    # shape openai_api.py's _values() reads and it takes the field more than
    # once. Allowlisted for the same reason response_format is: these strings
    # go into a frame this module builds by hand.
    granularities = request.query_params.getlist("timestamp_granularities")
    for value in granularities:
        if value not in GRANULARITIES:
            return error_response(
                400, f"timestamp_granularities must be one of "
                     f"{', '.join(sorted(GRANULARITIES))}, not {value!r}",
                code="invalid_granularity", param="timestamp_granularities")
    # The vocabulary profiles this request selected. Which names exist, and
    # which this person may see, is stt's to say and it answers 400 naming an
    # unknown one -- so the only check here is the frame's own: a value that
    # could end its part is refused before it is written into one.
    glossary = (query.get("glossary") or "").strip()
    if _FRAME_BREAKING.search(glossary):
        return error_response(
            400, "glossary names a vocabulary profile, and a profile name has "
                 "no quotes, backslashes or control characters in it",
            code="invalid_glossary", param="glossary")
    start, end = job.clip
    fields = [
        # Required by /v1 validation, and it does NOT choose an engine --
        # Parakeet runs regardless and says so in x-stt-engine.
        ("model", "parakeet"),
        ("response_format", wanted),
        *(("timestamp_granularities[]", value) for value in granularities),
        *((("glossary", glossary),) if glossary else ()),
        # The excerpt, on the file's own timeline. stt decodes only this window
        # and shifts the times it returns back onto that timeline, so the page
        # plays the whole file and the highlight still lines up. A float, so
        # nothing but digits, a point and a sign reaches the frame.
        *((("clip_start", str(float(start))),) if start is not None else ()),
        *((("clip_end", str(float(end))),) if end is not None else ()),
    ]

    async def upstream() -> AsyncIterator[bytes]:
        # 64 KiB at a time, as the gateway reads it: a 500 MiB download is
        # never in this process's memory.
        async with await anyio.open_file(path, "rb") as source:
            while chunk := await source.read(65536):
                yield chunk

    async def send(key: str) -> httpx.Response:
        # A boundary nobody can predict, because the file is somebody else's
        # bytes: a fixed one written into a video's audio track would end the
        # part early and open a field of the uploader's choosing.
        boundary = f"calliope-{secrets.token_hex(16)}"
        return await http.send(
            http.build_request(
                "POST", f"{config.GATEWAY_INTERNAL_URL}/v1/audio/transcriptions",
                # Named, never copied from the inbound request (D65). The
                # identity assertion is not among them and cannot be:
                # outbound_headers refuses it by name.
                headers=identity.outbound_headers(
                    authorization=f"Bearer {key}",
                    content_type=f"multipart/form-data; boundary={boundary}",
                    x_calliope_delegation=delegation),
                content=_multipart(boundary, fields, filename=downloads.filename(job),
                                   chunks=upstream()),
                timeout=httpx.Timeout(960.0, connect=5.0),
            ),
            stream=True,
        )

    key = CREDENTIALS.service_key() or ""
    try:
        upstream_response: httpx.Response | None = await send(key)
        if upstream_response.status_code == 401:
            # Our key, not the person's: a refused delegation is a 403
            # (§3.7). The gateway may have rotated the key, so read it again
            # and try once more if it changed (§2.4). The gateway checks the
            # key before it counts a use of the delegation, so the retry does
            # not spend the token's second use.
            await upstream_response.aclose()
            upstream_response = None
            fresh = CREDENTIALS.reload_service_key()
            if fresh is not None and fresh != key:
                upstream_response = await send(fresh)
                if upstream_response.status_code == 401:
                    await upstream_response.aclose()
                    upstream_response = None
        if upstream_response is None:
            # Never relayed as a 401, which the page reads as "sign in again"
            # (§4.3) and which no sign-in would fix.
            log.error("the gateway's internal listener refused this "
                      "service's key; link transcription is unavailable "
                      "until service.key matches")
            return error_response(
                503, "The gateway refused this service's own key, so the "
                     "link cannot be transcribed. It is a deployment fault, "
                     "not your sign-in.",
                type_="server_error", code="service_key_refused",
                headers={"Retry-After": "30"})
    except httpx.RequestError as exc:
        # The type only: an exception's text can carry a URL.
        log.warning("ingest fetch failed: %s", type(exc).__name__)
        return error_response(
            502, f"could not hand the downloaded audio to the gateway: "
                 f"{type(exc).__name__}",
            type_="server_error", code="ingestion_unavailable")

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()

    return StreamingResponse(
        relay(), status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type",
                                                 "application/json"))


@router.get("/ui/media")
async def media(request: Request, token: str) -> Response:
    """A finished download, to the browser's <audio> or <video>, byte ranges and all.

    THE PART THAT MATTERS IS THE RANGE. A media element seeks by asking for a
    byte range; served by something that ignores Range and answers 200 with
    the whole file, it plays from the start and every scrub is silently
    ignored. Starlette's FileResponse answers Range, If-Range and 416, and its
    ETag is built from the file's mtime and size, which touch() never moves.

    THE PATH COMES FROM THE JOB, NEVER FROM THE REQUEST. The caller's own
    finished job (D36), a media suffix and nothing else; a <video src> carries
    the session cookie like any other request, so a guessed URL is no way into
    someone else's download. No guard.check here: a playback makes dozens of
    range requests for a URL that is only looked up, never fetched.

    nosniff, `sandbox` and no-store, because the bytes are a stranger's: the
    browser plays them as the type this service names and never as a page.
    """
    job = _job(request, token)
    if isinstance(job, Response):
        return job
    if job.state != "finished" or job.path is None:
        return _not_ready(job)
    if job.path.suffix not in MEDIA_SUFFIXES:
        return error_response(
            409,
            "That download is not media this page can play. A captions "
            "download is read with POST /ui/captions instead.",
            code="not_media", param="token")
    path = downloads.file_of(job)
    if path is None:
        return _expired()
    return FileResponse(
        path, media_type=downloads.MEDIA_TYPES.get(path.suffix, "application/octet-stream"),
        headers={"X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "sandbox",
                 "Cache-Control": "private, no-store"})
