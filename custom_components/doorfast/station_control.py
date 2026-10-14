"""Bounded station control replies; public media and native control differ."""
from __future__ import annotations

from typing import Any

from .session_diagnostics import _STATION_CALL_ERRORS, _STATION_CALL_STAGES, _uint64

_ADMISSION_ERRORS = {
    "capacity_busy": "busy",
    "station_not_found": "station_unavailable",
    "station_disabled": "station_unavailable",
    "route_unavailable": "station_unavailable",
    "encoder_failed": "media_failed",
}


def validate_reply(reply: Any, runtime_id: str, station_id: str) -> dict[str, Any]:
    if not isinstance(reply, dict):
        raise ValueError("station control reply must be an object")
    for field, expected in (("runtime_id", runtime_id), ("station_id", station_id)):
        if field in reply and reply[field] != expected:
            raise ValueError("station control reply identity mismatch")
    for field in ("generation", "operation_id"):
        if field in reply and _uint64(reply[field]) is None:
            raise ValueError("station control reply identity must be uint64")
    if "queued" in reply and type(reply["queued"]) is not bool:
        raise ValueError("station control queued must be boolean")
    if "state" in reply and reply["state"] not in _STATION_CALL_STAGES | {"ringing", "preview"}:
        raise ValueError("station control state is invalid")
    if reply.get("operation_id", 0) > 0 and (
        any(field not in reply for field in ("runtime_id", "station_id", "generation", "state", "queued"))
    ):
        raise ValueError("station operation reply has incomplete identity")
    return {key: value for key, value in reply.items() if key in {
        "runtime_id", "station_id", "generation", "operation_id", "state", "queued",
    }}


def positive_condition(value: Any) -> int:
    if _uint64(value, 1) is None:
        raise ValueError("station control condition must be positive uint64")
    return value


def stable_error(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str):
        code = _ADMISSION_ERRORS.get(code, code)
    # Runtime/operation/generation conflicts have no operation-error category;
    # retain unknown rather than imply physical failure or expose raw text.
    return code if isinstance(code, str) and code in _STATION_CALL_ERRORS else "unknown"


def validate_call_parameters(primary_media_port: Any, secondary_media_port: Any,
                             duration_seconds: Any) -> None:
    for value, name in ((primary_media_port, "primary_media_port"),
                        (secondary_media_port, "secondary_media_port"),
                        (duration_seconds, "duration_seconds")):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
