"""A stand-in for app/fetcher.py, for the unit tests and the browser tests.

    python -I fake_fetcher.py probe -- URL
    python -I fake_fetcher.py fetch KIND CAP LANG -- URL

It takes the child's argv and speaks the child's protocol, one JSON object a
line, and it never opens a socket: stdlib only, no yt_dlp. A word in the URL
chooses what it does, so every branch of app/downloads.py and of the confirm
card can be reached without the network.

    probe   (none)       180 s, 1,440,000 bytes, no subtitles, no single file
            long         7200 s, 57,600,000 bytes
            subs         human subtitles, in English
            video        a single file with picture and sound
            unprobed     sleeps 30 s, so the parent's time limit ends it
            live         the child's live error
            playlist     the child's playlist error
            private      a refusal, as the guard words one
            unsupported  an extractor's failure
            flood        one line of 70,000 characters
            infinity     a duration of Infinity and a size of 1e400
            nested       60,000 [ on one line first, then the facts

    fetch   every run first appends its argv, environment, cwd, pid and
            whether it runs isolated (-I) to ../../fetches.log, which is the
            cache directory's fetches.log; sweep() never touches that name.
            (none)       three progress lines a second apart, then media.wav, a
                         12 s 16 kHz mono tone; audio and clip alike
            instant      one progress line and no wait (the unit tests)
            captions     the kind, not a word: media.en.vtt, three cues
            video        the kind: the same WAV bytes as media.mp4, video true
            webm         media.webm, video false: the parent names it .weba
            big          a sparse media.wav of 129 MiB, over ITEM_BYTES
            broken       a 403 after the progress, exit 1
            huge         the child's too_big error, exit 1
            twofiles, symlink, badsuffix, empty
                         two files; media.wav a symlink to /etc/hosts;
                         media.exe; a 0-byte media.wav. Each still says ok
            hang         sleeps an hour
            stall        one progress line, then sleeps an hour
            flood        one line of 70,000 characters
            silent       exits 0 and prints nothing
            junk         prints `not json` first, then runs as normal
            infinity     a progress line of Infinity, NaN and a 400-digit
                         number after the first, then runs as normal
            nested       60,000 [ on one line after the first progress line,
                         then runs as normal

THE REAL yt-dlp MUST NEVER RUN UNDER THE BROWSER TESTS. launch.py walls in the
page server's own sockets, not a child's, so services/ui/e2e/stack.py refuses
to start the page server without UI_FETCHER pointing here.
"""

from __future__ import annotations

import io
import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path


def say(**message: object) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def line(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


NESTED = "[" * 60_000     # under LINE_LIMIT, and a RecursionError to json.loads


def tone(seconds: float = 12.0, rate: int = 16000) -> bytes:
    frames = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 220 * n / rate)))
                      for n in range(int(seconds * rate)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    return buffer.getvalue()


VTT = ("WEBVTT\n\n"
       "00:00:00.000 --> 00:00:04.000\nThe first line.\n\n"
       "00:00:04.000 --> 00:00:08.000\nThe second line.\n\n"
       "00:00:08.000 --> 00:00:12.000\nAnd the third.\n")


def probe(url: str) -> int:
    if "unprobed" in url:
        time.sleep(30)
        return 1
    if "flood" in url:
        sys.stdout.write("x" * 70_000 + "\n")
        sys.stdout.flush()
        return 1
    if "nested" in url:
        line(NESTED)
    if "infinity" in url:
        say(facts={"title": "Probed talk", "duration": float("inf"), "bytes": 1e400})
        return 0
    if "live" in url:
        say(error="This is a live or upcoming stream.", code="live")
        return 1
    if "playlist" in url:
        say(error="That link is a playlist or a channel.", code="playlist")
        return 1
    if "private" in url:
        say(error="refusing to fetch: 10.0.0.5 is a private address", code="refused")
        return 1
    if "unsupported" in url:
        say(error=f"Unsupported URL: {url}", code="failed")
        return 1
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    long = "long" in url
    say(facts={"title": f"Probed talk {slug}", "uploader": "Example Channel",
               "duration": 7200.0 if long else 180.0,
               "bytes": 57_600_000 if long else 1_440_000,
               "has_subtitles": "subs" in url,
               "subtitles_lang": "en" if "subs" in url else None,
               "video": "video" in url})
    return 0


def fetch(kind: str, url: str) -> int:
    with open(Path("..", "..", "fetches.log"), "a") as log:
        log.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ),
                              "cwd": os.getcwd(), "pid": os.getpid(),
                              "isolated": bool(sys.flags.isolated)}) + "\n")
    if "hang" in url:
        time.sleep(3600)
    if "flood" in url:
        sys.stdout.write("x" * 70_000 + "\n")
        sys.stdout.flush()
    if "silent" in url:
        return 0
    if "junk" in url:
        sys.stdout.write("not json\n")
    steps = 1 if "instant" in url else 3
    for step in range(1, steps + 1):
        say(done=step * 128_000, total=steps * 128_000, speed=128_000.0,
            eta=float(steps - step))
        if "stall" in url:
            time.sleep(3600)
        if step == 1 and "infinity" in url:
            line('{"done": 1e400, "total": Infinity, "speed": NaN, "eta": -Infinity}')
            line('{"done": ' + "9" * 400 + ', "total": ' + "9" * 400 + '}')
        if step == 1 and "nested" in url:
            line(NESTED)
        if steps > 1:
            time.sleep(1)
    if "broken" in url:
        say(error="[generic] Unable to download webpage: HTTP Error 403: Forbidden",
            code="failed")
        return 1
    if "huge" in url:
        say(error="The download is over the 500 MiB limit for a link.", code="too_big")
        return 1

    video = None
    if kind == "captions":
        Path("media.en.vtt").write_text(VTT)
    elif "twofiles" in url:
        Path("media.wav").write_bytes(tone())
        Path("media.info.json").write_text("{}")
    elif "symlink" in url:
        os.symlink("/etc/hosts", "media.wav")
    elif "badsuffix" in url:
        Path("media.exe").write_bytes(b"MZ")
    elif "empty" in url:
        Path("media.wav").write_bytes(b"")
    elif "big" in url:
        with open("media.wav", "wb") as out:
            out.write(tone(0.1))
            out.truncate(129 * 2**20)
        video = False
    elif kind == "video":
        Path("media.mp4").write_bytes(tone())
        video = True
    elif "webm" in url:
        Path("media.webm").write_bytes(tone())
        video = False
    else:
        Path("media.wav").write_bytes(tone())
        video = False
    say(ok=True, video=video)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[:2] == ["probe", "--"]:
        return probe(argv[2])
    if len(argv) == 6 and argv[0] == "fetch" and argv[4] == "--":
        return fetch(argv[1], argv[5])
    say(error="bad arguments", code="failed")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
