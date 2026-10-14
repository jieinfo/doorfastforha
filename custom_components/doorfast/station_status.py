"""Station-scoped presentation of authoritative bridge status, not leases."""

from __future__ import annotations

from typing import Any

from .session_diagnostics import station_diagnostics


PREVIEW_PHASES = (
    "idle", "requesting", "awaiting_video", "live", "reconnecting",
    "in_call", "stopping", "preempted", "failed",
)


def preview_attributes(status: dict[str, Any], station_id: str) -> dict[str, Any]:
    """Keep publisher readiness separate from a real live source.

    A missing source_live flag from an older bridge is unknown, not live.
    No fallback to the first/default station or exposure of arbitrary fields.
    """
    attributes: dict[str, Any] = {
        "phase": None, "source_live": None, **station_diagnostics(status, station_id),
    }
    media = status.get("media")
    if not isinstance(media, dict) or media.get("available") is False:
        return attributes
    sessions = media.get("sessions")
    if not isinstance(sessions, list):
        return attributes
    if any(not isinstance(item, dict) for item in sessions):
        return attributes
    matches = [item for item in sessions if item.get("station_id") == station_id]
    if not matches:
        attributes["phase"] = "idle"
        # Capacity preemption replaces the victim's slot with the call, so
        # there is no retained victim session to inspect. The marker itself
        # is historical: only an active call makes it a current preemption.
        generation = media.get("preempted_generation")
        if (
            media.get("preempted_station_id") == station_id
            and type(generation) is int and 0 < generation < (1 << 64)
            and any(item.get("purpose") == "call" and item.get("state") in (
                "requesting", "awaiting_video", "publishing", "viewing",
            ) for item in sessions)
        ):
            attributes.update(phase="preempted", source_live=False,
                              preempted_generation=generation)
        return attributes
    if len(matches) != 1:
        return attributes
    session = matches[0]
    state = session.get("state")
    live = session.get("source_live")
    if isinstance(live, bool):
        attributes["source_live"] = live
    if state in ("idle", "stopping", "preempted", "failed"):
        attributes.update(phase=state, source_live=False)
    elif session.get("purpose") == "call":
        attributes["phase"] = "in_call"
    elif state in ("requesting", "queued", "awaiting_video"):
        attributes["phase"] = "requesting" if state == "queued" else state
    elif state in ("publishing", "viewing") and session.get("ready") is True:
        if live is True:
            attributes["phase"] = "live"
        elif live is False:
            attributes["phase"] = "reconnecting"
    for field in (
        "generation", "started_ms", "confirmed_ms", "first_frame_ms",
        "publication_ms", "control_requests", "busy_replies", "queue_drops",
        "last_error",
    ):
        value = session.get(field)
        if type(value) is int and 0 <= value < (1 << 64):
            attributes[field] = value
    started = attributes.get("started_ms")
    first = attributes.get("first_frame_ms")
    if started is not None and first is not None and first > 0 and first >= started:
        attributes["first_frame_latency_ms"] = first - started
    return attributes
