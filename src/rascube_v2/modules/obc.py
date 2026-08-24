from __future__ import annotations

from rascube_v2.constants import HostPort, InboundPort, validate_rf
from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.models.obc import FlashSettings, ObcInfo
from rascube_v2.protocol.bus import CommandBus
from rascube_v2.protocol.codecs import decode_flash_settings, decode_obc_info


class ObcModule:
    def __init__(self, bus: CommandBus) -> None:
        self._bus = bus

    async def get_info(self, *, timeout: float = 15.0) -> ObcInfo:
        frame = await self._bus.request(
            HostPort.OBC_INFO,
            b"\x00",
            key="obc-info",
            matcher=lambda item: item.port == InboundPort.OBC_INFO,
            timeout=timeout,
        )
        return decode_obc_info(frame)

    async def get_flash_settings(self, *, timeout: float = 15.0) -> FlashSettings:
        frame = await self._bus.request(
            HostPort.OBC_FLASH_SETTINGS_GET,
            b"\x00",
            key="obc-flash-settings",
            matcher=lambda item: item.port == InboundPort.OBC_FLASH_SETTINGS,
            timeout=timeout,
        )
        return decode_flash_settings(frame)

    async def set_flash_settings(self, settings: FlashSettings) -> SubmissionReceipt:
        if not 0 <= settings.raw_flags <= 0xFF:
            raise ValueError("flash setting flags must fit one byte")
        return await self._bus.submit(HostPort.OBC_FLASH_SETTINGS_SET, bytes((settings.raw_flags,)))

    async def set_startup_sound(
        self, enabled: bool, *, read_timeout: float = 15.0
    ) -> SubmissionReceipt:
        current = await self.get_flash_settings(timeout=read_timeout)
        return await self.set_flash_settings(current.with_startup_sound(enabled))

    async def set_telemetry_during_camera(self, enabled: bool) -> SubmissionReceipt:
        return await self._bus.submit(HostPort.OBC_TELEMETRY_DURING_CAMERA, bytes((int(enabled),)))

    async def set_rf_config(self, spreading_factor: int, bandwidth_index: int) -> SubmissionReceipt:
        """Expert operation: receiver and OBC RF changes are not atomic or acknowledged."""
        validate_rf(spreading_factor, bandwidth_index)
        return await self._bus.submit(
            HostPort.OBC_RF_CONFIG, bytes((spreading_factor, bandwidth_index))
        )
