from __future__ import annotations

from dataclasses import dataclass

from rascube_v2.constants import CalibrationStage
from rascube_v2.models.common import EventMetadata


@dataclass(frozen=True, slots=True)
class CalibrationCoefficients:
    gyroscope_offsets: tuple[float, float, float]
    magnetometer_offsets: tuple[float, float, float]
    magnetometer_scales: tuple[float, float, float]

    def values(self) -> tuple[float, ...]:
        return self.gyroscope_offsets + self.magnetometer_offsets + self.magnetometer_scales


@dataclass(frozen=True, slots=True)
class CalibrationEvent:
    stage: CalibrationStage
    metadata: EventMetadata


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    gyroscope: CalibrationEvent
    magnetometer: CalibrationEvent
