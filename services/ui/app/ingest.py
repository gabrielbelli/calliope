"""Resolve, confirm, fetch -- and never in a different order.

    POST /ui/resolve   guard -> MeTube /add auto_start:false -> probe
    POST /ui/commit    MeTube /start (or re-add, when a clip range was chosen)
    POST /ui/abandon   MeTube /delete from BOTH queues, then verify
    GET  /ui/progress  MeTube /history, as percent / speed / eta
    POST /ui/fetch     MeTube's file -> multipart -> the gateway's internal
                       listener, as the person who asked (D64) -> stt-stack
    POST /ui/captions  MeTube's .vtt/.srt -> the page. NO stt CALL AT ALL
    GET  /ui/media     MeTube's finished file -> the browser, byte ranges and all

EVERY LINK HAS AN OWNER, AND EVERY ROUTE BUT /ui/resolve CHECKS IT FIRST (D36).
The token is the URL, which anyone can guess, and MeTube's queue belongs to
nobody, so /ui/resolve records who asked (app/owners.py) and every other route
answers 404 to anybody else -- before MeTube is asked anything, so a stranger's
link costs no request and reveals nothing. A URL another person has pending is
a 409 on resolve. A record MeTube holds that nobody owns (this process
restarted) is reachable only with `jobs:read:all`, and resolving it again is
no way round that: it is a 409 on resolve to everyone else.

THE TRANSCRIPTION IS THE USER'S, NOT THIS SERVICE'S. /ui/fetch arrives with a
delegation token beside the identity assertion; it is sent on, with this
service's own key, to the gateway's internal listener, which re-checks the
person's session or key live and counts the token's uses (D64). The run record
is then owned by that person, and a person signed out since cannot use it.

/ui/media IS THE ONE ROUTE HERE THAT SENDS MEDIA DOWNWARDS, and it is opt-in at
every step rather than a hole in the design above. Everything else in this file
exists so that a two-hour podcast costs this laptop a transcript and not
131 MB; that stays the default and nothing fetches on its own. What /ui/media
adds is that the file MeTube has ALREADY downloaded can be played back, on
demand, when someone presses play -- which is what makes the caption band and
the karaoke highlight work for a link at all. Before it, both features were
upload-only and every screenshot the user sends is a pasted link.

WHY IT MUST BE THIS SERVICE THAT SERVES THE BYTES. MeTube emits no CORS headers
of any kind -- CORS_ALLOWED_ORIGINS is empty, on_prepare returns early, and its
socket.io server was built with cors_allowed_origins=[] -- so a browser cannot
fetch from port 30097 whatever the user does. Verified live.

AND WHY IT IS A RELAY RATHER THAN A RANGE SERVER. MeTube's static route already
does ranges properly: `Range: bytes=0-1023` came back `206 Partial Content`
with `Content-Range: bytes 0-1023/533915`, `Accept-Ranges: bytes` and
`Content-Type: video/mp4`. Parsing ranges here would be a second, worse
implementation of something already correct one hop away, and it would have to
agree with aiohttp about every edge -- an open-ended range, a range past the
end, a stale If-Range. So the header goes up untouched and the answer comes
back untouched.

NOTHING IS DOWNLOADED BEFORE THE USER SAYS SO, which is the requirement the
user pressed hardest on. `auto_start:false` on /add resolves the URL and parks
it; only /start moves bytes; /delete abandons it. All three verified against
MeTube's source and live against the running instance.

WHY /add COMES BEFORE THE PROBE, when probing first would be tidier. Because
POST /add is where MeTube's url_guard runs, and that guard is better than ours:
ingress validation plus a connect-time getaddrinfo hook inside the download
subprocess, so it also covers redirects and DNS rebinding. Putting our
subprocess first would make our own forty-line pre-filter the only thing that
had run before a URL was handed to yt-dlp.

THE COST OF THAT ORDERING, NAMED RATHER THAN DISCOVERED LATER. Every link the
user declines has already left a pending record in MeTube. So /ui/abandon is
not a nicety, it is the other half of /ui/resolve, and it is tested -- see
tests/test_ingest.py::test_abandon_reaps_a_declined_link. MeTube's /delete
answers {"status":"ok"} when it deletes nothing at all, so abandoning re-reads
/history and reports whether the record actually went.

THE ESTIMATES ARE NOT COMPUTED HERE, deliberately. This module returns FACTS --
title, duration, the size of the audio-only stream, is_live, whether real
subtitles exist -- and the page does the arithmetic with the realtime factor it
has measured on this box. Two reasons. The rate is a moving number the browser
keeps an EMA of from every transcription it runs, and a server-side estimate
would be a second, staler copy of it. And the download half and the transcribe
half are NEVER blended into one figure: for long media the download is the slow
half, and one merged number hides which half to blame when it drags.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from voice_common import identity
from voice_common.errors import error_response

from . import clips, config, guard, metube, owners, probe

log = logging.getLogger("voice-ui.ingest")

# identity.pub and service.key, from this service's credential volume (D7).
# One instance, shared with identity.install in main.py, so the key that
# verifies an assertion and the key that is sent on /ui/fetch are read from
# the same place.
CREDENTIALS = identity.Credentials()

# Who resolved each link. Empty at start, which is what makes a record MeTube
# kept across a restart reachable only with jobs:read:all.
OWNERS = owners.Owners()

# What /v1/audio/transcriptions accepts. Kept here rather than imported from
# the gateway because this service must not depend on that one's internals,
# and kept as a set rather than a regex because the whole vocabulary is five
# words -- see the interpolation guard in fetch() for why it is checked at all.
RESPONSE_FORMATS = frozenset({"json", "text", "srt", "vtt", "verbose_json"})

# What a captions download leaves behind. yt-dlp writes WebVTT by default and
# SubRip when it is asked for one, and MeTube passes the choice through -- so
# these two suffixes are how a finished record is told apart from a media one
# WITHOUT asking MeTube what it was, which it does not record: the /history
# entry for a captions download and for an audio download differ in the
# filename and in nothing else. That is why the check below is on the suffix.
CAPTION_SUFFIXES = (".vtt", ".srt")

# WHAT /ui/media WILL HAND TO A BROWSER, as an allowlist rather than "anything
# that is not a subtitle". The route serves a file off someone else's
# application by a name that application chose, and the difference between the
# two spellings is what happens to the file MeTube writes that nobody here
# anticipated -- a .part, a .json info sidecar, a .jpg thumbnail. An allowlist
# refuses those by default; a denylist serves them and waits to be corrected.
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
    # Asks MeTube for wav rather than opus. Set only by the clone sheet: a
    # reference clip is twenty seconds and has to be readable by the stdlib
    # `wave` module, and transcription keeps opus because that is what makes a
    # two-hour download cheap.
    for_clip: bool = False
    # Promoted out of any expert panel and onto the confirm card itself,
    # because for long media this is what turns "no, too much" into "yes, but
    # only this bit" -- and for a live stream, which has no end, it is the only
    # answer there is.
    clip_start: float | None = Field(default=None, ge=0)
    clip_end: float | None = Field(default=None, ge=0)
    # A video with real (non-ASR) captions already has a human transcript.
    # MeTube's captions type sets skip_download and returns it in about two
    # seconds for near-zero cost, which beats transcribing it however fast
    # Parakeet is.
    captions: bool = False
    # KEEP THE PICTURE, AND IT DEFAULTS OFF ON PURPOSE. Audio-only is what makes
    # a link affordable -- download_type "audio" never pulls the video stream at
    # all, so a 2h14m podcast is ~131 MB rather than gigabytes -- and that must
    # not change because a feature was added. This is per link, ticked on the
    # confirm card next to the size it changes, and the card says so before
    # anything is fetched.
    video: bool = False


class TokenRequest(BaseModel):
    token: str


def _client(request: Request) -> metube.MeTube:
    return metube.MeTube(request.app.state.client)


def _unavailable(exc: metube.MeTubeError) -> Response:
    """Render a MeTube failure as the thing that actually went wrong.

    A refusal is the caller's link; anything else is our dependency. Getting
    this backwards sent someone to debug a healthy MeTube because their own
    URL was rejected.
    """
    if isinstance(exc, metube.MeTubeRefused):
        return error_response(400, str(exc), code="refused_url", param="url")
    return error_response(502, str(exc), type_="server_error",
                          code="ingestion_unavailable")


def _unknown_token() -> Response:
    return error_response(404, "MeTube has no record of that link.",
                          code="unknown_token", param="token")


def _foreign(request: Request, token: str) -> Response | None:
    """The 404 for a link this caller did not resolve, or None if it may use it.

    The same answer MeTube's own silence gets, so a stranger's link and a link
    nobody has pasted cannot be told apart. A link nobody owns -- one MeTube
    kept across a restart of this process -- is reachable with jobs:read:all
    and by nobody else.
    """
    claims = identity.claims_of(request)
    holder = OWNERS.owner(token)
    if holder == claims.sub:
        return None
    if holder is None and identity.has(claims, "jobs:read:all"):
        return None
    return _unknown_token()


@router.post("/ui/resolve")
async def resolve(request: Request, body: ResolveRequest) -> Response:
    if not metube.configured():
        return error_response(
            501,
            "Link ingestion is not configured: set UI_METUBE_URL to the "
            "MeTube instance on this host. File upload still works.",
            code="ingestion_not_configured")

    claims = identity.claims_of(request)
    who = claims.sub
    if _rate_limited(who):
        return error_response(
            429, f"Too many links resolved; the limit is "
            f"{config.RESOLVE_PER_MINUTE} a minute.",
            code="rate_limited", headers={"Retry-After": "30"})

    # Layer one. Our own stdlib pre-filter, so a URL we hate never becomes a
    # log line in MeTube either. See app/guard.py for what is blocked and why.
    try:
        url = guard.check(body.url)
    except guard.GuardError as exc:
        return error_response(400, str(exc), code="refused_url", param="url")

    # WHOSE IT IS, decided before MeTube is told anything, and by one resolve
    # of this link at a time: a second person pasting it while the first
    # person's resolve is still waiting on MeTube is answered after it, so they
    # see the first person's claim and MeTube's record of it rather than the
    # gap between the two (owners.Owners.turn).
    client = _client(request)
    async with OWNERS.turn(url):
        holder = OWNERS.owner(url)
        if holder != who:
            try:
                held = await client.find(url)
            except metube.MeTubeError as exc:
                return _unavailable(exc)
            if held is None and holder is not None:
                # MeTube has let it go, so there is no download left to
                # protect: the link is nobody's and anyone may take it.
                OWNERS.release(url, holder)
                holder = None
            if held is not None and holder is not None:
                # One record is one download, and sharing it would hand one
                # person's progress, file and transcript to the other.
                return error_response(
                    409, "Someone else on this Calliope has that link in "
                         "progress. Try again once theirs is done.",
                    code="pending_for_another_user", param="url")
            if held is not None and not identity.has(claims, "jobs:read:all"):
                # A record nobody owns -- kept across a restart of this
                # process, or whose claim aged out -- may be somebody's live
                # download. Resolving it would make it the caller's, so the
                # rule every other route applies to it applies here too.
                return error_response(
                    409, "MeTube already holds a download of that link that "
                         "nobody here owns, so only an administrator can use "
                         "or clear it.",
                    code="pending_for_another_user", param="url")
            # Nothing else claims this link while the turn is held.
            OWNERS.claim(url, who)

        response: Response | None = None
        try:
            response = await _resolve_owned(client, url)
        finally:
            if response is not None and response.status_code == 200:
                OWNERS.keep(url, who)
            elif holder != who:
                # A resolve that did not succeed leaves no claim behind --
                # unless the caller held the link before this request, when a
                # failed second resolve must not cost them the first.
                OWNERS.release(url, who)
        return response


async def _resolve_owned(client: metube.MeTube, url: str) -> Response:
    """The rest of /ui/resolve, once the caller owns the link."""
    # Layer two, and the one that decides. MeTube's url_guard runs inside this
    # call; if it refuses we return its message verbatim and stop.
    try:
        await client.add(url, auto_start=False)
    except metube.MeTubeError as exc:
        return _unavailable(exc)

    try:
        found = await client.find(url)
    except metube.MeTubeError as exc:
        return _unavailable(exc)
    if found is None:
        return error_response(
            502, "MeTube accepted the link and then had no record of it.",
            type_="server_error", code="ingestion_unavailable")

    where, entry = found
    if where == "done" and entry.get("status") != metube.FINISHED:
        # A rejected add still creates a record, in `done`, with the reason in
        # `msg` or `error`. Surface it and clear it rather than leaving a dead
        # row behind.
        await client.abandon(url)
        return error_response(
            400, str(entry.get("error") or entry.get("msg")
                     or "MeTube could not resolve that link."),
            code="refused_url", param="url")

    # Layer three, and only now: a URL both guards have already accepted. The
    # probe resolves the host once more just before it spawns (D36); a name
    # that has moved to a private address since layer one is refused here, and
    # MeTube's pending record goes with it.
    try:
        facts = await probe.run(url) if probe.available() else None
    except probe.DestinationNotAllowed as exc:
        await client.abandon(url)
        return error_response(400, str(exc), code="destination_not_allowed",
                              param="url")

    title = (facts or {}).get("title") or entry.get("title") or url
    duration = (facts or {}).get("duration")
    size = (facts or {}).get("bytes")
    live = bool((facts or {}).get("is_live")) or entry.get("live_status") == "is_live"

    # WHEN TO NAG. Below both thresholds and not live, the page skips the
    # dialog entirely -- see config.CONFIRM_SECONDS for the defence of the
    # numbers. An unknown duration always confirms: not knowing is exactly the
    # case the dialog exists for.
    confirm = (live or duration is None
               or duration > config.CONFIRM_SECONDS
               or (size or 0) > config.CONFIRM_BYTES)

    return Response(media_type="application/json", content=_json({
        "token": url,
        "title": title,
        "uploader": (facts or {}).get("uploader"),
        "duration": duration,
        "bytes": size,
        "is_live": live,
        "has_subtitles": bool((facts or {}).get("has_subtitles")),
        "probed": facts is not None,
        "confirm": confirm,
        # So the card can say "duration unknown -- yt-dlp is not installed in
        # this image" rather than silently showing less than it should.
        "probe_enabled": probe.available(),
    }))


@router.post("/ui/commit")
async def commit(request: Request, body: CommitRequest) -> Response:
    if (refused := _foreign(request, body.token)) is not None:
        return refused
    client = _client(request)
    try:
        guard.check(body.token)
    except guard.GuardError as exc:
        return error_response(400, str(exc), code="refused_url", param="token")
    # The token exactly as _foreign judged it, not what guard.check returns:
    # that is stripped, so " " + someone else's link has no owner, passes
    # _foreign for a holder of jobs:read:all, and would then reach the other
    # person's record (D36). Every other route uses the token as given, too.
    url = body.token

    # THE GATE IS ENFORCED HERE, NOT ONLY IN THE PAGE. Without this, POST
    # /ui/commit succeeded on a token that had never been resolved, and with
    # clip_start or captions set it went straight to /add {auto_start:true} --
    # a download starting with no resolve step and no dialog. The page never
    # takes that path, and the caller is already authenticated, so it was a UX
    # gate rather than a boundary; "nothing is fetched until the user agrees"
    # was a property of the page and not of the service. Requiring the pending
    # record that /ui/resolve leaves behind makes it a property of both.
    try:
        parked = await client.find(url)
    except metube.MeTubeError as exc:
        return _unavailable(exc)
    if parked is None:
        return error_response(
            409,
            "That link was never resolved, so there is nothing to confirm. "
            "POST /ui/resolve first and commit the token it returns.",
            code="not_resolved", param="token")

    trimmed = body.clip_start is not None or body.clip_end is not None
    # WHICH OF THE THREE KINDS OF DOWNLOAD THIS IS, decided once. They are
    # mutually exclusive and the precedence is not arbitrary:
    #
    #   captions  wins over everything. It sets skip_download, so there is no
    #             media at all -- asking for a video AND for no media is a
    #             contradiction, and the page's captions button never offers
    #             the video tick anyway.
    #   for_clip  wins over video. The clone sheet needs a WAV the stdlib
    #             `wave` module can measure, and it never sets video; if some
    #             future caller sets both, silently handing clips.save an mp4
    #             would fail two services later with "that file is not a WAV".
    #   video     the only one that is new, and the only one the user ticks.
    if body.captions:
        download_type, file_format = "captions", None
    elif body.for_clip:
        download_type, file_format = "audio", "wav"
    elif body.video:
        download_type, file_format = "video", config.METUBE_VIDEO_FORMAT
    else:
        download_type, file_format = "audio", None
    try:
        if trimmed or download_type != "audio" or file_format is not None:
            # /start promotes a pending item with the options it was ADDED
            # with; there is no route that edits them. So a clip range, a
            # switch to captions or a switch to video means dropping the
            # pending record and adding it again with the final options and
            # auto_start:true. That costs a second extract_info on MeTube's
            # side and is the only correct way to apply any of them.
            #
            # The condition is written as "anything but a plain untrimmed audio
            # commit" rather than as a list of the cases that need it, because
            # the list was already two items long and each new one was a
            # download that quietly came back in the wrong format.
            await client.abandon(url)
            await client.add(url, auto_start=True,
                             download_type=download_type,
                             audio_format=file_format,
                             clip_start=body.clip_start,
                             clip_end=body.clip_end)
        else:
            await client.start(url)
    except metube.MeTubeError as exc:
        return _unavailable(exc)
    return Response(media_type="application/json",
                    content=_json({"token": url, "status": "started",
                                   # Echoed rather than assumed by the page: it
                                   # decides between the <video> and the
                                   # <audio> element from this and from the
                                   # finished filename, and a page that merely
                                   # remembered what it asked for would show a
                                   # black rectangle whenever the two differed.
                                   "video": download_type == "video"}))


@router.post("/ui/abandon")
async def abandon(request: Request, body: TokenRequest) -> Response:
    if (refused := _foreign(request, body.token)) is not None:
        return refused
    client = _client(request)
    try:
        reaped = await client.abandon(body.token)
    except metube.MeTubeError as exc:
        return _unavailable(exc)
    if reaped:
        # Nothing left to own, so the link is free for whoever pastes it next.
        OWNERS.release(body.token, identity.claims_of(request).sub)
    # Reported rather than assumed. A false here means MeTube still holds the
    # record and someone should look, which is strictly better than a green
    # tick over an orphan.
    return Response(media_type="application/json",
                    content=_json({"token": body.token, "reaped": reaped}))


@router.get("/ui/progress")
async def progress(request: Request, token: str) -> Response:
    if (refused := _foreign(request, token)) is not None:
        return refused
    client = _client(request)
    try:
        found = await client.find(token)
    except metube.MeTubeError as exc:
        return _unavailable(exc)
    if found is None:
        return _unknown_token()

    where, entry = found
    status = entry.get("status")
    # Terminal success is `finished` and nothing else: anything else in `done`
    # means _post_download_cleanup rewrote the status to "error" and nulled the
    # filename, so treating "in done" as "ready" hands the next step a null.
    ready = where == "done" and status == metube.FINISHED and entry.get("filename")
    return Response(media_type="application/json", content=_json({
        "token": token,
        "where": where,
        "status": status,
        "ready": bool(ready),
        # MeTube's own numbers, which are real, unlike anything we could
        # predict about someone else's bandwidth.
        "percent": entry.get("percent"),
        "speed": entry.get("speed"),
        "eta": entry.get("eta"),
        "filename": entry.get("filename"),
        "error": entry.get("error") or entry.get("msg"),
    }))


# What may not appear in a value written into a multipart header or field:
# a quote or backslash ends the quoted filename, and CR or LF ends the line.
_FRAME_BREAKING = re.compile(r'["\\\x00-\x1f\x7f]')


def _multipart(boundary: str, fields: list[tuple[str, str]], *, filename: str,
               chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """A multipart body streamed from a remote response, never buffered.

    A LIST OF PAIRS AND NOT A DICT, because `timestamp_granularities[]` is sent
    TWICE -- once for `word` and once for `segment` -- and a dict can hold one
    of them. That is not a detail: `word` is what the highlight follows and
    `segment` is what the caption band and the .srt sidecar are built from, and
    a mapping would have silently dropped whichever came second.

    Written by hand rather than handed to httpx's `files=`, which wants a
    file-like object it can size. The alternative was buffering the whole
    ingest -- 131 MB for the brief's own 2h14m example -- into a container with
    a 512 MB limit, or spooling it to a disk this service otherwise never
    touches. Multipart is four lines of framing; neither of those is worth it.

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


class ClipFromLink(BaseModel):
    token: str
    name: str
    replace: bool = False


@router.post("/ui/clips/from-link")
async def clip_from_link(request: Request, body: ClipFromLink) -> Response:
    """Turn a finished MeTube download into a reference clip for cloning.

    The sibling of /ui/fetch, and deliberately NOT the same route: that one
    streams into the gateway's transcription chain and never keeps a byte, this
    one keeps the bytes and never transcribes. Sharing them would mean a
    `purpose` flag deciding which of two unrelated things happens.

    WHY THIS DOES NOT STREAM. A reference clip is ten to thirty seconds at
    24 kHz -- about 1.4 MB, with a 25 MB ceiling -- and clips.save() validates
    the duration of the whole file before writing it. There is nothing to
    stream to; holding it is the point.

    THE TRIM ALREADY HAPPENED, at the source. The page sends clip_start and
    clip_end on /ui/commit, so yt-dlp fetched only the window asked for and a
    two-hour interview cost twenty seconds of download. That is why cloning
    from a link is cheap enough to be an ordinary thing to do rather than a
    reason to go and find the file yourself.

    THE CLIP IS THE CALLER'S, in their own namespace (D35), like an upload.
    """
    if (refused := _foreign(request, body.token)) is not None:
        return refused
    claims = identity.claims_of(request)
    client = _client(request)
    try:
        found = await client.find(body.token)
    except metube.MeTubeError as exc:
        return _unavailable(exc)
    if found is None:
        return _unknown_token()

    where, entry = found
    filename = entry.get("filename")
    if where != "done" or entry.get("status") != metube.FINISHED or not filename:
        return error_response(
            409, f"that download is {entry.get('status') or where}, not finished",
            code="not_ready", param="token")

    source = client.audio_url(str(filename), str(entry.get("folder") or ""))
    http: httpx.AsyncClient = request.app.state.client
    try:
        response = await http.get(source, timeout=120.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return error_response(
            502, f"could not read the downloaded audio ({type(exc).__name__})",
            type_="server_error", code="ingestion_unavailable")

    # The same ceiling the upload route applies, checked before the body is
    # handed on: a clip is small, and something this size arriving here means
    # the trim did not happen.
    if len(response.content) > config.MAX_CLIP_BYTES:
        return error_response(
            413,
            f"that download is {len(response.content) / 1024**2:.1f} MB; a "
            f"reference clip is capped at "
            f"{config.MAX_CLIP_BYTES / 1024**2:.0f} MB. Trim it on the card "
            f"before importing.",
            code="clip_too_large", param="token")

    owner = clips.owner_of(claims)
    try:
        saved = clips.save(body.name, response.content, owner=owner,
                           replace=body.replace)
    except clips.ClipError as exc:
        return error_response(400, str(exc), code="invalid_clip", param="name")

    # Reaped either way: the page is done with the download the moment the clip
    # is written, and leaving it in MeTube's `done` list is litter in somebody
    # else's application.
    try:
        if await client.abandon(body.token):
            OWNERS.release(body.token, claims.sub)
    except metube.MeTubeError:
        # The link itself is never logged: it is what this person fetched.
        log.warning("clip imported but the MeTube record was not reaped")

    return Response(media_type="application/json", status_code=201,
                    content=_json({"voice": saved,
                                   "voices": clips.listing(owner)}))


@router.post("/ui/captions")
async def captions(request: Request, body: TokenRequest) -> Response:
    """Hand a finished captions download to the page as text.

    THE SIBLING OF /ui/fetch, AND IT CALLS NOTHING. That route exists to move
    media it must never keep; this one returns a file that is already the
    answer. A video with real, human-written subtitles has a transcript
    attached to it, MeTube's captions type fetches only that track -- yt-dlp
    sets skip_download, so no media stream is pulled -- and it costs about two
    seconds and no transcription at all. Sending it on to stt would be paying
    minutes of compute to reproduce, worse, a file we already hold.

    IT IS RETURNED VERBATIM AND PARSED IN THE BROWSER. The page already has a
    SubRip/WebVTT parser for the karaoke highlight -- CUE_LINE and
    parseSubtitles in ui.html -- and a second one here would be two parsers
    that must agree about a cue, in two languages, with only one of them
    tested. So this route reads bytes and decides nothing about them.
    """
    if (refused := _foreign(request, body.token)) is not None:
        return refused
    client = _client(request)
    try:
        found = await client.find(body.token)
    except metube.MeTubeError as exc:
        return _unavailable(exc)
    if found is None:
        return _unknown_token()

    where, entry = found
    filename = entry.get("filename")
    if where != "done" or entry.get("status") != metube.FINISHED or not filename:
        return error_response(
            409, f"that download is {entry.get('status') or where}, not finished",
            code="not_ready", param="token")

    # The mirror of the guard in fetch(): media here would mean the page asked
    # the wrong route, and returning a few megabytes of opus as if it were text
    # is a worse answer than saying so.
    name = str(filename)
    if not name.lower().endswith(CAPTION_SUFFIXES):
        return error_response(
            409,
            "That download is media, not subtitles. Transcribe it with POST "
            "/ui/fetch.",
            code="not_captions", param="token")

    http: httpx.AsyncClient = request.app.state.client
    folder = str(entry.get("folder") or "")
    # /audio_download/ first because that is where everything lands on this
    # deployment, then /download/ -- see MeTube.video_url for why a subtitle
    # file is the one thing that can be in the other directory. The second
    # request only ever happens after the first has 404'd.
    last = ""
    body_bytes: bytes | None = None
    for source in (client.audio_url(name, folder), client.video_url(name, folder)):
        try:
            response = await http.get(source, timeout=30.0)
        except httpx.HTTPError as exc:
            return error_response(
                502, f"could not read the subtitle file ({type(exc).__name__})",
                type_="server_error", code="ingestion_unavailable")
        if response.status_code == 404:
            last = source
            continue
        if response.status_code >= 400:
            return error_response(
                502, f"MeTube answered {response.status_code} for the subtitle "
                     f"file it reported",
                type_="server_error", code="ingestion_unavailable")
        # Bounded because this one is buffered rather than streamed: a subtitle
        # track is on the order of 100 KB and there is nothing to stream it to,
        # but a name ending .vtt is not a promise about the size behind it. See
        # config.MAX_CAPTION_BYTES.
        if len(response.content) > config.MAX_CAPTION_BYTES:
            return error_response(
                413,
                f"that subtitle file is "
                f"{len(response.content) / 1024**2:.1f} MB, which is not a "
                f"subtitle file.",
                code="captions_too_large", param="token")
        body_bytes = response.content
        break
    if body_bytes is None:
        return error_response(
            502, f"MeTube reported {name} and then served it from neither "
                 f"directory (last tried {last}).",
            type_="server_error", code="ingestion_unavailable")

    return Response(media_type="application/json", content=_json({
        "token": body.token,
        "filename": name,
        # Which of the two the page is holding. It decides the extension the
        # Download button writes; the parser reads both from one pattern,
        # because stt's own _clock() writes both from one function.
        "format": "srt" if name.lower().endswith(".srt") else "vtt",
        # errors="replace", not "strict". yt-dlp writes UTF-8, but one bad byte
        # in a forty-minute subtitle track would otherwise be a 500 for a file
        # that is 99.99% readable, and a lozenge in one word is the better
        # failure by a wide margin.
        "text": body_bytes.decode("utf-8", "replace"),
    }))


@router.post("/ui/fetch")
async def fetch(request: Request, body: TokenRequest) -> Response:
    """Stream MeTube's finished file into the transcription route, as the person who asked.

    SERVER-SIDE, and that is the point: the browser never downloads the media.
    The 131 MB of a two-hour podcast goes MeTube -> here -> gateway ->
    stt-stack over the NAS's own network and the laptop sees only the
    transcript. It is also the only place it CAN happen: MeTube's
    CORS_ALLOWED_ORIGINS is empty, `on_prepare` returns early and emits no CORS
    headers, and its socket.io server was constructed with
    cors_allowed_origins=[], so a browser page cannot call MeTube at all.

    AS THE PERSON, NOT AS THIS SERVICE (D64). The gateway hands this request a
    delegation token for the signed-in user; it goes on, unread, to the
    gateway's internal listener beside this service's own key, and the gateway
    checks there that the person's session or key is still live. This service
    holds no scope that can transcribe on its own, so a request without a
    token stops here, before anything is downloaded.
    """
    if (refused := _foreign(request, body.token)) is not None:
        return refused
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

    client = _client(request)
    try:
        found = await client.find(body.token)
    except metube.MeTubeError as exc:
        return _unavailable(exc)
    if found is None:
        return _unknown_token()
    where, entry = found
    filename = entry.get("filename")
    if where != "done" or entry.get("status") != metube.FINISHED or not filename:
        return error_response(
            409, f"that download is {entry.get('status') or where}, not finished",
            code="not_ready", param="token")

    # THE BUG THIS ROUTE SHIPPED WITH, and the reason the guard is here rather
    # than only in the page. POST /ui/commit {captions:true} asks MeTube for
    # download_type "captions", which sets yt-dlp's skip_download and produces
    # a .vtt or .srt and no media. This route then took whatever `filename`
    # came back and streamed it into /v1/audio/transcriptions, so stt-stack was
    # handed a text file and asked to decode it as media. The confirm card's
    # "This has real subtitles already" button could not work, and the failure
    # arrived as a decode error from two services away.
    #
    # A captions download is ALREADY a transcript. It is read by /ui/captions
    # below and never transcribed, which is the entire point of offering it:
    # about two seconds and no compute at all.
    if str(filename).lower().endswith(CAPTION_SUFFIXES):
        return error_response(
            409,
            "That download is subtitles, not media. Read it with POST "
            "/ui/captions -- it is already a transcript.",
            code="not_media", param="token")

    # NEVER PREDICTED. `filename` is OUTPUT_TEMPLATE sanitised and byte-trimmed
    # by MeTube; reconstructing it from the title is wrong for every title with
    # a slash, a colon or a non-BMP character in it.
    source = client.audio_url(str(filename), str(entry.get("folder") or ""))
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
    fields = [
        # Required by /v1 validation, and it does NOT choose an engine --
        # Parakeet runs regardless and says so in x-stt-engine.
        ("model", "parakeet"),
        ("response_format", wanted),
        *(("timestamp_granularities[]", value) for value in granularities),
        *((("glossary", glossary),) if glossary else ()),
    ]

    async def upstream() -> AsyncIterator[bytes]:
        seen = 0
        async with http.stream("GET", source, timeout=60.0) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(65536):
                seen += len(chunk)
                if seen > config.MAX_UPLOAD_BYTES:
                    # A cap on what OUR code pulls, because the only other
                    # ceiling in this chain is stt-stack's memory limit and it
                    # meets it as an OOM kill rather than as an error.
                    raise httpx.ReadError(
                        f"ingested file exceeded {config.MAX_UPLOAD_BYTES} bytes")
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
                content=_multipart(boundary, fields, filename=str(filename),
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
        # The type only: an exception's text can carry a URL, and the link is
        # what this person fetched.
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


# The headers a ranged media response is made of. Relayed rather than
# regenerated, so this service never has an opinion about a range it did not
# parse:
#
#   content-range    which bytes these are, and how many there are in total.
#                    Without it a 206 is meaningless and Safari gives up.
#   content-length   of THIS response, which for a 206 is the slice and not
#                    the file. Recomputing it here is how a player ends up
#                    waiting for bytes that are never coming.
#   accept-ranges    what tells the element it may seek at all. Dropped, the
#                    scrub bar becomes decorative and playback restarts from
#                    zero on every attempt -- which is the exact failure this
#                    route exists to avoid.
#   content-type     video/mp4 or audio/*, from aiohttp's own guess off the
#                    suffix. A <video> given application/octet-stream declines
#                    to play it.
#   etag,
#   last-modified    what If-Range is COMPARED AGAINST. Relaying the request
#                    header while dropping these two makes every conditional
#                    range unconditional, which is a silently corrupted file
#                    the moment a download is replaced mid-playback.
#   content-encoding because the body is relayed raw (aiter_raw). Dropping it
#                    would label gzip bytes as mp4.
RELAYED = ("content-range", "content-length", "accept-ranges", "content-type",
           "etag", "last-modified", "content-encoding")


def _total_bytes(upstream: httpx.Response) -> int | None:
    """The size of the WHOLE file, from whichever header carries it.

    On a 206 that is the figure after the slash in `Content-Range: bytes
    0-1023/533915`, and Content-Length is the slice -- so reading Content-Length
    alone would compare a 1 KB first request against a 4 GiB ceiling and admit
    a file of any size at all, one range at a time.
    """
    ranged = upstream.headers.get("content-range", "")
    if "/" in ranged:
        total = ranged.rsplit("/", 1)[1].strip()
        if total.isdigit():
            return int(total)
    declared = upstream.headers.get("content-length", "")
    if declared.isdigit() and upstream.status_code == 200:
        return int(declared)
    return None


@router.get("/ui/media")
async def media(request: Request, token: str) -> Response:
    """Stream a finished download to the browser, byte ranges and all.

    THE PART THAT MATTERS IS THE RANGE, not the streaming. A <video> or
    <audio> element seeks by asking for a byte range; served by something that
    ignores Range and answers 200 with the whole file, it plays from the start
    and every scrub is silently ignored -- the picture moves back to zero and
    the user concludes the player is broken. So the header goes up and the 206
    comes back, and this function parses neither.

    WHY THE ANSWER IS NOT SIMPLY PROXIED. Three things are checked first, and
    each one is the boundary for a different failure:

      * MeTube must already hold a FINISHED record for this token. That is what
        "only a token this page actually resolved" means in practice, and it is
        the same gate /ui/fetch and /ui/captions apply. Without it this route
        is an open read of anything in someone else's download directory, by a
        name a caller supplies.
      * The filename must be media. A .vtt served as video/mp4 is a confusing
        failure; a name that got past the suffix check is one this route
        refuses rather than relays.
      * The whole file must be inside MAX_MEDIA_BYTES. See config: it is
        deliberately its own setting and not MAX_UPLOAD_BYTES, which bounds
        what services/stt reads into memory rather than what a laptop pulls
        down a domestic line.

    NO guard.check ON THE TOKEN HERE, and that is deliberate rather than an
    omission. guard.check resolves the hostname, and a single playback makes
    dozens of range requests -- one getaddrinfo each would be a DNS lookup per
    scrub for a URL that is never fetched on this path, only looked up in a
    dictionary. The URL was guarded twice before anything was downloaded, by
    /ui/resolve and by MeTube's own url_guard, and the /history lookup below is
    what proves this is that same link. And before either, the link must be
    the caller's own (D36): a <video src> carries the session cookie like any
    other request, so a guessed URL is no way into someone else's download.
    """
    if (refused := _foreign(request, token)) is not None:
        return refused
    client = _client(request)
    try:
        found = await client.find(token)
    except metube.MeTubeError as exc:
        return _unavailable(exc)
    if found is None:
        return _unknown_token()

    where, entry = found
    filename = entry.get("filename")
    if where != "done" or entry.get("status") != metube.FINISHED or not filename:
        return error_response(
            409, f"that download is {entry.get('status') or where}, not finished",
            code="not_ready", param="token")

    name = str(filename)
    if not name.lower().endswith(MEDIA_SUFFIXES):
        return error_response(
            409,
            "That download is not media this page can play. A captions "
            "download is read with POST /ui/captions instead.",
            code="not_media", param="token")

    source = client.audio_url(name, str(entry.get("folder") or ""))
    # UP UNTOUCHED. Range because that is the whole point, and If-Range because
    # a conditional range without it is not conditional: a player that holds a
    # stale ETag would be handed a slice of a DIFFERENT file and would splice
    # the two together with no error anywhere. These two by name and nothing
    # else (D65): MeTube has no authentication, and is told nothing about who
    # is asking.
    forwarded = identity.outbound_headers(range=request.headers.get("range"),
                                          if_range=request.headers.get("if-range"))

    http: httpx.AsyncClient = request.app.state.client
    try:
        upstream = await http.send(
            http.build_request("GET", source, headers=forwarded,
                               # No overall ceiling: a player holds a range
                               # open for as long as it is buffering, and a
                               # read timeout here would cut a paused video
                               # off mid-buffer.
                               timeout=httpx.Timeout(None, connect=5.0)),
            stream=True)
    except httpx.RequestError as exc:
        return error_response(
            502, f"could not read the downloaded media ({type(exc).__name__})",
            type_="server_error", code="ingestion_unavailable")

    if upstream.status_code >= 400:
        # A 416 is MeTube's honest answer to a range past the end and belongs
        # to the browser, which retries correctly; it is not our failure and
        # must not be dressed up as one. Anything else from a file MeTube
        # itself reported is an outage on that side.
        status = upstream.status_code
        await upstream.aclose()
        if status == 416:
            return Response(status_code=416,
                            headers={"accept-ranges": "bytes"})
        return error_response(
            502, f"MeTube answered {status} for the media file it reported",
            type_="server_error", code="ingestion_unavailable")

    total = _total_bytes(upstream)
    if total is not None and total > config.MAX_MEDIA_BYTES:
        await upstream.aclose()
        return error_response(
            413,
            f"that download is {total / 1024**3:.1f} GB and playback here is "
            f"capped at {config.MAX_MEDIA_BYTES / 1024**3:.1f} GB. The "
            f"transcript is unaffected.",
            code="media_too_large", param="token")

    async def relay() -> AsyncIterator[bytes]:
        try:
            # aiter_raw, so content-length and content-encoding stay true of
            # the bytes actually sent. Same rule as the proxy in app/main.py.
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    headers = {name: value for name in RELAYED
               if (value := upstream.headers.get(name)) is not None}
    # Belt and braces: MeTube sends this, but a 206 whose Accept-Ranges went
    # missing on some future hop would leave the scrub bar dead, and asserting
    # it costs one header.
    headers.setdefault("accept-ranges", "bytes")
    return StreamingResponse(relay(), status_code=upstream.status_code,
                             headers=headers)


def _json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()
