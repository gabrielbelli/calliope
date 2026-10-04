"""Every knob this service has, read once at import.

The process is the unit of configuration here, exactly as it is in the four
siblings: a container is restarted to change a setting, so reading the
environment at import means `docker inspect` and the startup log agree with
what the code will do for the whole life of the process.

EVERYTHING BELOW IS OPTIONAL AND EVERY DEFAULT DEGRADES RATHER THAN FAILS.
That is the stance for this service specifically: it is a convenience in front
of a stack that already works without it, and the worst outcome would be a UI
that refuses to start — or worse, starts and hangs — because the link cache is
not writable or yt-dlp is missing. Uploads and TTS must never depend on
ingestion being available.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from voice_common.identity import GATEWAY_INTERNAL

__all__ = [
    "GATEWAY_INTERNAL_URL", "IGNORED_INTERNAL_URL",
    "LINKS", "CACHE_DIR", "MAX_DOWNLOAD_BYTES", "CACHE_BYTES",
    "CLIP_SOURCE_SECONDS", "CLIP_SOURCE_BYTES", "FETCHER",
    "PROBE_TIMEOUT", "MAX_UPLOAD_BYTES", "MAX_CAPTION_BYTES",
    "CONFIRM_SECONDS", "CONFIRM_BYTES", "STT_RTF_SEED", "STT_BUDGET_SECONDS",
    "VOICE_DIR", "MAX_CLIP_BYTES", "MAX_CLIP_SECONDS", "RESOLVE_PER_MINUTE",
    "flag",
]


def flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _internal_url(raw: str | None) -> tuple[str, bool]:
    """Where /ui/fetch sends a download to be transcribed, and whether `raw` was refused.

    That request carries this service's own key (D6, D64), so the address is
    the gateway's internal listener and nothing a setting can point anywhere
    else. The one exception is an address on THIS machine's loopback, which is
    how the browser harness runs the whole stack on 127.0.0.1 without a
    `voice-gateway` name to resolve: the key still never leaves the box.

    Anything else falls back to the constant rather than switching link
    transcription off, because the constant is the only place the key may go
    and production works with it. The lifespan logs the refusal by name.
    """
    value = (raw or "").strip().rstrip("/")
    if not value or value == GATEWAY_INTERNAL:
        return GATEWAY_INTERNAL, False
    parts = urlsplit(value)
    try:
        loopback = ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (parts.scheme == "http" and loopback and not parts.username
            and not parts.password and not parts.path and not parts.query):
        return value, False
    return GATEWAY_INTERNAL, True


# THE ONLY ADDRESS THIS SERVICE SENDS A CREDENTIAL TO: the gateway's internal
# listener, plain HTTP inside the compose network and never published. The page
# calls the gateway itself with its session cookie; this process calls it once,
# from /ui/fetch, with its service key and the user's delegation token.
GATEWAY_INTERNAL_URL, IGNORED_INTERNAL_URL = _internal_url(
    os.getenv("UI_GATEWAY_INTERNAL_URL"))

# LINKS, on unless UI_LINKS=0, which hides the link box and leaves file
# upload. Pasted links are fetched by app/fetcher.py, a yt-dlp child of this
# process (see app/downloads.py); nothing outside this container takes part.
LINKS = flag("UI_LINKS", True)

# Where finished downloads are kept and downloads in progress are written: a
# named volume in compose. Losing it costs re-downloads and nothing else. Not
# writable means links are off, as UI_LINKS=0 would, and the log says so.
CACHE_DIR = Path(os.getenv("UI_CACHE_DIR", "/cache"))

# 500 MiB, the most one audio, clip or video download may write, enforced in
# the child three ways (RLIMIT_FSIZE, its progress hook and max_filesize). Below
# the gateway's 512 MiB transcription cap, which leaves room for the multipart
# framing /ui/fetch adds; keep it at or below GATEWAY_UPLOAD_MAX_BYTES.
MAX_DOWNLOAD_BYTES = int(os.getenv("UI_MAX_DOWNLOAD_BYTES", str(500 * 2**20)))

# 1 GiB, every cached file of 128 MiB or less together. A file bigger than
# this on its own is not cached either. 0 turns the cache off: a finished file
# is then kept an hour after its last use, for playback, and never reused. See
# app/downloads.py for the whole of the cache.
CACHE_BYTES = int(os.getenv("UI_CACHE_BYTES", str(2**30)))

# Ten minutes, the longest recording the clone sheet takes from a link. The
# browser holds the whole recording to cut the clip out of it, and AAC at ten
# minutes is about 10 MB.
CLIP_SOURCE_SECONDS = 600.0

# 64 MiB, the cap for that recording's download, because the ten minutes are
# the site's word and the browser decodes every byte that arrives. Six times
# ten minutes of AAC, and a short video where a site sends picture and sound
# only together. Never above MAX_DOWNLOAD_BYTES.
CLIP_SOURCE_BYTES = 64 * 2**20

# Tests only: a script run as `python -I <path> ...` in place of
# app/fetcher.py. The browser harness sets it so the real yt-dlp never runs
# outside its network wall, and the lifespan logs a warning when it is set.
FETCHER = os.getenv("UI_FETCHER") or None

# The probe child's time limit, the wait for a slot included. Past it the
# confirm card shows a title and no length or size, and the fetch still works.
PROBE_TIMEOUT = float(os.getenv("UI_PROBE_TIMEOUT", "20"))

# 2 GiB. services/stt/app/main.py:138 is a bare `file.file.read()` on an
# UploadFile -- no Content-Length check, no cap, no streaming -- so a 4 GB MKV
# is buffered whole into the stt container's 6 GB memory limit. The page's own
# uploads go to the gateway, which counts them against its own ceiling; this
# is the figure the page reads from /ui/config to decide when to extract the
# audio in the browser first.
MAX_UPLOAD_BYTES = int(os.getenv("UI_MAX_UPLOAD_BYTES", str(2 * 1024**3)))

# 8 MiB, the cap for a captions download, and it is a sanity bound rather than
# a real limit. A captions download is a subtitle track and nothing else --
# skip_download, so no media stream is fetched at all -- and an hour of dense
# dialogue is on the order of 100 KB of WebVTT. /ui/captions reads the whole
# file into memory to parse it, which is the right call for something that size
# and the wrong one for anything that is not, so the child is held to this
# many bytes the same three ways a media download is held to its own cap.
MAX_CAPTION_BYTES = int(os.getenv("UI_MAX_CAPTION_BYTES", str(8 * 1024**2)))

# WHEN TO NAG, and why these two numbers.
#
# Below BOTH thresholds the confirm dialog is skipped and the fetch starts. Ten
# minutes of audio is ~10 MB at opus and, at the conservative 8.5x figure the
# gateway's own timeout was built on, ~71 s of transcription -- an order of
# magnitude inside GATEWAY_STT_TIMEOUT (900 s) and well inside anyone's
# patience. A dialog there is pure friction, and a dialog that fires on
# everything is a dialog people learn to dismiss without reading, which is
# exactly how the three-hour stream gets through.
#
# The size threshold is the second gate rather than an alternative: a short
# video with an enormous audio stream is still a real download.
CONFIRM_SECONDS = float(os.getenv("UI_CONFIRM_SECONDS", "600"))
CONFIRM_BYTES = int(os.getenv("UI_CONFIRM_BYTES", str(50 * 1024**2)))

# THE NUMBER THIS REPOSITORY CONTRADICTS ITSELF ABOUT, seeded at the
# conservative end on purpose.
#
#   root README.md:95                 47-63x realtime
#   gateway/app/main.py:117-123       8.5-10.4x   <- the 900 s timeout and the
#                                                    504 help text are built
#                                                    on THIS one
#   stt/README.md:590                 ~5x on four cores
#
# A factor of twelve apart. The dialog that matters most -- "this is a 2h14m
# podcast" -- is wrong by 5x if the optimistic figure is quoted and the
# pessimistic one is true, and at 8.5x that file needs 946 s of compute, which
# EXCEEDS the gateway's own 900 s ceiling. So the seed is 8.5, the page
# labels it an estimate, the page keeps its own EMA from the realtime_factor
# the native /transcribe route returns on every real transcription, and the
# page warns when duration/rtf crosses the budget below. Someone must
# re-measure on the deployment host and correct main.py:121's help text in the same change,
# or the UI and the gateway's own 504 will disagree in front of one user.
STT_RTF_SEED = float(os.getenv("UI_STT_RTF", "8.5"))
STT_BUDGET_SECONDS = float(os.getenv("UI_STT_BUDGET", "900"))

# The reference-clip store. Shared with tts-long as a named volume: this
# service is the only WRITER, tts-long mounts it read-only and rescans when the
# directory changes. See app/clips.py and services/tts-long/app/voices.py.
VOICE_DIR = os.getenv("UI_VOICE_DIR", "/voices")

# Resemble's own guidance is 10-30 s of one speaker. 30 s of 24 kHz mono WAV is
# 1.4 MB; 25 MB is room for a phone recording that has not been transcoded yet
# and is still nowhere near a memory problem.
MAX_CLIP_BYTES = int(os.getenv("UI_MAX_CLIP_BYTES", str(25 * 1024**2)))
MAX_CLIP_SECONDS = float(os.getenv("UI_MAX_CLIP_SECONDS", "30"))

# /ui/resolve spawns a process that makes an outbound request on a URL a user
# chose. That is a scanning primitive if it is free, so it is not free. Counted
# per signed-in person (D36): a household behind one address is several
# allowances, and one person on two devices is one.
RESOLVE_PER_MINUTE = int(os.getenv("UI_RESOLVE_PER_MINUTE", "12"))
