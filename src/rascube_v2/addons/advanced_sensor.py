from __future__ import annotations

from rascube_v2.addons.base import AddonCommandSender
from rascube_v2.constants import AddonId
from rascube_v2.models.addons import AdvancedSensorSample, AdvancedSensorStatus
from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.protocol.events import EventStream


class AdvancedSensorModule:
    def __init__(self, manager: AddonCommandSender) -> None:
        self._manager = manager
        self.samples: EventStream[AdvancedSensorSample] = EventStream(default_queue_size=100)
        self.statuses: EventStream[AdvancedSensorStatus] = EventStream(default_queue_size=50)
        self.latest: AdvancedSensorSample | None = None
        self.latest_status: AdvancedSensorStatus | None = None

    async def start_recording(self) -> SubmissionReceipt:
        return await self._send(bytes((0x00, 0x00)))

    async def stop_recording(self) -> SubmissionReceipt:
        return await self._send(bytes((0x00, 0x01)))

    async def next_sample(self, *, timeout: float | None = None) -> AdvancedSensorSample:
        return await self.samples.next(timeout=timeout)

    async def next_status(self, *, timeout: float | None = None) -> AdvancedSensorStatus:
        return await self.statuses.next(timeout=timeout)

    async def _send(self, command: bytes) -> SubmissionReceipt:
        return await self._manager._send_addon_command(AddonId.ADVANCED_SENSOR, command)

    def _publish(self, sample: AdvancedSensorSample) -> None:
        self.latest = sample
        self.samples.publish(sample)

    def _publish_status(self, status: AdvancedSensorStatus) -> None:
        self.latest_status = status
        self.statuses.publish(status)
