"""Unified acceptance runner for Doorfast's HA PCM bridge."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .evidence import Evidence
from .fixture import FRAME_BYTES, FixtureHTTPServer
from .websocket import HACommandError, HAWebSocket, WebSocketError


DOORFASTFORHA_REF = "0bd2474"
DOORFAST_REF = "a5fcca3"
FRONTEND_FILES = {
    "doorfast-ptt-card.mjs": "DoorfastPttCard",
    "audio-stream.mjs": "PcmBatchSender",
    "doorfast-pcm-worklet.mjs": "doorfast-pcm-processor",
    "pcm-dsp.mjs": "FloatToPcm16Resampler",
}


class AcceptanceFailure(RuntimeError):
    pass


def _json_request(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, token: str | None = None) -> tuple[int, dict[str, Any] | bytes, dict[str, str]]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read(2 * 1024 * 1024)
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            if response_headers.get("content-type", "").startswith("application/json") and raw:
                return response.status, json.loads(raw.decode("utf-8")), response_headers
            return response.status, raw, response_headers
    except HTTPError as error:
        raw = error.read(2 * 1024 * 1024)
        try:
            parsed: dict[str, Any] | bytes = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = raw
        return error.code, parsed, {key.lower(): value for key, value in error.headers.items()}
    except URLError as error:
        raise AcceptanceFailure(f"HTTP request failed for {url.split('?', 1)[0]}") from error


def _ha_http_url(ha_url: str) -> str:
    return ha_url.rstrip("/")


def _load_token(args: argparse.Namespace) -> str:
    if args.ha_token:
        return args.ha_token
    if args.ha_token_file:
        return Path(args.ha_token_file).read_text(encoding="utf-8").strip()
    if args.ha_auth_store:
        storage = json.loads(Path(args.ha_auth_store).read_text(encoding="utf-8"))
        admin_ids = {user["id"] for user in storage.get("data", {}).get("users", []) if "system-admin" in user.get("group_ids", [])}
        candidates = [item for item in storage.get("data", {}).get("refresh_tokens", []) if item.get("user_id") in admin_ids and isinstance(item.get("jwt_key"), str) and isinstance(item.get("id"), str)]
        if candidates:
            return _access_token_from_refresh(candidates[-1])
    token = os.environ.get("HA_TOKEN", "").strip()
    if token:
        return token
    raise AcceptanceFailure("HA authentication token is required; use --ha-token-file or --ha-auth-store")


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _access_token_from_refresh(refresh: dict[str, Any]) -> str:
    """Mint the short-lived HA access JWT from a local refresh-token record."""
    now = int(time.time())
    expiration = int(float(refresh.get("access_token_expiration", 1800)))
    header = _base64url(b'{"alg":"HS256","typ":"JWT"}')
    payload = _base64url(json.dumps({"iss": refresh["id"], "iat": now, "exp": now + expiration}, separators=(",", ":")).encode())
    unsigned = f"{header}.{payload}".encode("ascii")
    # Home Assistant passes the stored hexadecimal string to PyJWT as a text
    # key; it does not decode the hex representation before signing.
    signature = _base64url(hmac.new(refresh["jwt_key"].encode("ascii"), unsigned, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def _fixture_url(server: FixtureHTTPServer, name: str, host: str) -> str:
    return f"{server.base_url(host)}/{name}"


def _pcm(frames: int = 2) -> str:
    body = bytes((index * 17) % 256 for index in range(FRAME_BYTES * frames))
    return base64.b64encode(body).decode("ascii")


def _capture_fingerprint(capture_id: str) -> dict[str, Any]:
    return {"sha256": hashlib.sha256(capture_id.encode()).hexdigest(), "length": len(capture_id)}


class AcceptanceRunner:
    def __init__(self, args: argparse.Namespace, evidence: Evidence, fixture: FixtureHTTPServer | None = None) -> None:
        self.args = args
        self.evidence = evidence
        self.fixture = fixture
        self.ws: HAWebSocket | None = None
        self.created_entries: list[str] = []
        self.checks = 0
        self.failures: list[str] = []
        self.skipped: list[str] = []

    @property
    def ha_url(self) -> str:
        return _ha_http_url(self.args.ha_url)

    def _check(self, name: str, condition: bool, **fields: Any) -> None:
        self.checks += 1
        self.evidence.record("check", name=name, passed=condition, **fields)
        if not condition:
            raise AcceptanceFailure(name)

    def _open_ws(self) -> HAWebSocket:
        url = self.ha_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        ws = HAWebSocket(url, self.args.token, timeout=self.args.timeout)
        ws.connect()
        return ws

    def _create_entry(self, ws: HAWebSocket, server_url: str) -> str:
        # HA 2024.11 exposes user config-flow creation over HTTP. The command
        # WebSocket is reserved for the audio lifecycle under test.
        status, flow_payload, _ = _json_request(
            f"{self.ha_url}/api/config/config_entries/flow",
            method="POST",
            payload={"handler": "doorfast"},
            token=self.args.token,
        )
        self._check("fixture config flow initialized", status == 200 and isinstance(flow_payload, dict), status=status)
        flow = flow_payload if isinstance(flow_payload, dict) else {}
        flow_id = flow.get("flow_id")
        self._check("fixture config flow initialized", isinstance(flow_id, str), flow_type=flow.get("type"))
        status, result_payload, _ = _json_request(
            f"{self.ha_url}/api/config/config_entries/flow/{flow_id}",
            method="POST",
            payload={"server_address": server_url, "poll_interval": 1},
            token=self.args.token,
        )
        result = result_payload if isinstance(result_payload, dict) else {}
        self._check("fixture config flow submitted", status == 200, status=status)
        self._check("fixture config entry created", result.get("type") == "create_entry", flow_type=result.get("type"))
        created = result.get("result") if isinstance(result.get("result"), dict) else {}
        entry_id = created.get("entry_id")
        self._check("fixture entry id returned", isinstance(entry_id, str) and bool(entry_id))
        self.created_entries.append(entry_id)
        self.evidence.record("entry_created", entry_id=entry_id, server_url=server_url)
        return entry_id

    def _start(self, ws: HAWebSocket, entry_id: str) -> str:
        result = ws.command({"type": "doorfast/audio/start", "config_entry_id": entry_id})
        self._check("audio start returned opaque capture", isinstance(result.get("capture_id"), str) and bool(result.get("capture_id")))
        forbidden = {"runtime", "runtime_id", "generation", "audio_session", "token", "lease_ms"}
        self._check("audio start hides producer identity", not forbidden.intersection(result), returned_keys=sorted(result))
        capture_id = result["capture_id"]
        self.evidence.record("capture_started", entry_id=entry_id, capture_id=capture_id, state=result.get("state"), sequence=result.get("sequence"))
        return capture_id

    def _submit(self, ws: HAWebSocket, entry_id: str, capture_id: str, *, frames: int = 2) -> dict[str, Any]:
        result = ws.command({"type": "doorfast/audio/submit", "config_entry_id": entry_id, "capture_id": capture_id, "pcm": _pcm(frames)})
        self._check("audio submit accepted frames", result.get("accepted_frames") == frames, accepted_frames=result.get("accepted_frames"))
        self.evidence.record("pcm_submitted", entry_id=entry_id, capture_id=capture_id, frames=frames, bytes=frames * FRAME_BYTES, next_sequence=result.get("next_sequence"))
        return result

    def _stop(self, ws: HAWebSocket, entry_id: str, capture_id: str) -> None:
        result = ws.command({"type": "doorfast/audio/stop", "config_entry_id": entry_id, "capture_id": capture_id})
        self._check("audio stop returned idle", result.get("state") == "idle", state=result.get("state"))
        self.evidence.record("capture_stopped", entry_id=entry_id, capture_id=capture_id)

    def _expect_not_found(self, ws: HAWebSocket, entry_id: str, capture_id: str, label: str) -> None:
        error = ws.expect_error({"type": "doorfast/audio/submit", "config_entry_id": entry_id, "capture_id": capture_id, "pcm": _pcm(1)})
        self._check(label, error.code == "not_found", error_code=error.code)
        self.evidence.record("capture_rejected", label=label, entry_id=entry_id, capture_id=capture_id, error_code=error.code)

    def _service_hangup(self, ws: HAWebSocket, entry_id: str) -> None:
        ws.command({"type": "call_service", "domain": "doorfast", "service": "hangup", "service_data": {"config_entry_id": entry_id, "reason": "e2e_acceptance"}})
        self.evidence.record("hangup_requested", entry_id=entry_id)

    def _wait_generation_release(self, ws: HAWebSocket, entry_id: str, capture_id: str) -> None:
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            error = ws.expect_error({"type": "doorfast/audio/submit", "config_entry_id": entry_id, "capture_id": capture_id, "pcm": _pcm(1)})
            if error.code == "not_found":
                self._check("generation change released capture", True, error_code=error.code)
                self.evidence.record("capture_rejected", label="generation change released capture", entry_id=entry_id, capture_id=capture_id, error_code=error.code)
                return
            time.sleep(0.25)
        raise AcceptanceFailure("generation change did not release capture")

    def _test_static_frontend(self) -> None:
        for name, marker in FRONTEND_FILES.items():
            status, body, headers = _json_request(f"{self.ha_url}/doorfast_static/{name}?v=0.1.0", token=self.args.token)
            text = body.decode("utf-8", "replace") if isinstance(body, bytes) else json.dumps(body)
            passed = status == 200 and marker in text and len(text) > 32
            self._check(f"static frontend {name}", passed, status=status, content_length=len(text), marker=marker)
            self.evidence.record("frontend_resource", path=f"/doorfast_static/{name}", status=status, content_length=len(text), sha256=hashlib.sha256(text.encode()).hexdigest(), content_type=headers.get("content-type", ""))

    def _test_core_capture(self, ws: HAWebSocket, entry_id: str) -> None:
        capture_id = self._start(ws, entry_id)
        self._submit(ws, entry_id, capture_id)
        self._stop(ws, entry_id, capture_id)

    def _test_disconnect(self, entry_id: str) -> None:
        first = self._open_ws()
        capture_id = self._start(first, entry_id)
        first.close()
        self.evidence.record("websocket_disconnected", entry_id=entry_id, capture_id=capture_id)
        second = self._open_ws()
        try:
            replacement = self._start(second, entry_id)
            self._check("disconnect released capture", replacement != capture_id)
            self._stop(second, entry_id, replacement)
        finally:
            second.close()

    def _test_multi_entry(self, ws: HAWebSocket, first_entry: str, second_entry: str) -> None:
        first = self._start(ws, first_entry)
        second = self._start(ws, second_entry)
        self._check("multi-entry captures are isolated", first != second)
        self._submit(ws, first_entry, first, frames=1)
        self._submit(ws, second_entry, second, frames=1)
        self._stop(ws, first_entry, first)
        self._stop(ws, second_entry, second)

    def _test_hangup(self, ws: HAWebSocket, entry_id: str) -> None:
        capture_id = self._start(ws, entry_id)
        self._service_hangup(ws, entry_id)
        self._expect_not_found(ws, entry_id, capture_id, "hangup released capture")

    def _test_generation(self, ws: HAWebSocket, entry_id: str) -> None:
        if self.fixture is None:
            self.skipped.append("generation mutation requires fixture control")
            self.evidence.record("skipped", test="generation cleanup", reason="installed endpoint has no declared test control")
            return
        state = self.fixture.states["one"]
        state.set_generation(7)
        capture_id = self._start(ws, entry_id)
        state.set_generation(8)
        self.evidence.record("fixture_generation_changed", entry_id=entry_id, generation=8)
        self._wait_generation_release(ws, entry_id, capture_id)

    def _test_unload(self, ws: HAWebSocket, entry_id: str) -> None:
        capture_id = self._start(ws, entry_id)
        # HA 2024.11 has no public unload/setup WebSocket commands. Disabling
        # and re-enabling the temporary entry exercises the same unload path
        # while remaining a supported admin API operation.
        ws.command({"type": "config_entries/disable", "entry_id": entry_id, "disabled_by": "user"})
        self.evidence.record("entry_unloaded", entry_id=entry_id)
        self._expect_not_found(ws, entry_id, capture_id, "entry unload released capture")
        ws.command({"type": "config_entries/disable", "entry_id": entry_id, "disabled_by": None})
        self.evidence.record("entry_reloaded", entry_id=entry_id)

    def _cleanup_entries(self, ws: HAWebSocket) -> None:
        for entry_id in reversed(self.created_entries):
            try:
                _json_request(
                    f"{self.ha_url}/api/config/config_entries/entry/{entry_id}",
                    method="DELETE",
                    token=self.args.token,
                )
            except WebSocketError:
                pass
            except (AcceptanceFailure, OSError):
                pass

    def run(self) -> bool:
        try:
            self.ws = self._open_ws()
            if self.fixture is not None:
                first_entry = self._create_entry(self.ws, _fixture_url(self.fixture, "one", self.args.fixture_advertise_host))
                second_entry = self._create_entry(self.ws, _fixture_url(self.fixture, "two", self.args.fixture_advertise_host))
            else:
                first_entry = self.args.entry_id
                second_entry = self.args.second_entry_id
                self._check("installed entry id supplied", isinstance(first_entry, str) and bool(first_entry))
            self.evidence.record("ha_authenticated", ha_url=self.ha_url, entry_id=first_entry)
            self._test_static_frontend()
            self._test_core_capture(self.ws, first_entry)
            self._test_disconnect(first_entry)
            if second_entry:
                self._test_multi_entry(self.ws, first_entry, second_entry)
            else:
                self.skipped.append("multi-entry requires --second-entry-id in installed mode")
                self.evidence.record("skipped", test="multi-entry isolation", reason="second entry not supplied")
            self._test_hangup(self.ws, first_entry)
            self._test_generation(self.ws, first_entry)
            self._test_unload(self.ws, first_entry)
            self.evidence.record("acceptance_passed", checks=self.checks)
            return True
        except (AcceptanceFailure, HACommandError, WebSocketError, OSError, ValueError) as error:
            message = str(error)
            self.failures.append(message)
            self.evidence.record("acceptance_failed", error_type=type(error).__name__, error=message)
            return False
        finally:
            if self.ws is not None:
                self._cleanup_entries(self.ws)
                self.ws.close()
            self.evidence.close(passed=not self.failures, checks=self.checks, failures=self.failures, skipped=self.skipped)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Doorfast HA PCM acceptance tests; fixture mode is loopback-only and deterministic.")
    parser.add_argument("--mode", choices=("fixture", "installed"), default="fixture")
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL", "http://127.0.0.1:18124"), help="Home Assistant HTTP base URL")
    parser.add_argument("--ha-token", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--ha-token-file", default=None, help="File containing a Home Assistant long-lived access token")
    parser.add_argument("--ha-auth-store", default=None, help="Home Assistant .storage/auth file; uses an administrator token without printing it")
    parser.add_argument("--entry-id", default=os.environ.get("DOORFAST_ENTRY_ID"), help="Configured Doorfast entry for installed mode")
    parser.add_argument("--second-entry-id", default=os.environ.get("DOORFAST_SECOND_ENTRY_ID"), help="Second configured entry for installed-mode isolation test")
    parser.add_argument("--evidence", type=Path, default=Path("artifacts/doorfast-ha-e2e.jsonl"))
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--fixture-bind-host", default="127.0.0.1", help="Fixture listen address; use 0.0.0.0 only when HA is in Docker")
    parser.add_argument("--fixture-advertise-host", default="127.0.0.1", help="Fixture hostname HA should use")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout <= 0:
        print("--timeout must be positive", file=sys.stderr)
        return 2
    try:
        args.token = _load_token(args)
    except (OSError, ValueError, json.JSONDecodeError, AcceptanceFailure) as error:
        print(str(error), file=sys.stderr)
        return 2
    fixture: FixtureHTTPServer | None = None
    if args.mode == "installed" and not args.entry_id:
        print("--entry-id is required in installed mode", file=sys.stderr)
        return 2
    if args.mode == "fixture":
        fixture = FixtureHTTPServer(bind_host=args.fixture_bind_host)
        fixture.start()
    refs = {"doorfastforha": DOORFASTFORHA_REF, "doorfast": DOORFAST_REF}
    evidence = Evidence(args.evidence, mode=args.mode, refs=refs)
    try:
        passed = AcceptanceRunner(args, evidence, fixture).run()
    finally:
        if fixture is not None:
            fixture.close()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
