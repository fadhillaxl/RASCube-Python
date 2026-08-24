from __future__ import annotations

from rascube_v2.constants import InboundPort
from rascube_v2.models.telemetry import (
    MainTelemetrySample,
    UserDataName,
    UserTelemetrySample,
)
from rascube_v2.protocol.bus import CommandBus
from rascube_v2.protocol.codecs import (
    decode_main_telemetry,
    decode_user_data_name,
    decode_user_telemetry,
)
from rascube_v2.protocol.events import EventStream
from rascube_v2.protocol.frame import UsbFrame


class TelemetryModule:
    def __init__(self, bus: CommandBus) -> None:
        self.samples: EventStream[MainTelemetrySample] = EventStream(default_queue_size=100)
        self.user_samples: EventStream[UserTelemetrySample] = EventStream(default_queue_size=100)
        self.user_names: EventStream[UserDataName] = EventStream(default_queue_size=50)
        self.latest: MainTelemetrySample | None = None
        self.latest_user: UserTelemetrySample | None = None
        self.user_name_by_index: dict[int, str] = {}

        bus.register_handler(InboundPort.MAIN_TELEMETRY, self._handle_main)
        bus.register_handler(InboundPort.USER_TELEMETRY, self._handle_user)
        bus.register_handler(InboundPort.USER_DATA_NAME, self._handle_name)

    async def next_sample(self, *, timeout: float | None = None) -> MainTelemetrySample:
        return await self.samples.next(timeout=timeout)

    def _handle_main(self, frame: UsbFrame) -> None:
        sample = decode_main_telemetry(frame)
        self.latest = sample
        self.samples.publish(sample)

    def _handle_user(self, frame: UsbFrame) -> None:
        sample = decode_user_telemetry(frame)
        self.latest_user = sample
        self.user_samples.publish(sample)

    def _handle_name(self, frame: UsbFrame) -> None:
        update = decode_user_data_name(frame)
        self.user_name_by_index[update.index] = update.name
        self.user_names.publish(update)

    def interrupt(self, error: BaseException) -> None:
        self.samples.interrupt(error)
        self.user_samples.interrupt(error)
        self.user_names.interrupt(error)

    def reset_connection(self) -> None:
        self.latest = None
        self.latest_user = None
        self.user_name_by_index.clear()
