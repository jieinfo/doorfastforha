"""Validation and ordering for Doorfast push events."""
from __future__ import annotations
from collections import deque
from typing import Any

EVENT_NAMES = frozenset({"incoming_call", "call_established", "hangup", "timeout", "preempted"})
SCHEMA_VERSION = 1


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
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event": event,
        "generation": generation,
        "timestamp_ms": timestamp_ms,
    }


class EventGate:
    """Suppress duplicate and stale generations from a push transport."""

    def __init__(self, capacity: int = 128) -> None:
        self._seen: set[tuple[int, str]] = set()
        self._capacity = capacity
        self._order: deque[tuple[int, str]] = deque()
        self.highest_generation = 0

    def accept(self, payload: dict[str, Any], current_generation: int | None = None) -> bool:
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
    call = client.status.get("call")
    current = call.get("generation") if isinstance(call, dict) else None
    if not gate.accept(event, current if isinstance(current, int) and not isinstance(current, bool) else None):
        return 202, {"status": "ignored", "reason": "duplicate_or_stale"}
    client.status["event"] = event["event"]
    client.status["event_id"] = event["event_id"]
    client.status["event_generation"] = event["generation"]
    dispatch("status", client.status)
    dispatch("latest_event", event)
    dispatch("ring_status", bool(ring_state(client.status)))
    return 200, {"status": "success"}
