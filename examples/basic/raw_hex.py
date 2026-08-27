import argparse
import json
import sys

from rascube_v2 import (
    SyncRASCube,
    decode_main_telemetry_hex,
    decode_telemetry_to_dict,
    prompt_connection,
)
from rascube_v2.models.telemetry import MainTelemetrySample


def print_decoded_hex(hex_str: str) -> None:
    """Decodes and prints complete telemetry structure from HEX."""
    print("=" * 65)
    print("🛰️ RASCube Telemetry HEX Decoder")
    print("=" * 65)
    print(f"Raw HEX ({len(hex_str)//2} bytes):\n{hex_str}\n")

    sample = decode_main_telemetry_hex(hex_str)
    print("--- 📡 Decoded Satellite Metrics ---")
    print(f"Sequence Counter  : #{sample.packet_sequence}")
    print(f"System Uptime     : {sample.device_uptime_ms / 1000.0:.2f} s")
    print(f"Battery Voltage   : {sample.eps.battery_charge.bus_voltage_v:.3f} V")
    print(f"5V Rail Voltage   : {sample.eps.main_5v_v:.3f} V")
    print(f"3.3V Rail Voltage : {sample.eps.main_3v3_v:.3f} V")
    print(f"Barometer Temp    : {sample.barometer.temperature_c:.1f} °C")
    print(f"Barometer Altitude: {sample.barometer.altitude_m:.2f} m")
    print(f"Barometer Pressure: {sample.barometer.pressure_hpa:.2f} hPa")
    print(
        f"IMU Accel (g)     : X={sample.imu.accelerometer_g.x:.3f}, "
        f"Y={sample.imu.accelerometer_g.y:.3f}, Z={sample.imu.accelerometer_g.z:.3f}"
    )
    print(
        f"IMU Gyro (°/s)    : X={sample.imu.gyroscope_dps.x:.2f}, "
        f"Y={sample.imu.gyroscope_dps.y:.2f}, Z={sample.imu.gyroscope_dps.z:.2f}"
    )
    print(
        f"GPS Position      : Lat={sample.gps.latitude:.6f}°, "
        f"Lon={sample.gps.longitude:.6f}°, Alt={sample.gps.altitude_m:.1f} m, Sats={sample.gps.satellites}"
    )
    print(f"Radio Link        : RSSI={sample.receiver_rssi:.1f} dBm, SNR={sample.receiver_snr:.2f} dB")
    print("=" * 65)


def format_summary(sample: MainTelemetrySample, count: int) -> str:
    """Format single line live telemetry summary."""
    return (
        f"[{count}] Seq #{sample.packet_sequence} | "
        f"Uptime: {sample.device_uptime_ms / 1000.0:.1f}s | "
        f"Vbat: {sample.eps.battery_charge.bus_voltage_v:.2f}V | "
        f"Temp: {sample.barometer.temperature_c:.1f}°C | "
        f"Alt: {sample.barometer.altitude_m:.1f}m | "
        f"GPS: ({sample.gps.latitude:.4f}°, {sample.gps.longitude:.4f}°, {sample.gps.satellites} sats) | "
        f"RSSI: {sample.receiver_rssi:.1f}dBm | SNR: {sample.receiver_snr:.1f}dB"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="RASCube Raw HEX & Telemetry Streamer")
    parser.add_argument("--port", type=str, default=None, help="Serial COM port (optional)")
    parser.add_argument("--sat", type=int, default=None, help="Satellite serial number (optional)")
    parser.add_argument("--json", action="store_true", help="Print telemetry stream in JSON format")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary metrics stream")
    parser.add_argument("--decode", type=str, default=None, help="Decode a specific HEX string and exit")
    args = parser.parse_args()

    if args.decode:
        print_decoded_hex(args.decode)
        return

    if args.port is not None and args.sat is not None:
        port = args.port
        serial_number = args.sat
    else:
        port, serial_number = prompt_connection()

    with SyncRASCube(port, serial_number=serial_number) as cube:
        print(f"Connected to satellite {serial_number} on {port}")
        mode_label = "JSON Telemetry" if args.json else ("Summary Metrics" if args.summary else "Raw HEX Packets")
        print(f"Streaming {mode_label} (Ctrl+C to stop)...\n")

        count = 0
        for sample in cube.telemetry.iter_samples(timeout=15):
            count += 1
            # Raw full packet: Port (1 byte) + Length (1 byte) + Payload (121 bytes)
            raw_packet = (
                bytes([sample.metadata.port, len(sample.metadata.raw_payload)])
                + sample.metadata.raw_payload
            )

            if args.json:
                d = decode_telemetry_to_dict(raw_packet)
                print(json.dumps(d), flush=True)
            elif args.summary:
                print(format_summary(sample, count), flush=True)
            else:
                print(raw_packet.hex().upper(), flush=True)


if __name__ == "__main__":
    main()

