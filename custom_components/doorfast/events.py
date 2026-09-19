"""Validation and ordering for Doorfast push events."""
from __future__ import annotations
from collections import deque
import json
from typing import Any

CALL_EVENT_NAMES = frozenset({"incoming_call", "call_established", "hangup", "timeout", "preempted"})
MONITOR_EVENT_NAMES = frozenset({
    "monitor_requested",
    "monitor_confirmed",
    "monitor_media_ready",
    "monitor_publishing",
    "monitor_failed",
    "monitor_stopped",
    "monitor_preempted",
})
EVENT_NAMES = CALL_EVENT_NAMES | MONITOR_EVENT_NAMES
SCHEMA_VERSION = 1
_SENSITIVE_KEYS = frozenset({
    "password", "token", "authorization", "sdp", "candidate", "url"
})
_MAX_STATUS_BYTES = 16 * 1024


def _validate_monitor_status(status: Any) -> dict[str, Any]:
    if not isinstance(status, dict):
        raise ValueError("monitor status must be an object")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or key.lower() in _SENSITIVE_KEYS:
                    raise ValueError("sensitive monitor status field")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif not isinstance(value, (str, int, float, bool)) and value is not None:
            raise ValueError("invalid monitor status value")

    visit(status)
    try:
        encoded = json.dumps(status, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid monitor status") from error
    if len(encoded.encode("utf-8")) > _MAX_STATUS_BYTES:
        raise ValueError("monitor status is too large")
    return status


def validate_event(payload: Any) -> dict[str, Any]:
    """Validate and return a normalized event payload."""
    if not isinstance(payload, dict):
        raise ValueError("event must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported event schema")
    event = payload.get("event")
    event_id = payload.get("event_id")
    generation = payload.get("generation")
    timestamp_ms = payload.get("timestamp_ms")
    if event not in EVENT_NAMES:
        raise ValueError("unsupported event name")
    for name, value in (("event_id", event_id), ("generation", generation), ("timestamp_ms", timestamp_ms)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"invalid {name}")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event": event,
        "generation": generation,
        "timestamp_ms": timestamp_ms,
    }
    if event in MONITOR_EVENT_NAMES:
        runtime_id = payload.get("runtime_id")
        station_id = payload.get("station_id")
        if (
            not isinstance(runtime_id, str)
            or not runtime_id
            or len(runtime_id) > 64
            or not runtime_id.isascii()
        ):
            raise ValueError("invalid runtime_id")
        if (
            not isinstance(station_id, str)
            or not 1 <= len(station_id) <= 32
            or not station_id.isascii()
            or not "a" <= station_id[0] <= "z"
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in station_id[1:]
            )
        ):
            raise ValueError("invalid station_id")
        status_revision = payload.get("status_revision")
        if (
            isinstance(status_revision, bool)
            or not isinstance(status_revision, int)
            or status_revision <= 0
        ):
            raise ValueError("invalid status_revision")
        normalized["status_revision"] = status_revision
        normalized["runtime_id"] = runtime_id
        normalized["station_id"] = station_id
        normalized["status"] = _validate_monitor_status(payload.get("status"))
    return normalized


class EventGate:
    """Suppress duplicate and stale generations from a push transport."""

    def __init__(self, capacity: int = 128) -> None:
        self._seen: set[tuple[int, str]] = set()
        self._capacity = capacity
        self._order: deque[tuple[int, str]] = deque()
        self.highest_generation = 0
        self.runtime_id: str | None = None
        self._monitor_states: dict[tuple[str, str], dict[str, Any]] = {}

    def accept(
        self,
        payload: dict[str, Any],
        current_generation: int | None = None,
        runtime_id: str | None = None,
    ) -> bool:
        if isinstance(runtime_id, str) and runtime_id:
            if self.runtime_id != runtime_id:
                self._seen.clear()
                self._order.clear()
                self.highest_generation = 0
                self.runtime_id = runtime_id
        generation = payload["generation"]
        event = payload["event"]
        key = (generation, event)
        if key in self._seen:
            return False
        floor = self.highest_generation
        if isinstance(current_generation, int) and not isinstance(current_generation, bool):
            floor = max(floor, current_generation)
        if generation < floor:
            return False
        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self._capacity:
            self._seen.discard(self._order.popleft())
        self.highest_generation = max(self.highest_generation, generation)
        return True

    def accept_monitor(
        self,
        payload: dict[str, Any],
        current_generation: int | None = None,
        current_revision: int | None = None,
        runtime_id: str | None = None,
    ) -> bool:
        payload_runtime = payload["runtime_id"]
        station_id = payload["station_id"]
        if (
            isinstance(runtime_id, str)
            and runtime_id
            and payload_runtime != runtime_id
        ):
            return False
        key = (payload_runtime, station_id)
        state = self._monitor_states.setdefault(
            key,
            {
                "seen": set(),
                "order": deque(),
                "generation": 0,
                "revision": 0,
            },
        )
        event_id = payload["event_id"]
        generation = payload["generation"]
        revision = payload["status_revision"]
        if event_id in state["seen"]:
            return False
        if (
            isinstance(current_generation, int)
            and not isinstance(current_generation, bool)
            and generation != current_generation
        ):
            return False
        if (
            isinstance(current_revision, int)
            and not isinstance(current_revision, bool)
            and generation == current_generation
            and revision < current_revision
        ):
            return False
        if generation < state["generation"]:
            return False
        if (
            generation == state["generation"]
            and revision <= state["revision"]
        ):
            return False
        state["seen"].add(event_id)
        state["order"].append(event_id)
        while len(state["order"]) > self._capacity:
            state["seen"].discard(state["order"].popleft())
        if generation > state["generation"]:
            state["generation"] = generation
            state["revision"] = revision
        else:
            state["revision"] = max(state["revision"], revision)
        return True


def _monitor_session(
    status: dict[str, Any], station_id: str
) -> dict[str, Any] | None:
    media = status.get("media")
    if not isinstance(media, dict):
        return None
    sessions = media.get("sessions")
    if isinstance(sessions, list):
        for session in sessions:
            if isinstance(session, dict) and session.get("station_id") == station_id:
                return session
        return None
    # Older bridge replies expose one media snapshot. Keep that compatibility
    # path only when it explicitly identifies the same station, or has no
    # station identity at all.
    if media.get("station_id") not in (None, station_id):
        return None
    return media


async def process_event(
    payload: Any,
    client: Any,
    gate: EventGate,
    dispatch,
    ring_state,
    station_ids: tuple[str, ...] | None = None,
    monitor: Any | None = None,
    sync_monitor: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    """Refresh status, gate an event, and dispatch it in a deterministic order."""
    try:
        event = validate_event(payload)
    except (TypeError, ValueError):
        return 400, {"error": "invalid Doorfast event"}
    try:
        await client.refresh()
    except Exception:
        client.online = False
        return 503, {"error": "Doorfast status unavailable"}
    if event["event"] in MONITOR_EVENT_NAMES:
        station_id = event["station_id"]
        if station_ids is not None and station_id not in station_ids:
            return 202, {"status": "ignored", "reason": "duplicate_or_stale"}
        coordinator = None
        if monitor is not None:
            try:
                coordinator = monitor.monitor(station_id)
            except KeyError:
                return 202, {"status": "ignored", "reason": "duplicate_or_stale"}
        media = _monitor_session(client.status, station_id)
        current_generation = (
            getattr(coordinator, "generation", None)
            if coordinator is not None
            else (media.get("generation") if isinstance(media, dict) else None)
        )
        current_revision = (
            getattr(coordinator, "status_revision", None)
            if coordinator is not None
            else (media.get("status_revision") if isinstance(media, dict) else None)
        )
        current_runtime = (
            getattr(coordinator, "runtime_id", None)
            if coordinator is not None
            else client.status.get("runtime_id")
        )
        accepted = gate.accept_monitor(
            event,
            current_generation if isinstance(current_generation, int) and not isinstance(current_generation, bool) else None,
            current_revision if isinstance(current_revision, int) and not isinstance(current_revision, bool) else None,
            current_runtime if isinstance(current_runtime, str) else None,
        )
    else:
        call = client.status.get("call")
        current = call.get("generation") if isinstance(call, dict) else None
        accepted = gate.accept(
            event,
            current if isinstance(current, int) and not isinstance(current, bool) else None,
            client.status.get("runtime_id") if isinstance(client.status.get("runtime_id"), str) else None,
        )
    if not accepted:
        return 202, {"status": "ignored", "reason": "duplicate_or_stale"}
    if event["event"] in MONITOR_EVENT_NAMES:
        if sync_monitor is not None:
            try:
                await sync_monitor(event["station_id"], event)
            except Exception:
                client.online = False
                return 503, {"error": "Doorfast status unavailable"}
        client.status["media_event"] = event
    else:
        client.status["event"] = event["event"]
        client.status["event_id"] = event["event_id"]
        client.status["event_generation"] = event["generation"]
    dispatch("status", client.status)
    dispatch("latest_event", event)
    dispatch("ring_status", bool(ring_state(client.status)))
    return 200, {"status": "success"}
