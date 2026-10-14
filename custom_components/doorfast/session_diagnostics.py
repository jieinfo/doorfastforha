"""Pure, bounded presentation of the bridge's sampled session diagnostics."""

from __future__ import annotations

from typing import Any

from .client_types import require_station_id


_CALL_TIMERS = frozenset({"none", "confirmation", "session"})
_MEDIA_TIMERS = _CALL_TIMERS | {"first_frame", "reconnect"}
_MEDIA_ENDS = frozenset({
    "none", "local_hangup", "peer_hangup", "session_timeout",
    "confirmation_timeout", "preempted", "runtime_reset", "unknown",
})
_CALL_ENDS = _MEDIA_ENDS | {"heartbeat_timeout"}
_CALL_FAULTS = frozenset({
    "none", "dispatch_failed", "dispatch_timeout", "confirmation_rejected",
    "confirmation_timeout", "preempted", "cancelled", "runtime_reset", "unknown",
    "heartbeat_timeout",
})
_MEDIA_FAULTS = (_CALL_FAULTS - {"heartbeat_timeout"}) | {
    "first_frame_timeout", "source_stalled", "encoder_failed", "publisher_failed",
    "capacity_busy",
}
_STATION_CALL_STAGES = frozenset({
    "idle", "preparing", "calling", "talking", "ending", "ended", "failed",
})
_STATION_CALL_STAGE_ORDER = {"idle": 0, "preparing": 1, "calling": 2,
                             "talking": 3, "ending": 4, "ended": 5, "failed": 5}
_STATION_CALL_TIMERS = _CALL_TIMERS | {"startup"}
_STATION_CALL_ERRORS = frozenset({
    "none", "busy", "station_unavailable", "ack_timeout", "dispatch_failed",
    "dispatch_timeout", "media_failed", "preempted", "cancelled", "runtime_reset", "unknown",
})


def _enum(value: Any, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def _uint64(value: Any, minimum: int = 0) -> int | None:
    return value if type(value) is int and minimum <= value < (1 << 64) else None


def _station_id(value: Any) -> str | None:
    try:
        return require_station_id(value)
    except ValueError:
        return None


def station_call_operation(status: Any) -> dict[str, Any] | None:
    """Validate the bounded outbound record against its sampled top-level runtime.

    Extra wire fields are discarded. A malformed present object is not the same
    as an older bridge which does not publish operations at all.
    """
    if not isinstance(status, dict):
        return None
    record = status.get("station_call")
    runtime_id = status.get("runtime_id")
    if (not isinstance(record, dict) or not isinstance(runtime_id, str)
            or len(runtime_id) != 16
            or any(character not in "0123456789abcdef" for character in runtime_id)):
        return None
    stage = record.get("stage")
    timer = record.get("timer")
    error = record.get("error")
    if (not isinstance(stage, str) or stage not in _STATION_CALL_STAGES
            or not isinstance(timer, str) or timer not in _STATION_CALL_TIMERS
            or not isinstance(error, str) or error not in _STATION_CALL_ERRORS):
        return None
    numbers = {field: _uint64(record.get(field)) for field in (
        "operation_id", "media_generation", "call_generation", "remaining_ms",
    )}
    if any(value is None for value in numbers.values()):
        return None
    station_id = _station_id(record.get("station_id"))
    if stage == "idle":
        if (record.get("station_id") != "" or any(numbers.values())
                or timer != "none" or error != "none"):
            return None
    elif station_id is None or numbers["operation_id"] == 0:
        return None
    if stage in ("ended", "failed") and (timer != "none" or numbers["remaining_ms"] != 0):
        return None
    return {"runtime_id": runtime_id, "station_id": station_id, "stage": stage,
            "timer": timer, "error": error, **numbers}


def station_call_diagnostics(status: Any, station_id: Any) -> dict[str, Any]:
    """Keep outbound identities and safe codes scoped to the target station."""
    result = {f"station_call_{field}": None for field in (
        "runtime_id", "operation_id", "station_id", "stage", "media_generation",
        "call_generation", "timer", "remaining_ms", "error",
    )}
    operation = station_call_operation(status)
    if (_station_id(station_id) is not None and operation is not None
            and operation["station_id"] == station_id):
        result.update({f"station_call_{field}": value for field, value in operation.items()})
    return result


def station_call_phase(status: Any, station_id: Any) -> str | None:
    """Admit TALKING only through the native owner and exact outbound mapping.

    Native control generation and public media generation are separate
    namespaces. Incoming TALKING does not depend on video availability; terminal
    operation history cannot claim a newer incoming owner.
    """
    if not isinstance(status, dict) or _station_id(station_id) is None:
        return None
    operation = station_call_operation(status)
    if "station_call" in status and operation is None:
        return None
    active = operation is not None and operation["stage"] in (
        "preparing", "calling", "talking", "ending",
    )
    if active:
        if operation["station_id"] != station_id:
            return None
        if operation["stage"] in ("preparing", "calling"):
            return "calling"
        if operation["stage"] == "ending":
            return "stopping"
    call = status.get("call")
    if (not isinstance(call, dict) or call.get("session") != "talking"
            or call.get("station_id") != station_id or station_id == "unknown"
            or _uint64(call.get("generation"), 1) is None):
        return None
    if not active:
        return "in_call"
    media = status.get("media")
    if not isinstance(media, dict) or media.get("available") is False:
        return None
    session = _matching_record(media.get("sessions"), station_id)
    if session is None or session.get("state") not in (
        "requesting", "queued", "awaiting_video", "publishing", "viewing",
    ):
        return None
    if (
        operation["call_generation"] != call["generation"]
        or operation["media_generation"] != session["generation"]
        or session.get("purpose") != "call"
    ):
        return None
    return "in_call"


def _fields(record: dict[str, Any], prefix: str, timers: frozenset[str],
            ends: frozenset[str], faults: frozenset[str], fault_field: str) -> dict[str, Any]:
    timer = _enum(record.get("timer"), timers)
    return {
        f"{prefix}_timer": timer,
        f"{prefix}_remaining_ms": (
            _uint64(record.get("remaining_ms")) if timer != "unknown" else None
        ),
        f"{prefix}_end_reason": _enum(record.get("end_reason"), ends),
        f"{prefix}_{fault_field}": _enum(record.get(fault_field), faults),
    }


def call_diagnostics(status: Any) -> dict[str, Any]:
    """Expose only known call codes and one validated terminal identity pair."""
    call = status.get("call") if isinstance(status, dict) else None
    call = call if isinstance(call, dict) else {}
    result = _fields(call, "call", _CALL_TIMERS, _CALL_ENDS, _CALL_FAULTS, "fault")
    station_id = _station_id(call.get("last_station_id"))
    # This wire sentinel cannot distinguish an unresolved owner from that name.
    if station_id == "unknown":
        station_id = None
    generation = _uint64(call.get("last_generation"), 1)
    result["call_last_station_id"] = station_id if generation is not None else None
    result["call_last_generation"] = generation if station_id is not None else None
    return result


def _matching_record(records: Any, station_id: str) -> dict[str, Any] | None:
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        return None
    matches = [item for item in records if item.get("station_id") == station_id]
    if len(matches) != 1 or _uint64(matches[0].get("generation"), 1) is None:
        return None
    return matches[0]


def station_diagnostics(status: Any, station_id: Any) -> dict[str, Any]:
    """Current diagnostics win; the last marker belongs only to terminal history.

    Missing/ambiguous active collections never fall back to a past reason.
    History is read from this snapshot only, with no cache across runtimes.
    """
    result = _fields({}, "monitor", _MEDIA_TIMERS, _MEDIA_ENDS, _MEDIA_FAULTS,
                     "failure_reason")
    result["monitor_last_generation"] = None
    media = status.get("media") if isinstance(status, dict) else None
    if _station_id(station_id) is None or not isinstance(media, dict) or media.get("available") is False:
        return result

    terminal = _matching_record(media.get("last_results"), station_id)
    if terminal is not None and (
        terminal.get("timer") != "none"
        or type(terminal.get("remaining_ms")) is not int
        or terminal["remaining_ms"] != 0
    ):
        terminal = None
    if terminal is not None:
        result["monitor_last_generation"] = terminal["generation"]

    sessions = media.get("sessions")
    if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
        return result
    # Presence of even an invalid matching current record blocks history fallback.
    has_current = any(item.get("station_id") == station_id for item in sessions)
    current = _matching_record(sessions, station_id) if has_current else terminal
    if current is not None:
        result.update(_fields(current, "monitor", _MEDIA_TIMERS, _MEDIA_ENDS,
                              _MEDIA_FAULTS, "failure_reason"))
    return result
