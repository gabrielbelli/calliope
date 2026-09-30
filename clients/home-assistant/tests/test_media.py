"""ffmpeg's command line (the PCM WAV the hub names, a chime joined to a
message, and what each input may open), and reading ffmpeg's output. No
ffmpeg runs and nothing plays: where ffmpeg would run, a small Python script
stands in for it."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.calliope.media import (
    FILE_PROTOCOLS,
    WEB_PROTOCOLS,
    ffmpeg_args,
    wav_chunks,
)

SIGNED = "http://192.0.2.10:8123/api/tts_proxy/abc.mp3?authSig=secret.token"


def _after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _whitelists(args: list[str]) -> dict[str, str]:
    """Each input, and the protocols it may open."""
    return {args[i + 1]: args[i - 1] for i, a in enumerate(args) if a == "-i"}


def _ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Home Assistant's ffmpeg is this Python script instead, whatever the
    arguments."""
    script = tmp_path / "ffmpeg"
    script.write_text(f"#!{sys.executable}\nimport os, sys, time\n{body}\n")
    script.chmod(0o755)
    monkeypatch.setattr(
        "custom_components.calliope.media.get_ffmpeg_manager",
        lambda hass: SimpleNamespace(binary=str(script)),
    )


def test_ffmpeg_args_convert_to_the_satellites_pcm_wav() -> None:
    """16-bit PCM WAV at the satellite's rate and channels on stdout, from a
    web stream or HLS radio, which may open nothing but the web."""
    args = ffmpeg_args("/usr/bin/ffmpeg", ["https://radio.example/live"], 44100, 2)
    assert args[0] == "/usr/bin/ffmpeg"
    assert "-nostdin" in args
    assert WEB_PROTOCOLS == "http,https,tcp,tls,crypto,hls"
    assert _whitelists(args) == {"https://radio.example/live": WEB_PROTOCOLS}
    assert _after(args, "-ac") == "2"
    assert _after(args, "-ar") == "44100"
    assert _after(args, "-c:a") == "pcm_s16le"
    assert _after(args, "-f") == "wav"
    assert args[-1] == "pipe:1"
    assert "-filter_complex" not in args
    assert "-vn" in args


def test_only_a_file_media_source_resolved_may_be_opened_as_a_file() -> None:
    """A Path comes only from media_source (My media, the TTS cache) and is
    read as a file; a URL, even a file: one, may not open a file."""
    args = ffmpeg_args(
        "ffmpeg", [Path("/media/song.flac"), "file:///config/secrets.yaml"], 48000, 1
    )
    assert FILE_PROTOCOLS == "file"
    assert _whitelists(args) == {
        "/media/song.flac": FILE_PROTOCOLS,
        "file:///config/secrets.yaml": WEB_PROTOCOLS,
    }


def test_ffmpeg_args_join_the_chime_and_the_message() -> None:
    """Two inputs, each allowed only the web, brought to one rate and layout
    and joined in order into one WAV."""
    chime = "http://192.0.2.10:8123/api/assist_satellite/static/preannounce.mp3"
    message = "http://192.0.2.10:8123/api/tts_proxy/abc.mp3"
    args = ffmpeg_args("ffmpeg", [chime, message], 48000, 1)
    inputs = [args[i + 1] for i, a in enumerate(args) if a == "-i"]
    assert inputs == [chime, message]
    assert args.count("-protocol_whitelist") == 2
    for i, a in enumerate(args):
        if a == "-i":
            assert args[i - 2 : i] == ["-protocol_whitelist", WEB_PROTOCOLS]
    fmt = "aformat=sample_rates=48000:channel_layouts=mono"
    assert _after(args, "-filter_complex") == (
        f"[0:a]{fmt}[p];[1:a]{fmt}[m];[p][m]concat=n=2:v=0:a=1[a]"
    )
    assert _after(args, "-map") == "[a]"
    assert _after(args, "-ac") == "1"
    assert _after(args, "-ar") == "48000"
    assert args[-1] == "pipe:1"


async def test_wav_chunks_are_what_ffmpeg_writes(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every byte, in order, however ffmpeg's writes are cut into chunks."""
    wav = tmp_path / "made.wav"
    wav.write_bytes(b"RIFF" + bytes(range(256)) * 1024)  # four reads' worth
    _ffmpeg(
        tmp_path,
        monkeypatch,
        f"sys.stdout.buffer.write(open({str(wav)!r}, 'rb').read())",
    )
    got = [c async for c in wav_chunks(hass, [SIGNED], 48000, 1, satellite="kitchen")]
    assert b"".join(got) == wav.read_bytes()


async def test_wav_chunks_say_why_ffmpeg_failed_without_the_signed_query(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg's own reason, naming the satellite; the ?authSig= Home
    Assistant signed its URL with stays out of the error and the log."""
    _ffmpeg(
        tmp_path,
        monkeypatch,
        f"sys.stderr.write({SIGNED + ': Server returned 404 Not Found'!r} + '\\n')\n"
        "sys.exit(1)",
    )
    with pytest.raises(HomeAssistantError) as err:
        async for _ in wav_chunks(hass, [SIGNED], 48000, 1, satellite="kitchen"):
            pass
    assert err.value.translation_key == "play_failed"
    placeholders = err.value.translation_placeholders
    assert placeholders["satellite"] == "kitchen"
    assert placeholders["error"] == (
        "http://192.0.2.10:8123/api/tts_proxy/abc.mp3?… Server returned 404 Not Found"
    )
    assert "secret" not in str(placeholders)


async def test_wav_chunks_kill_ffmpeg_when_the_reader_stops(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hub stopped the stream, or the task was cancelled: ffmpeg, still
    writing, is killed and reaped, not left running."""
    _ffmpeg(
        tmp_path,
        monkeypatch,
        "sys.stdout.write(str(os.getpid())); sys.stdout.flush(); time.sleep(60)",
    )
    chunks = wav_chunks(hass, [SIGNED], 48000, 1, satellite="kitchen")
    pid = int(await anext(chunks))
    await chunks.aclose()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
