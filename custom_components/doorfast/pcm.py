"""Bounded, generation-bound producer for Doorfast microphone PCM."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Protocol, Sequence

from .client_types import PcmHttpReply

_FRAME_BYTES = 320
_MAX_FRAMES = 5
_UINT64_MAX = (1 << 64) - 1
_RUNTIME_RE = re.compile(r"[0-9a-f]{16}\Z")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
_ERROR_CODES = frozenset({
    "audio_tx_inactive",
    "call_not_talking",
    "clock_unavailable",
    "generation_mismatch",
    "pacing_unavailable",
    "producer_busy",
    "random_unavailable",
    "runtime_mismatch",
    "send_unavailable",
    "sequence_duplicate",
    "sequence_gap",
    "session_expired",
    "session_mismatch",
    "state_unavailable",
    "status_unavailable",
})


class PcmProducerState(Enum):
    IDLE = "idle"
    OPENING = "opening"
    ACTIVE = "active"
    RECOVERING = "recovering"
    STOPPING = "stopping"


class PcmProducerError(RuntimeError):
    """A bounded producer failure with a stable machine-readable category."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PcmSubmitOutcome:
    accepted_frames: int
    next_sequence: int
    complete: bool
    recovered: bool


class _Client(Protocol):
    status: dict[str, Any]

    async def refresh(self) -> dict[str, Any]: ...
    async def pcm_session_open(self, runtime: str, generation: int) -> PcmHttpReply: ...
    async def pcm_submit(self, runtime: str, generation: int, sequence: int, token: str, body: bytes) -> PcmHttpReply: ...
    async def pcm_session_end(self, runtime: str, generation: int, token: str) -> PcmHttpReply: ...


def _u64(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _UINT64_MAX


def _status_identity(value: object) -> tuple[str, int]:
    if not isinstance(value, dict):
        raise PcmProducerError("status_invalid")
    runtime = value.get("runtime_id")
    call = value.get("call")
    audio_tx = value.get("audio_tx")
    if (
        not isinstance(runtime, str)
        or _RUNTIME_RE.fullmatch(runtime) is None
        or not isinstance(call, dict)
        or not isinstance(audio_tx, dict)
    ):
        raise PcmProducerError("status_invalid")
    generation = call.get("generation")
    audio_generation = audio_tx.get("generation")
    if (
        call.get("state") != "talking"
        or not _u64(generation)
        or generation == 0
        or audio_tx.get("active") is not True
        or not _u64(audio_generation)
        or audio_generation != generation
    ):
        raise PcmProducerError("status_not_ready")
    return runtime, generation


def _common_payload(reply: PcmHttpReply) -> tuple[dict[str, Any], str, int, int, int]:
    payload = reply.payload
    if not isinstance(payload, dict):
        raise PcmProducerError("invalid_response")
    runtime = payload.get("runtime_id")
    generation = payload.get("generation")
    accepted = payload.get("accepted_frames")
    next_sequence = payload.get("next_sequence")
    if (
        not isinstance(runtime, str)
        or _RUNTIME_RE.fullmatch(runtime) is None
        or not _u64(generation)
        or not _u64(accepted)
        or not _u64(next_sequence)
    ):
        raise PcmProducerError("invalid_response")
    return payload, runtime, generation, accepted, next_sequence


class PcmProducer:
    """Own one private Doorfast producer lease and never retain PCM bodies."""

    def __init__(self, client: _Client):
        self._client = client
        self._lock = asyncio.Lock()
        self.state = PcmProducerState.IDLE
        self._runtime: str | None = None
        self._generation: int | None = None
        self._token: str | None = None
        self._sequence = 0

    def __repr__(self) -> str:
        return f"{type(self).__name__}(state={self.state.value!r})"

    def _clear(self) -> None:
        self._runtime = None
        self._generation = None
        self._token = None
        self._sequence = 0
        self.state = PcmProducerState.IDLE

    async def _fresh_identity(self) -> tuple[str, int]:
        await self._client.refresh()
        return _status_identity(self._client.status)

    def _require_identity(self) -> tuple[str, int, str]:
        if self._runtime is None or self._generation is None or self._token is None:
            raise PcmProducerError("not_active")
        return self._runtime, self._generation, self._token

    def _validate_success(
        self,
        reply: PcmHttpReply,
        runtime: str,
        generation: int,
        operation: str,
        frames: int = 0,
        sequence: int = 0,
    ) -> tuple[dict[str, Any], int, int]:
        if reply.status != 200:
            raise PcmProducerError("invalid_response")
        payload, actual_runtime, actual_generation, accepted, next_sequence = _common_payload(reply)
        lease = payload.get("lease_ms")
        token = payload.get("audio_session")
        if (
            payload.get("status") != "success"
            or actual_runtime != runtime
            or actual_generation != generation
            or not _u64(lease)
            or not isinstance(token, str)
        ):
            raise PcmProducerError("invalid_response")
        if operation == "open":
            if lease == 0 or _TOKEN_RE.fullmatch(token) is None or accepted != 0:
                raise PcmProducerError("invalid_response")
        elif operation == "submit":
            if lease == 0 or token != "":
                raise PcmProducerError("invalid_response")
        elif lease != 0 or token != "":
            raise PcmProducerError("invalid_response")
        if operation == "submit" and (
            accepted != frames or next_sequence != sequence + frames
        ):
            raise PcmProducerError("invalid_response")
        if operation == "end" and (accepted != 0 or next_sequence != sequence):
            raise PcmProducerError("invalid_response")
        return payload, accepted, next_sequence

    def _validate_error(
        self, reply: PcmHttpReply
    ) -> tuple[str, str, int, int, int]:
        if reply.status not in {409, 503}:
            raise PcmProducerError("invalid_response")
        payload, runtime, generation, accepted, next_sequence = _common_payload(reply)
        code = payload.get("error")
        if code not in _ERROR_CODES or "status" in payload:
            raise PcmProducerError("invalid_response")
        return code, runtime, generation, accepted, next_sequence

    async def _open(self, runtime: str, generation: int) -> None:
        reply = await self._client.pcm_session_open(runtime, generation)
        if reply.status != 200:
            code, actual_runtime, actual_generation, _accepted, _next = self._validate_error(reply)
            if actual_runtime != runtime or actual_generation != generation:
                raise PcmProducerError("identity_changed")
            raise PcmProducerError(code)
        payload, _accepted, next_sequence = self._validate_success(
            reply, runtime, generation, "open"
        )
        self._runtime = runtime
        self._generation = generation
        self._token = payload["audio_session"]
        self._sequence = next_sequence

    async def start(self) -> None:
        async with self._lock:
            if self.state is not PcmProducerState.IDLE:
                raise PcmProducerError("already_started")
            self.state = PcmProducerState.OPENING
            try:
                runtime, generation = await self._fresh_identity()
                await self._open(runtime, generation)
                self.state = PcmProducerState.ACTIVE
            except BaseException:
                self._clear()
                raise

    async def _recover_identity(self) -> None:
        runtime, generation, _token = self._require_identity()
        fresh_runtime, fresh_generation = await self._fresh_identity()
        if fresh_runtime != runtime or fresh_generation != generation:
            self._clear()
            raise PcmProducerError("identity_changed")

    async def submit(self, frames: Sequence[bytes]) -> PcmSubmitOutcome:
        if (
            not isinstance(frames, Sequence)
            or not 1 <= len(frames) <= _MAX_FRAMES
            or any(not isinstance(frame, bytes) or len(frame) != _FRAME_BYTES for frame in frames)
        ):
            raise ValueError("PCM requires one to five exact 320-byte frames")
        body = b"".join(frames)
        async with self._lock:
            if self.state is not PcmProducerState.ACTIVE:
                raise PcmProducerError("not_active")
            runtime, generation, token = self._require_identity()
            sequence = self._sequence
            if sequence > _UINT64_MAX - len(frames):
                raise PcmProducerError("sequence_overflow")
            try:
                reply = await self._client.pcm_submit(
                    runtime, generation, sequence, token, body
                )
            except BaseException as error:
                self.state = PcmProducerState.RECOVERING
                try:
                    await self._recover_identity()
                except BaseException:
                    self._clear()
                    raise
                self.state = PcmProducerState.ACTIVE
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise PcmProducerError("response_lost") from error

            try:
                if reply.status == 200:
                    _payload, accepted, next_sequence = self._validate_success(
                        reply, runtime, generation, "submit", len(frames), sequence
                    )
                    self._sequence = next_sequence
                    return PcmSubmitOutcome(accepted, next_sequence, True, False)

                code, actual_runtime, actual_generation, accepted, next_sequence = self._validate_error(reply)
                if actual_runtime != runtime or actual_generation != generation:
                    raise PcmProducerError("identity_changed")
                if accepted > len(frames):
                    raise PcmProducerError("invalid_response")
                if reply.status == 503:
                    if next_sequence != sequence + accepted:
                        raise PcmProducerError("invalid_response")
                    self._sequence = next_sequence
                    self.state = PcmProducerState.RECOVERING
                    await self._recover_identity()
                    self.state = PcmProducerState.ACTIVE
                    return PcmSubmitOutcome(accepted, next_sequence, False, True)
                if accepted != 0:
                    raise PcmProducerError("invalid_response")
                if code in {"sequence_duplicate", "sequence_gap"}:
                    self._sequence = next_sequence
                    self.state = PcmProducerState.RECOVERING
                    await self._recover_identity()
                    self.state = PcmProducerState.ACTIVE
                    return PcmSubmitOutcome(0, next_sequence, False, True)
                if code == "session_expired":
                    self.state = PcmProducerState.RECOVERING
                    await self._recover_identity()
                    await self._open(runtime, generation)
                    self.state = PcmProducerState.ACTIVE
                    return PcmSubmitOutcome(0, self._sequence, False, True)
                raise PcmProducerError(code)
            except BaseException:
                self._clear()
                raise

    async def stop(self) -> None:
        async with self._lock:
            if self.state is PcmProducerState.IDLE:
                return
            self.state = PcmProducerState.STOPPING
            runtime = self._runtime
            generation = self._generation
            token = self._token
            try:
                if runtime is not None and generation is not None and token is not None:
                    reply = await self._client.pcm_session_end(runtime, generation, token)
                    if reply.status == 200:
                        self._validate_success(
                            reply, runtime, generation, "end", sequence=self._sequence
                        )
                    else:
                        _code, actual_runtime, actual_generation, _accepted, _next = (
                            self._validate_error(reply)
                        )
                        if actual_runtime != runtime or actual_generation != generation:
                            raise PcmProducerError("identity_changed")
            except BaseException:
                pass
            finally:
                self._clear()
