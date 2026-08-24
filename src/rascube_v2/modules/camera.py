from __future__ import annotations

import asyncio

from rascube_v2.constants import HostPort, InboundPort
from rascube_v2.exceptions import CameraAssemblyError, RequestTimeoutError, SessionBusyError
from rascube_v2.models.camera import CameraBlock, CameraImage
from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.protocol.bus import CommandBus
from rascube_v2.protocol.camera import CameraAssembler
from rascube_v2.protocol.codecs import decode_camera_block
from rascube_v2.protocol.events import EventStream
from rascube_v2.protocol.frame import UsbFrame


class CameraModule:
    def __init__(self, bus: CommandBus) -> None:
        self._bus = bus
        self._assembler = CameraAssembler()
        self._capture_lock = asyncio.Lock()
        self._capture_future: asyncio.Future[CameraImage] | None = None
        self._poisoned = False
        self.blocks: EventStream[CameraBlock] = EventStream(default_queue_size=100)
        self.images: EventStream[CameraImage] = EventStream(default_queue_size=5)
        bus.register_handler(InboundPort.JPEG_CAMERA, self._handle_block)

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    async def _request(self) -> SubmissionReceipt:
        return await self._bus.submit(HostPort.OBC_CAMERA, b"\x00")

    async def capture(self, *, timeout: float = 30.0) -> CameraImage:
        if self._capture_lock.locked():
            raise SessionBusyError("a camera capture is already active")
        if self._poisoned:
            raise CameraAssemblyError(
                "camera stream is ambiguous until the timed-out transfer reaches JPEG EOI"
            )
        async with self._capture_lock:
            self._assembler.reset()
            self._capture_future = asyncio.get_running_loop().create_future()
            try:
                try:
                    async with asyncio.timeout(timeout):
                        await self._request()
                        return await asyncio.shield(self._capture_future)
                except TimeoutError as exc:
                    self._poisoned = True
                    raise RequestTimeoutError(
                        f"camera did not produce a complete JPEG within {timeout:.1f}s"
                    ) from exc
            except BaseException:
                self._poisoned = True
                raise
            finally:
                if self._capture_future is not None and not self._capture_future.done():
                    self._capture_future.cancel()
                self._capture_future = None

    def reset_connection(self) -> None:
        if not self._poisoned:
            self._assembler.reset()

    def interrupt(self, error: BaseException) -> None:
        if self._capture_future is not None and not self._capture_future.done():
            self._poisoned = True
            self._capture_future.set_exception(error)
        self.blocks.interrupt(error)
        self.images.interrupt(error)

    def _handle_block(self, frame: UsbFrame) -> None:
        block = decode_camera_block(frame)
        self.blocks.publish(block)
        if self._poisoned:
            # Keep parsing the timed-out stream. EOI is the only available evidence
            # that the unlabelled camera transfer has finished.
            if self._assembler.add(block) is not None:
                self._assembler.reset()
                self._poisoned = False
            return
        if self._capture_future is None or self._capture_future.done():
            return
        try:
            jpeg = self._assembler.add(block)
        except CameraAssemblyError as exc:
            self._capture_future.set_exception(exc)
            raise
        if jpeg is None:
            return
        image = CameraImage(
            jpeg=jpeg,
            block_count=self._assembler.block_count,
            duplicate_blocks=self._assembler.duplicates,
            metadata=block.metadata,
        )
        self.images.publish(image)
        self._capture_future.set_result(image)
