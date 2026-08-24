from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from rascube_v2.exceptions import ConnectionClosedError, ConnectionLostError

WriteHandler = Callable[[bytes], Awaitable[None] | None]


class FakeTransport:
    """Deterministic in-memory transport for tests and protocol simulations."""

    def __init__(self, on_write: WriteHandler | None = None) -> None:
        self.on_write = on_write
        self.writes: list[bytes] = []
        self.dtr_history: list[bool] = []
        self._reads: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()
        self._write_events: asyncio.Queue[bytes] = asyncio.Queue()
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    async def open(self) -> None:
        self._reads = asyncio.Queue()
        self._open = True

    async def close(self) -> None:
        if not self._open:
            return
        self._open = False
        self._reads.put_nowait(None)

    async def read(self, max_bytes: int = 4096) -> bytes:
        if not self._open:
            raise ConnectionClosedError("fake transport is closed")
        item = await self._reads.get()
        if item is None:
            raise ConnectionLostError("fake transport closed")
        if isinstance(item, Exception):
            raise item
        data = item
        if len(data) <= max_bytes:
            return data
        self._reads.put_nowait(data[max_bytes:])
        return data[:max_bytes]

    async def write(self, data: bytes) -> None:
        if not self._open:
            raise ConnectionClosedError("fake transport is closed")
        copied = bytes(data)
        self.writes.append(copied)
        self._write_events.put_nowait(copied)
        if self.on_write is not None:
            result = self.on_write(copied)
            if result is not None:
                await result

    async def set_dtr(self, enabled: bool) -> None:
        if not self._open:
            raise ConnectionClosedError("fake transport is closed")
        self.dtr_history.append(enabled)

    def inject(self, data: bytes) -> None:
        self._reads.put_nowait(bytes(data))

    def inject_error(self, error: Exception) -> None:
        self._reads.put_nowait(error)

    async def next_write(self, *, timeout: float = 1.0) -> bytes:
        return await asyncio.wait_for(self._write_events.get(), timeout)
