from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from math import isfinite
from time import monotonic

from rascube_v2.constants import DEFAULT_SATELLITE_STALE_SECONDS, InboundPort
from rascube_v2.models.common import LinkStatistics, SatelliteLinkState, TransportState
from rascube_v2.protocol.events import EventStream
from rascube_v2.protocol.frame import UsbFrame


class ConnectionMonitor:
    def __init__(self, *, stale_after: float = DEFAULT_SATELLITE_STALE_SECONDS) -> None:
        if not isfinite(stale_after) or stale_after <= 0:
            raise ValueError("stale_after must be a finite positive number")
        self.stale_after = stale_after
        self.transport_state = TransportState.DISCONNECTED
        self.satellite_state = SatelliteLinkState.NEVER_SEEN
        self.transport_events: EventStream[TransportState] = EventStream(default_queue_size=10)
        self.satellite_events: EventStream[SatelliteLinkState] = EventStream(default_queue_size=10)
        self.statistics_events: EventStream[LinkStatistics] = EventStream(default_queue_size=10)
        self.statistics = LinkStatistics()
        self.last_satellite_frame_monotonic: float | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self.last_satellite_frame_monotonic = None
            self._set_satellite_state(SatelliteLinkState.NEVER_SEEN)
            self._task = asyncio.create_task(self._run(), name="rascube-link-monitor")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def interrupt(self, error: BaseException) -> None:
        self.transport_events.interrupt(error)
        self.satellite_events.interrupt(error)
        self.statistics_events.interrupt(error)

    def set_transport_state(self, state: TransportState) -> None:
        if state is self.transport_state:
            return
        self.transport_state = state
        self.transport_events.publish(state)

    def record_outbound(self, payload_length: int) -> None:
        self.statistics = replace(
            self.statistics,
            outbound_bytes=self.statistics.outbound_bytes + payload_length + 2,
            outbound_frames=self.statistics.outbound_frames + 1,
        )
        self.statistics_events.publish(self.statistics)

    def record_inbound(self, frame: UsbFrame) -> None:
        self.statistics = replace(
            self.statistics,
            inbound_bytes=self.statistics.inbound_bytes + len(frame.payload) + 2,
            inbound_frames=self.statistics.inbound_frames + 1,
        )
        self.statistics_events.publish(self.statistics)

        if self._is_satellite_frame(frame):
            self.last_satellite_frame_monotonic = frame.received_monotonic or monotonic()
            self._set_satellite_state(SatelliteLinkState.ACTIVE)

    @property
    def satellite_age(self) -> float | None:
        if self.last_satellite_frame_monotonic is None:
            return None
        return max(0.0, monotonic() - self.last_satellite_frame_monotonic)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(min(1.0, self.stale_after / 4.0))
            age = self.satellite_age
            if age is not None and age > self.stale_after:
                self._set_satellite_state(SatelliteLinkState.STALE)

    def _set_satellite_state(self, state: SatelliteLinkState) -> None:
        if state is self.satellite_state:
            return
        self.satellite_state = state
        self.satellite_events.publish(state)

    @staticmethod
    def _is_satellite_frame(frame: UsbFrame) -> bool:
        if frame.port in (
            InboundPort.USB_INFO,
            InboundPort.USB_MODE,
            InboundPort.BOOTLOADER_RESPONSE,
        ):
            return False
        if frame.port == InboundPort.STATUS:
            return len(frame.payload) == 3 and frame.payload[0] in (0, 1)
        return True
