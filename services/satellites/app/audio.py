"""The little signal work the hub does itself: WAV framing, a test tone, and
bringing Kokoro's 24 kHz up to the speaker's 48 kHz."""

from __future__ import annotations

import io
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
