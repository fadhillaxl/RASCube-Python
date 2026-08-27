#!/usr/bin/env python3
"""Pluto+ SDR Satellite Telemetry Receiver (with Serial Uplink TX & Raw HEX Downlink).

Usage:
  # Auto-detects USB serial transmitter for uplink & tunes Pluto+ SDR for downlink
  python examples/sdr/pluto_receiver.py --sat 1581

  # Specify Pluto+ SDR IP and USB Serial Port
  python examples/sdr/pluto_receiver.py --sat 1581 --uri ip:192.168.2.10 --serial-port /dev/cu.usbmodem20623154594D1

  # Decoded metrics format
  python examples/sdr/pluto_receiver.py --sat 1581 --decode

  # SDR Receiver only (no serial uplink transmission)
  python examples/sdr/pluto_receiver.py --sat 1581 --no-serial
"""

from __future__ import annotations

import argparse
import sys
import time

from serial.tools import list_ports

from rascube_v2 import SyncRASCube
from rascube_v2.constants import USB_PID_V2, USB_VID
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


def find_rascube_serial_port() -> str | None:
    """Auto-detects RASCube USB serial port by exact VID/PID or device description."""
    for p in list_ports.comports():
        if p.vid == USB_VID and p.pid == USB_PID_V2:
            return p.device
        if p.description and "RASCube" in p.description:
            return p.device
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Pluto+ SDR Satellite Telemetry Receiver & Serial TX")
    parser.add_argument("--sat", type=int, default=None, help="Satellite numeric serial number (prompts if omitted)")
    parser.add_argument("--serial-port", default=None, help="Serial port for uplink (prompts if omitted)")
    parser.add_argument("--uri", default="ip:192.168.2.10", help="Pluto+ SDR URI (default: ip:192.168.2.10)")
    parser.add_argument("--gain", type=float, default=50.0, help="SDR RX hardware gain in dB (default: 50.0)")
    parser.add_argument("--sf", type=int, default=7, help="LoRa Spreading Factor 5-12 (default: 7)")
    parser.add_argument("--bw", type=int, default=125_000, help="LoRa Bandwidth in Hz (default: 125000)")
    parser.add_argument("--decode", action="store_true", help="Print decoded table instead of raw HEX packets")
    parser.add_argument("--no-serial", action="store_true", help="Skip serial uplink initialization")
    parser.add_argument("--udp", action="store_true", help="Listen for demodulated packets from GNU Radio via UDP")
    parser.add_argument("--port", type=int, default=9090, help="UDP port for GNU Radio bridge (default: 9090)")

    args = parser.parse_args()

    serial_port = args.serial_port
    serial_number = args.sat

    # 1. Interactive Prompt if not specified via CLI flags
    if not args.no_serial and (serial_port is None or serial_number is None):
        try:
            from rascube_v2 import prompt_connection
            prompted_port, prompted_sat = prompt_connection()
            if serial_port is None:
                serial_port = prompted_port
            if serial_number is None:
                serial_number = prompted_sat
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)
        except Exception:
            # Fallback if non-interactive
            if serial_port is None:
                serial_port = find_rascube_serial_port()
            if serial_number is None:
                serial_number = 1581

    if serial_number is None:
        serial_number = 1581

    # 2. Transmit Serial Uplink Initializations (Receiver, OBC, Add-ons)
    if serial_port and not args.no_serial:
        try:
            with SyncRASCube(serial_port, serial_number=serial_number) as cube:
                receiver = cube.receiver.get_info()
                print("Receiver:", receiver)

                obc = None
                try:
                    obc = cube.obc.get_info(timeout=3.0)
                    print("OBC:", obc)
                except Exception as exc:
                    print(f"OBC: (no response / {exc})")

                try:
                    presence = cube.addons.refresh_enabled(timeout=2.0)
                    print("Enabled add-ons:", sorted(presence.enabled_ids))
                except Exception:
                    print("Enabled add-ons: []")
        except Exception as exc:
            print(f"[Serial Notice] Could not open {serial_port}: {exc}")

    # 3. Pluto+ SDR Downlink Configuration
    freq_mhz = (916_000_000 + (serial_number % 18) * 600_000) / 1e6
    channel = serial_number % 18

    print("=" * 65)
    print("🛰️ RASCubeV2 Pluto+ SDR Telemetry Receiver")
    print(f"📡 Satellite Serial : #{serial_number}")
    print(f"📻 Target Frequency : {freq_mhz:.3f} MHz (Channel {channel})")
    print(f"⚙️ LoRa Modulation  : SF={args.sf}, BW={args.bw/1000:.1f} kHz, CR=4/5")
    print(f"📋 Output Mode      : {'Decoded Metrics Table' if args.decode else 'Raw HEX Packets'}")
    print("=" * 65)

    config = SDRLoRaConfig(
        serial_number=serial_number,
        rx_gain_db=args.gain,
        spreading_factor=args.sf,
        bandwidth_hz=args.bw,
        sdr_uri=args.uri,
    )

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
            # Also start UDP listener for external bridges
            receiver.start_udp_listener(port=args.port)
        except RuntimeError as exc:
            print(f"\n[Notice] {exc}")
            print("\nSwitching to UDP Bridge mode (listening on port 9090)...")
            receiver.start_udp_listener(port=args.port)

    print(f"\nStreaming live telemetry from Satellite #{serial_number} (Press Ctrl+C to stop)...\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping receiver...")
        receiver.stop()
        print("Done.")


if __name__ == "__main__":
    main()


