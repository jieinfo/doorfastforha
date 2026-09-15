"""Transport-only value types shared by the Doorfast HTTP clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PcmHttpReply:
    """An HTTP status and already-decoded JSON object from the PCM bridge."""

    status: int
    payload: dict[str, Any]
