"""Revision-aware station registry owned by one Doorfast config entry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .client_types import DoorfastStation, DoorfastStationSnapshot
from .monitor import MonitorCoordinator


StationListener = Callable[[str, str], None]
MonitorFactory = Callable[[Any, str, DoorfastStation], MonitorCoordinator]


def _default_monitor_factory(
    client: Any, runtime_id: str, station: DoorfastStation
) -> MonitorCoordinator:
    return MonitorCoordinator(client, runtime_id, station.station_id)


class StationRegistryCoordinator:
    """Own validated station snapshots and their monitor coordinators."""

    def __init__(
        self,
        client: Any,
        *,
        entry_id: str,
        monitor_factory: MonitorFactory = _default_monitor_factory,
    ) -> None:
        self.client = client
        self.entry_id = entry_id
        self._monitor_factory = monitor_factory
        self._snapshot: DoorfastStationSnapshot | None = None
        self._stations: dict[str, DoorfastStation] = {}
        self._monitors: dict[str, MonitorCoordinator] = {}
        self._listeners: set[StationListener] = set()
        self._closed = False

    @property
    def snapshot(self) -> DoorfastStationSnapshot | None:
        return self._snapshot

    @property
    def station_ids(self) -> tuple[str, ...]:
        return tuple(self._stations)

    def station(self, station_id: str) -> DoorfastStation:
        return self._stations[station_id]

    def monitor(self, station_id: str) -> MonitorCoordinator:
        return self._monitors[station_id]

    def add_listener(self, listener: StationListener) -> Callable[[], None]:
        if self._closed:
            raise RuntimeError("station registry is closed")
        self._listeners.add(listener)

        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    def _notify(self, event: str, station_id: str) -> None:
        for listener in tuple(self._listeners):
            listener(event, station_id)

    def _new_monitor(
        self, runtime_id: str, station: DoorfastStation
    ) -> MonitorCoordinator:
        return self._monitor_factory(self.client, runtime_id, station)

    async def _remove(self, station_id: str) -> None:
        monitor = self._monitors[station_id]
        await monitor.async_close()
        self._monitors.pop(station_id)
        self._stations.pop(station_id, None)
        self._notify("removed", station_id)

    async def _replace_runtime(self, snapshot: DoorfastStationSnapshot) -> None:
        for station_id in tuple(self._stations):
            await self._remove(station_id)
        for station in snapshot.stations:
            if not station.enabled:
                continue
            monitor = self._new_monitor(snapshot.runtime_id, station)
            self._stations[station.station_id] = station
            self._monitors[station.station_id] = monitor
            self._notify("added", station.station_id)

    async def async_refresh(self) -> bool:
        if self._closed:
            raise RuntimeError("station registry is closed")
        snapshot = await self.client.stations()
        current = self._snapshot
        if current is not None and (
            current.runtime_id,
            current.revision,
        ) == (snapshot.runtime_id, snapshot.revision):
            return False

        if current is None or current.runtime_id != snapshot.runtime_id:
            await self._replace_runtime(snapshot)
            self._snapshot = snapshot
            return True

        incoming = {
            station.station_id: station
            for station in snapshot.stations
            if station.enabled
        }
        for station_id in tuple(self._stations):
            if station_id not in incoming:
                await self._remove(station_id)

        for station in snapshot.stations:
            if not station.enabled:
                continue
            previous = self._stations.get(station.station_id)
            if previous is None:
                monitor = self._new_monitor(snapshot.runtime_id, station)
                self._stations[station.station_id] = station
                self._monitors[station.station_id] = monitor
                self._notify("added", station.station_id)
            elif previous != station:
                self._stations[station.station_id] = station
                self._notify("updated", station.station_id)

        self._snapshot = snapshot
        return True

    async def async_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        first_error: Exception | None = None
        for monitor in tuple(self._monitors.values()):
            try:
                await monitor.async_close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        self._monitors.clear()
        self._stations.clear()
        self._listeners.clear()
        self._snapshot = None
        if first_error is not None:
            raise first_error
