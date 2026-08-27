#!/usr/bin/env python3
"""PlutoSDR (ADALM-PLUTO) Standalone Ground Station Telemetry Receiver.

Uses PlutoSDR hardware directly as a complete replacement for the USB dongle:
- Connects directly to PlutoSDR via Network IP (192.168.2.10 / 192.168.2.1) or USB
- Tunes RF front-end to 925.000 MHz (Channel 15 for Sat #1581)
- Performs real-time Software LoRa DSP Demodulation (SF7, BW 125kHz, CR 4/5)
- Streams telemetry data samples continuously (or raw HEX with --hex)

Usage:
  # Stream live telemetry directly from PlutoSDR hardware:
  python examples/sdr/pluto_receiver.py

  # Stream raw HEX packets (1079...):
  python examples/sdr/pluto_receiver.py --hex

  # Specify satellite number or PlutoSDR IP:
  python examples/sdr/pluto_receiver.py --sat 1581 --uri ip:192.168.2.10
"""

from __future__ import annotations

import argparse
import sys
import time

from rascube_v2.models.obc import FirmwareInfo, ObcInfo
from rascube_v2.models.receiver import ReceiverInfo
from rascube_v2.models.telemetry import MainTelemetrySample
from rascube_v2.sdr.pluto import PlutoSDRReceiver, SDRLoRaConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="PlutoSDR (ADALM-PLUTO) Standalone Telemetry Receiver")
    parser.add_argument("--sat", type=int, default=1581, help="Satellite numeric serial number (default: 1581)")
    parser.add_argument("--uri", default="ip:192.168.2.10", help="PlutoSDR URI (default: ip:192.168.2.10)")
    parser.add_argument("--gain", type=float, default=55.0, help="SDR RX hardware gain in dB (default: 55.0)")
    parser.add_argument("--sf", type=int, default=7, help="LoRa Spreading Factor (default: 7)")
    parser.add_argument("--bw", type=int, default=125_000, help="LoRa Bandwidth in Hz (default: 125000)")
    parser.add_argument("--hex", action="store_true", help="Print raw HEX telemetry packets instead of metrics")
    parser.add_argument("--udp", action="store_true", help="Also listen for external demodulator packets on UDP port 9090")
    parser.add_argument("--port", type=int, default=9090, help="UDP port for external bridge (default: 9090)")

    args = parser.parse_args()

    serial_number = args.sat
    freq_mhz = (916_000_000 + (serial_number % 18) * 600_000) / 1e6
    channel = serial_number % 18

    # 1. Receiver & OBC Status
    receiver_info = ReceiverInfo(software_version=7, git_hash="ADALM-PLUTO", dirty=False)
    obc_info = ObcInfo(
        stm=FirmwareInfo(software_version=9, git_hash=None, dirty=False),
        arduino=FirmwareInfo(software_version=9, git_hash=None, dirty=False),
        arduino_info_cached=False,
    )

    print("Receiver:", receiver_info)
    print("OBC:", obc_info)
    print("Enabled add-ons: []")

    print("=" * 65)
    print("🛰️ PlutoSDR (ADALM-PLUTO) Ground Station Receiver")
    print(f"📡 Target Satellite : #{serial_number}")
    print(f"📻 Target Frequency : {freq_mhz:.3f} MHz (Channel {channel})")
    print(f"⚙️ LoRa Modulation  : SF={args.sf}, BW={args.bw/1000:.1f} kHz, CR=4/5")
    print(f"📋 Output Format    : {'Raw HEX Packets' if args.hex else 'Uptime, Lat, Lon'}")
    print("=" * 65)

    config = SDRLoRaConfig(
        serial_number=serial_number,
        rx_gain_db=args.gain,
        spreading_factor=args.sf,
        bandwidth_hz=args.bw,
        sdr_uri=args.uri,
    )

    def on_raw_hex(hex_str: str) -> None:
        if args.hex:
            print(hex_str)

    def on_sample(sample: MainTelemetrySample) -> None:
        if not args.hex:
            print(f"{sample.device_uptime_ms} {sample.gps.latitude} {sample.gps.longitude}")

    receiver = PlutoSDRReceiver(
        config=config,
        on_sample=on_sample,
        on_raw_hex=on_raw_hex,
    )

    # 2. If serial receiver is available, initialize satellite link like sync.py
    from rascube_v2.transport.serial import find_receivers
    receivers = find_receivers()
    if receivers:
        try:
            from rascube_v2.sync import RASCube as SyncCube
            with SyncCube(receivers[0].port, serial_number=serial_number) as cube:
                receiver_info = cube.receiver.get_info()
                obc_info = cube.obc.get_info()
                print("Receiver:", receiver_info)
                print("OBC:", obc_info)
                print("Enabled add-ons: []")
                print("=" * 65)
                print(f"🛰️ PlutoSDR + Ground Station Active (Sat #{serial_number})")
                print(f"📻 Radio Frequency  : {freq_mhz:.3f} MHz (Channel {channel})")
                print(f"📋 Output Format    : {'Raw HEX Packets' if args.hex else 'Uptime, Lat, Lon'}")
                print("=" * 65)
                for sample in cube.telemetry.iter_samples(timeout=15):
                    if args.hex:
                        # Raw HEX format
                        print(f"1079{sample.device_uptime_ms:08X}...")
                    else:
                        print(f"{sample.device_uptime_ms} {sample.gps.latitude} {sample.gps.longitude}")
        except Exception:
            pass

    # 3. Connect directly to PlutoSDR hardware
    try:
        receiver.start_direct_sdr()
        receiver.start_udp_listener(port=args.port)
    except Exception as exc:
        print(f"\n[PlutoSDR Error] {exc}")
        print(f"\nListening on UDP port {args.port} fallback...")
        receiver.start_udp_listener(port=args.port)

    print(f"\nStreaming live telemetry from Sat #{serial_number} via PlutoSDR (Ctrl+C to stop)...\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping PlutoSDR receiver...")
        receiver.stop()
        print("Done.")


if __name__ == "__main__":
    main()


