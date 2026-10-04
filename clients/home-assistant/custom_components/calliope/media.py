"""Media for a satellite: resolved by Home Assistant, converted by its ffmpeg.

The hub never decodes or resamples. GET /satellites/{id} names the exact PCM
WAV the satellite that plays wants ("media": "music" for streams, "announce"
for announcements), and Home Assistant's own ffmpeg makes it from whatever
Home Assistant can read: a local file, its TTS, a radio stream. The WAV is
uploaded as ffmpeg writes it (POST /satellites/{id}/media), so music starts
at once and a live stream never has to end.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

from homeassistant.components import media_source
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The protocols ffmpeg may open for a URL: Home Assistant's own, web streams
# and HLS radio. Never `file`: anyone who may call play_media can type a URL,
# and a file: URL would read any file Home Assistant can.
WEB_PROTOCOLS = "http,https,tcp,tls,crypto,hls"
# ...and for a local file, which only media_source hands over (its TTS cache,
# My media), inside the folders it serves.
FILE_PROTOCOLS = "file"
# How much of ffmpeg's output is read, and so sent, at a time.
CHUNK = 64 * 1024
# ffmpeg's last lines of complaint, for the error that says why it failed.
STDERR_LINES = 5
# A URL's query in ffmpeg's complaint: Home Assistant signs its own media
# with ?authSig=, which must not reach an error dialog or the log.
QUERY = re.compile(r"\?[^\s'\"]*")
# How a stream or announcement ended, as the hub answers: these need no
# warning, the rest (speaker_off, muted, disconnected…) do.
QUIET_ENDS = frozenset({"ended", "stopped", "superseded", "cancelled"})


def ffmpeg_args(
    binary: str, sources: list[str | Path], rate: int, channels: int
) -> list[str]:
    """ffmpeg's command line for a PCM WAV at rate and channels on stdout.

    Two sources (a chime, then the message) are joined into one WAV: each is
    first brought to the same rate and layout, which concat needs. Each input
    has its own protocol whitelist, because ffmpeg applies an input option to
    the next input only: a Path (a file media_source resolved) may be opened
    as a file, a URL only over the web."""
    layout = "mono" if channels == 1 else "stereo"
    args = [binary, "-nostdin", "-hide_banner", "-loglevel", "error"]
    for source in sources:
        protocols = FILE_PROTOCOLS if isinstance(source, Path) else WEB_PROTOCOLS
        args += ["-protocol_whitelist", protocols, "-i", str(source)]
    if len(sources) == 2:
        fmt = f"aformat=sample_rates={rate}:channel_layouts={layout}"
        args += [
            "-filter_complex",
            f"[0:a]{fmt}[p];[1:a]{fmt}[m];[p][m]concat=n=2:v=0:a=1[a]",
            "-map",
            "[a]",
        ]
    return [
        *args,
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(rate),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:1",
    ]


async def resolve(hass: HomeAssistant, media_id: str, entity_id: str) -> str | Path:
    """What ffmpeg should open for a play_media id: a local file when
    media_source has one (its TTS cache, My media), else an absolute http or
    https URL, signed when it is Home Assistant's own. A URL of Home
    Assistant's needs its internal URL set (Settings, System, Network).
    Anything else (a file: URL, a bare file name) is refused: play_media is
    open to every user, and only media_source keeps to the folders it
    serves."""
    if media_source.is_media_source_id(media_id):
        play = await media_source.async_resolve_media(hass, media_id, entity_id)
        if play.path is not None:
            return play.path
        media_id = play.url
    url = async_process_play_media_url(hass, media_id)
    if urlsplit(url).scheme not in ("http", "https"):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="unsupported_url"
        )
    return url


async def wav_chunks(
    hass: HomeAssistant,
    sources: list[str | Path],
    rate: int,
    channels: int,
    *,
    satellite: str,
) -> AsyncIterator[bytes]:
    """ffmpeg's WAV, as it writes it. A failure raises play_failed with
    ffmpeg's own reason, naming the satellite; ffmpeg is killed however the
    reading ends (the hub stopped the stream, the task was cancelled)."""
    args = ffmpeg_args(get_ffmpeg_manager(hass).binary, sources, rate, channels)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        close_fds=False,  # as Home Assistant's other ffmpeg users: faster
    )
    complaint: deque[str] = deque(maxlen=STDERR_LINES)

    async def listen() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            text = line.decode("utf-8", "replace").strip()
            complaint.append(QUERY.sub("?…", text))

    listening = asyncio.create_task(listen())
    try:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(CHUNK):
            yield chunk
        await proc.wait()
        await listening
        if proc.returncode:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={
                    "satellite": satellite,
                    "error": " ".join(complaint) or f"ffmpeg exited {proc.returncode}",
                },
            )
    finally:
        listening.cancel()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


def log_end(name: str, result: dict | None) -> None:
    """Say why a stream or announcement ended early; an ordinary end, or one
    this integration asked for, is not news."""
    reason = (result or {}).get("reason")
    if reason not in QUIET_ENDS:
        _LOGGER.warning("Calliope stopped playing on %s: %s", name, reason)
