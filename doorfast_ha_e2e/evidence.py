"""Redacted JSONL evidence for the HA/VM acceptance runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_SECRET_KEYS = {
    "access_token",
    "audio_session",
    "authorization",
    "body",
    "password",
    "pcm",
    "session_token",
    "token",
}


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _redact(value: Any, key: str = "") -> Any:
    lower = key.lower()
    if lower in _SECRET_KEYS:
        return "[redacted]"
    if lower in {"capture_id", "session_id"} and isinstance(value, str):
        return {"sha256": hashlib.sha256(value.encode()).hexdigest(), "length": len(value)}
    if lower.endswith("url") and isinstance(value, str):
        return _safe_url(value)
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, key) for item in value]
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    return value


class Evidence:
    """Append only evidence writer whose public output never contains raw audio or tokens."""

    def __init__(self, path: Path, *, mode: str, refs: dict[str, str]) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("w", encoding="utf-8")
        self.record("run_started", mode=mode, source_refs=refs)

    def record(self, event: str, **fields: Any) -> None:
        payload = {"schema": 1, "event": event, **fields}
        self._stream.write(json.dumps(_redact(payload), sort_keys=True, separators=(",", ":")) + "\n")
        self._stream.flush()

    def close(self, *, passed: bool, checks: int, failures: list[str], skipped: list[str]) -> None:
        self.record("run_finished", passed=passed, checks=checks, failures=failures, skipped=skipped)
        self._stream.close()


__all__ = ["Evidence", "_redact"]
