from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from time import monotonic

from rascube_v2.exceptions import ConnectionClosedError
from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.protocol.frame import UsbFrame, UsbFrameCodec
from rascube_v2.transport.base import AsyncByteTransport

FrameCallback = Callable[[UsbFrame], Awaitable[None] | None]
ErrorCallback = Callable[[Exception], None]


class FramedLink:
    def __init__(
        self,
        transport: AsyncByteTransport,
        on_frame: FrameCallback,
        on_error: ErrorCallback,
    ) -> None:
        self.transport = transport
        self._on_frame = on_frame
        self._on_error = on_error
        self._codec = UsbFrameCodec()
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def is_open(self) -> bool:
        task = self._reader_task
        return self.transport.is_open and task is not None and not task.done()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.is_open:
                return
            if self._close_task is not None and not self._close_task.done():
                await asyncio.shield(self._close_task)
            try:
                await self.transport.open()
                self._codec.reset()
                self._reader_task = asyncio.create_task(self._reader_loop(), name="rascube-reader")
            except BaseException:
                await asyncio.shield(self.transport.close())
                raise

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._close_task is None or self._close_task.done():
                self._close_task = asyncio.create_task(
                    self._close_impl(), name="rascube-link-close"
                )
            close_task = self._close_task
        cancelled = False
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                cancelled = True
        close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close_impl(self) -> None:
        task = self._reader_task
        self._reader_task = None
        current = asyncio.current_task()
        if task is not None and task is not current:
            task.cancel()
        try:
            await self.transport.close()
        finally:
            if task is not None and task is not current:
                with suppress(asyncio.CancelledError):
                    await task
            self._codec.reset()

    async def send(self, frame: UsbFrame) -> SubmissionReceipt:
        if not self.is_open:
            raise ConnectionClosedError("receiver connection is closed")
        encoded = self._codec.encode(frame)
        async with self._write_lock:
            await self.transport.write(encoded)
        return SubmissionReceipt(frame.port, len(frame.payload), monotonic())

    async def set_dtr(self, enabled: bool) -> None:
        await self.transport.set_dtr(enabled)

    async def _reader_loop(self) -> None:
        try:
            while True:
                data = await self.transport.read()
                if not data:
                    await asyncio.sleep(0.01)
                    continue
                for frame in self._codec.feed(data):
                    result = self._on_frame(frame)
                    if result is not None:
                        await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._on_error(exc)
        finally:
            if self._reader_task is asyncio.current_task():
                self._reader_task = None
