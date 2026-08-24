from __future__ import annotations

from dataclasses import dataclass

from rascube_v2.constants import ReceiverMode, ReceiverStatusFlag
from rascube_v2.models.common import EventMetadata


@dataclass(frozen=True, slots=True)
class ReceiverInfo:
    software_version: int
    git_hash: str | None
    dirty: bool


@dataclass(frozen=True, slots=True)
class ReceiverStatus:
    source: int
    flags: ReceiverStatusFlag
    metadata: EventMetadata

    def has(self, flag: ReceiverStatusFlag) -> bool:
        return bool(self.flags & flag)


@dataclass(frozen=True, slots=True)
class ReceiverIdentity:
    mode: ReceiverMode
    info: ReceiverInfo | None = None
