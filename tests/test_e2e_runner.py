from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from urllib.request import Request, urlopen

from doorfast_ha_e2e.evidence import _redact
from doorfast_ha_e2e.fixture import FixtureHTTPServer, FRAME_BYTES
from doorfast_ha_e2e.runner import _json_request
from doorfast_ha_e2e.websocket import decode_frame, encode_frame


class E2ERunnerUnitTest(unittest.TestCase):
    def test_evidence_redacts_secrets_and_hashes_capture(self):
        capture = "opaque-capture-id"
        result = _redact({"token": "secret", "pcm": b"audio", "capture_id": capture, "server_url": "http://127.0.0.1:1234/x?token=bad"})
        self.assertEqual("[redacted]", result["token"])
        self.assertEqual("[redacted]", result["pcm"])
        self.assertEqual(hashlib.sha256(capture.encode()).hexdigest(), result["capture_id"]["sha256"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("audio", json.dumps(result))
        self.assertNotIn("?token", json.dumps(result))

    def test_masked_frame_round_trip(self):
        import socket

        left, right = socket.socketpair()
        try:
            left.sendall(encode_frame(b"hello"))
            self.assertEqual((1, b"hello", True), decode_frame(right))
        finally:
            left.close()
            right.close()

    def test_private_fixture_accepts_pcm_and_records_digest_only(self):
        with FixtureHTTPServer() as server:
            status_url = f"{server.base_url()}/one/cgi-bin/doorfast/api/v1/status"
            with urlopen(status_url) as response:
                status = json.loads(response.read())
            self.assertEqual("talking", status["call"]["session"])
            runtime = status["runtime_id"]
            generation = status["call"]["generation"]
            open_url = f"{server.base_url()}/one/cgi-bin/doorfast/api/v1/audio/session?runtime={runtime}&generation={generation}"
            request = Request(open_url, data=b"", method="POST")
            with urlopen(request) as response:
                opened = json.loads(response.read())
            token = opened["audio_session"]
            body = bytes(range(256)) + bytes(range(64))
            submit_url = f"{server.base_url()}/one/cgi-bin/doorfast/api/v1/audio/submit.pcm?runtime={runtime}&generation={generation}&sequence=0"
            request = Request(submit_url, data=body, method="POST", headers={"Content-Type": "application/octet-stream", "X-Doorfast-Audio-Session": token})
            with urlopen(request) as response:
                result = json.loads(response.read())
            self.assertEqual(1, result["accepted_frames"])
            self.assertEqual(1, len(server.states["one"].audio_hashes))
            self.assertNotIn("audio_session", server.states["one"].audio_hashes[0])

    def test_fixture_generation_change_invalidates_session(self):
        with FixtureHTTPServer() as server:
            state = server.states["one"]
            state.set_generation(8)
            self.assertEqual(8, state.status()["call"]["generation"])
            self.assertEqual("talking", state.status()["call"]["session"])
            state.hangup()
            self.assertFalse(state.status()["audio_tx"]["active"])

    def test_fixture_exposes_two_independent_station_states(self):
        with FixtureHTTPServer() as server:
            self.assertEqual({"one", "two"}, set(server.states))
            self.assertNotEqual(server.states["one"].runtime_id, server.states["two"].runtime_id)
            server.states["one"].set_generation(8)
            self.assertEqual(8, server.states["one"].generation)
            self.assertEqual(7, server.states["two"].generation)

    def test_json_request_posts_bearer_and_json_payload(self):
        observed = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                return

            def do_POST(self):  # noqa: N802 - stdlib API name
                observed["authorization"] = self.headers.get("Authorization")
                observed["content_type"] = self.headers.get("Content-Type")
                length = int(self.headers.get("Content-Length", "0"))
                observed["payload"] = json.loads(self.rfile.read(length))
                body = b'{"flow_id":"fixture-flow"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, payload, _ = _json_request(
                f"http://127.0.0.1:{server.server_port}/api/config/config_entries/flow",
                method="POST",
                payload={"handler": "doorfast"},
                token="test-bearer",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(200, status)
        self.assertEqual({"flow_id": "fixture-flow"}, payload)
        self.assertEqual("Bearer test-bearer", observed["authorization"])
        self.assertEqual("application/json", observed["content_type"])
        self.assertEqual({"handler": "doorfast"}, observed["payload"])


if __name__ == "__main__":
    unittest.main()
