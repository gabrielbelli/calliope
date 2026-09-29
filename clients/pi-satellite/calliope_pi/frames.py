"""The binary frames of the satellite protocol.

  1 mic      satellite -> hub   kind, 0, channels, 0, seq u32, capture time us u64, then s16le
  2 speaker  hub -> satellite   the same 16-byte header, then mono s16le at the speaker rate
  3 firmware hub -> satellite   kind, 0, 0, 0, offset u32, then up to 8 KB of the update
  4 earcon   hub -> satellite   kind, 0, 0, 0, offset u32, then up to 8 KB of s16le at 48 kHz
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MIC, SPEAKER, FIRMWARE, EARCON = 1, 2, 3, 4
AUDIO = struct.Struct("<BBBBIQ")   # 16 bytes
CHUNK = struct.Struct("<BBBBI")    # 8 bytes


@dataclass(frozen=True)
class Frame:
    kind: int
    payload: bytes
    offset: int | None = None      # firmware and earcon
    seq: int | None = None         # audio


def mic(seq: int, channels: int, capture_us: int, pcm: bytes) -> bytes:
    return AUDIO.pack(MIC, 0, channels, 0, seq & 0xFFFFFFFF, capture_us) + pcm


def parse(data: bytes) -> Frame | None:
    """A frame from the hub, or None for one too short or of a kind unknown."""
    if not data:
        return None
    kind = data[0]
    if kind == SPEAKER and len(data) >= AUDIO.size:
        _, _, _, _, seq, _ = AUDIO.unpack_from(data)
        return Frame(kind, data[AUDIO.size:], seq=seq)
    if kind in (FIRMWARE, EARCON) and len(data) >= CHUNK.size:
        offset = CHUNK.unpack_from(data)[4]
        return Frame(kind, data[CHUNK.size:], offset=offset)
    return None
