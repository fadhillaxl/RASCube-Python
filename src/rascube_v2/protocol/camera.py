from __future__ import annotations

from rascube_v2.exceptions import CameraAssemblyError
from rascube_v2.models.camera import CameraBlock


class CameraAssembler:
    def __init__(self) -> None:
        self._blocks: dict[int, bytes] = {}
        self._duplicates: set[int] = set()

    @property
    def duplicates(self) -> frozenset[int]:
        return frozenset(self._duplicates)

    @property
    def block_count(self) -> int:
        return len(self._blocks)

    def reset(self) -> None:
        self._blocks.clear()
        self._duplicates.clear()

    def add(self, block: CameraBlock) -> bytes | None:
        if block.index in self._blocks:
            self._duplicates.add(block.index)
            if self._blocks[block.index] != block.data:
                raise CameraAssemblyError(
                    f"camera block {block.index} was repeated with different data"
                )
        self._blocks[block.index] = block.data

        if 0 not in self._blocks:
            return None

        contiguous = bytearray()
        index = 0
        while index in self._blocks:
            contiguous.extend(self._blocks[index])
            index += 1

        if len(contiguous) >= 2 and contiguous[:2] != b"\xff\xd8":
            raise CameraAssemblyError("contiguous camera data does not begin with JPEG SOI")

        eoi = contiguous.find(b"\xff\xd9")
        if eoi < 0:
            return None
        return bytes(contiguous[: eoi + 2])
