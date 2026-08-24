from __future__ import annotations

import struct
from collections.abc import Callable

from rascube_v2.constants import BANDWIDTH_KHZ, HostPort, InboundPort, ReceiverMode, validate_rf
from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.models.receiver import ReceiverInfo, ReceiverStatus
from rascube_v2.protocol.bus import CommandBus
from rascube_v2.protocol.codecs import (
    decode_receiver_info,
    decode_receiver_mode,
    decode_receiver_status,
)
from rascube_v2.protocol.events import EventStream
from rascube_v2.protocol.frame import UsbFrame


class ReceiverModule:
    def __init__(
        self,
        bus: CommandBus,
        on_satellite_selected: Callable[[int], None] | None = None,
    ) -> None:
        self._bus = bus
        self._on_satellite_selected = on_satellite_selected
        self.statuses: EventStream[ReceiverStatus] = EventStream(default_queue_size=50)
        bus.register_handler(InboundPort.STATUS, self._handle_status)

    async def get_info(self, *, timeout: float = 2.0) -> ReceiverInfo:
        frame = await self._bus.request(
            HostPort.USB_INFO,
            b"\x00",
            key="receiver-info",
            matcher=lambda item: item.port == InboundPort.USB_INFO,
            timeout=timeout,
            radio_bound=False,
        )
        return decode_receiver_info(frame)

    async def get_mode(self, *, timeout: float = 2.0) -> ReceiverMode:
        frame = await self._bus.request(
            HostPort.USB_MODE,
            b"\x00",
            key="receiver-mode",
            matcher=lambda item: item.port == InboundPort.USB_MODE,
            timeout=timeout,
            radio_bound=False,
        )
        return decode_receiver_mode(frame)

    async def set_satellite_serial_number(self, serial_number: int) -> SubmissionReceipt:
        if not 0 <= serial_number <= 0xFFFFFFFF:
            raise ValueError("serial_number must fit an unsigned 32-bit integer")
        receipt = await self._bus.submit(
            HostPort.USB_SERIAL_FILTER,
            struct.pack("<I", serial_number),
            radio_bound=False,
        )
        if self._on_satellite_selected is not None:
            self._on_satellite_selected(serial_number)
        return receipt

    async def set_rf_config(
        self,
        spreading_factor: int,
        bandwidth_index: int,
        *,
        coding_rate_index: int | None = None,
    ) -> SubmissionReceipt:
        validate_rf(spreading_factor, bandwidth_index)
        payload = bytes((spreading_factor, bandwidth_index))
        if coding_rate_index is not None:
            if coding_rate_index != 0:
                raise ValueError(
                    "the OBC is fixed at coding rate 4/5; only coding_rate_index=0 is safe"
                )
            payload += bytes((coding_rate_index,))
        return await self._bus.submit(HostPort.USB_RF_CONFIG, payload, radio_bound=False)

    async def enter_bootloader(self) -> SubmissionReceipt:
        return await self._bus.submit(HostPort.USB_BOOTLOADER_ENTER, b"\x00", radio_bound=False)

    @staticmethod
    def wireless_channel(serial_number: int) -> int:
        return serial_number % 18

    @classmethod
    def frequency_hz(cls, serial_number: int) -> int:
        return 916_000_000 + cls.wireless_channel(serial_number) * 600_000

    @staticmethod
    def radio_address(serial_number: int) -> int:
        return serial_number & 0xFFFF

    @staticmethod
    def bandwidth_khz(index: int) -> int:
        if not 0 <= index < len(BANDWIDTH_KHZ):
            raise ValueError("bandwidth index must be between 0 and 9")
        return BANDWIDTH_KHZ[index]

    def _handle_status(self, frame: UsbFrame) -> None:
        if len(frame.payload) == 3 and frame.payload[0] == 2:
            self.statuses.publish(decode_receiver_status(frame))

    def interrupt(self, error: BaseException) -> None:
        self.statuses.interrupt(error)
