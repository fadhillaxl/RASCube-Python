from __future__ import annotations

import asyncio
from collections.abc import Hashable
from contextlib import suppress

from rascube_v2.addons.manager import AddonManager
from rascube_v2.connection import ConnectionMonitor
from rascube_v2.constants import MAX_RADIO_PAYLOAD, MAX_USB_PAYLOAD, ReceiverMode
from rascube_v2.exceptions import ConnectionClosedError, ConnectionLostError
from rascube_v2.models.common import ProtocolIssue, SubmissionReceipt, TransportState
from rascube_v2.modules.arduino import ArduinoModule
from rascube_v2.modules.calibration import CalibrationModule
from rascube_v2.modules.camera import CameraModule
from rascube_v2.modules.obc import ObcModule
from rascube_v2.modules.receiver import ReceiverModule
from rascube_v2.modules.telemetry import TelemetryModule
from rascube_v2.protocol.frame import UsbFrame
from rascube_v2.protocol.link import FramedLink
from rascube_v2.protocol.requests import FrameMatcher, RequestBroker
from rascube_v2.protocol.router import FrameHandler, FrameRouter
from rascube_v2.transport.base import AsyncByteTransport
from rascube_v2.transport.serial import SerialTransport


class RASCube:
    """Asynchronous composition root for one RASCubeV2 receiver connection."""

    def __init__(
        self,
        port: str | None = None,
        *,
        serial_number: int,
        transport: AsyncByteTransport | None = None,
        initialize: bool = True,
        stale_after: float = 20.0,
    ) -> None:
        if transport is None:
            if port is None:
                raise ValueError("port is required when transport is not supplied")
            transport = SerialTransport(port)
        self._validate_serial_number(serial_number)

        self.serial_number = serial_number
        self.initialize = initialize
        self._broker = RequestBroker()
        self._router = FrameRouter(self._broker)
        self._link = FramedLink(transport, self._on_frame, self._on_link_error)
        self.connection = ConnectionMonitor(stale_after=stale_after)
        self._opened = False
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._open_task: asyncio.Task[RASCube] | None = None
        self._open_waiters = 0
        self._close_task: asyncio.Task[None] | None = None
        self._failure_task: asyncio.Task[None] | None = None
        self._initialization_task: asyncio.Task[object] | None = None
        self.mode: ReceiverMode | None = None

        self.receiver = ReceiverModule(self, self._set_selected_satellite)
        self.obc = ObcModule(self)
        self.arduino = ArduinoModule(self)
        self.telemetry = TelemetryModule(self)
        self.camera = CameraModule(self)
        self.calibration = CalibrationModule(self)
        self.addons = AddonManager(self)

        self.raw_frames = self._router.raw_frames
        self.protocol_issues = self._router.protocol_issues
        self._event_streams = (
            self.raw_frames,
            self.protocol_issues,
            self.connection.transport_events,
            self.connection.satellite_events,
            self.connection.statistics_events,
            self.receiver.statuses,
            self.telemetry.samples,
            self.telemetry.user_samples,
            self.telemetry.user_names,
            self.camera.blocks,
            self.camera.images,
            self.calibration.events,
            self.addons.presence_events,
            self.addons.version_events,
            self.addons.reaction_wheel.samples,
            self.addons.advanced_sensor.samples,
            self.addons.advanced_sensor.statuses,
            self.addons.environmental_sensor.samples,
        )

    async def __aenter__(self) -> RASCube:
        return await self.open()

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def is_open(self) -> bool:
        return self._opened and self._link.is_open

    async def open(self) -> RASCube:
        self._ensure_owner_loop()
        self._bind_event_streams()
        while True:
            async with self._lifecycle_lock:
                if self.is_open:
                    return self
                close_task = self._close_task
                if close_task is None or close_task.done():
                    if self._open_task is None or self._open_task.done():
                        self._open_task = asyncio.create_task(
                            self._open_impl(), name="rascube-open"
                        )
                    open_task = self._open_task
                    self._open_waiters += 1
                    break
            await self._wait_for_cleanup(close_task)
        waiter_removed = False
        try:
            return await asyncio.shield(open_task)
        except asyncio.CancelledError:
            async with self._lifecycle_lock:
                self._open_waiters -= 1
                waiter_removed = True
                cancel_open = self._open_waiters == 0 and not open_task.done()
                if cancel_open:
                    open_task.cancel()
            if cancel_open:
                while not open_task.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.shield(open_task)
            raise
        finally:
            if not waiter_removed:
                async with self._lifecycle_lock:
                    self._open_waiters -= 1

    async def _open_impl(self) -> RASCube:
        self.connection.set_transport_state(TransportState.CONNECTING)
        try:
            await self._link.start()
            self.connection.start()
            self.telemetry.reset_connection()
            self.addons.reset_connection()
            self.camera.reset_connection()

            await self._link.set_dtr(False)
            await asyncio.sleep(0.05)
            await self._link.set_dtr(True)
            await asyncio.sleep(0.25)
            if not self._link.is_open:
                raise ConnectionLostError("receiver reader stopped during initialization")

            self._initialization_task = asyncio.current_task()
            try:
                if self.initialize:
                    self.mode = await self.receiver.get_mode()
                    if self.mode is ReceiverMode.APPLICATION:
                        await self.select_satellite(self.serial_number)
            finally:
                self._initialization_task = None

            if not self._link.is_open:
                raise ConnectionLostError("receiver reader stopped during initialization")
            self._opened = True
            self.connection.set_transport_state(TransportState.CONNECTED)
            return self
        except BaseException as error:
            self.connection.set_transport_state(TransportState.FAILED)
            cleanup = asyncio.create_task(
                self._cleanup(
                    ConnectionLostError(f"receiver initialization failed: {error}"),
                    TransportState.FAILED,
                )
            )
            await self._wait_for_cleanup(cleanup)
            raise

    async def close(self) -> None:
        self._ensure_owner_loop()
        self._bind_event_streams()
        async with self._lifecycle_lock:
            if self._close_task is None or self._close_task.done():
                open_task = self._open_task
                if open_task is not None and not open_task.done():
                    open_task.cancel()
                self._close_task = asyncio.create_task(
                    self._close_impl(open_task), name="rascube-close"
                )
            close_task = self._close_task
        await self._wait_for_cleanup(close_task)

    async def _close_impl(self, open_task: asyncio.Task[RASCube] | None) -> None:
        if open_task is not None and open_task is not asyncio.current_task():
            with suppress(BaseException):
                await open_task
        await self._cleanup(
            ConnectionClosedError("receiver connection closed"),
            TransportState.DISCONNECTED,
        )

    async def select_satellite(self, serial_number: int) -> SubmissionReceipt:
        self._ensure_owner_loop()
        self._validate_serial_number(serial_number)
        return await self.receiver.set_satellite_serial_number(serial_number)

    async def submit(
        self, port: int, payload: bytes, *, radio_bound: bool = True
    ) -> SubmissionReceipt:
        self._ensure_owner_loop()
        if not self._opened and asyncio.current_task() is not self._initialization_task:
            raise ConnectionClosedError("receiver connection is not open")
        limit = MAX_RADIO_PAYLOAD if radio_bound else MAX_USB_PAYLOAD
        if len(payload) > limit:
            raise ValueError(f"payload exceeds the {limit}-byte limit")
        if radio_bound and not payload:
            raise ValueError("radio-bound commands must include at least one payload byte")
        receipt = await self._link.send(UsbFrame(int(port), bytes(payload)))
        self.connection.record_outbound(len(payload))
        return receipt

    async def request(
        self,
        port: int,
        payload: bytes,
        *,
        key: Hashable,
        matcher: FrameMatcher,
        timeout: float,
        radio_bound: bool = True,
    ) -> UsbFrame:
        self._ensure_owner_loop()
        return await self._broker.request(
            key,
            matcher,
            lambda: self.submit(port, payload, radio_bound=radio_bound),
            timeout=timeout,
        )

    def register_handler(self, port: int, handler: FrameHandler) -> None:
        self._router.register(int(port), handler)

    async def _on_frame(self, frame: UsbFrame) -> None:
        self.connection.record_inbound(frame)
        self._router.dispatch(frame)

    def _on_link_error(self, error: Exception) -> None:
        lost = ConnectionLostError(f"receiver connection lost: {error}")
        self._broker.cancel_all(str(lost))
        self._interrupt_waiters(lost)
        if self._open_task is not None and not self._open_task.done():
            self._open_task.cancel()
            return
        if self._failure_task is None or self._failure_task.done():
            self._failure_task = asyncio.create_task(
                self._handle_link_failure(lost), name="rascube-link-failure"
            )
            self._failure_task.add_done_callback(self._report_background_failure)

    async def _handle_link_failure(self, error: ConnectionLostError) -> None:
        async with self._lifecycle_lock:
            if self._close_task is not None and not self._close_task.done():
                return
            self.connection.set_transport_state(TransportState.FAILED)
            self._close_task = asyncio.create_task(
                self._cleanup(error, TransportState.FAILED),
                name="rascube-failure-close",
            )
            close_task = self._close_task
        await self._wait_for_cleanup(close_task)

    async def _cleanup(self, error: BaseException, state: TransportState) -> None:
        self._opened = False
        self._broker.cancel_all(str(error))
        self._interrupt_waiters(error)
        try:
            try:
                await self.connection.stop()
            finally:
                await self._link.close()
        finally:
            self.connection.set_transport_state(state)
            self.connection.interrupt(error)

    def _interrupt_waiters(self, error: BaseException) -> None:
        self.receiver.interrupt(error)
        self.telemetry.interrupt(error)
        self.camera.interrupt(error)
        self.calibration.interrupt(error)
        self.addons.interrupt(error)
        self._router.interrupt(error)

    @staticmethod
    async def _wait_for_cleanup(task: asyncio.Task[None]) -> None:
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    def _bind_event_streams(self) -> None:
        for stream in self._event_streams:
            stream.bind()

    def _ensure_owner_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._owner_loop is None:
            self._owner_loop = loop
        elif self._owner_loop is not loop:
            raise RuntimeError("a RASCube client may only be used from its owner event loop")

    def _set_selected_satellite(self, serial_number: int) -> None:
        self.serial_number = serial_number

    def _report_background_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            asyncio.get_running_loop().call_exception_handler(
                {
                    "message": "RASCube background cleanup failed",
                    "exception": error,
                    "task": task,
                }
            )
            self.protocol_issues.publish(
                ProtocolIssue(
                    "background cleanup failed",
                    exception=error if isinstance(error, Exception) else None,
                )
            )

    @staticmethod
    def _validate_serial_number(serial_number: int) -> None:
        if not 0 <= serial_number <= 0xFFFFFFFF:
            raise ValueError("serial_number must fit an unsigned 32-bit integer")
