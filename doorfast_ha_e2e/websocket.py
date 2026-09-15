"""Small dependency-free Home Assistant WebSocket client for acceptance tests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class WebSocketError(RuntimeError):
    pass


class AuthenticationError(WebSocketError):
    pass


@dataclass
class HACommandError(WebSocketError):
    code: str
    message: str

    def __str__(self) -> str:
        return f"Home Assistant command failed: {self.code}"


def _read_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise WebSocketError("WebSocket closed while reading a frame")
        data.extend(chunk)
    return bytes(data)


def encode_frame(payload: bytes, *, opcode: int = 1) -> bytes:
    """Encode one client-to-server masked RFC 6455 frame."""
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, 0x80 | length])
    elif length <= 0xFFFF:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", length)
    mask = os.urandom(4)
    return header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def decode_frame(sock: socket.socket) -> tuple[int, bytes, bool]:
    first, second = _read_exact(sock, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(sock, 8))[0]
        if length > (1 << 24):
            raise WebSocketError("WebSocket frame is too large")
    mask = _read_exact(sock, 4) if masked else b""
    body = bytearray(_read_exact(sock, length))
    if masked:
        for index in range(length):
            body[index] ^= mask[index % 4]
    return opcode, bytes(body), fin


class HAWebSocket:
    """Authenticate and send commands over Home Assistant's native WebSocket API."""

    def __init__(self, url: str, token: str, *, timeout: float = 10.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in {"ws", "wss"} or not parts.hostname:
            raise ValueError("HA WebSocket URL must use ws:// or wss://")
        self.url = url
        self.token = token
        self.timeout = timeout
        self._parts = parts
        self._socket: socket.socket | None = None
        self._next_id = 1
        self._fragments: list[bytes] = []

    def connect(self) -> None:
        if self._socket is not None:
            return
        port = self._parts.port or (443 if self._parts.scheme == "wss" else 80)
        raw = socket.create_connection((self._parts.hostname, port), self.timeout)
        raw.settimeout(self.timeout)
        if self._parts.scheme == "wss":
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=self._parts.hostname)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = self._parts.path or "/"
        if self._parts.query:
            path += "?" + self._parts.query
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {self._parts.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        raw.sendall(request)
        response = bytearray()
        # Read only through the header terminator. HA may send its
        # auth_required frame immediately after the 101 response; a large
        # recv could consume that frame and silently discard it.
        while b"\r\n\r\n" not in response:
            chunk = raw.recv(1)
            if not chunk:
                raw.close()
                raise WebSocketError("WebSocket handshake closed")
            response.extend(chunk)
            if len(response) > 65536:
                raw.close()
                raise WebSocketError("WebSocket handshake headers are too large")
        header = bytes(response).split(b"\r\n\r\n", 1)[0].decode("latin-1")
        status = header.split("\r\n", 1)[0]
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        header_map = {}
        for line in header.split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                header_map[name.lower().strip()] = value.strip()
        if not status.startswith("HTTP/1.1 101") or header_map.get("sec-websocket-accept") != expected:
            raw.close()
            raise WebSocketError("Home Assistant WebSocket upgrade failed")
        self._socket = raw
        hello = self._receive_json()
        if hello.get("type") != "auth_required":
            self.close()
            raise AuthenticationError("Home Assistant did not request authentication")
        self._send_json({"type": "auth", "access_token": self.token})
        auth = self._receive_json()
        if auth.get("type") != "auth_ok":
            self.close()
            error = auth.get("message") if isinstance(auth.get("message"), str) else "authentication failed"
            raise AuthenticationError("Home Assistant WebSocket authentication failed: " + error)

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is None:
            return
        try:
            sock.sendall(encode_frame(b"", opcode=8))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def __enter__(self) -> "HAWebSocket":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _send_json(self, payload: dict[str, Any]) -> None:
        if self._socket is None:
            raise WebSocketError("WebSocket is not connected")
        self._socket.sendall(encode_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8")))

    def _receive_json(self) -> dict[str, Any]:
        if self._socket is None:
            raise WebSocketError("WebSocket is not connected")
        while True:
            opcode, payload, final = decode_frame(self._socket)
            if opcode == 8:
                raise WebSocketError("Home Assistant WebSocket closed")
            if opcode == 9:
                self._socket.sendall(encode_frame(payload, opcode=10))
                continue
            if opcode == 10:
                continue
            if opcode == 0:
                self._fragments.append(payload)
                if not final:
                    continue
                payload = b"".join(self._fragments)
                self._fragments.clear()
            elif not final:
                self._fragments = [payload]
                continue
            if opcode not in {1, 0}:
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise WebSocketError("Home Assistant sent invalid JSON") from error
            if not isinstance(message, dict):
                raise WebSocketError("Home Assistant sent a non-object message")
            return message

    def command(self, payload: dict[str, Any], *, allow_error: bool = False) -> dict[str, Any]:
        command_id = self._next_id
        self._next_id += 1
        message = {"id": command_id, **payload}
        self._send_json(message)
        while True:
            response = self._receive_json()
            if response.get("id") != command_id:
                continue
            if response.get("type") != "result":
                raise WebSocketError("Home Assistant returned an unexpected command response")
            if response.get("success") is False:
                error = response.get("error") if isinstance(response.get("error"), dict) else {}
                exc = HACommandError(str(error.get("code", "unknown_error")), str(error.get("message", "command failed")))
                if allow_error:
                    return response
                raise exc
            result = response.get("result")
            return result if isinstance(result, dict) else {"value": result}

    def expect_error(self, payload: dict[str, Any]) -> HACommandError:
        response = self.command(payload, allow_error=True)
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        if response.get("success") is not False:
            raise WebSocketError("Expected Home Assistant command to fail")
        return HACommandError(str(error.get("code", "unknown_error")), str(error.get("message", "command failed")))


__all__ = ["HAWebSocket", "HACommandError", "AuthenticationError", "WebSocketError", "decode_frame", "encode_frame"]
