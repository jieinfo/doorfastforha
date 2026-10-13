"""Transport-only value types shared by the Doorfast HTTP clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PcmHttpReply:
    """An HTTP status and already-decoded JSON object from the PCM bridge."""

    status: int
    payload: dict[str, Any]


_UINT64_MAX = (1 << 64) - 1


class AudioStreamError(RuntimeError):
    """Stable error raised while validating a station audio stream."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AudioIdentity:
    """Identity binding for one talking station call's audio stream."""

    runtime_id: str
    station_id: str
    generation: int

    @classmethod
    def from_status(
        cls,
        status: Any,
        station_id: Any,
        expected_generation: int | None = None,
    ) -> "AudioIdentity":
        """Extract and validate the identity permitted to read station audio."""
        try:
            requested_station = require_station_id(station_id)
        except ValueError as error:
            raise AudioStreamError("audio_identity_mismatch") from error
        if expected_generation is not None and (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or not 1 <= expected_generation <= _UINT64_MAX
        ):
            raise AudioStreamError("audio_identity_mismatch")
        if not isinstance(status, dict):
            raise AudioStreamError("audio_identity_mismatch")
        runtime_id = status.get("runtime_id")
        call = status.get("call")
        audio = status.get("audio")
        if (
            not isinstance(runtime_id, str)
            or len(runtime_id) != 16
            or any(character not in "0123456789abcdef" for character in runtime_id)
            or not isinstance(call, dict)
            or not isinstance(audio, dict)
            or call.get("session") != "talking"
            or call.get("station_id") != requested_station
            or (
                isinstance(audio.get("pcm_stream_version"), bool)
                or not isinstance(audio.get("pcm_stream_version"), int)
                or audio.get("pcm_stream_version") != 1
            )
        ):
            raise AudioStreamError("audio_identity_mismatch")
        generation = call.get("generation")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 1 <= generation <= _UINT64_MAX
            or (
                expected_generation is not None
                and generation != expected_generation
            )
        ):
            raise AudioStreamError("audio_identity_mismatch")
        return cls(runtime_id, requested_station, generation)


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """One bounded PCM payload and its bridge revision metadata."""

    identity: AudioIdentity
    previous_revision: int
    revision: int
    pcm: bytes = field(repr=False)
    timestamp_ms: int
    latest_timestamp_ms: int
    dropped_samples: int
    discontinuity: bool


@dataclass(frozen=True, slots=True)
class AudioReadReply:
    """Result of one independent v1 audio read."""

    state: str
    chunk: AudioChunk | None = None

    def __post_init__(self) -> None:
        if self.state not in {"chunk", "empty", "retry", "resync"}:
            raise ValueError("invalid audio read state")
        if (self.state == "chunk") != (self.chunk is not None):
            raise ValueError("chunk state requires an audio chunk")


def require_station_id(value: Any) -> str:
    """Return a configured station identifier or reject an invalid value."""
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not 1 <= len(value) <= 32
        or not "a" <= value[0] <= "z"
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in value[1:]
        )
    ):
        raise ValueError("station id must be a lowercase ASCII identifier")
    return value


def require_text(value: Any, field: str, maximum_length: int) -> str:
    """Return a non-empty bounded ASCII text field."""
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or len(value) > maximum_length
    ):
        raise ValueError(f"{field} must be non-empty ASCII text")
    return value


def require_logical_address(value: Any) -> str:
    """Return a six-byte Doorfast station logical address."""
    if (
        not isinstance(value, str)
        or len(value) != 17
        or value[2::3] != ":::::"
    ):
        raise ValueError("logical_address must be a six-byte address")
    parts = value.split(":")
    if (
        len(parts) != 6
        or parts[0] != "32"
        or any(
            len(part) != 2
            or any(character not in "0123456789abcdef" for character in part)
            for part in parts
        )
    ):
        raise ValueError("logical_address must be a lowercase 0x32 address")
    return value


def require_stream_name(value: Any) -> str:
    """Return a configured stream name."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or not value.isascii()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789_-"
            for character in value
        )
    ):
        raise ValueError("stream_name must be ASCII letters, digits, '_' or '-'")
    return value


def require_route_source(value: Any) -> str:
    """Return a known route source."""
    if value not in {"none", "discovered", "configured"}:
        raise ValueError("route_source must be none, discovered, or configured")
    return value


def require_bool(value: Any, field: str) -> bool:
    """Return a real boolean, not a truthy JSON-compatible substitute."""
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def optional_non_negative_int(value: Any) -> int | None:
    """Return a non-negative integer or the JSON null representation."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("value must be a non-negative integer or null")
    return value


@dataclass(frozen=True, slots=True)
class DoorfastStation:
    """A validated configured door station from the Doorfast bridge."""

    station_id: str
    name: str
    logical_address: str
    enabled: bool
    stream_name: str
    route_source: str
    route_fresh: bool
    monitorable: bool
    last_seen_ms: int | None

    @property
    def reachable(self) -> bool:
        return self.enabled and self.route_fresh and self.monitorable

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DoorfastStation":
        if not isinstance(payload, dict):
            raise ValueError("station payload must be an object")
        return cls(
            station_id=require_station_id(payload.get("id")),
            name=require_text(payload.get("name"), "name", 128),
            logical_address=require_logical_address(payload.get("logical_address")),
            enabled=require_bool(payload.get("enabled"), "enabled"),
            stream_name=require_stream_name(payload.get("stream_name")),
            route_source=require_route_source(payload.get("route_source")),
            route_fresh=require_bool(payload.get("route_fresh"), "route_fresh"),
            monitorable=require_bool(payload.get("monitorable"), "monitorable"),
            last_seen_ms=optional_non_negative_int(payload.get("last_seen_ms")),
        )


@dataclass(frozen=True, slots=True)
class DoorfastStationSnapshot:
    """An immutable revisioned collection of configured stations."""

    runtime_id: str
    revision: int
    stations: tuple[DoorfastStation, ...]
