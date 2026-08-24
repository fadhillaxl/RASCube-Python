from __future__ import annotations

from rascube_v2.models.addons import EnvironmentalSensorSample
from rascube_v2.protocol.events import EventStream


class EnvironmentalSensorModule:
    """Telemetry-only decoder based on the current UI's 22-byte payload profile."""

    def __init__(self) -> None:
        self.samples: EventStream[EnvironmentalSensorSample] = EventStream(default_queue_size=100)
        self.latest: EnvironmentalSensorSample | None = None

    async def next_sample(self, *, timeout: float | None = None) -> EnvironmentalSensorSample:
        return await self.samples.next(timeout=timeout)

    def _publish(self, sample: EnvironmentalSensorSample) -> None:
        self.latest = sample
        self.samples.publish(sample)
