"""The little signal work the hub does itself: WAV framing, a test tone, and
bringing Kokoro's 24 kHz up to the speaker's 48 kHz. Media from Home Assistant
is only read here, never decoded or resampled: it arrives as the PCM the
satellite plays (main.py, POST /satellites/{id}/media)."""

from __future__ import annotations

import io
import struct
import wave
from collections.abc import AsyncIterable, AsyncIterator

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


def wav_format(head: bytes) -> tuple[int, int, int] | None:
    """(rate, channels, where the audio starts) from the start of a WAV, or
    None when more of it is needed to tell. For a WAV that is still arriving
    (POST /satellites/{id}/media), so it reads only the header: the sizes of
    a WAV written to a pipe say nothing (see pcm_from_wav), and any chunk
    before the audio (ffmpeg writes a LIST of tags) is stepped over.
    ValueError when it is not a RIFF WAVE of 16-bit PCM."""
    if head[:4] != b"RIFF"[:len(head)] or (len(head) >= 12 and head[8:12] != b"WAVE"):
        raise ValueError("not a RIFF WAVE file")
    pos, fmt = 12, None
    while pos + 8 <= len(head):
        kind, size = head[pos:pos + 4], struct.unpack_from("<I", head, pos + 4)[0]
        body = pos + 8
        if kind == b"fmt ":
            if size < 16:
                raise ValueError("a fmt chunk too short to read")
            if body + 16 > len(head):
                return None
            tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", head, body)
            # 0xFFFE is WAVE_FORMAT_EXTENSIBLE, which ffmpeg writes for PCM too.
            if tag not in (1, 0xFFFE) or bits != 16 or channels < 1 or rate < 1:
                raise ValueError(f"format {tag}, {bits}-bit, {channels} channel(s): "
                                 "only 16-bit PCM is taken")
            fmt = (rate, channels)
        elif kind == b"data":
            if fmt is None:
                raise ValueError("audio before its format")
            return fmt[0], fmt[1], body
        elif size == 0xFFFFFFFF:
            raise ValueError("a chunk of no stated size before the audio")
        pos = body + size + (size & 1)  # chunks are padded to an even length
    return None


async def wav_stream(chunks: AsyncIterable[bytes], head_max: int = 64 * 1024,
                     ) -> tuple[tuple[int, int], AsyncIterator[bytes]]:
    """((rate, channels), the PCM) of a WAV arriving in `chunks`, read no
    further than its header before it answers. The PCM is not bounded here:
    a WAV from a pipe says 0 or 0xFFFFFFFF bytes of audio and means "until
    it ends", and a live stream has no end. ValueError for what wav_format
    refuses, and for a header that does not end within `head_max` bytes."""
    source = aiter(chunks)
    head = b""
    while (found := wav_format(head)) is None:
        if len(head) >= head_max:
            raise ValueError(f"no audio within its first {head_max} bytes")
        try:
            head += await anext(source)
        except StopAsyncIteration:
            raise ValueError("it ends before its audio") from None
    rate, channels, start = found
    if start > head_max:
        raise ValueError(f"no audio within its first {head_max} bytes")

    async def pcm() -> AsyncIterator[bytes]:
        if len(head) > start:
            yield head[start:]
        async for chunk in source:
            yield chunk
    return (rate, channels), pcm()


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
