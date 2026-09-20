"""Doorfast active monitor lifecycle shared by camera and WebRTC paths."""

from __future__ import annotations

import asyncio
from typing import Any


_READY_STATES = frozenset(("publishing", "viewing"))
_IDLE_STATES = frozenset(("idle", "stopped", "failed", "unavailable"))


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class MonitorCoordinator:
    """Own one Doorfast monitor generation and its HA viewer references.

    The Doorfast API has one viewer flag for a generation while HA can have
    several WebRTC sessions.  The coordinator converts those session edges to
    one start/stop lifecycle and delays the final stop to absorb reconnects.
    """

    def __init__(
        self,
        client: Any,
        runtime_id: str,
        station_id: str,
        grace_seconds: float = 15.0,
    ) -> None:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        self._client = client
        self.runtime_id = runtime_id
        self.station_id = station_id
        self._grace_seconds = grace_seconds
        self._lock = asyncio.Lock()
        self._stop_task: asyncio.Task[None] | None = None
        self._generation: int | None = None
        self._state = "idle"
        self._ready = False
        self._viewer_count = 0
        self._status_revision = 0
        self._unloaded = False
        self._status_event = asyncio.Event()

    @property
    def generation(self) -> int | None:
        return self._generation

    @property
    def state(self) -> str:
        return self._state

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def viewer_count(self) -> int:
        return self._viewer_count

    @property
    def status_revision(self) -> int:
        return self._status_revision

    @property
    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "station_id": self.station_id,
            "generation": self._generation,
            "state": self._state,
            "ready": self._ready,
            "viewer_count": self._viewer_count,
            "status_revision": self._status_revision,
        }

    @staticmethod
    def _response_generation(response: dict[str, Any]) -> int:
        return _positive_int(response.get("generation"), "monitor generation")

    @staticmethod
    def _response_state(response: dict[str, Any]) -> str:
        state = response.get("state")
        if not isinstance(state, str) or not state or len(state) > 32:
            raise ValueError("monitor state must be a non-empty string")
        return state

    def _cancel_grace_locked(self) -> None:
        current = asyncio.current_task()
        if self._stop_task is not None and self._stop_task is not current:
            self._stop_task.cancel()
        if self._stop_task is not current:
            self._stop_task = None

    def _set_response_locked(self, response: dict[str, Any]) -> None:
        self._validate_identity(response)
        generation = self._response_generation(response)
        state = self._response_state(response)
        revision = response.get("status_revision", self._status_revision)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("monitor status revision must be a non-negative integer")
        self._generation = generation
        self._state = state
        self._ready = response.get("ready") is True or state in _READY_STATES
        self._status_revision = revision
        self._status_event.set()

    def _validate_identity(self, payload: dict[str, Any]) -> None:
        runtime_id = payload.get("runtime_id")
        if runtime_id is not None and runtime_id != self.runtime_id:
            raise ValueError("monitor status runtime does not match coordinator")
        station_id = payload.get("station_id")
        if station_id is not None and station_id != self.station_id:
            raise ValueError("monitor status station does not match coordinator")
        generation = payload.get("generation")
        if (
            self._generation is not None
            and generation not in (None, 0, self._generation)
        ):
            _positive_int(generation, "monitor generation")
            raise ValueError("monitor status generation does not match coordinator")

    async def _start_locked(self) -> int:
        if self._unloaded:
            raise RuntimeError("monitor coordinator is unloaded")
        self._cancel_grace_locked()
        response = await self._client.start_monitor(
            self.runtime_id, self.station_id
        )
        if not isinstance(response, dict):
            raise ValueError("Doorfast monitor start returned a non-object")
        self._set_response_locked(response)
        return self._generation  # type: ignore[return-value]

    async def async_start(self) -> int:
        """Start a generation unless one is already active."""
        async with self._lock:
            if self._generation is not None and self._state not in _IDLE_STATES:
                return self._generation
            return await self._start_locked()

    async def async_wait_ready(self, generation: int, timeout: float = 10.0) -> None:
        """Wait until the exact generation is publishing/viewing."""
        if self._generation == generation and self._ready:
            return
        await asyncio.wait_for(self._wait_ready(generation), timeout)

    async def _wait_ready(self, generation: int) -> None:
        while True:
            async with self._lock:
                if (
                    self._generation != generation
                    or self._state
                    in {"failed", "idle", "stopped", "stopping", "unavailable"}
                    or self._unloaded
                ):
                    raise RuntimeError("monitor generation is no longer ready")
                if self._ready:
                    return
                self._status_event.clear()
            await self._status_event.wait()

    async def async_acquire_viewer(self) -> int:
        """Register one HA viewer and return the active Doorfast generation."""
        async with self._lock:
            self._cancel_grace_locked()
            if self._generation is None or self._state in _IDLE_STATES:
                await self._start_locked()
            if self._state == "stopping":
                # A reconnect can arrive after Doorfast has accepted the
                # delayed stop but before its status event reaches HA. Finish
                # that old generation locally, then create a fresh one rather
                # than exposing the transient state to the WebRTC caller.
                await self._stop_locked(self._generation)
                await self._start_locked()
            generation = self._generation
            if generation is None:
                raise RuntimeError("monitor generation was not created")
        try:
            # Doorfast accepts the viewer edge only after the monitor reaches
            # publishing/viewing. Keep this wait outside the state lock so
            # relay or poll updates can advance the generation.
            await self.async_wait_ready(generation)
            async with self._lock:
                if (
                    self._generation != generation
                    or not self._ready
                    or self._state in _IDLE_STATES
                ):
                    raise RuntimeError("monitor generation is no longer ready")
                if self._viewer_count == 0:
                    try:
                        await self._client.set_monitor_viewer(
                            self.runtime_id, self.station_id, generation, True
                        )
                    except Exception:
                        await self._stop_locked(generation)
                        raise
                self._viewer_count += 1
                return generation
        except asyncio.CancelledError:
            async with self._lock:
                if (
                    self._generation == generation
                    and self._viewer_count == 0
                    and self._state not in _IDLE_STATES
                ):
                    await self._stop_locked(generation)
            raise
        except Exception:
            async with self._lock:
                if (
                    self._generation == generation
                    and self._viewer_count == 0
                    and self._state not in _IDLE_STATES
                ):
                    await self._stop_locked(generation)
            raise

    async def async_release_viewer(self) -> None:
        """Release one viewer and schedule a delayed stop at the last edge."""
        async with self._lock:
            if self._viewer_count == 0:
                return
            self._viewer_count -= 1
            generation = self._generation
            if self._viewer_count != 0 or generation is None:
                return
            try:
                await self._client.set_monitor_viewer(
                    self.runtime_id, self.station_id, generation, False
                )
            except Exception:
                self._viewer_count = 1
                raise
            self._cancel_grace_locked()
            self._stop_task = asyncio.create_task(
                self._stop_after_grace(generation)
            )

    async def _stop_after_grace(self, generation: int) -> None:
        try:
            await asyncio.sleep(self._grace_seconds)
            async with self._lock:
                if self._generation == generation and self._viewer_count == 0:
                    await self._stop_locked(generation)
        except asyncio.CancelledError:
            return
        finally:
            if self._stop_task is asyncio.current_task():
                self._stop_task = None

    async def _stop_locked(self, generation: int | None = None) -> None:
        if self._generation is None:
            self._state = "idle"
            self._ready = False
            self._viewer_count = 0
            self._status_event.set()
            return
        active_generation = self._generation
        if generation is not None and generation != active_generation:
            return
        self._cancel_grace_locked()
        try:
            try:
                response = await self._client.stop_monitor(
                    self.runtime_id, self.station_id, active_generation
                )
            except Exception as error:
                # Doorfast returns 409 when another lifecycle edge already
                # stopped this generation. For HA this is equivalent to a
                # successful idempotent stop; always clear local ownership.
                if getattr(error, "status", None) != 409:
                    raise
                response = None
            if isinstance(response, dict):
                self._set_response_locked(response)
        finally:
            self._generation = None
            self._state = "idle"
            self._ready = False
            self._viewer_count = 0
            self._status_event.set()

    async def async_stop(self) -> None:
        """Stop the current generation immediately and clear local state."""
        async with self._lock:
            await self._stop_locked()

    async def async_preempt(self) -> None:
        """Stop preview immediately when a call or a newer generation wins."""
        await self.async_stop()

    async def async_unload(self) -> None:
        """Stop once during config-entry unload; subsequent calls are no-ops."""
        async with self._lock:
            if self._unloaded:
                return
            self._unloaded = True
            await self._stop_locked()

    async def async_close(self) -> None:
        """Compatibility name used by config-entry cleanup."""
        await self.async_unload()

    async def async_apply_status(self, payload: dict[str, Any]) -> None:
        """Apply a validated monitor status or relay event snapshot."""
        if not isinstance(payload, dict):
            raise ValueError("monitor status must be an object")
        status = payload.get("status")
        if isinstance(status, dict):
            merged = dict(status)
            if "generation" in payload:
                merged["generation"] = payload["generation"]
            if "status_revision" in payload:
                merged["status_revision"] = payload["status_revision"]
        else:
            merged = dict(payload)
        for field in ("runtime_id", "station_id"):
            if field in payload:
                merged[field] = payload[field]
        self._validate_identity(merged)
        event = payload.get("event")
        if event in {"monitor_preempted", "monitor_stopped"}:
            async with self._lock:
                self._cancel_grace_locked()
                self._generation = None
                self._state = "idle"
                self._ready = False
                self._viewer_count = 0
                self._status_event.set()
            return
        if event == "monitor_failed":
            async with self._lock:
                self._cancel_grace_locked()
                self._generation = None
                self._state = "failed"
                self._ready = False
                self._viewer_count = 0
                self._status_event.set()
            return
        state = merged.get("state")
        if state in _IDLE_STATES:
            async with self._lock:
                generation = merged.get("generation")
                if generation not in (None, 0):
                    _positive_int(generation, "monitor generation")
                self._cancel_grace_locked()
                self._generation = None
                self._state = (
                    state if state in {"failed", "unavailable"} else "idle"
                )
                self._ready = False
                self._viewer_count = 0
                revision = merged.get("status_revision", self._status_revision)
                if isinstance(revision, int) and not isinstance(revision, bool):
                    self._status_revision = revision
                self._status_event.set()
            return
        async with self._lock:
            self._set_response_locked(merged)


DoorfastMonitorCoordinator = MonitorCoordinator
