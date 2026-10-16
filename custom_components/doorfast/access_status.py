"""Translate Doorfast access-control status for Home Assistant."""

from __future__ import annotations

from typing import Any
import re


ACCESS_FIELDS = (
    "configured",
    "state",
    "generation",
    "raw_status",
    "physical_result_confirmed",
    "station_id",
    "manual_state",
    "manual_station_id",
)

ACCESS_STATES = (
    "idle", "waiting", "protocol_completed", "protocol_rejected",
    "expired", "cancelled", "send_failed",
)
MANUAL_STATES = ("idle", "waiting_media", "waiting_reply")


def _valid_field(field: str, value: Any) -> bool:
    if field in ("station_id", "manual_station_id"):
        return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value) is not None
    if field == "state":
        return isinstance(value, str) and value in ACCESS_STATES
    if field == "manual_state":
        return isinstance(value, str) and value in MANUAL_STATES
    if field in ("configured", "physical_result_confirmed"):
        return isinstance(value, bool)
    maximum = 255 if field == "raw_status" else (1 << 64) - 1
    return type(value) is int and 0 <= value <= maximum


def access_attributes(status: dict[str, Any]) -> dict[str, Any]:
    """Return the supported, non-sensitive access-control status fields."""
    access = status.get("access")
    if not isinstance(access, dict):
        return {}
    attributes = {
        field: access[field] for field in ACCESS_FIELDS
        if field in access and _valid_field(field, access[field])
    }
    if "physical_result_confirmed" in attributes:
        attributes["physical_result_confirmed"] = False
    return attributes


UNLOCK_PHASES = (
    "idle", "waiting_media", "waiting_reply", "protocol_completed",
    "protocol_rejected", "preparation_timeout", "expired", "cancelled", "send_failed",
    "material_unavailable", "disabled", "unknown",
)
OPERATION_PHASES = UNLOCK_PHASES[1:9]
PENDING_PHASES = ("waiting_media", "waiting_reply")
TIMING_FIELDS = (
    "remaining_ms", "elapsed_ms", "first_request_ms", "confirmed_ms",
    "first_media_ms", "sent_ms", "reply_ms", "preparation_ms", "reply_latency_ms",
)
_REASONS = ("none", "disabled", "material_missing", "material_invalid", "store_unavailable", "station_unavailable")


def _hex_id(value: Any, *, request: bool = False) -> bool:
    return (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value) is not None
            and (not request or value != "0" * 16))


def _uint64(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= (1 << 64) - 1 else None


def _choice(value: Any, choices: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in choices else "unknown"


def _version_one(access: Any) -> bool:
    return isinstance(access, dict) and type(access.get("version")) is int and access["version"] == 1


def station_unlock_configuration(status: dict[str, Any], station_id: str) -> dict[str, Any]:
    """Return only an unambiguous, internally consistent station capability."""
    access = status.get("access")
    if (not _valid_field("station_id", station_id) or not _hex_id(status.get("runtime_id")) or not _version_one(access)
            or not isinstance(access.get("stations"), list)):
        return {}
    rows = [row for row in access["stations"] if isinstance(row, dict) and row.get("station_id") == station_id]
    if len(rows) != 1:
        return {}
    row = rows[0]
    mode, source, configured, reason = (row.get(key) for key in ("mode", "credential_source", "configured", "reason"))
    if (mode not in ("inherit", "direct", "disabled") or type(configured) is not bool
            or source not in ("none", "global", "station") or reason not in _REASONS):
        return {}
    if configured:
        if reason != "none" or source != {"inherit": "global", "direct": "station"}.get(mode):
            return {}
    elif source != "none" or reason == "none" or (reason == "disabled" and mode != "disabled"):
        return {}
    return {"mode": mode, "configured": configured, "credential_source": source, "reason": reason}


def station_unlock_record(status: dict[str, Any], station_id: str) -> dict[str, Any] | None:
    """Select exactly one station record owned by this runtime, never access.request."""
    access = status.get("access")
    runtime = status.get("runtime_id")
    if (not _valid_field("station_id", station_id) or not _version_one(access)
            or not _hex_id(runtime) or not isinstance(access.get("requests"), list)):
        return None
    rows = access["requests"]
    matches = [row for row in rows if isinstance(row, dict) and row.get("station_id") == station_id]
    if len(matches) != 1:
        return None
    row = matches[0]
    request = row.get("request_id")
    if (row.get("runtime_id") != runtime or not _hex_id(request, request=True)
            or not isinstance(row.get("phase"), str) or row["phase"] not in OPERATION_PHASES
            or sum(isinstance(other, dict) and other.get("runtime_id") == runtime
                   and other.get("request_id") == request for other in rows) != 1):
        return None
    return row


def validate_unlock_reply(reply: Any, runtime: str | None, station_id: str) -> dict[str, Any]:
    """Accepted means submitted to the bridge, never physical door movement."""
    if not isinstance(reply, dict):
        raise ValueError("unlock reply must be an object")
    has_identity = any(key in reply for key in ("runtime_id", "request_id", "phase"))
    if not has_identity:
        # queued is retained for older bridges predating submitted.
        key = "submitted" if "submitted" in reply else "queued"
        if reply.get(key) is not True or ("station_id" in reply and reply["station_id"] != station_id):
            raise ValueError("unlock reply is not an acceptance")
        return {key: True}
    if (reply.get("submitted") is not True or reply.get("station_id") != station_id
            or not _hex_id(reply.get("runtime_id"))
            or (runtime is not None and reply["runtime_id"] != runtime)
            or not _hex_id(reply.get("request_id"), request=True)
            or not isinstance(reply.get("phase"), str) or reply["phase"] not in OPERATION_PHASES):
        raise ValueError("unlock reply has invalid identity or phase")
    return {key: reply[key] for key in ("submitted", "station_id", "runtime_id", "request_id", "phase")}


def station_unlock_attributes(status: dict[str, Any], station_id: str) -> dict[str, Any]:
    access = status.get("access")
    if not isinstance(access, dict) or "version" not in access:
        return _legacy_station_access_attributes(status, station_id)
    attrs: dict[str, Any] = {"phase": "unknown", "physical_result_confirmed": False}
    if not _version_one(access):
        return attrs
    config = station_unlock_configuration(status, station_id)
    attrs.update(config)
    row = station_unlock_record(status, station_id)
    if row is None:
        if config.get("configured") is False:
            attrs["phase"] = "disabled" if config["mode"] == "disabled" else "material_unavailable"
        elif config.get("configured") is True and access.get("requests") == [] and "request" not in access:
            attrs["phase"] = "idle"
        return attrs
    attrs.update({key: row[key] for key in ("station_id", "runtime_id", "request_id", "phase")})
    for field, choices in (
        ("wait_reason", ("none", "busy", "cooldown", "waiting_media", "compatibility", "station_unavailable", "unknown")),
        ("preparation_path", ("lightweight", "full_media", "reused")),
        ("mode", ("inherit", "direct", "disabled")),
        ("credential_source", ("none", "global", "station")),
        ("error", ("none", "unknown")), ("cleanup_error", ("none", "cleanup_failed")),
    ):
        attrs[field] = _choice(row.get(field), choices)
    attrs["reused_session"] = row.get("reused_session") if type(row.get("reused_session")) is bool else None
    for field in ("attempts", "busy_replies", "request_to_acceptance_ms"):
        attrs[field] = _uint64(row.get(field))
    elapsed = _uint64(row.get("elapsed_ms"))
    for field in TIMING_FIELDS:
        value = _uint64(row.get(field))
        if field not in ("elapsed_ms", "remaining_ms") and (elapsed is None or value is not None and value > elapsed):
            value = None
        if field == "remaining_ms" and row["phase"] not in PENDING_PHASES and value != 0:
            value = None
        if field == "remaining_ms" and value is not None and value > (1000 if row["phase"] == "waiting_reply" else 30000):
            value = None
        attrs[field] = value
    sent, reply = attrs["sent_ms"], attrs["reply_ms"]
    if sent is not None and attrs["preparation_ms"] != sent:
        attrs["preparation_ms"] = None
    if (sent is None or reply is None or reply < sent or reply - sent >= 1000
            or row["phase"] not in ("protocol_completed", "protocol_rejected")):
        attrs["reply_ms"] = attrs["reply_latency_ms"] = None
    elif attrs["reply_latency_ms"] != reply - sent:
        attrs["reply_latency_ms"] = None
    if row["phase"] in ("protocol_completed", "protocol_rejected") and _valid_field("raw_status", row.get("raw_status")):
        attrs["raw_status"] = row["raw_status"]
    return attrs


def station_access_attributes(status: dict[str, Any], station_id: str) -> dict[str, Any]:
    """Compatibility name retained for existing station sensors."""
    return station_unlock_attributes(status, station_id)


def _legacy_station_access_attributes(status: dict[str, Any], station_id: str) -> dict[str, Any]:
    """Scope progress and the latest protocol transaction to its actual target."""
    access = access_attributes(status)
    attributes: dict[str, Any] = {"phase": None}
    if "configured" in access:
        attributes["configured"] = access["configured"]
    manual = access.get("manual_state")
    if access.get("manual_station_id") == station_id and manual in (
        "waiting_media", "waiting_reply",
    ):
        attributes.update(phase=manual, manual_state=manual)
        # While obtaining media, access.result can still refer to an old
        # transaction, even for this station. Do not display it as this request.
        if manual == "waiting_media":
            return attributes
    target = access.get("station_id")
    if target == station_id:
        attributes.update(access)
        state = access.get("state")
        attributes["phase"] = "waiting_reply" if state == "waiting" else state
        if manual == "waiting_reply" and access.get("manual_station_id") == station_id:
            attributes["phase"] = "waiting_reply"
    elif attributes["phase"] is None:
        if target is not None or access.get("state") == "idle":
            attributes["phase"] = "idle"
    return attributes
