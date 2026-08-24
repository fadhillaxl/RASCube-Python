from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FirmwareInfo:
    software_version: int
    git_hash: str | None
    dirty: bool


@dataclass(frozen=True, slots=True)
class ObcInfo:
    stm: FirmwareInfo
    arduino: FirmwareInfo
    arduino_info_cached: bool


@dataclass(frozen=True, slots=True)
class FlashSettings:
    raw_flags: int

    @property
    def startup_sound_enabled(self) -> bool:
        return bool(self.raw_flags & 0x01)

    def with_startup_sound(self, enabled: bool) -> FlashSettings:
        flags = self.raw_flags | 0x01 if enabled else self.raw_flags & ~0x01
        return FlashSettings(flags)
