"""Doorfast active monitor lifecycle shared by camera and WebRTC paths."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .const import MONITOR_READY_TIMEOUT


_READY_STATES = frozenset(("publishing", "viewing"))
_IDLE_STATES = frozenset(("idle", "stopped", "failed", "unavailable"))


@dataclass(frozen=True)
class MonitorLease:
    generation: int
    lease_id: int
    epoch: int


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class _RetryAcquire(RuntimeError):
    """The generation changed during the first-frame registration window."""


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
        self._lifecycle_lock = asyncio.Lock()
        self._stop_task: asyncio.Task[None] | None = None
        self._generation: int | None = None
        self._recoverable_generation: int | None = None
        self._start_inflight = False
        self._state = "idle"
        self._ready = False
        self._next_lease_id = 0
        self._leases: dict[int, MonitorLease] = {}
        self._terminal_epoch = 0
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
        if self._generation is None:
            return 0
        return sum(lease.generation == self._generation for lease in self._leases.values())

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
            "viewer_count": self.viewer_count,
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

    def _set_response_locked(
        self, response: dict[str, Any], *, allow_generation_change: bool = False
    ) -> None:
        self._validate_identity(response, check_generation=not allow_generation_change)
        generation = self._response_generation(response)
        state = self._response_state(response)
        revision = response.get("status_revision", self._status_revision)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("monitor status revision must be a non-negative integer")
        self._generation = generation
        self._state = state
        self._ready = response.get("ready") is True or state in _READY_STATES
        self._status_revision = revision
        if state == "stopping":
            self._recoverable_generation = generation
        else:
            self._recoverable_generation = None
        self._status_event.set()

    def _validate_identity(
        self, payload: dict[str, Any], *, check_generation: bool = True
    ) -> None:
        runtime_id = payload.get("runtime_id")
        if runtime_id is not None and runtime_id != self.runtime_id:
            raise ValueError("monitor status runtime does not match coordinator")
        station_id = payload.get("station_id")
        if station_id is not None and station_id != self.station_id:
            raise ValueError("monitor status station does not match coordinator")
        generation = payload.get("generation")
        if check_generation and (
            self._generation is not None
            and generation not in (None, 0, self._generation)
        ):
            _positive_int(generation, "monitor generation")
            raise ValueError("monitor status generation does not match coordinator")

    async def async_start(self) -> int:
        """Start a generation unless one is already active."""
        async with self._lock:
            if self._generation is not None and self._state not in _IDLE_STATES:
                return self._generation
            request_epoch = self._terminal_epoch
            old_generation = self._generation or 0
        return await self._start_generation(request_epoch, old_generation)

    async def async_wait_ready(
        self, generation: int, timeout: float = MONITOR_READY_TIMEOUT
    ) -> None:
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

    async def _await_start_response(self) -> dict[str, Any]:
        """Keep an accepted start alive long enough to clean it up on cancel."""
        start_task = asyncio.Task(
            self._client.start_monitor(self.runtime_id, self.station_id),
            eager_start=True,
        )
        try:
            response = await asyncio.shield(start_task)
        except asyncio.CancelledError:
            response = await asyncio.shield(start_task)
            if not isinstance(response, dict):
                raise ValueError("Doorfast monitor start returned a non-object")
            generation = self._response_generation(response)
            await self._stop_remote(generation)
            raise
        if not isinstance(response, dict):
            raise ValueError("Doorfast monitor start returned a non-object")
        return response

    async def _start_generation(self, request_epoch: int, old_generation: int) -> int:
        """Start one generation while serializing remote lifecycle calls."""
        async with self._lifecycle_lock:
            async with self._lock:
                if self._unloaded or request_epoch != self._terminal_epoch:
                    raise RuntimeError("monitor request was terminated")
                follow_generation = (
                    self._generation
                    if self._generation is not None
                    and self._generation > old_generation
                    else None
                )
                if follow_generation is not None:
                    return follow_generation
                self._cancel_grace_locked()
                self._start_inflight = True

            try:
                response = await self._await_start_response()
            except BaseException:
                async with self._lock:
                    self._start_inflight = False
                raise
            accepted_generation = self._response_generation(response)

            async with self._lock:
                self._start_inflight = False
                terminated = (
                    self._unloaded or request_epoch != self._terminal_epoch
                )
                current_generation = self._generation
                if not terminated and (
                    (
                        current_generation is None
                        and self._recoverable_generation != accepted_generation
                    )
                    or current_generation == old_generation
                    or current_generation == accepted_generation
                ):
                    if (
                        current_generation == accepted_generation
                        and self._state == "stopping"
                    ):
                        return current_generation
                    if (
                        current_generation is None
                        and self._recoverable_generation == accepted_generation
                    ):
                        return accepted_generation
                    self._set_response_locked(
                        response, allow_generation_change=True
                    )
                    return accepted_generation
                if not terminated and current_generation is not None:
                    return current_generation

            # The service accepted a generation after a terminal event. Do not
            # adopt its response; clean up only the generation this request
            # started while the lifecycle lock still excludes other starts.
            await self._stop_remote(accepted_generation)
            raise RuntimeError("monitor request was terminated")

    async def _stop_remote(self, generation: int) -> dict[str, Any] | None:
        try:
            response = await self._client.stop_monitor(
                self.runtime_id, self.station_id, generation
            )
        except Exception as error:
            if getattr(error, "status", None) != 409:
                raise
            return None
        if response is not None and not isinstance(response, dict):
            raise ValueError("Doorfast monitor stop returned a non-object")
        return response

    async def _recover_generation(
        self, request_epoch: int, old_generation: int
    ) -> int:
        """Finish a recoverable stop, then share one newly started generation."""
        async with self._lifecycle_lock:
            async with self._lock:
                if self._unloaded or request_epoch != self._terminal_epoch:
                    raise RuntimeError("monitor request was terminated")
                follow_generation = (
                    self._generation
                    if self._generation is not None
                    and self._generation > old_generation
                    else None
                )
                stopping_generation = (
                    self._generation
                    if self._generation == old_generation
                    and self._state == "stopping"
                    else None
                )
                if follow_generation is not None:
                    return follow_generation

            if stopping_generation is not None:
                await self._stop_remote(stopping_generation)
                async with self._lock:
                    if (
                        self._unloaded
                        or request_epoch != self._terminal_epoch
                    ):
                        raise RuntimeError("monitor request was terminated")
                    if self._generation == stopping_generation:
                        self._generation = None
                        self._state = "idle"
                        self._ready = False
                        self._leases.clear()
                        self._status_event.set()

            async with self._lock:
                if self._unloaded or request_epoch != self._terminal_epoch:
                    raise RuntimeError("monitor request was terminated")
                follow_generation = (
                    self._generation
                    if self._generation is not None
                    and self._generation > old_generation
                    else None
                )
                if follow_generation is not None:
                    return follow_generation
                self._start_inflight = True

            try:
                response = await self._await_start_response()
            except BaseException:
                async with self._lock:
                    self._start_inflight = False
                raise
            accepted_generation = self._response_generation(response)
            async with self._lock:
                self._start_inflight = False
                terminated = (
                    self._unloaded or request_epoch != self._terminal_epoch
                )
                current_generation = self._generation
                if not terminated and (
                    (
                        current_generation is None
                        and self._recoverable_generation != accepted_generation
                    )
                    or current_generation == old_generation
                ):
                    self._set_response_locked(
                        response, allow_generation_change=True
                    )
                    return accepted_generation
                if not terminated and current_generation is not None:
                    return current_generation

            await self._stop_remote(accepted_generation)
            raise RuntimeError("monitor request was terminated")

    async def async_acquire_viewer(self) -> MonitorLease:
        """Register one HA viewer and return its generation lease."""
        async with self._lock:
            request_epoch = self._terminal_epoch
            old_generation = self._generation or self._recoverable_generation or 0

        while True:
            async with self._lock:
                if self._unloaded or request_epoch != self._terminal_epoch:
                    raise RuntimeError("monitor request was terminated")
                generation = self._generation
                state = self._state
                recoverable = self._recoverable_generation
            if generation is None or state in _IDLE_STATES:
                if recoverable is not None:
                    generation = await self._recover_generation(
                        request_epoch, old_generation
                    )
                else:
                    generation = await self._start_generation(
                        request_epoch, old_generation
                    )
            elif state == "stopping":
                generation = await self._recover_generation(
                    request_epoch, old_generation
                )

            try:
                await self.async_wait_ready(generation)
            except RuntimeError:
                async with self._lock:
                    if (
                        self._unloaded
                        or request_epoch != self._terminal_epoch
                    ):
                        raise RuntimeError("monitor request was terminated")
                    recoverable = self._recoverable_generation
                    if self._generation is not None and self._generation != generation:
                        if self._generation > generation:
                            generation = self._generation
                            continue
                    if recoverable not in (None, generation):
                        raise
                    if self._state == "stopping" or self._generation is None:
                        old_generation = max(old_generation, generation)
                        continue
                raise

            try:
                return await self._register_lease(generation, request_epoch)
            except _RetryAcquire:
                old_generation = max(old_generation, generation)
                continue

    async def _register_lease(
        self, generation: int, request_epoch: int
    ) -> MonitorLease:
        """Enable the Doorfast viewer edge and register one local lease."""
        async with self._lifecycle_lock:
            async with self._lock:
                if (
                    self._unloaded
                    or request_epoch != self._terminal_epoch
                ):
                    raise RuntimeError("monitor request was terminated")
                if (
                    self._generation != generation
                    or not self._ready
                    or self._state in _IDLE_STATES
                    or self._state == "stopping"
                ):
                    raise _RetryAcquire
                enable_viewer = self.viewer_count == 0

            if enable_viewer:
                try:
                    await self._client.set_monitor_viewer(
                        self.runtime_id, self.station_id, generation, True
                    )
                except Exception:
                    try:
                        await self._stop_remote(generation)
                    finally:
                        async with self._lock:
                            if self._generation == generation:
                                self._generation = None
                                self._state = "idle"
                                self._ready = False
                                self._leases.clear()
                                self._status_event.set()
                    raise

            async with self._lock:
                valid = (
                    not self._unloaded
                    and request_epoch == self._terminal_epoch
                    and self._generation == generation
                    and self._ready
                    and self._state not in _IDLE_STATES
                    and self._state != "stopping"
                )
            if not valid:
                if enable_viewer:
                    await self._client.set_monitor_viewer(
                        self.runtime_id, self.station_id, generation, False
                    )
                async with self._lock:
                    terminated = (
                        self._unloaded
                        or request_epoch != self._terminal_epoch
                    )
                if terminated:
                    raise RuntimeError("monitor request was terminated")
                raise _RetryAcquire
            async with self._lock:
                if (
                    self._unloaded
                    or request_epoch != self._terminal_epoch
                    or self._generation != generation
                    or not self._ready
                    or self._state in _IDLE_STATES
                    or self._state == "stopping"
                ):
                    raise _RetryAcquire
                self._next_lease_id += 1
                lease = MonitorLease(
                    generation, self._next_lease_id, self._terminal_epoch
                )
                self._leases[lease.lease_id] = lease
                return lease

    async def async_release_viewer(self, lease: MonitorLease) -> None:
        """Release one viewer and schedule a delayed stop at the last edge."""
        async with self._lock:
            current = self._leases.get(lease.lease_id)
            if current != lease:
                return
            del self._leases[lease.lease_id]
            generation = self._generation
            if self.viewer_count != 0 or generation is None or lease.generation != generation:
                return
            self._cancel_grace_locked()
        try:
            async with self._lifecycle_lock:
                await self._client.set_monitor_viewer(
                    self.runtime_id, self.station_id, generation, False
                )
        except Exception:
            async with self._lock:
                if self._generation == generation:
                    self._leases[lease.lease_id] = lease
            raise
        async with self._lock:
            if self._generation != generation or self.viewer_count != 0:
                return
            self._stop_task = asyncio.create_task(
                self._stop_after_grace(generation)
            )

    async def _stop_after_grace(self, generation: int) -> None:
        try:
            await asyncio.sleep(self._grace_seconds)
            await self._stop_generation(generation)
        except asyncio.CancelledError:
            return
        finally:
            if self._stop_task is asyncio.current_task():
                self._stop_task = None

    async def _stop_generation(
        self, generation: int | None = None, *, terminal: bool = False
    ) -> None:
        async with self._lifecycle_lock:
            async with self._lock:
                active_generation = self._generation
                if active_generation is None:
                    self._state = "idle"
                    self._ready = False
                    self._leases.clear()
                    self._status_event.set()
                    return
                if generation is not None and generation != active_generation:
                    return
                self._cancel_grace_locked()
                stop_epoch = self._terminal_epoch
            response = None
            try:
                response = await self._stop_remote(active_generation)
            finally:
                async with self._lock:
                    if self._generation == active_generation:
                        if (
                            not terminal
                            and stop_epoch == self._terminal_epoch
                            and isinstance(response, dict)
                        ):
                            self._set_response_locked(
                                response, allow_generation_change=True
                            )
                        self._generation = None
                        self._state = "idle"
                        self._ready = False
                        self._leases.clear()
                        self._status_event.set()

    async def async_stop(self) -> None:
        """Stop the current generation immediately and clear local state."""
        generation = self._begin_terminal()
        await self._stop_generation(generation, terminal=True)

    async def async_preempt(self) -> None:
        """Stop preview immediately when a call or a newer generation wins."""
        generation = self._begin_terminal()
        await self._stop_generation(generation, terminal=True)

    async def async_unload(self) -> None:
        """Stop once during config-entry unload; subsequent calls are no-ops."""
        generation = self._begin_terminal(unload=True)
        if generation is not None:
            await self._stop_generation(generation, terminal=True)

    async def async_close(self) -> None:
        """Compatibility name used by config-entry cleanup."""
        await self.async_unload()

    def _begin_terminal(self, *, unload: bool = False) -> int | None:
        """Mark termination synchronously before waiting for lifecycle state."""
        if unload and self._unloaded:
            return None
        generation = self._generation or self._recoverable_generation
        self._terminal_epoch += 1
        self._recoverable_generation = None
        if unload:
            self._unloaded = True
        return generation

    def lease_active(self, lease: MonitorLease) -> bool:
        return (
            self._leases.get(lease.lease_id) == lease
            and self._generation == lease.generation
            and lease.epoch == self._terminal_epoch
            and self._ready
        )

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
        event = payload.get("event")
        state = merged.get("state")
        self._validate_identity(merged, check_generation=False)
        generation = merged.get("generation")
        if generation not in (None, 0):
            _positive_int(generation, "monitor generation")
        has_revision = "status_revision" in merged
        revision = merged.get("status_revision", self._status_revision)
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise ValueError(
                "monitor status revision must be a non-negative integer"
            )
        if has_revision and revision <= self._status_revision:
            return
        current_generation = self._generation or self._recoverable_generation
        if (
            current_generation is not None
            and generation not in (None, 0, current_generation)
        ):
            raise ValueError("monitor status generation does not match coordinator")

        terminal_event = event in {"monitor_preempted", "monitor_failed"}
        if state == "failed":
            terminal_event = True
        if (
            terminal_event
            and current_generation is None
            and self._start_inflight
            and generation not in (None, 0)
        ):
            # The remote start may already have accepted this generation even
            # though its HTTP response has not returned locally yet.
            current_generation = generation
        terminal_target = (
            current_generation
            if terminal_event
            and current_generation is not None
            and generation in (None, 0, current_generation)
            else None
        )
        if terminal_target is not None:
            # This mutation intentionally happens before the first await. A
            # recovery request that is waiting on a lifecycle HTTP call must
            # observe the terminal epoch and stop before starting a new one.
            if not has_revision or revision > self._status_revision:
                self._terminal_epoch += 1
                self._recoverable_generation = None

        async with self._lock:
            if has_revision and revision <= self._status_revision:
                return
            current_generation = self._generation or self._recoverable_generation
            if (
                current_generation is not None
                and generation not in (None, 0, current_generation)
            ):
                raise ValueError("monitor status generation does not match coordinator")
            if event in {"monitor_preempted", "monitor_stopped"}:
                self._cancel_grace_locked()
                self._generation = None
                self._state = "idle"
                self._ready = False
                self._leases.clear()
                if event == "monitor_stopped" and current_generation is not None:
                    self._recoverable_generation = current_generation
                else:
                    self._recoverable_generation = None
                self._status_revision = revision
                self._status_event.set()
                return
            if event == "monitor_failed":
                self._cancel_grace_locked()
                self._generation = None
                self._state = "failed"
                self._ready = False
                self._leases.clear()
                self._recoverable_generation = None
                self._status_revision = revision
                self._status_event.set()
                return
            if state in _IDLE_STATES:
                self._cancel_grace_locked()
                self._generation = None
                self._state = (
                    state if state in {"failed", "unavailable"} else "idle"
                )
                self._ready = False
                self._leases.clear()
                if state == "stopped" and current_generation is not None:
                    self._recoverable_generation = current_generation
                else:
                    self._recoverable_generation = None
                self._status_revision = revision
                self._status_event.set()
                return
            self._set_response_locked(merged)


DoorfastMonitorCoordinator = MonitorCoordinator
