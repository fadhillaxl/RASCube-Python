from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import time


class TransportState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


class SatelliteLinkState(Enum):
    NEVER_SEEN = "never_seen"
    ACTIVE = "active"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class EventMetadata:
    port: int
    received_monotonic: float
    received_wall_time: float = field(default_factory=time)
    raw_payload: bytes = b""


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    """Confirms local USB submission, not remote delivery or execution."""

    port: int
    payload_length: int
    submitted_monotonic: float


@dataclass(frozen=True, slots=True)
class ProtocolIssue:
    message: str
    port: int | None = None
    payload: bytes = b""
    exception: Exception | None = None


@dataclass(frozen=True, slots=True)
class LinkStatistics:
    inbound_bytes: int = 0
    inbound_frames: int = 0
    outbound_bytes: int = 0
    outbound_frames: int = 0
