#!/usr/bin/env python3
"""Pluto+ SDR Satellite Telemetry Receiver (Raw HEX & Live Decoder).

Usage:
  # Stream raw HEX telemetry frames from Pluto+ SDR (Default Sat #1581 -> 925.000 MHz)
  python examples/sdr/pluto_receiver.py --sat 1581

  # Specify Pluto+ SDR IP address or USB context
  python examples/sdr/pluto_receiver.py --sat 1581 --uri ip:192.168.2.10

  # Decoded telemetry format
  python examples/sdr/pluto_receiver.py --sat 1581 --decode

  # UDP Bridge mode (e.g. from GNU Radio / gr-lorasdr)
  python examples/sdr/pluto_receiver.py --sat 1581 --udp --port 9090
"""

from __future__ import annotations

import argparse
import sys
import time

from rascube_v2.models.telemetry import MainTelemetrySample
from rascube_v2.sdr.pluto import PlutoSDRReceiver, SDRLoRaConfig


def handle_decoded_sample(sample: MainTelemetrySample) -> None:
    """Prints human-readable tabular metrics when --decode flag is provided."""
    print(
        f"[Seq #{sample.packet_sequence:05d}] "
        f"Uptime: {sample.device_uptime_ms/1000:7.1f}s | "
        f"Temp: {sample.barometer.temperature_c:4.1f}°C | "
        f"Pres: {sample.barometer.pressure_hpa:6.1f}hPa | "
        f"Batt: {sample.eps.battery_charge.bus_voltage_v:4.2f}V | "
        f"GPS: ({sample.gps.latitude:9.4f}, {sample.gps.longitude:9.4f}) | "
        f"RSSI: {sample.receiver_rssi:5.1f}dBm"
    )


def handle_raw_hex(hex_str: str) -> None:
    """Streams raw HEX telemetry packet just like examples/basic/raw_hex.py."""
    print(hex_str)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pluto+ SDR Satellite Telemetry Receiver")
    parser.add_argument("--sat", type=int, default=1581, help="Satellite numeric serial number (default: 1581)")
    parser.add_argument("--uri", default="ip:192.168.2.10", help="Pluto+ SDR URI (e.g. ip:192.168.2.10, ip:192.168.2.1 or usb:...)")
    parser.add_argument("--gain", type=float, default=50.0, help="SDR RX hardware gain in dB (default: 50.0)")
    parser.add_argument("--sf", type=int, default=7, help="LoRa Spreading Factor 5-12 (default: 7)")
    parser.add_argument("--bw", type=int, default=125_000, help="LoRa Bandwidth in Hz (default: 125000)")
    parser.add_argument("--decode", action="store_true", help="Print human-readable decoded metrics instead of raw HEX")
    parser.add_argument("--udp", action="store_true", help="Listen for demodulated packets from GNU Radio via UDP")
    parser.add_argument("--port", type=int, default=9090, help="UDP port for GNU Radio bridge (default: 9090)")

    args = parser.parse_args()

    config = SDRLoRaConfig(
        serial_number=args.sat,
        rx_gain_db=args.gain,
        spreading_factor=args.sf,
        bandwidth_hz=args.bw,
        sdr_uri=args.uri,
    )

    freq_mhz = config.frequency_hz / 1e6
    channel = args.sat % 18

    print("=" * 65)
    print("🛰️ RASCubeV2 Pluto+ SDR Telemetry Receiver")
    print(f"📡 Satellite Serial : #{args.sat}")
    print(f"📻 Target Frequency : {freq_mhz:.3f} MHz (Channel {channel})")
    print(f"⚙️ LoRa Modulation  : SF={args.sf}, BW={args.bw/1000:.1f} kHz, CR=4/5")
    print(f"📋 Output Mode      : {'Decoded Metrics Table' if args.decode else 'Raw HEX Packets'}")
    print("=" * 65)

    receiver = PlutoSDRReceiver(
        config=config,
        on_sample=handle_decoded_sample if args.decode else None,
        on_raw_hex=handle_raw_hex if not args.decode else None,
    )

    if args.udp:
        print(f"Listening on UDP port {args.port} for demodulated packets...")
        receiver.start_udp_listener(port=args.port)
    else:
        try:
            receiver.start_direct_sdr()
            # Also enable UDP bridge listener on the side for flexibility
            receiver.start_udp_listener(port=args.port)
        except RuntimeError as exc:
            print(f"\n[Notice] {exc}")
            print("\nSwitching to UDP Bridge mode (listening on port 9090)...")
            receiver.start_udp_listener(port=args.port)

    print(f"\nStreaming live telemetry from Satellite #{args.sat} (Press Ctrl+C to stop)...\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping receiver...")
        receiver.stop()
        print("Done.")


if __name__ == "__main__":
    main()

