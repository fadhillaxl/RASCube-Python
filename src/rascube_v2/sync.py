from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Iterator
from typing import Any, TypeVar

from rascube_v2.client import RASCube as AsyncRASCube
from rascube_v2.constants import ReceiverMode
from rascube_v2.models.addons import (
    AddonPresence,
    AddonVersions,
    AdvancedSensorSample,
    AdvancedSensorStatus,
    EnvironmentalSensorSample,
    ReactionWheelSample,
)
from rascube_v2.models.calibration import CalibrationCoefficients, CalibrationResult
from rascube_v2.models.camera import CameraBlock, CameraImage
from rascube_v2.models.common import (
    LinkStatistics,
    SatelliteLinkState,
    SubmissionReceipt,
    TransportState,
)
from rascube_v2.models.obc import FlashSettings, ObcInfo
from rascube_v2.models.receiver import ReceiverInfo, ReceiverStatus
from rascube_v2.models.telemetry import MainTelemetrySample, UserDataName, UserTelemetrySample
from rascube_v2.transport.base import AsyncByteTransport

T = TypeVar("T")


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="rascube-sync", daemon=True)
        self._state_lock = threading.Lock()
        self._stopping = False

    def start(self) -> None:
        self._thread.start()
        self._ready.wait()

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> T:
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError("blocking RASCube methods cannot run on the internal event loop")
        with self._state_lock:
            if self._stopping or not self._thread.is_alive():
                coroutine.close()
                raise RuntimeError("RASCube event loop is not running")
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result()

    def stop(self) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError("the RASCube event loop cannot stop itself")
        with self._state_lock:
            if self._stopping:
                return
            self._stopping = True
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise RuntimeError("RASCube event-loop thread did not stop")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()


class _SyncService:
    def __init__(self, owner: RASCube) -> None:
        self._owner = owner

    def _call(self, coroutine: Coroutine[Any, Any, T]) -> T:
        return self._owner._call(coroutine)


class ReceiverModule(_SyncService):
    def get_info(self, *, timeout: float = 2.0) -> ReceiverInfo:
        return self._call(self._owner._client.receiver.get_info(timeout=timeout))

    def get_mode(self, *, timeout: float = 2.0) -> ReceiverMode:
        return self._call(self._owner._client.receiver.get_mode(timeout=timeout))

    def set_satellite_serial_number(self, serial_number: int) -> SubmissionReceipt:
        return self._call(self._owner._client.select_satellite(serial_number))

    def set_rf_config(
        self,
        spreading_factor: int,
        bandwidth_index: int,
        *,
        coding_rate_index: int | None = None,
    ) -> SubmissionReceipt:
        return self._call(
            self._owner._client.receiver.set_rf_config(
                spreading_factor,
                bandwidth_index,
                coding_rate_index=coding_rate_index,
            )
        )

    def next_status(self, *, timeout: float | None = None) -> ReceiverStatus:
        return self._call(self._owner._client.receiver.statuses.next(timeout=timeout))

    def enter_bootloader(self) -> SubmissionReceipt:
        return self._call(self._owner._client.receiver.enter_bootloader())


class ObcModule(_SyncService):
    def get_info(self, *, timeout: float = 15.0) -> ObcInfo:
        return self._call(self._owner._client.obc.get_info(timeout=timeout))

    def get_flash_settings(self, *, timeout: float = 15.0) -> FlashSettings:
        return self._call(self._owner._client.obc.get_flash_settings(timeout=timeout))

    def set_startup_sound(self, enabled: bool, *, timeout: float = 15.0) -> SubmissionReceipt:
        return self._call(self._owner._client.obc.set_startup_sound(enabled, read_timeout=timeout))

    def set_flash_settings(self, settings: FlashSettings) -> SubmissionReceipt:
        return self._call(self._owner._client.obc.set_flash_settings(settings))

    def set_telemetry_during_camera(self, enabled: bool) -> SubmissionReceipt:
        return self._call(self._owner._client.obc.set_telemetry_during_camera(enabled))

    def set_rf_config(self, spreading_factor: int, bandwidth_index: int) -> SubmissionReceipt:
        return self._call(self._owner._client.obc.set_rf_config(spreading_factor, bandwidth_index))


class ArduinoModule(_SyncService):
    def set_rgb(self, red: int, green: int, blue: int) -> SubmissionReceipt:
        return self._call(self._owner._client.arduino.set_rgb(red, green, blue))

    def play_startup_song(self) -> SubmissionReceipt:
        return self._call(self._owner._client.arduino.play_startup_song())


class TelemetryModule(_SyncService):
    @property
    def latest(self) -> MainTelemetrySample | None:
        return self._owner._client.telemetry.latest

    def next_sample(self, *, timeout: float | None = None) -> MainTelemetrySample:
        return self._call(self._owner._client.telemetry.next_sample(timeout=timeout))

    @property
    def latest_user(self) -> UserTelemetrySample | None:
        return self._owner._client.telemetry.latest_user

    @property
    def user_names(self) -> dict[int, str]:
        return dict(self._owner._client.telemetry.user_name_by_index)

    def next_user_sample(self, *, timeout: float | None = None) -> UserTelemetrySample:
        return self._call(self._owner._client.telemetry.user_samples.next(timeout=timeout))

    def next_user_name(self, *, timeout: float | None = None) -> UserDataName:
        return self._call(self._owner._client.telemetry.user_names.next(timeout=timeout))

    def iter_samples(self, *, timeout: float | None = None) -> Iterator[MainTelemetrySample]:
        while self._owner.is_open:
            yield self.next_sample(timeout=timeout)


class CalibrationModule(_SyncService):
    def run(
        self,
        *,
        gyroscope_timeout: float = 60.0,
        magnetometer_timeout: float = 120.0,
    ) -> CalibrationResult:
        return self._call(
            self._owner._client.calibration.run(
                gyroscope_timeout=gyroscope_timeout,
                magnetometer_timeout=magnetometer_timeout,
            )
        )

    def apply_runtime(self, coefficients: CalibrationCoefficients) -> SubmissionReceipt:
        return self._call(self._owner._client.calibration.apply_runtime(coefficients))


class CameraModule(_SyncService):
    def capture(
        self,
        *,
        timeout: float = 30.0,
        on_block: Callable[[CameraBlock], None] | None = None,
    ) -> CameraImage:
        camera = self._owner._client.camera
        if on_block is None:
            return self._call(camera.capture(timeout=timeout))

        async def capture_with_progress() -> CameraImage:
            async with camera.blocks.subscribe() as blocks:
                capture_task = asyncio.create_task(camera.capture(timeout=timeout))
                block_task = asyncio.create_task(anext(blocks))
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            (capture_task, block_task), return_when=asyncio.FIRST_COMPLETED
                        )
                        if block_task in done:
                            on_block(block_task.result())
                        if capture_task in done:
                            return await capture_task
                        block_task = asyncio.create_task(anext(blocks))
                finally:
                    if not block_task.done():
                        block_task.cancel()
                    await asyncio.gather(block_task, return_exceptions=True)
                    if not capture_task.done():
                        capture_task.cancel()
                    await asyncio.gather(capture_task, return_exceptions=True)

        return self._call(capture_with_progress())


class ReactionWheelModule(_SyncService):
    @property
    def latest(self) -> ReactionWheelSample | None:
        return self._owner._client.addons.reaction_wheel.latest

    def enable(self) -> SubmissionReceipt:
        return self._call(self._owner._client.addons.reaction_wheel.enable())

    def disable(self) -> SubmissionReceipt:
        return self._call(self._owner._client.addons.reaction_wheel.disable())

    def set_target(self, target: int) -> SubmissionReceipt:
        return self._call(self._owner._client.addons.reaction_wheel.set_target(target))

    def set_pid_gains(self, p: float, i: float, d: float) -> SubmissionReceipt:
        return self._call(self._owner._client.addons.reaction_wheel.set_pid_gains(p, i, d))

    def next_sample(self, *, timeout: float | None = None) -> ReactionWheelSample:
        return self._call(self._owner._client.addons.reaction_wheel.next_sample(timeout=timeout))


class AdvancedSensorModule(_SyncService):
    @property
    def latest(self) -> AdvancedSensorSample | None:
        return self._owner._client.addons.advanced_sensor.latest

    @property
    def latest_status(self) -> AdvancedSensorStatus | None:
        return self._owner._client.addons.advanced_sensor.latest_status

    def start_recording(self) -> SubmissionReceipt:
        return self._call(self._owner._client.addons.advanced_sensor.start_recording())

    def stop_recording(self) -> SubmissionReceipt:
        return self._call(self._owner._client.addons.advanced_sensor.stop_recording())

    def next_sample(self, *, timeout: float | None = None) -> AdvancedSensorSample:
        return self._call(self._owner._client.addons.advanced_sensor.next_sample(timeout=timeout))

    def next_status(self, *, timeout: float | None = None) -> AdvancedSensorStatus:
        return self._call(self._owner._client.addons.advanced_sensor.next_status(timeout=timeout))


class EnvironmentalSensorModule(_SyncService):
    @property
    def latest(self) -> EnvironmentalSensorSample | None:
        return self._owner._client.addons.environmental_sensor.latest

    def next_sample(self, *, timeout: float | None = None) -> EnvironmentalSensorSample:
        return self._call(
            self._owner._client.addons.environmental_sensor.next_sample(timeout=timeout)
        )


class AddonManager(_SyncService):
    def __init__(self, owner: RASCube) -> None:
        super().__init__(owner)
        self.reaction_wheel = ReactionWheelModule(owner)
        self.advanced_sensor = AdvancedSensorModule(owner)
        self.environmental_sensor = EnvironmentalSensorModule(owner)

    def refresh_enabled(self, *, timeout: float = 15.0) -> AddonPresence:
        return self._call(self._owner._client.addons.refresh_enabled(timeout=timeout))

    def get_versions(self, *, timeout: float = 15.0) -> AddonVersions:
        return self._call(self._owner._client.addons.get_versions(timeout=timeout))

    def request_status(self) -> SubmissionReceipt:
        return self._call(self._owner._client.addons.request_status())


class RASCube:
    """Blocking facade over the asynchronous RASCubeV2 client."""

    def __init__(
        self,
        port: str | None = None,
        *,
        serial_number: int,
        transport: AsyncByteTransport | None = None,
        initialize: bool = True,
        stale_after: float = 20.0,
    ) -> None:
        self._options = (port, serial_number, transport, initialize, stale_after)
        self._loop_thread: _LoopThread | None = None
        self._client_instance: AsyncRASCube | None = None
        self._state_lock = threading.RLock()

    def __enter__(self) -> RASCube:
        with self._state_lock:
            if self._loop_thread is not None:
                if self._client_instance is not None:
                    return self
                raise RuntimeError("RASCube connection is already opening")
            loop_thread = _LoopThread()
            self._loop_thread = loop_thread
            port, serial_number, transport, initialize, stale_after = self._options
        loop_thread.start()

        async def create() -> AsyncRASCube:
            client = AsyncRASCube(
                port,
                serial_number=serial_number,
                transport=transport,
                initialize=initialize,
                stale_after=stale_after,
            )
            await client.open()
            with self._state_lock:
                if self._loop_thread is loop_thread:
                    self._client_instance = client
                    return client
            await client.close()
            raise RuntimeError("RASCube connection was closed while opening")

        try:
            loop_thread.submit(create())
        except BaseException:
            with self._state_lock:
                self._client_instance = None
                if self._loop_thread is loop_thread:
                    self._loop_thread = None
            if loop_thread._thread.is_alive():
                loop_thread.stop()
            raise
        with self._state_lock:
            if self._client_instance is None or self._loop_thread is not loop_thread:
                raise RuntimeError("RASCube connection closed during startup")
            self.receiver = ReceiverModule(self)
            self.obc = ObcModule(self)
            self.arduino = ArduinoModule(self)
            self.telemetry = TelemetryModule(self)
            self.calibration = CalibrationModule(self)
            self.camera = CameraModule(self)
            self.addons = AddonManager(self)
            return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        with self._state_lock:
            return self._client_instance is not None and self._client_instance.is_open

    @property
    def serial_number(self) -> int:
        return self._client.serial_number

    @property
    def mode(self) -> ReceiverMode | None:
        return self._client.mode

    @property
    def transport_state(self) -> TransportState:
        return self._client.connection.transport_state

    @property
    def satellite_state(self) -> SatelliteLinkState:
        return self._client.connection.satellite_state

    @property
    def statistics(self) -> LinkStatistics:
        return self._client.connection.statistics

    def select_satellite(self, serial_number: int) -> SubmissionReceipt:
        return self._call(self._client.select_satellite(serial_number))

    @property
    def _client(self) -> AsyncRASCube:
        with self._state_lock:
            if self._client_instance is None:
                raise RuntimeError("use RASCube as a context manager before accessing modules")
            return self._client_instance

    def close(self) -> None:
        with self._state_lock:
            loop_thread = self._loop_thread
            client = self._client_instance
            self._client_instance = None
            self._loop_thread = None
        if loop_thread is None:
            return
        try:
            if client is not None:
                loop_thread.submit(client.close())
        finally:
            loop_thread.stop()

    def _call(self, coroutine: Coroutine[Any, Any, T]) -> T:
        with self._state_lock:
            loop_thread = self._loop_thread
            if loop_thread is None:
                coroutine.close()
                raise RuntimeError("RASCube connection is not open")
        return loop_thread.submit(coroutine)


__all__ = ["RASCube"]
