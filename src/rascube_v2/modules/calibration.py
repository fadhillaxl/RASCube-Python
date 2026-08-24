from __future__ import annotations

import asyncio
import struct
from math import isfinite
from time import monotonic

from rascube_v2.constants import CalibrationStage, HostPort, InboundPort
from rascube_v2.exceptions import RequestTimeoutError, SessionBusyError
from rascube_v2.models.calibration import (
    CalibrationCoefficients,
    CalibrationEvent,
    CalibrationResult,
)
from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.protocol.bus import CommandBus
from rascube_v2.protocol.codecs import decode_calibration_event
from rascube_v2.protocol.events import EventStream, Subscription
from rascube_v2.protocol.frame import UsbFrame


class CalibrationModule:
    def __init__(self, bus: CommandBus) -> None:
        self._bus = bus
        self._run_lock = asyncio.Lock()
        self._poisoned = False
        self.events: EventStream[CalibrationEvent] = EventStream(default_queue_size=20)
        bus.register_handler(InboundPort.CALIBRATION_STATUS, self._handle_status)

    async def _start(self) -> SubmissionReceipt:
        return await self._bus.submit(HostPort.ARDUINO_CALIBRATION_START, b"\x00")

    async def run(
        self,
        *,
        gyroscope_timeout: float = 60.0,
        magnetometer_timeout: float = 120.0,
    ) -> CalibrationResult:
        """Wait for sensor calibration stages; OBC flash persistence is not acknowledged."""
        if self._run_lock.locked():
            raise SessionBusyError("a calibration session is already active")
        if self._poisoned:
            raise SessionBusyError(
                "a prior calibration may still be running; wait for its magnetometer event"
            )
        async with self._run_lock, self.events.subscribe(max_queue=20) as subscription:
            try:
                await self._start()
                gyroscope = await self._wait_for_stage(
                    subscription, CalibrationStage.GYROSCOPE_COMPLETE, gyroscope_timeout
                )
                magnetometer = await self._wait_for_stage(
                    subscription, CalibrationStage.MAGNETOMETER_COMPLETE, magnetometer_timeout
                )
                return CalibrationResult(gyroscope, magnetometer)
            except BaseException:
                self._poisoned = True
                raise

    async def apply_runtime(self, coefficients: CalibrationCoefficients) -> SubmissionReceipt:
        """Apply non-persistent coefficients to the running Arduino firmware."""
        if self._poisoned or self._run_lock.locked():
            raise SessionBusyError("calibration state is ambiguous or active")
        if not all(isfinite(value) for value in coefficients.values()):
            raise ValueError("calibration coefficients must be finite")
        return await self._bus.submit(
            HostPort.ARDUINO_CALIBRATION_SET,
            struct.pack("<9f", *coefficients.values()),
        )

    async def _wait_for_stage(
        self,
        subscription: Subscription[CalibrationEvent],
        stage: CalibrationStage,
        timeout: float,
    ) -> CalibrationEvent:
        deadline = monotonic() + timeout
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RequestTimeoutError(
                    f"calibration did not reach {stage.name} within {timeout:.1f}s"
                )
            try:
                event = await asyncio.wait_for(anext(subscription), remaining)
            except TimeoutError as exc:
                raise RequestTimeoutError(
                    f"calibration did not reach {stage.name} within {timeout:.1f}s"
                ) from exc
            if event.stage is stage:
                return event

    def _handle_status(self, frame: UsbFrame) -> None:
        event = decode_calibration_event(frame)
        self.events.publish(event)
        if self._poisoned and event.stage is CalibrationStage.MAGNETOMETER_COMPLETE:
            self._poisoned = False

    def interrupt(self, error: BaseException) -> None:
        if self._run_lock.locked():
            self._poisoned = True
        self.events.interrupt(error)
