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
        status_revision = payload.get("status_revision")
        if (
            isinstance(status_revision, bool)
            or not isinstance(status_revision, int)
            or status_revision <= 0
        ):
            raise ValueError("invalid status_revision")
        normalized["status_revision"] = status_revision
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
        self._monitor_seen: set[int] = set()
        self._monitor_order: deque[int] = deque()
        self.highest_monitor_generation = 0
        self.highest_monitor_revision = 0
        self.monitor_runtime_id: str | None = None

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
        if isinstance(runtime_id, str) and runtime_id:
            if self.monitor_runtime_id != runtime_id:
                self._monitor_seen.clear()
                self._monitor_order.clear()
                self.highest_monitor_generation = 0
                self.highest_monitor_revision = 0
                self.monitor_runtime_id = runtime_id
        event_id = payload["event_id"]
        generation = payload["generation"]
        revision = payload["status_revision"]
        if event_id in self._monitor_seen:
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
        if generation < self.highest_monitor_generation:
            return False
        if (
            generation == self.highest_monitor_generation
            and revision <= self.highest_monitor_revision
        ):
            return False
        self._monitor_seen.add(event_id)
        self._monitor_order.append(event_id)
        while len(self._monitor_order) > self._capacity:
            self._monitor_seen.discard(self._monitor_order.popleft())
        if generation > self.highest_monitor_generation:
            self.highest_monitor_generation = generation
            self.highest_monitor_revision = revision
        else:
            self.highest_monitor_revision = max(
                self.highest_monitor_revision, revision
            )
        return True


async def process_event(payload: Any, client: Any, gate: EventGate, dispatch, ring_state) -> tuple[int, dict[str, Any]]:
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
        media = client.status.get("media")
        current_generation = media.get("generation") if isinstance(media, dict) else None
        current_revision = media.get("status_revision") if isinstance(media, dict) else None
        accepted = gate.accept_monitor(
            event,
            current_generation if isinstance(current_generation, int) and not isinstance(current_generation, bool) else None,
            current_revision if isinstance(current_revision, int) and not isinstance(current_revision, bool) else None,
            client.status.get("runtime_id") if isinstance(client.status.get("runtime_id"), str) else None,
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
        client.status["media_event"] = event
    else:
        client.status["event"] = event["event"]
        client.status["event_id"] = event["event_id"]
        client.status["event_generation"] = event["generation"]
    dispatch("status", client.status)
    dispatch("latest_event", event)
    dispatch("ring_status", bool(ring_state(client.status)))
    return 200, {"status": "success"}
