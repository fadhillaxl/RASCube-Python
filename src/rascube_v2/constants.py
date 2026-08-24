from __future__ import annotations

from enum import IntEnum, IntFlag

USB_VID = 0x0483
USB_PID_V2 = 0x5740
DEFAULT_BAUDRATE = 1_000_000
MAX_USB_PAYLOAD = 255
MAX_RADIO_PAYLOAD = 247
DEFAULT_SATELLITE_STALE_SECONDS = 20.0


class HostPort(IntEnum):
    USB_INFO = 0x00
    USB_SERIAL_FILTER = 0x01
    USB_RF_CONFIG = 0x02
    USB_MODE = 0x03
    USB_BOOTLOADER_ENTER = 0x0A

    OBC_INFO = 0x12
    OBC_CAMERA = 0x13
    OBC_RF_CONFIG = 0x14
    OBC_TELEMETRY_DURING_CAMERA = 0x15
    OBC_FLASH_SETTINGS_SET = 0x16
    OBC_FLASH_SETTINGS_GET = 0x17

    ARDUINO_RGB = 0x80
    ARDUINO_BUZZER = 0x81
    ARDUINO_CALIBRATION_START = 0x82
    ARDUINO_CALIBRATION_SET = 0x83
    ARDUINO_STARTUP_SONG = 0x84
    ADDONS_ENABLED_GET = 0x85
    ADDON_COMMAND = 0x86
    ADDONS_STATUS_GET = 0x87
    ADDONS_INFO_GET = 0x88


class InboundPort(IntEnum):
    USB_INFO = 0x00
    USB_MODE = 0x01
    BOOTLOADER_RESPONSE = 0x0A
    MAIN_TELEMETRY = 0x10
    USER_TELEMETRY = 0x11
    OBC_INFO = 0x12
    LEGACY_CAMERA = 0x13
    OBC_FLASH_SETTINGS = 0x14
    JPEG_CAMERA = 0x15
    CALIBRATION_STATUS = 0x80
    USER_DATA_NAME = 0x82
    ADDONS_ENABLED = 0x86
    ADDON_TELEMETRY = 0x87
    ADDON_STATUS = 0x88
    ADDONS_INFO = 0x89
    STATUS = 0xF0


class AddonId(IntEnum):
    REACTION_WHEEL = 1
    ADVANCED_SENSOR = 2
    ENVIRONMENTAL_SENSOR = 3


class ReceiverMode(IntEnum):
    BOOTLOADER = 0
    BOOTLOADER_PROGRAMMING = 1
    APPLICATION = 2


class CalibrationStage(IntEnum):
    GYROSCOPE_COMPLETE = 1
    MAGNETOMETER_COMPLETE = 2


class ReceiverStatusFlag(IntFlag):
    HARDWARE_GOOD = 1 << 0
    USB_PACKET_DROPPED = 1 << 1
    RADIO_PACKET_DROPPED = 1 << 2
    RADIO_WAITING_FOR_RX = 1 << 3
    RADIO_PACKET_RECEIVED = 1 << 4
    RADIO_PACKET_TRANSMITTING = 1 << 5


BANDWIDTH_KHZ = (7, 10, 15, 20, 31, 41, 62, 125, 250, 500)
CODING_RATES = ("4/5", "4/6", "4/7", "4/8")


def validate_rf(spreading_factor: int, bandwidth_index: int) -> None:
    if not 5 <= spreading_factor <= 12:
        raise ValueError("spreading_factor must be between 5 and 12")
    if not 0 <= bandwidth_index < len(BANDWIDTH_KHZ):
        raise ValueError("bandwidth_index must be between 0 and 9")
