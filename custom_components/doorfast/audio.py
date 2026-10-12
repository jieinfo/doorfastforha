"""Bounded v1 station audio payload decoding."""

from __future__ import annotations

import struct
from typing import Any

from .client_types import AudioStreamError


_MAX_PCM_BYTES = 3200


def _invalid() -> AudioStreamError:
    return AudioStreamError("audio_invalid_response")


def decode_pcm_wav(body: Any) -> bytes:
    """Decode one strict PCM WAV response and return its bounded PCM bytes."""
    if not isinstance(body, (bytes, bytearray)):
        raise _invalid()
    raw = bytes(body)
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise _invalid()
    (riff_size,) = struct.unpack_from("<I", raw, 4)
    if riff_size != len(raw) - 8:
        raise _invalid()

    fmt: tuple[int, int, int, int, int, int] | None = None
    pcm: bytes | None = None
    offset = 12
    while offset < len(raw):
        if len(raw) - offset < 8:
            raise _invalid()
        chunk_id = raw[offset : offset + 4]
        (chunk_size,) = struct.unpack_from("<I", raw, offset + 4)
        data_start = offset + 8
        data_end = data_start + chunk_size
        padded_end = data_end + (chunk_size & 1)
        if data_end > len(raw) or padded_end > len(raw):
            raise _invalid()
        chunk = raw[data_start:data_end]
        if chunk_id == b"fmt ":
            if fmt is not None or chunk_size != 16:
                raise _invalid()
            fmt = struct.unpack("<HHIIHH", chunk)
        elif chunk_id == b"data":
            if pcm is not None or chunk_size > _MAX_PCM_BYTES:
                raise _invalid()
            pcm = chunk
        offset = padded_end

    if offset != len(raw) or fmt is None or pcm is None or not pcm or len(pcm) % 2:
        raise _invalid()
    audio_format, channels, sample_rate, byte_rate, block_align, bits = fmt
    if (
        audio_format != 1
        or channels != 1
        or sample_rate != 8000
        or byte_rate != 16000
        or block_align != 2
        or bits != 16
    ):
        raise _invalid()
    return pcm
