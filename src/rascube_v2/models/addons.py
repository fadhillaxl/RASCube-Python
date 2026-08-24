from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from rascube_v2.models.common import EventMetadata


class WheelSpeedUnit(Enum):
    UNKNOWN = "unknown"
    RPM_X10 = "rpm_x10"
    ELECTRICAL_REVOLUTIONS_PER_SECOND_X10 = "electrical_revolutions_per_second_x10"


@dataclass(frozen=True, slots=True)
class AddonPresence:
    enabled_ids: frozenset[int]
    raw_bitmap: bytes

    def is_enabled(self, addon_id: int) -> bool:
        return addon_id in self.enabled_ids


@dataclass(frozen=True, slots=True)
class AddonVersions:
    versions: Mapping[int, int]

    @classmethod
    def create(cls, versions: dict[int, int]) -> AddonVersions:
        return cls(MappingProxyType(dict(versions)))


@dataclass(frozen=True, slots=True)
class ReactionWheelSample:
    device_uptime_ms: int
    measured_orientation: int
    target_orientation: int
    p_contribution: int
    i_contribution: int
    d_contribution: int
    target_speed: int
    measured_speed: int
    speed_unit: WheelSpeedUnit
    metadata: EventMetadata


@dataclass(frozen=True, slots=True)
class AdvancedSensorSample:
    device_uptime_ms: int
    dba: float
    dba_slow: float
    dba_fast: float
    db_spl: float
    db_spl_slow: float
    db_spl_fast: float
    radiation_events: int
    cpm_slow: float
    cpm_fast: float
    metadata: EventMetadata


@dataclass(frozen=True, slots=True)
class AdvancedSensorStatus:
    sd_raw: int
    microphone_raw: int
    radiation_raw: int
    metadata: EventMetadata

    @property
    def recording(self) -> bool:
        return bool(self.sd_raw & (1 << 3))

    @property
    def sd_ready(self) -> bool:
        required = (1 << 0) | (1 << 1) | (1 << 2)
        return self.sd_raw & required == required

    @property
    def microphone_initialized(self) -> bool:
        return bool(self.microphone_raw & (1 << 0))

    @property
    def radiation_initialized(self) -> bool:
        return bool(self.radiation_raw & (1 << 1))


@dataclass(frozen=True, slots=True)
class EnvironmentalSensorSample:
    device_uptime_ms: int
    sht20_temperature_c: float
    sht20_humidity_percent: float
    co2_temperature_c: float
    co2_humidity_percent: float
    co2_ppm: int
    voc_raw: int
    nox_raw: int
    voc_index: int
    nox_index: int
    metadata: EventMetadata
