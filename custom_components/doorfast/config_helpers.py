"""Pure helpers for Doorfast configuration."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit


BRIDGE_PATH = "/cgi-bin/doorfast"


def normalize_bridge_url(address: str) -> str:
    """Return the canonical Doorfast HTTP bridge base URL."""
    normalized = address.rstrip("/")
    if not normalized.endswith(BRIDGE_PATH):
        normalized += BRIDGE_PATH
    return normalized


def normalize_go2rtc_api_url(address: str) -> str:
    """Return a credential-free HTTP(S) go2rtc API base URL."""
    if not isinstance(address, str):
        raise ValueError("go2rtc API URL must be text")
    parsed = urlsplit(address)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("go2rtc API URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("go2rtc API URL must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("go2rtc API URL must not include query or fragment")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("go2rtc API URL has an invalid port") from error
    if any(character.isspace() for character in parsed.netloc + parsed.path):
        raise ValueError("go2rtc API URL must not include whitespace")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def is_doorfast_status(payload: dict[str, Any]) -> bool:
    """Return whether a response identifies a running Doorfast bridge."""
    return payload.get("running") is True
