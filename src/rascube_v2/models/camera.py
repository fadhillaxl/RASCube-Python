from __future__ import annotations

from dataclasses import dataclass

from rascube_v2.models.common import EventMetadata


@dataclass(frozen=True, slots=True)
class CameraBlock:
    index: int
    data: bytes
    metadata: EventMetadata


@dataclass(frozen=True, slots=True)
class CameraImage:
    jpeg: bytes
    block_count: int
    duplicate_blocks: frozenset[int]
    metadata: EventMetadata
