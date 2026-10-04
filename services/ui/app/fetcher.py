"""yt-dlp in a child process: what a pasted link is, or its one file, and nothing else.

    python -I app/fetcher.py probe -- URL
    python -I app/fetcher.py fetch KIND CAP LANG -- URL

THE ONLY PROCESS THAT IMPORTS yt_dlp. app/downloads.py spawns it and reads one
JSON object per line from its stdout. The server never imports the library,
so nothing yt-dlp does with a hostile page runs in the process that holds the
routes, and a crash, a hang or a gzip bomb costs one child.

BEFORE yt_dlp IS IMPORTED, in this order:

  1. argv is checked strictly: the mode, the kind, CAP an integer, LANG a
     language tag, then `--` and exactly one URL. Anything else is an error
     line and exit 2.
  2. The guard is installed. app/guard.py's rules, applied to every answer
     socket.getaddrinfo gives and to the peer and port of every connect,
     connect_ex and sendto. That covers redirects, URLs found inside a page,
     DASH fragments and DNS rebinding, for the probe and the download alike.
     TLS verification stays on.
  3. Memory: RLIMIT_DATA at CHILD_MEMORY, and oom_score_adj 1000, so a body
     read whole (yt-dlp decompresses gzip in memory) ends in MemoryError, and
     under cgroup pressure the kernel kills a child before the server.
  4. Disk: RLIMIT_FSIZE at CAP (0 for a probe, which writes nothing), with
     SIGXFSZ ignored, so a write past it fails with EFBIG instead of killing
     the process without a word.

NO ffmpeg, SO NOTHING IS MERGED, CONVERTED OR TRIMMED. Every format below is
one native file over http, https or http_dash_segments. HLS is never chosen:
without ffmpeg, m3u8_native leaves MPEG-TS in an .mp4 that no browser plays.
A site that offers only HLS fails at the probe, with a message that says so.

THE GUARD IS NOT A SANDBOX. Code running in this process can undo a patch on
its own socket module. What it stops is honest code being steered somewhere
private; what limits a compromised yt-dlp is the deployment's egress rule
(services/ui/README.md).
"""

from __future__ import annotations

import json
import os
import re
import resource
import signal
import socket
import sys
import time
from pathlib import Path

KINDS = ("audio", "clip", "video", "captions")
LANGUAGE = re.compile(r"[A-Za-z0-9_-]{1,32}")
MAX_CAP = 2**31
CHILD_MEMORY = 256 * 2**20

PLAYLIST = "That link is a playlist or a channel. Paste the link of one video."
LIVE = ("This is a live or upcoming stream. Recording one needs ffmpeg, which "
        "this server does not carry. Try again once it has ended.")
NO_FFMPEG = "This site offers no stream this server can fetch without ffmpeg."
NOTHING = ("Nothing was downloaded: the file is over the size limit, or the "
           "link is a live or upcoming stream.")

REFUSED: list[str] = []   # the first refusal decides the error code
TOO_BIG: list[int] = []   # set by the progress hook
LAST = [0.0]              # when the last progress line was printed

FORMATS = {
    # Opus at or under 96 kbit/s is YouTube's itag 250, about 0.5 MB a minute
    # and plenty for 16 kHz recognition. Then any audio-only stream. Then, for
    # a site that only sends picture and sound together, the best such file at
    # 480p or less, else the smallest one, because only its sound is wanted.
    "audio": ("ba[acodec=opus][abr<=?96][protocol^=http]/ba[protocol^=http]"
              "/b[height<=?480][protocol^=http]/w[protocol^=http]"),
    # AAC first for a voice clip, because every browser's decodeAudioData
    # reads it. About 10 MB at the 10-minute ceiling.
    "clip": ("ba[ext=m4a][protocol^=http]/ba[protocol^=http]"
             "/b[height<=?480][protocol^=http]/w[protocol^=http]"),
    # One file with picture and sound, 720p or less preferred. YouTube offers
    # none without a JS runtime; X, TikTok, Facebook and direct .mp4 links do.
    "video": ("b[height<=?720][ext=mp4][protocol^=http]"
              "/b[height<=?720][protocol^=http]/b[protocol^=http]"),
}

# NEVER SET, and test_fetcher.py snapshots these dicts so none can arrive
# unnoticed: max_downloads (extract_info raises after the file is written and
# returns nothing), enable_file_urls, cookiefile, cookiesfrombrowser, usenetrc,
# netrc_cmd, external_downloader, impersonate, remote_components (it lets deno
# fetch packages over its own network stack), wait_for_video, live_from_start,
# load_info_filename, compat_opts, and any postprocessor. Library mode reads no
# config file, and YTDLP_NO_PLUGINS stops plugins loading.
BASE = {
    "quiet": True, "no_warnings": True, "noprogress": True,
    "noplaylist": True, "extract_flat": "in_playlist",
    "allowed_extractors": ["default"],
    "postprocessors": [],
    "cachedir": False, "updatetime": False, "proxy": "",
    "socket_timeout": 10, "retries": 3, "fragment_retries": 3,
    "extractor_retries": 1, "concurrent_fragment_downloads": 1,
}

PROBE = {**BASE, "simulate": True, "format": FORMATS["audio"]}


class Usage(ValueError):
    """argv is not one this script takes."""


def say(**message: object) -> None:
    """One JSON object on one line of stdout, flushed: the whole protocol."""
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def parse(argv: list[str]) -> tuple[str, str, int, str, str]:
    """(mode, kind, cap, lang, url), or Usage. A probe is kind "audio", cap 0."""
    if len(argv) == 3 and argv[0] == "probe" and argv[1] == "--" and argv[2]:
        return "probe", "audio", 0, "-", argv[2]
    if (len(argv) == 6 and argv[0] == "fetch" and argv[1] in KINDS
            and re.fullmatch(r"[0-9]{1,10}", argv[2]) and int(argv[2]) <= MAX_CAP
            and (argv[3] == "-" or LANGUAGE.fullmatch(argv[3]))
            and argv[4] == "--" and argv[5]):
        return "fetch", argv[1], int(argv[2]), argv[3], argv[5]
    raise Usage(argv)


def install_guard(forbidden, ports) -> None:  # noqa: ANN001
    """app/guard.py's rules at every resolution and every connection in this process."""
    real_getaddrinfo = socket.getaddrinfo

    def refuse(why: str):  # noqa: ANN202
        REFUSED.append(why)
        raise ConnectionRefusedError(f"refused: {why}")

    def getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        infos = real_getaddrinfo(host, port, *args, **kwargs)
        for family, _type, _proto, _name, address in infos:
            if family not in (socket.AF_INET, socket.AF_INET6):
                refuse(f"{host} resolves to a non-internet address")
            if why := forbidden(address[0]):
                refuse(f"{host}: {why}")
        return infos

    def checked(real):  # noqa: ANN001, ANN202
        def call(self, *args):  # noqa: ANN001, ANN002, ANN202
            address = args[-1]
            if self.family not in (socket.AF_INET, socket.AF_INET6):
                refuse("not an internet socket")
            if why := forbidden(str(address[0]).split("%", 1)[0]):
                refuse(why)
            if address[1] not in ports:
                refuse(f"port {address[1]} is not 80 or 443")
            return real(self, *args)
        return call

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = checked(socket.socket.connect)
    socket.socket.connect_ex = checked(socket.socket.connect_ex)
    socket.socket.sendto = checked(socket.socket.sendto)


def _limit(which: int, value: int) -> None:
    hard = resource.getrlimit(which)[1]
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    resource.setrlimit(which, (value, value))


def prepare(cap: int) -> None:
    """Everything that must hold before `import yt_dlp`, in the order the docstring gives."""
    # /srv in the image, owned by root. -I left the script's own directory off
    # sys.path, along with every PYTHON* variable and the user site.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import guard  # noqa: PLC0415 - stdlib only, and only once the path is set

    install_guard(guard._forbidden, guard.ALLOWED_PORTS)
    linux = sys.platform.startswith("linux")
    try:
        _limit(resource.RLIMIT_DATA, CHILD_MEMORY)
    except (ValueError, OSError):
        # macOS refuses to lower it below what the process already maps, and
        # does not enforce it anyway. The image is Linux, where it must hold.
        if linux:
            raise
    try:
        # Raising the score needs no privilege. Absent off Linux.
        with open("/proc/self/oom_score_adj", "w") as score:
            score.write("1000")
    except OSError:
        if linux:
            raise
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    _limit(resource.RLIMIT_FSIZE, cap)
    os.environ["YTDLP_NO_PLUGINS"] = "1"


def single_file(f: dict) -> bool:
    """A format with picture and sound in one native file."""
    return (f.get("vcodec") != "none" and f.get("acodec") != "none"
            and str(f.get("protocol") or "https").startswith("http"))


def facts(info: dict) -> dict:
    """The few values the confirm card needs, from the info-dict, and nothing else."""
    duration = info.get("duration")
    duration = float(duration) if isinstance(duration, (int, float)) else None
    size = info.get("filesize") or info.get("filesize_approx")   # the SELECTED format
    rate = info.get("abr") or info.get("tbr")
    if not (isinstance(size, (int, float)) and size > 0):
        size = (duration * rate * 125 if duration and isinstance(rate, (int, float))
                else duration / 60 * 1024**2 if duration else None)
    subs = {k: v for k, v in (info.get("subtitles") or {}).items() if v and k != "live_chat"}
    lang = info.get("language") if info.get("language") in subs else next(
        (k for k in subs if k.split("-")[0] == "en"), next(iter(subs), None))
    formats = [f for f in (info.get("formats") or [info]) if isinstance(f, dict)]
    return {
        "title": str(info.get("title") or "")[:300] or None,
        "uploader": str(info.get("uploader") or info.get("channel") or "")[:200] or None,
        "duration": duration,
        "bytes": int(size) if size else None,
        "has_subtitles": bool(subs),
        "subtitles_lang": lang if lang and LANGUAGE.fullmatch(lang) else None,
        "video": any(single_file(f) for f in formats),
    }


def _refusal(info: dict) -> dict | None:
    if info.get("_type") in ("playlist", "multi_video"):
        return {"error": PLAYLIST, "code": "playlist"}
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return {"error": LIVE, "code": "live"}
    return None


def probe(url: str) -> int:
    from yt_dlp import YoutubeDL  # noqa: PLC0415

    with YoutubeDL(dict(PROBE)) as ydl:
        # In two steps rather than extract_info's one, so a live stream is
        # named as one before format selection fails on its HLS-only formats.
        info = ydl.extract_info(url, download=False, process=False) or {}
        if (refused := _refusal(info)) is None:
            info = ydl.process_ie_result(info, download=False) or {}
            refused = _refusal(info)
    if refused is not None:
        say(**refused)
        return 1
    say(facts=facts(info))
    return 0


def progress_hook(cap: int):  # noqa: ANN201
    from yt_dlp.utils import DownloadError  # noqa: PLC0415

    def hook(progress: dict) -> None:
        done = int(progress.get("downloaded_bytes") or 0)
        if done > cap:                      # gzip bodies and DASH fragments
            TOO_BIG.append(done)            # are not caught by max_filesize
            raise DownloadError(f"over the {cap // 2**20} MiB limit for a link")
        if progress.get("status") == "downloading" and time.monotonic() - LAST[0] >= 1.0:
            LAST[0] = time.monotonic()
            say(done=done,
                total=progress.get("total_bytes") or progress.get("total_bytes_estimate"),
                speed=progress.get("speed"), eta=progress.get("eta"))
    return hook


def fetch_params(kind: str, cap: int, lang: str) -> dict:
    from yt_dlp.utils import match_filter_func  # noqa: PLC0415

    params = {**BASE,
              "paths": {"home": ".", "temp": "."},
              "outtmpl": {"default": "media.%(ext)s"},
              "restrictfilenames": True,
              "max_filesize": cap,
              "match_filter": match_filter_func("!is_live & live_status!=?is_upcoming"),
              "progress_hooks": [progress_hook(cap)]}
    if kind == "captions":
        params.update(skip_download=True, writesubtitles=True, writeautomaticsub=False,
                      subtitleslangs=[re.escape(lang if lang != "-" else "en")],
                      subtitlesformat="vtt/srt")
    else:
        params["format"] = FORMATS[kind]
    return params


def fetch(kind: str, cap: int, lang: str, url: str) -> int:
    """Download into the working directory, which the parent made for this run."""
    from yt_dlp import YoutubeDL  # noqa: PLC0415

    with YoutubeDL(fetch_params(kind, cap, lang)) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    if TOO_BIG:
        say(**failure(None, cap))
        return 1
    if not any(Path(".").glob("media.*")):
        say(error=NOTHING, code="failed")
        return 1
    # The selected format's fields: a direct WAV link gives vcodec "none".
    vcodec = info.get("vcodec")
    say(ok=True, video=None if vcodec is None else vcodec != "none")
    return 0


def failure(exc: BaseException | None, cap: int = 0) -> dict:
    """The error line for whatever stopped the run."""
    if REFUSED:
        return {"error": f"refusing to fetch: {REFUSED[0]}", "code": "refused"}
    if TOO_BIG:
        return {"error": f"The download is over the {cap // 2**20} MiB limit for a link.",
                "code": "too_big"}
    text = (str(exc) if exc is not None else "").strip().removeprefix("ERROR: ")
    text = (text.splitlines() or [""])[0][:300] or type(exc).__name__
    if "Requested format is not available" in text:
        return {"error": NO_FFMPEG, "code": "failed"}
    return {"error": text, "code": "failed"}


def main(argv: list[str]) -> int:
    try:
        mode, kind, cap, lang, url = parse(argv)
    except Usage:
        say(error="bad arguments", code="failed")
        return 2
    try:
        prepare(cap)
        import yt_dlp  # noqa: F401, PLC0415 - only now, with every limit in place
        return probe(url) if mode == "probe" else fetch(kind, cap, lang, url)
    except BaseException as exc:  # noqa: BLE001 - MemoryError included: one line, always
        say(**failure(exc, cap))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
