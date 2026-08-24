from __future__ import annotations

import struct
from math import isfinite

from rascube_v2.addons.base import AddonCommandSender
from rascube_v2.constants import AddonId
from rascube_v2.models.addons import ReactionWheelSample
from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.protocol.events import EventStream


class ReactionWheelModule:
    def __init__(self, manager: AddonCommandSender) -> None:
        self._manager = manager
        self.samples: EventStream[ReactionWheelSample] = EventStream(default_queue_size=100)
        self.latest: ReactionWheelSample | None = None

    async def enable(self) -> SubmissionReceipt:
        return await self._send(bytes((0x00, 0x01)))

    async def disable(self) -> SubmissionReceipt:
        return await self._send(bytes((0x00, 0x00)))

    async def set_target(self, target: int) -> SubmissionReceipt:
        if not -(1 << 31) <= target < (1 << 31):
            raise ValueError("target must fit a signed 32-bit integer")
        return await self._send(bytes((0x01,)) + struct.pack("<i", target))

    async def set_pid_gains(self, p: float, i: float, d: float) -> SubmissionReceipt:
        if not all(isfinite(value) for value in (p, i, d)):
            raise ValueError("PID gains must be finite")
        if i == 0:
            raise ValueError("the current Arduino PID implementation requires a nonzero I gain")
        return await self._send(bytes((0x02,)) + struct.pack("<fff", p, i, d))

    async def next_sample(self, *, timeout: float | None = None) -> ReactionWheelSample:
        return await self.samples.next(timeout=timeout)

    async def _send(self, command: bytes) -> SubmissionReceipt:
        return await self._manager._send_addon_command(AddonId.REACTION_WHEEL, command)

    def _publish(self, sample: ReactionWheelSample) -> None:
        self.latest = sample
        self.samples.publish(sample)
