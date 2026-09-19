"""Loopback-only deterministic Doorfast HTTP fixture for HA acceptance tests."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit


FRAME_BYTES = 320


@dataclass
class FixtureState:
    """Mutable state for one synthetic Doorfast host."""

    name: str
    runtime_id: str
    generation: int = 7
    call_session: str = "talking"
    audio_active: bool = True
    session_token: str | None = None
    next_sequence: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)
    audio_hashes: list[dict[str, Any]] = field(default_factory=list)
    stations_revision: int = 1
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": True,
                "mode": "passive",
                "runtime_id": self.runtime_id,
                "call": {"session": self.call_session, "generation": self.generation},
                "audio_tx": {"active": self.audio_active, "generation": self.generation},
                "audio": {"snapshot_ready": False, "generation": self.generation, "snapshot_packet_count": 0},
                "video": {"ready": False, "generation": self.generation},
            }

    def stations(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "revision": self.stations_revision,
            "stations": [
                {"id": "gate_main", "name": "Main gate", "logical_address": "32:00:00:00:00:01", "stream_name": "doorfast_one_main", "enabled": True, "route_source": "discovered", "route_fresh": True, "monitorable": True, "last_seen_ms": 1},
                {"id": "gate_side", "name": "Side gate", "logical_address": "32:00:00:00:00:02", "stream_name": "doorfast_one_side", "enabled": True, "route_source": "discovered", "route_fresh": True, "monitorable": True, "last_seen_ms": 1},
            ],
        }

    def monitor_status(self) -> dict[str, Any]:
        return {"runtime_id": self.runtime_id, "sessions": []}

    def set_generation(self, generation: int, *, talking: bool = True) -> None:
        if generation <= 0:
            raise ValueError("generation must be positive")
        with self.lock:
            self.generation = generation
            self.call_session = "talking" if talking else "idle"
            self.audio_active = talking
            self.session_token = None
            self.next_sequence = 0

    def hangup(self) -> None:
        with self.lock:
            self.call_session = "idle"
            self.audio_active = False
            self.session_token = None
            self.requests.append({"operation": "hangup", "generation": self.generation})


class _Handler(BaseHTTPRequestHandler):
    server: "FixtureHTTPServer"

    def log_message(self, *_: Any) -> None:
        return

    def _state_and_path(self) -> tuple[FixtureState | None, str]:
        parts = urlsplit(self.path)
        segments = PurePosixPath(parts.path).parts
        if len(segments) < 4 or segments[2] != "cgi-bin" or segments[3] != "doorfast":
            return None, ""
        state = self.server.states.get(segments[1])
        return state, "/" + "/".join(segments[4:])

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 8192:
            raise ValueError("request body too large")
        return self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802 - stdlib API name
        state, relative = self._state_and_path()
        if state is not None and relative == "/api/v1/status":
            self._send_json(200, state.status())
            return
        if state is not None and relative == "/api/v1/stations":
            self._send_json(200, state.stations())
            return
        if state is not None and relative == "/api/v1/monitor/status":
            self._send_json(200, state.monitor_status())
            return
        self._send_json(404, {"status": "error", "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib API name
        state, relative = self._state_and_path()
        if state is None:
            self._send_json(404, {"status": "error", "error": "not_found"})
            return
        parts = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
        try:
            body = self._read_body()
            if relative == "/api/v1/audio/session":
                self._open(state, query)
            elif relative == "/api/v1/audio/submit.pcm":
                self._submit(state, query, body)
            elif relative == "/api/v1/audio/session/end":
                self._end(state, query)
            elif relative == "/api/v1/hangup":
                state.hangup()
                self._send_json(200, {"status": "success", "accepted": True})
            elif relative in {"/api/v1/unlock", "/api/v1/answer", "/api/v1/call_elevator"}:
                with state.lock:
                    state.requests.append({"operation": relative.rsplit("/", 1)[-1], "generation": state.generation})
                self._send_json(200, {"status": "success", "accepted": True})
            else:
                self._send_json(404, {"status": "error", "error": "not_found"})
        except ValueError as error:
            self._send_json(400, {"status": "error", "error": "invalid_request", "message": str(error)})

    @staticmethod
    def _success(state: FixtureState, *, accepted: int, next_sequence: int, token: str = "", lease_ms: int = 0) -> dict[str, Any]:
        return {
            "status": "success",
            "runtime_id": state.runtime_id,
            "generation": state.generation,
            "accepted_frames": accepted,
            "next_sequence": next_sequence,
            "lease_ms": lease_ms,
            "audio_session": token,
        }

    def _open(self, state: FixtureState, query: dict[str, str]) -> None:
        with state.lock:
            if state.call_session != "talking" or not state.audio_active:
                self._send_json(409, {"status": "error", "error": "call_not_talking"})
                return
            if query.get("runtime") != state.runtime_id or query.get("generation") != str(state.generation):
                self._send_json(409, {"status": "error", "error": "generation_mismatch"})
                return
            if state.session_token is not None:
                self._send_json(409, {"status": "error", "error": "producer_busy"})
                return
            state.session_token = secrets.token_hex(16)
            state.next_sequence = 0
            self._send_json(200, self._success(state, accepted=0, next_sequence=0, token=state.session_token, lease_ms=2000))

    def _submit(self, state: FixtureState, query: dict[str, str], body: bytes) -> None:
        with state.lock:
            token = self.headers.get("X-Doorfast-Audio-Session")
            if state.session_token is None or token != state.session_token:
                self._send_json(409, {"status": "error", "error": "session_mismatch"})
                return
            if state.call_session != "talking" or not state.audio_active:
                self._send_json(409, {"status": "error", "error": "call_not_talking"})
                return
            if query.get("runtime") != state.runtime_id or query.get("generation") != str(state.generation):
                self._send_json(409, {"status": "error", "error": "generation_mismatch"})
                return
            try:
                sequence = int(query.get("sequence", "-1"))
            except ValueError:
                sequence = -1
            if sequence != state.next_sequence:
                self._send_json(409, {"status": "error", "error": "sequence_gap"})
                return
            if not body or len(body) % FRAME_BYTES or not FRAME_BYTES <= len(body) <= FRAME_BYTES * 5:
                self._send_json(400, {"status": "error", "error": "invalid_pcm"})
                return
            frames = len(body) // FRAME_BYTES
            state.audio_hashes.append({"generation": state.generation, "sequence": sequence, "frames": frames, "sha256": hashlib.sha256(body).hexdigest()})
            state.next_sequence += frames
            # The producer keeps the lease alive on every accepted submit.  A
            # zero lease is valid for session/end only; returning it here
            # makes HA reject an otherwise valid PCM response.
            self._send_json(200, self._success(state, accepted=frames, next_sequence=state.next_sequence, lease_ms=2000))

    def _end(self, state: FixtureState, query: dict[str, str]) -> None:
        with state.lock:
            token = self.headers.get("X-Doorfast-Audio-Session")
            if state.session_token is None or token != state.session_token:
                self._send_json(409, {"status": "error", "error": "session_mismatch"})
                return
            if query.get("runtime") != state.runtime_id or query.get("generation") != str(state.generation):
                self._send_json(409, {"status": "error", "error": "generation_mismatch"})
                return
            state.session_token = None
            self._send_json(200, self._success(state, accepted=0, next_sequence=state.next_sequence))


class FixtureHTTPServer(ThreadingHTTPServer):
    """Threaded loopback fixture with one isolated state per path component."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bind_host: str = "127.0.0.1") -> None:
        super().__init__((bind_host, 0), _Handler)
        self.states = {
            "one": FixtureState("one", "1111111111111111"),
            "two": FixtureState("two", "2222222222222222"),
        }
        self._thread: threading.Thread | None = None

    def base_url(self, host: str | None = None) -> str:
        return f"http://{host or self.server_address[0]}:{self.server_port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="doorfast-fixture", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "FixtureHTTPServer":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = ["FixtureHTTPServer", "FixtureState", "FRAME_BYTES"]
