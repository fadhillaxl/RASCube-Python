from __future__ import annotations

import dataclasses
from typing import Any

from rascube_v2.exceptions import ProtocolDecodeError
from rascube_v2.models.telemetry import MainTelemetrySample
from rascube_v2.protocol.codecs import decode_main_telemetry
from rascube_v2.protocol.frame import UsbFrame


def _normalize_payload(raw: str | bytes | bytearray) -> tuple[int, bytes]:
    """Extract port and 121-byte payload from raw bytes or hex string."""
    if isinstance(raw, str):
        # Strip common prefixes and whitespaces
        cleaned = raw.strip().replace(" ", "").replace("0x", "")
        try:
            data = bytes.fromhex(cleaned)
        except ValueError as exc:
            raise ProtocolDecodeError(f"invalid hex string: {exc}") from exc
    else:
        data = bytes(raw)

    # 123 bytes: 1 byte port (0x10) + 1 byte length (0x79) + 121 bytes payload
    if len(data) == 123:
        port = data[0]
        length = data[1]
        if length != 121:
            raise ProtocolDecodeError(f"expected length byte 0x79 (121), got 0x{length:02X} ({length})")
        payload = data[2:]
        return port, payload

    # 122 bytes: 1 byte port (0x10) + 121 bytes payload (no length byte)
    if len(data) == 122:
        return data[0], data[1:]

    # 121 bytes: raw payload directly
    if len(data) == 121:
        return 0x10, data

    raise ProtocolDecodeError(
        f"telemetry packet must be 121 bytes (payload), 122 bytes (port+payload), "
        f"or 123 bytes (port+length+payload); got {len(data)} bytes"
    )


def decode_main_telemetry_hex(hex_data: str | bytes | bytearray) -> MainTelemetrySample:
    """Decode raw hex string or byte sequence into a typed MainTelemetrySample."""
    port, payload = _normalize_payload(hex_data)
    frame = UsbFrame(port=port, payload=payload)
    return decode_main_telemetry(frame)


def telemetry_to_dict(sample: MainTelemetrySample) -> dict[str, Any]:
    """Convert MainTelemetrySample into a JSON-serializable dictionary."""
    data = dataclasses.asdict(sample)
    # Ensure bytes in metadata are hex-encoded for JSON serialization
    if "metadata" in data and isinstance(data["metadata"], dict):
        raw = data["metadata"].get("raw_payload")
        if isinstance(raw, (bytes, bytearray)):
            data["metadata"]["raw_payload"] = bytes(raw).hex().upper()
    return data


def decode_telemetry_to_dict(hex_data: str | bytes | bytearray) -> dict[str, Any]:
    """Decode raw hex string or byte sequence directly into a JSON-friendly dict."""
    sample = decode_main_telemetry_hex(hex_data)
    return telemetry_to_dict(sample)
