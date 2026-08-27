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


def prompt_rascube_port() -> tuple[str, int]:
    """Prompts for COM port, placing RASCube devices at the top of the list."""
    ports = list(list_ports.comports())
    if not ports:
        raise RuntimeError("No serial ports found")

    # Sort so RASCube receiver appears first
    def sort_key(p: Any) -> int:
        if (p.vid == USB_VID and p.pid == USB_PID_V2) or ("RASCube" in (p.description or "")):
            return 0
        return 1

    ports.sort(key=sort_key)

    import questionary

    port = questionary.select(
        "Select a COM port for RASCube USB Receiver:",
        choices=[
            questionary.Choice(
                title=f"{p.device} - {p.description}{' ⭐ (RASCube)' if (p.vid == USB_VID and p.pid == USB_PID_V2) or 'RASCube' in (p.description or '') else ''}",
                value=p.device,
            )
            for p in ports
        ],
    ).ask()
    if port is None:
        raise KeyboardInterrupt

    serial_number = questionary.text(
        "Satellite serial number:",
        default="1581",
    ).ask()
    if serial_number is None:
        raise KeyboardInterrupt

    return str(port), int(serial_number)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pluto+ SDR Satellite Telemetry Receiver & Serial TX")
    parser.add_argument("--sat", type=int, default=None, help="Satellite numeric serial number (default: 1581)")
    parser.add_argument("--serial-port", default=None, help="Serial port for RASCube USB (auto-detected if omitted)")
    parser.add_argument("--uri", default="ip:192.168.2.10", help="Pluto+ SDR URI (default: ip:192.168.2.10)")
    parser.add_argument("--gain", type=float, default=50.0, help="SDR RX hardware gain in dB (default: 50.0)")
    parser.add_argument("--sf", type=int, default=7, help="LoRa Spreading Factor 5-12 (default: 7)")
    parser.add_argument("--bw", type=int, default=125_000, help="LoRa Bandwidth in Hz (default: 125000)")
    parser.add_argument("--hex", action="store_true", help="Print raw HEX packets (default prints uptime/lat/lon like async.py)")
    parser.add_argument("--no-serial", action="store_true", help="Skip serial receiver connection")
    parser.add_argument("--no-sdr", action="store_true", help="Skip Pluto+ SDR tuning")
    parser.add_argument("--udp", action="store_true", help="Listen for demodulated packets from GNU Radio via UDP")
    parser.add_argument("--port", type=int, default=9090, help="UDP port for GNU Radio bridge (default: 9090)")

    args = parser.parse_args()

    serial_port = args.serial_port
    serial_number = args.sat

    # 1. Interactive Prompt if not specified via CLI
    if not args.no_serial and (serial_port is None or serial_number is None):
        try:
            prompted_port, prompted_sat = prompt_rascube_port()
            if serial_port is None:
                serial_port = prompted_port
            if serial_number is None:
                serial_number = prompted_sat
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)
        except Exception:
            if serial_port is None:
                serial_port = find_rascube_serial_port()
            if serial_number is None:
                serial_number = 1581

    if serial_number is None:
        serial_number = 1581

    # 2. Pluto+ SDR Downlink Tuning
    if not args.no_sdr:
        freq_mhz = (916_000_000 + (serial_number % 18) * 600_000) / 1e6
        channel = serial_number % 18

        print("=" * 65)
        print("🛰️ RASCubeV2 Pluto+ SDR & Ground Station Receiver")
        print(f"📡 Satellite Serial : #{serial_number}")
        print(f"📻 Target Frequency : {freq_mhz:.3f} MHz (Channel {channel})")
        print(f"⚙️ LoRa Modulation  : SF={args.sf}, BW={args.bw/1000:.1f} kHz, CR=4/5")
        print("=" * 65)

        config = SDRLoRaConfig(
            serial_number=serial_number,
            rx_gain_db=args.gain,
            spreading_factor=args.sf,
            bandwidth_hz=args.bw,
            sdr_uri=args.uri,
        )

        def on_sdr_hex(h: str) -> None:
            if args.hex:
                print(f"[SDR HEX] {h}")

        def on_sdr_sample(s: MainTelemetrySample) -> None:
            if not args.hex:
                print(f"{s.device_uptime_ms} {s.gps.latitude} {s.gps.longitude}")

        sdr_receiver = PlutoSDRReceiver(
            config=config,
            on_sample=on_sdr_sample,
            on_raw_hex=on_sdr_hex,
        )

        if args.udp:
            print(f"Listening on UDP port {args.port} for demodulated packets from GNU Radio...")
            sdr_receiver.start_udp_listener(port=args.port)
        else:
            try:
                sdr_receiver.start_direct_sdr()
                sdr_receiver.start_udp_listener(port=args.port)
            except RuntimeError as exc:
                print(f"\n[SDR Notice] {exc}")
                print("Switching to UDP Bridge mode (port 9090)...")
                sdr_receiver.start_udp_listener(port=args.port)

    # 3. Serial Port Connection & Live Streaming (Async/Sync style)
    if serial_port and not args.no_serial:
        try:
            with SyncRASCube(serial_port, serial_number=serial_number) as cube:
                # Query receiver info
                receiver = cube.receiver.get_info()
                print("Receiver:", receiver)

                # Query OBC info
                try:
                    obc = cube.obc.get_info(timeout=3.0)
                    print("OBC:", obc)
                except Exception as exc:
                    print(f"OBC: (no response / {exc})")

                # Query enabled add-ons
                try:
                    presence = cube.addons.refresh_enabled(timeout=2.0)
                    print("Enabled add-ons:", sorted(presence.enabled_ids))
                except Exception:
                    print("Enabled add-ons: []")

                print(f"\nStreaming live telemetry from Sat #{serial_number} (Ctrl+C to stop)...\n")

                for sample in cube.telemetry.iter_samples(timeout=15):
                    if args.hex:
                        raw_packet = (
                            bytes([sample.metadata.port, len(sample.metadata.raw_payload)])
                            + sample.metadata.raw_payload
                        )
                        print(raw_packet.hex().upper())
                    else:
                        print(f"{sample.device_uptime_ms} {sample.gps.latitude} {sample.gps.longitude}")

        except KeyboardInterrupt:
            print("\nStopped by user.")
        except Exception as exc:
            print(f"\n[Serial Error] {exc}")
    else:
        print("\nListening for incoming SDR / UDP packets (Press Ctrl+C to stop)...\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()


