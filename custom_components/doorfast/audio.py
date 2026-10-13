"""Bounded v1 station audio decoding and shared listener delivery."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
import struct
from typing import Any

from .client_types import AudioChunk, AudioIdentity, AudioReadReply, AudioStreamError


_MAX_PCM_BYTES = 3200


@dataclass(slots=True)
class _Listener:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cursor: int | None = None
    discontinuity: bool = False


class AudioListenerHub:
    """Share one serialized bridge cursor across at most four listeners.

    Bridge timestamps measure source lag; the injected monotonic clock measures
    time spent in this cache and the continuous invalid-result deadline.
    """

    def __init__(
        self,
        client: Any,
        identity: AudioIdentity,
        clock: Callable[[], float],
        *,
        fetch_lock: asyncio.Lock | None = None,
    ):
        self._client = client
        self._identity = identity
        self._clock = clock
        self._listeners: dict[str, _Listener] = {}
        self._cache: deque[tuple[AudioChunk, float]] = deque(maxlen=4)
        self._fetch_lock = fetch_lock or asyncio.Lock()
        self._epoch = 0
        self._closed = False
        self._after: int | None = None
        self._high_water: int | None = None
        self._discarded_through: int | None = None
        self._latest_timestamp_ms = 0
        self._invalid_since: float | None = None
        self._cooldown_until = 0.0
        self._cooldown_reply = AudioReadReply("empty")

    @property
    def identity(self) -> AudioIdentity:
        """The immutable call identity to which this hub is bound."""
        return self._identity

    @property
    def closed(self) -> bool:
        """Whether this hub has synchronously invalidated all listeners."""
        return self._closed

    def add(self, listener_id: str) -> None:
        self._check_open(self._epoch)
        if listener_id in self._listeners:
            return
        if len(self._listeners) == 4:
            raise AudioStreamError("audio_listener_limit")
        self._listeners[listener_id] = _Listener()

    def remove(self, listener_id: str) -> None:
        self._listeners.pop(listener_id, None)
        if not self._listeners:
            self.close()

    def close(self) -> None:
        """Invalidate in-flight reads without waiting for their network result."""
        self._closed = True
        self._epoch += 1
        self._listeners.clear()
        self._cache.clear()
        self._after = None
        self._high_water = None
        self._discarded_through = None
        self._latest_timestamp_ms = 0
        self._invalid_since = None
        self._cooldown_until = 0.0
        self._cooldown_reply = AudioReadReply("empty")

    def _check_open(self, epoch: int) -> None:
        if self._closed or epoch != self._epoch:
            raise AudioStreamError("audio_listener_closed")
        if self._invalid_since is not None and self._clock() - self._invalid_since >= 5:
            self.close()
            raise AudioStreamError("audio_connection_lost")

    def _check_listener(self, listener_id: str, listener: _Listener, epoch: int) -> None:
        self._check_open(epoch)
        if self._listeners.get(listener_id) is not listener:
            raise AudioStreamError("audio_listener_not_found")

    def _cached(self, listener: _Listener) -> AudioReadReply | None:
        if self._discarded_through is not None and (
            listener.cursor is None or listener.cursor < self._discarded_through
        ):
            listener.cursor = self._discarded_through
            listener.discontinuity = True
        now = self._clock()
        for chunk, acquired_at in self._cache:
            if listener.cursor is not None and chunk.revision <= listener.cursor:
                continue
            previous = listener.cursor
            listener.cursor = chunk.revision
            if (
                (now - acquired_at) * 1000
                + self._latest_timestamp_ms - chunk.timestamp_ms > 300
            ):
                listener.discontinuity = True
                # Once this listener falls behind, discard its intermediate
                # backlog and resume at the newest unconsumed cache entry.
                chunk, acquired_at = self._cache[-1]
                listener.cursor = chunk.revision
                if (
                    (now - acquired_at) * 1000
                    + self._latest_timestamp_ms - chunk.timestamp_ms > 300
                ):
                    return None
            discontinuity = (
                listener.discontinuity or chunk.discontinuity
                or (previous is not None and chunk.previous_revision != previous)
            )
            listener.discontinuity = False
            return AudioReadReply("chunk", replace(chunk, discontinuity=discontinuity))
        return None

    def _cooldown(self, reply: AudioReadReply) -> AudioReadReply:
        self._cooldown_reply = reply
        self._cooldown_until = self._clock() + 0.05
        return reply

    def _retry(self, request_started_at: float) -> AudioReadReply:
        if self._invalid_since is None:
            self._invalid_since = request_started_at
        self._check_open(self._epoch)
        return self._cooldown(AudioReadReply("retry"))

    async def read(self, listener_id: str) -> AudioReadReply:
        """Read one fresh chunk, advancing only this listener's delivery cursor."""
        epoch = self._epoch
        self._check_open(epoch)
        listener = self._listeners.get(listener_id)
        if listener is None:
            raise AudioStreamError("audio_listener_not_found")
        async with listener.lock:
            self._check_listener(listener_id, listener, epoch)
            cached = self._cached(listener)
            if cached is not None:
                return cached
            async with self._fetch_lock:
                self._check_listener(listener_id, listener, epoch)
                cached = self._cached(listener)
                if cached is not None:
                    return cached
                if self._clock() < self._cooldown_until:
                    return self._cooldown_reply
                request_started_at = self._clock()
                try:
                    reply = await self._client.read_audio_chunk(self._identity, after=self._after)
                except AudioStreamError as error:
                    self._check_open(epoch)
                    if error.code != "audio_invalid_response":
                        self.close()
                        raise
                    self._check_listener(listener_id, listener, epoch)
                    return self._retry(request_started_at)
                except Exception:
                    # The client boundary can raise transport-specific exceptions.
                    # Cancellation (a BaseException) intentionally propagates.
                    self._check_listener(listener_id, listener, epoch)
                    return self._retry(request_started_at)
                self._check_open(epoch)
                if reply.state == "retry":
                    self._check_listener(listener_id, listener, epoch)
                    return self._retry(request_started_at)
                if reply.state == "chunk" and reply.chunk.identity != self._identity:
                    self.close()
                    raise AudioStreamError("audio_identity_mismatch")
                self._invalid_since = None
                if reply.state == "resync":
                    self._cache.clear()
                    self._after = None
                    for current in self._listeners.values():
                        current.cursor = None
                        current.discontinuity = True
                    self._check_listener(listener_id, listener, epoch)
                    return reply
                if reply.state == "empty":
                    self._check_listener(listener_id, listener, epoch)
                    return self._cooldown(reply)
                chunk = reply.chunk
                self._after = chunk.revision
                if chunk.latest_timestamp_ms - chunk.timestamp_ms > 300:
                    # An uncursored read jumps to current speech instead of
                    # fetching every old revision in the bridge's window.
                    self._after = None
                if self._high_water is None or chunk.revision > self._high_water:
                    self._high_water = chunk.revision
                    self._latest_timestamp_ms = max(
                        self._latest_timestamp_ms, chunk.latest_timestamp_ms
                    )
                    if len(self._cache) == 4:
                        self._discarded_through = self._cache[0][0].revision
                    self._cache.append((chunk, self._clock()))
                self._check_listener(listener_id, listener, epoch)
                cached = self._cached(listener)
                if cached is not None:
                    return cached
                return self._cooldown(AudioReadReply("empty"))


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
