from __future__ import annotations
import json
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlsplit
import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import DEFAULT_AUDIO_PORT, DEFAULT_CALL_DURATION, DEFAULT_VIDEO_PORT
from .generation import resolve_generation
from .client_types import (
    AudioChunk,
    AudioIdentity,
    AudioReadReply,
    AudioStreamError,
    DoorfastStation,
    DoorfastStationSnapshot,
    PcmHttpReply,
    require_station_id,
)
from .audio import decode_pcm_wav
from .station_control import positive_condition, validate_call_parameters, validate_reply, stable_error
from .session_diagnostics import station_call_operation, _STATION_CALL_STAGE_ORDER
from .access_status import station_unlock_record, validate_unlock_reply, PENDING_PHASES

_UINT64_MAX = (1 << 64) - 1


class DoorfastTransportError(RuntimeError):
    def __init__(self, code: str | None = None):
        super().__init__(code or "Doorfast request failed")
        self.code = code

class DoorfastClient:
    """Client for the Doorfast JSON bridge mapped to the local ubus API."""
    def __init__(self, hass, base_url: str):
        self.hass, self.base_url = hass, base_url.rstrip("/")
        self.session = async_get_clientsession(hass)
        self.status: dict[str, Any] = {}
        self.online = False
        self._video_generation: int | None = None
        self._video_etag: str | None = None
        self._video_frame: bytes | None = None
        self._audio_generation: int | None = None
        self._audio_revision: int | None = None
        self._audio_etag: str | None = None
        self._refresh_sequence = 0
        self._unlock_epochs: dict[str, int] = {}
        self._unlock_acceptance_epochs: dict[str, int] = {}
        self._station_control_epochs: dict[str, int] = {}
        self._status_control_epochs: dict[str, int] = {}
        self._accepted_station_calls: dict[str, tuple[str, int, int]] = {}
        self._preview_stop_fences: dict[str, tuple[str, int, tuple[str, int, int] | None]] = {}
        self.monitor_status_callback: Callable[[dict[str, Any]], None] | None = None
    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        async with self.session.request(method, f"{self.base_url}{path}", json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            station_control = (method == "POST" and path in {"/api/v1/call", "/api/v1/hangup"}
                               and isinstance(payload, dict) and "station_id" in payload)
            if station_control and response.status >= 400:
                try:
                    data = await response.json(content_type=None)
                    error = data.get("error") if isinstance(data, dict) else None
                    code = error.get("code") if isinstance(error, dict) else None
                except Exception:
                    code = None
                raise DoorfastTransportError(stable_error(DoorfastTransportError(code)))
            response.raise_for_status()
            data = await response.json(content_type=None)
            if not isinstance(data, dict): raise ValueError("Doorfast bridge returned a non-object response")
            return data
    async def _pcm_request(self, path: str, params: dict[str, str], headers: dict[str, str], body: bytes) -> PcmHttpReply:
        # Keep URL Basic authentication available on legacy HTTP bridges.
        if urlsplit(self.base_url).username is not None:
            headers = {key: value for key, value in headers.items() if key != "Authorization"}
        async with self.session.request(
            "POST",
            f"{self.base_url}{path}",
            params=params,
            headers=headers,
            data=body,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            payload = await response.json(content_type=None)
            if not isinstance(payload, dict):
                raise ValueError("Doorfast PCM bridge returned a non-object response")
            return PcmHttpReply(response.status, payload)

    async def pcm_session_open(self, runtime_id: str, generation: int) -> PcmHttpReply:
        return await self._pcm_request(
            "/api/v1/audio/session",
            {"runtime": runtime_id, "generation": str(generation)},
            {},
            b"",
        )

    async def pcm_submit(self, runtime_id: str, generation: int, sequence: int, session_token: str, body: bytes) -> PcmHttpReply:
        return await self._pcm_request(
            "/api/v1/audio/submit.pcm",
            {
                "runtime": runtime_id,
                "generation": str(generation),
                "sequence": str(sequence),
            },
            {
                "Content-Type": "application/octet-stream",
                "Authorization": f"DoorfastAudio {session_token}",
                "X-Doorfast-Audio-Session": session_token,
            },
            body,
        )

    async def pcm_session_end(self, runtime_id: str, generation: int, session_token: str) -> PcmHttpReply:
        return await self._pcm_request(
            "/api/v1/audio/session/end",
            {"runtime": runtime_id, "generation": str(generation)},
            {
                "Authorization": f"DoorfastAudio {session_token}",
                "X-Doorfast-Audio-Session": session_token,
            },
            b"",
        )

    async def refresh(self):
        self._refresh_sequence += 1
        refresh_sequence = self._refresh_sequence
        control_epochs = dict(getattr(self, "_station_control_epochs", {}))
        unlock_epochs = dict(getattr(self, "_unlock_epochs", {}))
        acceptance_epochs = dict(getattr(self, "_unlock_acceptance_epochs", {}))
        status = await self._request("GET", "/api/v1/status")
        accepted_during_fetch = {
            station for station, epoch in getattr(self, "_unlock_acceptance_epochs", {}).items()
            if epoch != acceptance_epochs.get(station)
        }
        # Fence the returned observation too, including the superseded-GET
        # path below; callers may dispatch that value independently.
        status = self._fence_unlock_status(status, accepted_during_fetch)
        if refresh_sequence != self._refresh_sequence:
            return status
        if unlock_epochs != getattr(self, "_unlock_epochs", {}):
            return self.status
        if (control_epochs != getattr(self, "_station_control_epochs", {})
                and status.get("runtime_id") == self.status.get("runtime_id")):
            # An observation begun before a newer station request cannot be
            # the shared authority for sensors, audio or coordinators.
            return self.status
        old_operation = station_call_operation(self.status)
        operation = station_call_operation(status)
        if (old_operation is not None and operation is not None
                and old_operation["runtime_id"] == operation["runtime_id"]
                and (operation["operation_id"] < old_operation["operation_id"] or (
                    operation["operation_id"] == old_operation["operation_id"]
                    and _STATION_CALL_STAGE_ORDER[operation["stage"]]
                        < _STATION_CALL_STAGE_ORDER[old_operation["stage"]]
                ))):
            return self.status
        previous_runtime_id = self.status.get("runtime_id")
        runtime_changed = (
            isinstance(previous_runtime_id, str)
            and previous_runtime_id
            and status.get("runtime_id") != previous_runtime_id
        )
        if runtime_changed:
            self._preview_stop_fences = {}
        media = status.get("media")
        if isinstance(media, dict):
            status = {**status, "media": self._fence_stopped_previews(status, media)}
        self.status = status
        self._status_control_epochs = control_epochs
        self.online = True
        if runtime_changed or self._current_video_generation() != self._video_generation:
            self._clear_video_cache()
        if runtime_changed or self._current_audio_generation() != self._audio_generation:
            self._clear_audio_cursor()
        return self.status

    def _current_runtime_id(self) -> str | None:
        runtime_id = self.status.get("runtime_id")
        if (
            not isinstance(runtime_id, str)
            or len(runtime_id) != 16
            or any(character not in "0123456789abcdef" for character in runtime_id)
        ):
            return None
        return runtime_id

    def _control_runtime_id(self) -> str:
        runtime_id = self._current_runtime_id()
        if runtime_id is None:
            raise ValueError("Doorfast status has no valid runtime_id")
        return runtime_id

    async def unlock(self, station_id: str | None = None):
        if station_id is None:
            call = self.status.get("call")
            station_id = call.get("station_id") if isinstance(call, dict) else None
        return await self.unlock_station(station_id)

    async def unlock_station(self, station_id: str):
        station_id = require_station_id(station_id)
        runtime = self._current_runtime_id()
        epochs = getattr(self, "_unlock_epochs", {})
        epoch = epochs.get(station_id, 0) + 1
        epochs[station_id] = epoch
        self._unlock_epochs = epochs
        started = monotonic()
        response = validate_unlock_reply(await self._request("POST", "/api/v1/unlock", {
            "station_id": station_id,
        }), runtime, station_id)
        duration = max(0, int((monotonic() - started) * 1000))
        if ("request_id" in response and self._unlock_epochs.get(station_id) == epoch
                and self._current_runtime_id() == runtime and runtime is not None):
            access = self.status.get("access")
            access = access if isinstance(access, dict) else {}
            row = {key: value for key, value in response.items() if key != "submitted"}
            row.update(physical_result_confirmed=False, request_to_acceptance_ms=duration)
            old = station_unlock_record(self.status, station_id)
            same_request = old is not None and old["request_id"] == row["request_id"]
            if old is None or self._unlock_record_is_newer(row, old) or same_request:
                if same_request and not self._unlock_record_is_newer(row, old):
                    row = {**old, "request_to_acceptance_ms": duration}
                rows = access.get("requests", [])
                rows = rows if isinstance(rows, list) else []
                rows = [item for item in rows if isinstance(item, dict) and item.get("station_id") != station_id]
                self.status = {**self.status, "access": {**access, "version": 1, "requests": [*rows, row]}}
                acceptances = getattr(self, "_unlock_acceptance_epochs", {})
                acceptances[station_id] = acceptances.get(station_id, 0) + 1
                self._unlock_acceptance_epochs = acceptances
        return response

    @staticmethod
    def _unlock_record_is_newer(candidate, previous):
        if candidate["request_id"] != previous["request_id"]:
            return candidate["request_id"] > previous["request_id"]
        order = lambda row: PENDING_PHASES.index(row["phase"]) if row["phase"] in PENDING_PHASES else 2
        return order(candidate) > order(previous)

    def _fence_unlock_status(self, status, accepted_during_fetch=()):
        """Fence independent unlock identities without relying on media revisions."""
        access = status.get("access")
        if (not isinstance(access, dict) or type(access.get("version")) is not int
                or access["version"] != 1 or status.get("runtime_id") != self.status.get("runtime_id")
                or not isinstance(access.get("requests"), list)):
            return status
        rows = list(access["requests"])
        if accepted_during_fetch and all(
                isinstance(row, dict) and isinstance(row.get("station_id"), str)
                and station_unlock_record(status, row["station_id"]) is not None for row in rows):
            # A valid observation captured before acceptance may omit that
            # station entirely. Preserve only acceptances newer than this GET,
            # never substitute cached success for malformed operation records.
            for station_id in sorted(accepted_during_fetch):
                if any(row["station_id"] == station_id for row in rows):
                    continue
                previous = station_unlock_record(self.status, station_id)
                if previous is not None:
                    rows.append(previous)
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("station_id"), str):
                continue
            station_id = row["station_id"]
            candidate = station_unlock_record(status, station_id)
            previous = station_unlock_record(self.status, station_id)
            if candidate is None or previous is None:
                continue
            if self._unlock_record_is_newer(previous, candidate):
                rows[index] = previous
            elif candidate["request_id"] == previous["request_id"] and "request_to_acceptance_ms" in previous:
                rows[index] = {**candidate, "request_to_acceptance_ms": previous["request_to_acceptance_ms"]}
        return {**status, "access": {**access, "requests": rows}}
    async def answer(self, generation=None, primary_media_port=DEFAULT_VIDEO_PORT, secondary_media_port=DEFAULT_AUDIO_PORT, duration_seconds=DEFAULT_CALL_DURATION):
        generation = resolve_generation(self.status, generation)
        return await self._request("POST", "/api/v1/answer", {"runtime_id": self._control_runtime_id(), "generation": generation, "primary_media_port": primary_media_port, "secondary_media_port": secondary_media_port, "duration_seconds": duration_seconds})
    async def hangup(self, generation=None, reason="ha"):
        generation = resolve_generation(self.status, generation)
        return await self._request("POST", "/api/v1/hangup", {"runtime_id": self._control_runtime_id(), "generation": generation, "reason": reason})
    async def call_station(self, station_id: str, primary_media_port=DEFAULT_VIDEO_PORT,
                           secondary_media_port=DEFAULT_AUDIO_PORT,
                           duration_seconds=DEFAULT_CALL_DURATION):
        station_id = require_station_id(station_id)
        validate_call_parameters(primary_media_port, secondary_media_port, duration_seconds)
        runtime_id = self._control_runtime_id()
        response = await self._request("POST", "/api/v1/call", {
            "runtime_id": runtime_id, "station_id": station_id,
        })
        response = validate_reply(response, runtime_id, station_id)
        if (getattr(self, "status", {}).get("runtime_id") == runtime_id
                and response.get("generation", 0) > 0
                and response.get("state") in {"preparing", "calling", "talking", "preview", "ringing"}):
            accepted = getattr(self, "_accepted_station_calls", {})
            previous = accepted.get(station_id)
            accepted[station_id] = (runtime_id, response["generation"], previous[2] + 1 if previous else 1)
            self._accepted_station_calls = accepted
        return response

    async def hangup_station(self, station_id: str, reason="ha", *, generation=None, operation_id=None):
        station_id = require_station_id(station_id)
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be non-empty text")
        runtime_id = self._control_runtime_id()
        payload = {"runtime_id": runtime_id, "station_id": station_id}
        for key, value in (("generation", generation), ("operation_id", operation_id)):
            if value is not None:
                payload[key] = positive_condition(value)
        response = await self._request("POST", "/api/v1/hangup", payload)
        response = validate_reply(response, runtime_id, station_id)
        for key in ("operation_id", "generation"):
            if key in payload and key in response and payload[key] != response[key]:
                raise ValueError("station hangup reply condition mismatch")
        return response
    async def call_elevator(self, direction="up"):
        if direction not in {"up", "down"}: raise ValueError("direction must be up or down")
        return await self._request("POST", "/api/v1/call_elevator", {"runtime_id": self._control_runtime_id(), "direction": direction})

    @staticmethod
    def _snapshot_runtime_id(value: Any) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 16
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("station snapshot has no valid runtime_id")
        return value

    @staticmethod
    def _snapshot_revision(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("station snapshot revision must be non-negative")
        return value

    async def stations(self) -> DoorfastStationSnapshot:
        payload = await self._request("GET", "/api/v1/stations")
        if not isinstance(payload, dict):
            raise ValueError("station snapshot must be an object")
        runtime_id = self._snapshot_runtime_id(payload.get("runtime_id"))
        revision = self._snapshot_revision(payload.get("revision"))
        raw_stations = payload.get("stations")
        if not isinstance(raw_stations, list):
            raise ValueError("station snapshot stations must be an array")
        stations = tuple(DoorfastStation.from_payload(raw) for raw in raw_stations)
        if len({station.station_id for station in stations}) != len(stations):
            raise ValueError("station snapshot contains duplicate station ids")
        if len({station.stream_name for station in stations}) != len(stations):
            raise ValueError("station snapshot contains duplicate station stream names")
        return DoorfastStationSnapshot(runtime_id, revision, stations)

    @staticmethod
    def _monitor_generation(generation: Any) -> int:
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            raise ValueError("monitor generation must be a positive integer")
        return generation

    async def start_monitor(
        self, runtime_id: str, station_id: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/api/v1/monitor/start",
            {"runtime_id": runtime_id, "station_id": require_station_id(station_id)},
        )
        # Reads begun before this accepted lifecycle edge cannot replace it.
        self._refresh_sequence = getattr(self, "_refresh_sequence", 0) + 1
        return response

    async def stop_monitor(
        self, runtime_id: str, station_id: str, generation: int
    ) -> dict[str, Any]:
        generation = self._monitor_generation(generation)
        station_id = require_station_id(station_id)
        accepted_call = getattr(self, "_accepted_station_calls", {}).get(station_id)
        response = await self._request(
            "POST",
            "/api/v1/monitor/stop",
            {
                "runtime_id": runtime_id,
                "station_id": station_id,
                "generation": generation,
            },
        )
        if (response.get("runtime_id", runtime_id) != runtime_id
                or response.get("station_id", station_id) != station_id
                or type(response.get("generation", generation)) is not int
                or response.get("generation", generation) != generation):
            raise ValueError("monitor stop reply identity mismatch")
        self._refresh_sequence = getattr(self, "_refresh_sequence", 0) + 1
        status = getattr(self, "status", {})
        if status.get("runtime_id") == runtime_id:
            fences = getattr(self, "_preview_stop_fences", {})
            fences[station_id] = (runtime_id, generation, accepted_call)
            self._preview_stop_fences = fences
            media = status.get("media")
            if isinstance(media, dict):
                sessions = media.get("sessions")
                if (isinstance(sessions, list) and media.get("available") is not False
                        and not self._call_owns_station(status, station_id)
                        and getattr(self, "_accepted_station_calls", {}).get(station_id) == accepted_call
                        and not any(isinstance(item, dict) and item.get("station_id") == station_id
                                    for item in sessions)):
                    # A start can be accepted before the first global sample.
                    # Expose only the acknowledged pending stop, never fake idle.
                    media = {**media, "sessions": [*sessions, {"station_id": station_id,
                        "generation": generation, "purpose": "preview", "state": "stopping",
                        "ready": False, "source_live": False}]}
                self.status = {**status, "media": self._fence_stopped_previews(status, media)}
            callback = getattr(self, "monitor_status_callback", None)
            if callback is not None:
                callback(self.status)
        return response

    @staticmethod
    def _call_owns_station(status, station_id, media_generation=None):
        native = status.get("call")
        operation = station_call_operation(status)
        if (operation is not None and operation["station_id"] == station_id
                and operation["stage"] in {"preparing", "calling", "talking", "ending"}):
            return media_generation is None or operation["media_generation"] == media_generation
        if not (isinstance(native, dict) and native.get("station_id") == station_id
                and native.get("session") in {"preview", "ringing", "talking", "ending"}
                and type(native.get("generation")) is int and 0 < native["generation"] <= _UINT64_MAX):
            return False
        if media_generation is None:
            return True
        # Native control generations are not public media generations. Incoming
        # calls borrow only their separately observed, matching media owner.
        media = status.get("media")
        sessions = media.get("sessions") if isinstance(media, dict) else None
        return isinstance(sessions, list) and any(
            isinstance(item, dict) and item.get("station_id") == station_id
            and item.get("generation") == media_generation and item.get("purpose") == "call"
            for item in sessions)

    def _fence_stopped_previews(self, status, media, *, ownership=False):
        """A stop ACK is pending, not proof of remote completion.

        Keep the exact stopped preview visible as stopping, but never let it
        reacquire local ownership. New native/control owners remain authoritative.
        """
        sessions = media.get("sessions")
        if not isinstance(sessions, list):
            return media
        fences = getattr(self, "_preview_stop_fences", {})
        rows = []
        for item in sessions:
            if not isinstance(item, dict):
                rows.append(item)
                continue
            station_id = item.get("station_id")
            fence = fences.get(station_id) if isinstance(station_id, str) else None
            identity = (status.get("runtime_id"), item.get("generation"))
            accepted = (getattr(self, "_accepted_station_calls", {}).get(station_id)
                        if fence is not None else None)
            borrowed = (accepted is not None and accepted != fence[2] and accepted[:2] == identity)
            if (fence is not None and fence[:2] == identity and not borrowed
                    and item.get("purpose") == "preview"
                    and not self._call_owns_station(status, station_id, item.get("generation"))):
                if ownership:
                    continue
                if item.get("state") not in {"idle", "stopped", "failed"}:
                    item = {**item, "state": "stopping", "ready": False, "source_live": False}
            rows.append(item)
        return {**media, "sessions": rows}

    async def set_monitor_viewer(
        self, runtime_id: str, station_id: str, generation: int, active: bool
    ) -> dict[str, Any]:
        generation = self._monitor_generation(generation)
        if not isinstance(active, bool):
            raise ValueError("monitor viewer active must be a boolean")
        return await self._request(
            "POST",
            "/api/v1/monitor/viewer",
            {
                "runtime_id": runtime_id,
                "station_id": require_station_id(station_id),
                "generation": generation,
                "active": active,
            },
        )

    async def monitor_status(self) -> dict[str, Any]:
        request_runtime = getattr(self, "status", {}).get("runtime_id")
        response = await self._request("GET", "/api/v1/monitor/status")
        # Older bridges omit this field. Bind their observation to the runtime
        # at request submission, never a runtime that appeared while awaiting it.
        if "runtime_id" not in response and isinstance(request_runtime, str):
            response = {**response, "runtime_id": request_runtime}
        status = getattr(self, "status", {})
        if response.get("runtime_id") == status.get("runtime_id"):
            return self._fence_stopped_previews(status, response, ownership=True)
        return response
    def _current_video_generation(self) -> int | None:
        call = self.status.get("call")
        video = self.status.get("video")
        if not isinstance(call, dict) or not isinstance(video, dict):
            return None
        call_generation = call.get("generation")
        video_generation = video.get("generation")
        if (
            video.get("ready") is not True
            or isinstance(call_generation, bool)
            or isinstance(video_generation, bool)
            or not isinstance(call_generation, int)
            or not isinstance(video_generation, int)
            or call_generation <= 0
            or video_generation != call_generation
        ):
            return None
        return call_generation

    def _clear_video_cache(self) -> None:
        self._video_generation = None
        self._video_etag = None
        self._video_frame = None

    def _video_request_is_current(self, runtime_id: str, generation: int) -> bool:
        return (
            self._current_runtime_id() == runtime_id
            and self._current_video_generation() == generation
        )

    def _clear_video_cache_for_request(
        self, runtime_id: str, generation: int
    ) -> None:
        if self._video_request_is_current(runtime_id, generation):
            self._clear_video_cache()

    def _current_audio_generation(self) -> int | None:
        call = self.status.get("call")
        audio = self.status.get("audio")
        if not isinstance(call, dict) or not isinstance(audio, dict):
            return None
        call_generation = call.get("generation")
        audio_generation = audio.get("generation")
        revision = audio.get("snapshot_packet_count")
        if (
            audio.get("snapshot_ready") is not True
            or isinstance(call_generation, bool)
            or isinstance(audio_generation, bool)
            or isinstance(revision, bool)
            or not isinstance(call_generation, int)
            or not isinstance(audio_generation, int)
            or not isinstance(revision, int)
            or call_generation <= 0
            or revision <= 0
            or audio_generation != call_generation
        ):
            return None
        return call_generation

    def _clear_audio_cursor(self) -> None:
        self._audio_generation = None
        self._audio_revision = None
        self._audio_etag = None

    def _audio_request_is_current(
        self, runtime_id: str, generation: int, previous_revision: int | None
    ) -> bool:
        if (
            self._current_runtime_id() != runtime_id
            or self._current_audio_generation() != generation
        ):
            return False
        if previous_revision is None:
            return self._audio_generation is None and self._audio_revision is None
        return (
            self._audio_generation == generation
            and self._audio_revision == previous_revision
        )

    def _clear_audio_cursor_for_request(
        self, runtime_id: str, generation: int, previous_revision: int | None
    ) -> None:
        if self._audio_request_is_current(
            runtime_id, generation, previous_revision
        ):
            self._clear_audio_cursor()

    @staticmethod
    def _audio_header_int(headers, name: str) -> int | None:
        raw = headers.get(name)
        if (
            not isinstance(raw, str)
            or not raw.isascii()
            or not raw.isdigit()
        ):
            return None
        return int(raw)

    async def latest_video_frame(self) -> bytes | None:
        runtime_id = self._current_runtime_id()
        generation = self._current_video_generation()
        if runtime_id is None or generation is None:
            self._clear_video_cache()
            return None
        headers = {}
        if self._video_generation == generation and self._video_etag is not None:
            headers["If-None-Match"] = self._video_etag
        async with self.session.get(
            f"{self.base_url}/api/v1/video/latest.jpg",
            params={"generation": generation},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status == 304:
                return (
                    self._video_frame
                    if self._video_request_is_current(runtime_id, generation)
                    and self._video_generation == generation
                    else None
                )
            if response.status in {404, 409}:
                self._clear_video_cache_for_request(runtime_id, generation)
                return None
            if response.status == 503:
                return (
                    self._video_frame
                    if self._video_request_is_current(runtime_id, generation)
                    and self._video_generation == generation
                    else None
                )
            response.raise_for_status()
            response_generation = response.headers.get("X-Doorfast-Generation")
            if response_generation != str(generation):
                self._clear_video_cache_for_request(runtime_id, generation)
                return None
            frame = await response.read()
            if (
                not frame
                or not self._video_request_is_current(runtime_id, generation)
            ):
                self._clear_video_cache_for_request(runtime_id, generation)
                return None
            self._video_generation = generation
            self._video_etag = response.headers.get("ETag")
            self._video_frame = frame
            return frame

    async def latest_audio_chunk(self) -> bytes | None:
        runtime_id = self._current_runtime_id()
        generation = self._current_audio_generation()
        if runtime_id is None or generation is None:
            self._clear_audio_cursor()
            return None
        latest_revision = self.status["audio"]["snapshot_packet_count"]
        params = {"generation": generation}
        headers = {}
        expected_previous = None
        if self._audio_generation == generation and self._audio_revision is not None:
            expected_previous = self._audio_revision
            params["after"] = expected_previous
            if self._audio_etag is not None:
                headers["If-None-Match"] = self._audio_etag
        async with self.session.get(
            f"{self.base_url}/api/v1/audio/latest.wav",
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status == 304:
                return None
            if response.status in {404, 409}:
                self._clear_audio_cursor_for_request(
                    runtime_id, generation, expected_previous
                )
                return None
            if response.status == 503:
                return None
            response.raise_for_status()
            response_generation = self._audio_header_int(
                response.headers, "X-Doorfast-Generation"
            )
            previous_revision = self._audio_header_int(
                response.headers, "X-Doorfast-Audio-Previous-Revision"
            )
            revision = self._audio_header_int(
                response.headers, "X-Doorfast-Audio-Revision"
            )
            chunk = await response.read()
            if (
                response_generation != generation
                or previous_revision is None
                or revision is None
                or revision <= previous_revision
                or (
                    expected_previous is None
                    and revision < latest_revision
                )
                or (
                    expected_previous is not None
                    and previous_revision != expected_previous
                )
                or not chunk
                or not self._audio_request_is_current(
                    runtime_id, generation, expected_previous
                )
            ):
                self._clear_audio_cursor_for_request(
                    runtime_id, generation, expected_previous
                )
                return None
            self._audio_generation = generation
            self._audio_revision = revision
            expected_etag = f'"df-audio-{generation}-{revision}"'
            response_etag = response.headers.get("ETag")
            self._audio_etag = (
                response_etag if response_etag == expected_etag else None
            )
            return chunk

    @staticmethod
    def _audio_response_identity(headers, identity: AudioIdentity) -> None:
        if (
            headers.get("X-Doorfast-Runtime-Id") != identity.runtime_id
            or headers.get("X-Doorfast-Station-Id") != identity.station_id
            or headers.get("X-Doorfast-Generation") != str(identity.generation)
        ):
            raise AudioStreamError("audio_identity_mismatch")

    @staticmethod
    def _audio_response_u64(headers, name: str) -> int:
        raw = headers.get(name)
        if (
            not isinstance(raw, str)
            or not raw.isascii()
            or not raw.isdigit()
        ):
            raise AudioStreamError("audio_invalid_response")
        value = int(raw)
        if value > _UINT64_MAX:
            raise AudioStreamError("audio_invalid_response")
        return value

    @staticmethod
    async def _bounded_audio_body(response) -> bytes:
        body = bytearray()
        while True:
            if len(body) == 4096:
                raise AudioStreamError("audio_invalid_response")
            part = await response.content.read(min(1024, 4096 - len(body)))
            if not isinstance(part, (bytes, bytearray)):
                raise AudioStreamError("audio_invalid_response")
            if not part:
                break
            body.extend(part)
            if len(body) > 4096:
                raise AudioStreamError("audio_invalid_response")
        return bytes(body)

    @staticmethod
    def _audio_error_code(body: bytes) -> str:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AudioStreamError("audio_invalid_response") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("code"), str):
            raise AudioStreamError("audio_invalid_response")
        return payload["code"]

    async def read_audio_chunk(
        self, identity: AudioIdentity, after: int | None = None
    ) -> AudioReadReply:
        """Read one independently-cursor'd station PCM chunk."""
        if not isinstance(identity, AudioIdentity):
            raise AudioStreamError("invalid_request")
        try:
            require_station_id(identity.station_id)
        except ValueError as error:
            raise AudioStreamError("audio_identity_mismatch") from error
        if (
            not isinstance(identity.runtime_id, str)
            or len(identity.runtime_id) != 16
            or any(character not in "0123456789abcdef" for character in identity.runtime_id)
            or isinstance(identity.generation, bool)
            or not isinstance(identity.generation, int)
            or not 1 <= identity.generation <= _UINT64_MAX
        ):
            raise AudioStreamError("audio_identity_mismatch")
        if after is not None and (
            isinstance(after, bool)
            or not isinstance(after, int)
            or not 0 <= after <= _UINT64_MAX
        ):
            raise AudioStreamError("invalid_request")
        params = {
            "runtime": identity.runtime_id,
            "station_id": identity.station_id,
            "generation": identity.generation,
        }
        if after is not None:
            params["after"] = after
        async with self.session.get(
            f"{self.base_url}/api/v1/audio/latest.wav",
            params=params,
            headers={},
            timeout=aiohttp.ClientTimeout(total=2),
        ) as response:
            if response.status == 304:
                self._audio_response_identity(response.headers, identity)
                return AudioReadReply("empty")

            body = await self._bounded_audio_body(response)
            if response.status == 400:
                code = self._audio_error_code(body)
                if code == "invalid_request":
                    raise AudioStreamError(code)
                raise AudioStreamError("audio_invalid_response")
            if response.status == 404:
                self._audio_response_identity(response.headers, identity)
                code = self._audio_error_code(body)
                if code == "audio_unavailable":
                    return AudioReadReply("empty")
                raise AudioStreamError("audio_invalid_response")
            if response.status == 409:
                code = self._audio_error_code(body)
                if code in {"audio_cursor_expired", "audio_identity_mismatch"}:
                    if code == "audio_cursor_expired":
                        return AudioReadReply("resync")
                    raise AudioStreamError(code)
                raise AudioStreamError("audio_invalid_response")
            if response.status == 503:
                code = self._audio_error_code(body)
                if code == "audio_temporarily_unavailable":
                    return AudioReadReply("retry")
                raise AudioStreamError("audio_invalid_response")
            if response.status != 200:
                raise AudioStreamError("audio_invalid_response")

            self._audio_response_identity(response.headers, identity)
            previous_revision = self._audio_response_u64(
                response.headers, "X-Doorfast-Audio-Previous-Revision"
            )
            revision = self._audio_response_u64(
                response.headers, "X-Doorfast-Audio-Revision"
            )
            if revision <= previous_revision or (
                after is not None and previous_revision != after
            ):
                raise AudioStreamError("audio_invalid_response")
            dropped_samples = self._audio_response_u64(
                response.headers, "X-Doorfast-Audio-Dropped-Bytes"
            )
            discontinuity_raw = response.headers.get("X-Doorfast-Audio-Discontinuity")
            if discontinuity_raw not in {"0", "1"}:
                raise AudioStreamError("audio_invalid_response")
            timestamp_ms = self._audio_response_u64(
                response.headers, "X-Doorfast-Audio-Timestamp-Ms"
            )
            latest_timestamp_ms = self._audio_response_u64(
                response.headers, "X-Doorfast-Audio-Latest-Timestamp-Ms"
            )
            if timestamp_ms > latest_timestamp_ms:
                raise AudioStreamError("audio_invalid_response")
            if (
                response.headers.get("X-Doorfast-Audio-Format") != "s16le"
                or response.headers.get("X-Doorfast-Audio-Sample-Rate") != "8000"
                or response.headers.get("X-Doorfast-Audio-Channels") != "1"
            ):
                raise AudioStreamError("audio_invalid_response")
            pcm = decode_pcm_wav(body)
            return AudioReadReply(
                "chunk",
                AudioChunk(
                    identity=identity,
                    previous_revision=previous_revision,
                    revision=revision,
                    pcm=pcm,
                    timestamp_ms=timestamp_ms,
                    latest_timestamp_ms=latest_timestamp_ms,
                    dropped_samples=dropped_samples,
                    discontinuity=discontinuity_raw == "1",
                ),
            )

    @property
    def latest_audio_url(self) -> str:
        return f"{self.base_url}/api/v1/audio/latest.wav"
