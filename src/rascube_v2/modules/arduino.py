from __future__ import annotations

from rascube_v2.constants import HostPort
from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.protocol.bus import CommandBus


class ArduinoModule:
    def __init__(self, bus: CommandBus) -> None:
        self._bus = bus

    async def set_rgb(self, red: int, green: int, blue: int) -> SubmissionReceipt:
        values = (red, green, blue)
        if any(not 0 <= value <= 0xFF for value in values):
            raise ValueError("RGB components must be between 0 and 255")
        return await self._bus.submit(HostPort.ARDUINO_RGB, bytes(values))

    async def play_startup_song(self) -> SubmissionReceipt:
        return await self._bus.submit(HostPort.ARDUINO_STARTUP_SONG, b"\x00")
