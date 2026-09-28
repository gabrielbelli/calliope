"""The little signal work the hub does itself: WAV framing, a test tone, and
bringing Kokoro's 24 kHz up to the speaker's 48 kHz."""

from __future__ import annotations

import io
import struct
import wave

import numpy as np


def wav(pcm: bytes, rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def pcm_from_wav(data: bytes) -> tuple[bytes, int]:
    """16-bit PCM and its rate from a WAV, mixed down to mono. Read by hand
    rather than with `wave`, because a WAV written to a pipe (ffmpeg's, which
    is how Home Assistant converts speech) cannot go back to fill in its
    sizes: its data chunk says 0 or 0xFFFFFFFF bytes, and means "to the end".
    ValueError for anything else."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF WAVE file")
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        kind, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if kind == b"fmt ":
            if size < 16 or body + 16 > len(data):
                raise ValueError("a fmt chunk too short to read")
            tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, body)
            fmt = (tag, channels, rate, bits)
        elif kind == b"data":
            if fmt is None:
                raise ValueError("audio before its format")
            tag, channels, rate, bits = fmt
            # 0xFFFE is WAVE_FORMAT_EXTENSIBLE, which ffmpeg writes for PCM too.
            if tag not in (1, 0xFFFE) or bits != 16 or channels < 1:
                raise ValueError(f"format {tag}, {bits}-bit, {channels} channel(s)")
            end = len(data) if size in (0, 0xFFFFFFFF) else min(len(data), body + size)
            pcm = data[body:end]
            pcm = pcm[:len(pcm) // (2 * channels) * 2 * channels]
            if channels > 1:
                x = np.frombuffer(pcm, "<i2").reshape(-1, channels).mean(axis=1)
                pcm = np.round(x).astype("<i2").tobytes()
            return pcm, rate
        if size in (0xFFFFFFFF,):
            break
        pos = body + size + (size & 1)  # chunks are padded to an even length
    raise ValueError("no audio in it")


def tone(freq: float, seconds: float, rate: int, level: float = 0.3) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    y = level * np.sin(2 * np.pi * freq * t)
    fade = min(len(y) // 2, int(0.01 * rate))  # 10 ms ramps, or it clicks
    if fade:
        ramp = np.linspace(0, 1, fade)
        y[:fade] *= ramp
        y[-fade:] *= ramp[::-1]
    return (y * 32767).astype("<i2").tobytes()


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    """Linear interpolation. Good enough for speech prompts; not for music."""
    if src == dst:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = int(len(x) * dst / src)
    y = np.interp(np.arange(n) * src / dst, np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()
