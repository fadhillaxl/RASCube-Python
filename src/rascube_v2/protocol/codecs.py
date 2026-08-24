from __future__ import annotations

import struct
from time import monotonic

from rascube_v2.constants import AddonId, CalibrationStage, ReceiverMode, ReceiverStatusFlag
from rascube_v2.exceptions import ProtocolDecodeError
from rascube_v2.models.addons import (
    AddonPresence,
    AddonVersions,
    AdvancedSensorSample,
    AdvancedSensorStatus,
    EnvironmentalSensorSample,
    ReactionWheelSample,
    WheelSpeedUnit,
)
from rascube_v2.models.calibration import CalibrationEvent
from rascube_v2.models.camera import CameraBlock
from rascube_v2.models.common import EventMetadata
from rascube_v2.models.obc import FirmwareInfo, FlashSettings, ObcInfo
from rascube_v2.models.receiver import ReceiverInfo, ReceiverStatus
from rascube_v2.models.telemetry import (
    BarometerTelemetry,
    EpsTelemetry,
    GpsTelemetry,
    ImuTelemetry,
    MainTelemetrySample,
    PowerMeasurement,
    UserDataName,
    UserTelemetrySample,
    Vector3,
)
from rascube_v2.protocol.frame import UsbFrame


def metadata(frame: UsbFrame) -> EventMetadata:
    return EventMetadata(
        port=frame.port,
        received_monotonic=frame.received_monotonic or monotonic(),
        raw_payload=frame.payload,
    )


def _require_length(frame: UsbFrame, expected: int) -> bytes:
    if len(frame.payload) != expected:
        raise ProtocolDecodeError(
            f"port 0x{frame.port:02X} expected {expected} bytes, got {len(frame.payload)}"
        )
    return frame.payload


def _decode_hash(raw: bytes) -> str | None:
    if not any(raw):
        return None
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolDecodeError("firmware hash is not ASCII") from exc


def decode_receiver_info(frame: UsbFrame) -> ReceiverInfo:
    payload = frame.payload
    if len(payload) == 4:
        version = struct.unpack_from("<I", payload)[0]
        return ReceiverInfo(version, None, False)
    if len(payload) == 13:
        version = struct.unpack_from("<I", payload)[0]
        return ReceiverInfo(version, _decode_hash(payload[4:12]), bool(payload[12]))
    raise ProtocolDecodeError(f"receiver info expected 4 or 13 bytes, got {len(payload)}")


def decode_receiver_mode(frame: UsbFrame) -> ReceiverMode:
    payload = _require_length(frame, 1)
    try:
        return ReceiverMode(payload[0])
    except ValueError as exc:
        raise ProtocolDecodeError(f"unknown receiver mode {payload[0]}") from exc


def decode_receiver_status(frame: UsbFrame) -> ReceiverStatus:
    payload = _require_length(frame, 3)
    return ReceiverStatus(
        source=payload[0],
        flags=ReceiverStatusFlag(struct.unpack_from("<H", payload, 1)[0]),
        metadata=metadata(frame),
    )


def decode_obc_info(frame: UsbFrame) -> ObcInfo:
    payload = frame.payload
    if len(payload) == 4:
        stm_version, arduino_version = struct.unpack("<HH", payload)
        return ObcInfo(
            FirmwareInfo(stm_version, None, False),
            FirmwareInfo(arduino_version, None, False),
            arduino_info_cached=False,
        )
    if len(payload) == 26:
        stm = FirmwareInfo(
            struct.unpack_from("<I", payload, 0)[0],
            _decode_hash(payload[4:12]),
            bool(payload[12]),
        )
        arduino_hash = _decode_hash(payload[17:25])
        arduino = FirmwareInfo(
            struct.unpack_from("<I", payload, 13)[0],
            arduino_hash,
            bool(payload[25]),
        )
        return ObcInfo(stm, arduino, arduino_info_cached=arduino_hash is not None)
    raise ProtocolDecodeError(f"OBC info expected 4 or 26 bytes, got {len(payload)}")


def decode_flash_settings(frame: UsbFrame) -> FlashSettings:
    return FlashSettings(_require_length(frame, 1)[0])


def decode_main_telemetry(frame: UsbFrame) -> MainTelemetrySample:
    payload = _require_length(frame, 121)

    def u16(offset: int) -> int:
        return int(struct.unpack_from("<H", payload, offset)[0])

    def i16(offset: int) -> int:
        return int(struct.unpack_from("<h", payload, offset)[0])

    def f32(offset: int) -> float:
        return float(struct.unpack_from("<f", payload, offset)[0])

    magnetometer_raw = (i16(44), i16(46), i16(48))
    accelerometer_raw = (i16(60), i16(62), i16(64))
    gyroscope_raw = (i16(66), i16(68), i16(70))

    eps = EpsTelemetry(
        main_5v_v=u16(4) / 1000.0,
        main_3v3_v=u16(6) / 1000.0,
        solar_ldr_raw=(u16(8), u16(10), u16(12)),
        battery_charge=PowerMeasurement(i16(14) / 1000.0, i16(16) / 1000.0),
        usb=PowerMeasurement(i16(18) / 1000.0, i16(20) / 1000.0),
        battery_draw=PowerMeasurement(i16(22) / 1000.0, i16(24) / 1000.0),
        solar=(
            PowerMeasurement(i16(26) / 1000.0, i16(28) / 1000.0),
            PowerMeasurement(i16(30) / 1000.0, i16(32) / 1000.0),
            PowerMeasurement(i16(34) / 1000.0, i16(36) / 1000.0),
        ),
        charging_complete=bool(payload[38]),
        charge_power_good=bool(payload[39]),
    )
    imu = ImuTelemetry(
        magnetometer_raw=magnetometer_raw,
        magnetometer_gauss=Vector3(*(value * 8.0 / 32768.0 for value in magnetometer_raw)),
        accelerometer_raw=accelerometer_raw,
        accelerometer_g=Vector3(*(value * 0.000061 for value in accelerometer_raw)),
        gyroscope_raw=gyroscope_raw,
        gyroscope_dps=Vector3(*(value * 0.0175 for value in gyroscope_raw)),
        orientation_degrees=Vector3(f32(98), f32(102), f32(106)),
    )
    gps = GpsTelemetry(
        latitude=f32(72),
        longitude=f32(76),
        altitude_m=f32(80),
        speed_raw=f32(84),
        course_degrees=f32(88),
        hdop=f32(92),
        satellites=payload[96],
        fix=bool(payload[97]),
    )
    return MainTelemetrySample(
        packet_sequence=struct.unpack_from("<I", payload, 0)[0],
        device_uptime_ms=struct.unpack_from("<I", payload, 40)[0],
        eps=eps,
        barometer=BarometerTelemetry(i16(50) / 10.0, f32(52) / 100.0, f32(56)),
        imu=imu,
        gps=gps,
        error_code=u16(110),
        stm_version=payload[112],
        receiver_rssi=f32(113),
        receiver_snr=f32(117),
        metadata=metadata(frame),
    )


def decode_user_telemetry(frame: UsbFrame) -> UserTelemetrySample:
    if len(frame.payload) % 4:
        raise ProtocolDecodeError("user telemetry length must be divisible by four")
    count = len(frame.payload) // 4
    values = struct.unpack(f"<{count}f", frame.payload) if count else ()
    return UserTelemetrySample(tuple(values), metadata(frame))


def decode_user_data_name(frame: UsbFrame) -> UserDataName:
    if not frame.payload:
        raise ProtocolDecodeError("user-data name payload is empty")
    try:
        name = frame.payload[1:].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolDecodeError("user-data name is not ASCII") from exc
    return UserDataName(frame.payload[0], name, metadata(frame))


def decode_addon_presence(frame: UsbFrame) -> AddonPresence:
    payload = _require_length(frame, 8)
    enabled = {
        byte_index * 8 + bit
        for byte_index, value in enumerate(payload)
        for bit in range(8)
        if value & (1 << bit)
    }
    return AddonPresence(frozenset(enabled), payload)


def decode_addon_versions(frame: UsbFrame) -> AddonVersions:
    if len(frame.payload) < 3 or len(frame.payload) % 3:
        raise ProtocolDecodeError("add-on version payload must contain nonempty records")
    versions: dict[int, int] = {}
    for offset in range(0, len(frame.payload), 3):
        addon_id = frame.payload[offset]
        if addon_id in versions:
            raise ProtocolDecodeError(f"duplicate add-on version record for ID {addon_id}")
        versions[addon_id] = struct.unpack_from("<H", frame.payload, offset + 1)[0]
    return AddonVersions.create(versions)


def decode_reaction_wheel(frame: UsbFrame) -> ReactionWheelSample:
    payload = _require_length(frame, 19)
    if payload[0] != AddonId.REACTION_WHEEL:
        raise ProtocolDecodeError("not a reaction-wheel telemetry payload")
    values = struct.unpack_from("<I7h", payload, 1)
    return ReactionWheelSample(
        device_uptime_ms=values[0],
        measured_orientation=values[1],
        target_orientation=values[2],
        p_contribution=values[3],
        i_contribution=values[4],
        d_contribution=values[5],
        target_speed=values[6],
        measured_speed=values[7],
        speed_unit=WheelSpeedUnit.UNKNOWN,
        metadata=metadata(frame),
    )


def decode_advanced_sensor(frame: UsbFrame) -> AdvancedSensorSample:
    payload = _require_length(frame, 25)
    if payload[0] != AddonId.ADVANCED_SENSOR:
        raise ProtocolDecodeError("not an Advanced Sensor telemetry payload")
    uptime, dba, dba_slow, dba_fast, spl, spl_slow, spl_fast, events, cpm_slow, cpm_fast = (
        struct.unpack_from("<I6HI2H", payload, 1)
    )
    return AdvancedSensorSample(
        uptime,
        dba / 100.0,
        dba_slow / 100.0,
        dba_fast / 100.0,
        spl / 100.0,
        spl_slow / 100.0,
        spl_fast / 100.0,
        events,
        cpm_slow / 50.0,
        cpm_fast / 50.0,
        metadata(frame),
    )


def decode_advanced_sensor_status(frame: UsbFrame) -> AdvancedSensorStatus:
    payload = _require_length(frame, 4)
    if payload[0] != AddonId.ADVANCED_SENSOR:
        raise ProtocolDecodeError("not an Advanced Sensor status payload")
    return AdvancedSensorStatus(payload[1], payload[2], payload[3], metadata(frame))


def decode_environmental_sensor(frame: UsbFrame) -> EnvironmentalSensorSample:
    payload = _require_length(frame, 23)
    if payload[0] != AddonId.ENVIRONMENTAL_SENSOR:
        raise ProtocolDecodeError("not an environmental-sensor telemetry payload")
    uptime, sht_temp, sht_humidity, co2_temp, co2_humidity, co2_ppm, voc, nox, voc_i, nox_i = (
        struct.unpack_from("<I7H2h", payload, 1)
    )
    return EnvironmentalSensorSample(
        device_uptime_ms=uptime,
        sht20_temperature_c=sht_temp * 175.72 / 65536.0 - 46.85,
        sht20_humidity_percent=sht_humidity * 125.0 / 65536.0 - 6.0,
        co2_temperature_c=-45.0 + co2_temp * 175.0 / 65535.0,
        co2_humidity_percent=co2_humidity * 100.0 / 65535.0,
        co2_ppm=co2_ppm,
        voc_raw=voc,
        nox_raw=nox,
        voc_index=voc_i,
        nox_index=nox_i,
        metadata=metadata(frame),
    )


def decode_calibration_event(frame: UsbFrame) -> CalibrationEvent:
    payload = _require_length(frame, 1)
    try:
        stage = CalibrationStage(payload[0])
    except ValueError as exc:
        raise ProtocolDecodeError(f"unknown calibration status {payload[0]}") from exc
    return CalibrationEvent(stage, metadata(frame))


def decode_camera_block(frame: UsbFrame) -> CameraBlock:
    payload = _require_length(frame, 242)
    return CameraBlock(struct.unpack_from("<H", payload, 0)[0], payload[2:], metadata(frame))
