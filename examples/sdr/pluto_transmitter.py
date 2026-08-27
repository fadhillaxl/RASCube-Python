#!/usr/bin/env python3
"""PlutoSDR (ADALM-PLUTO) Ground Station Uplink Transmitter.

Transmits radio-bound commands and serial requests over the air to RASCube satellites:
- Tunes AD9363 RF transmitter to target channel (e.g. 925.000 MHz for Sat #1581)
- Modulates commands into LoRa CSS chirps (BW=500 kHz, SF=7, CR=4/5, SyncWord=0x12)
- Transmits RF bursts via PlutoSDR TX port

Commands supported:
  --ping              Send OBC_INFO request (triggers telemetry broadcast)
  --rgb R G B         Set Arduino RGB LED colors (e.g. --rgb 255 0 0 for Red)
  --song              Play satellite buzzer startup song
  --raw-hex HEX       Transmit custom raw hex radio frame (e.g. 120100)
  --beacon [INTERVAL] Transmit periodic keep-alive ping beacon every N seconds

Usage examples:
  python examples/sdr/pluto_transmitter.py --sat 1581 --ping
  python examples/sdr/pluto_transmitter.py --sat 1581 --rgb 0 255 0
  python examples/sdr/pluto_transmitter.py --sat 1581 --song
  python examples/sdr/pluto_transmitter.py --sat 1581 --beacon 5
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

sys.path.insert(0, "src")
from rascube_v2.constants import HostPort
from rascube_v2.sdr.pluto import PlutoSDRTransmitter, SDRLoRaConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="PlutoSDR Uplink Ground Station Transmitter")
    parser.add_argument("--sat", type=int, default=1581, help="Target satellite serial number (default: 1581)")
    parser.add_argument("--freq", type=float, default=None, help="Custom frequency in MHz (e.g. 925.0)")
    parser.add_argument("--uri", default="usb:", help="PlutoSDR URI (default: usb:, or ip:192.168.2.1)")
    parser.add_argument("--tx-gain", type=float, default=0.0, help="SDR TX hardware attenuation in dB (0.0=max power ~+10dBm, -20.0=low power)")
    parser.add_argument("--sf", type=int, default=7, help="LoRa Spreading Factor (default: 7)")
    parser.add_argument("--bw", type=int, default=500_000, help="LoRa Bandwidth in Hz (default: 500000)")

    # Command options
    cmd_group = parser.add_mutually_exclusive_group(required=True)
    cmd_group.add_argument("--ping", action="store_true", help="Send OBC Info / Wake-up ping")
    cmd_group.add_argument("--rgb", nargs=3, type=int, metavar=("R", "G", "B"), help="Set satellite RGB LED (0-255)")
    cmd_group.add_argument("--song", action="store_true", help="Play satellite startup sound")
    cmd_group.add_argument("--raw-hex", type=str, help="Transmit raw hex payload over the air (e.g. 120100)")
    cmd_group.add_argument("--blink", action="store_true", help="Blink satellite RGB LED continuously with colors")
    cmd_group.add_argument("--beacon", type=float, nargs="?", const=5.0, metavar="SECONDS", help="Continuous periodic keep-alive beacon")

    args = parser.parse_args()

    channel = args.sat % 18
    freq_hz = int(args.freq * 1e6) if args.freq is not None else (916_000_000 + channel * 600_000)

    print("=" * 65)
    print("🛰️ PlutoSDR (ADALM-PLUTO) Ground Station Uplink Transmitter")
    print(f"📡 Target Satellite : #{args.sat}")
    print(f"📻 Target Frequency : {freq_hz/1e6:.3f} MHz (Channel {channel})")
    print(f"⚙️ LoRa Modulation  : SF={args.sf}, BW={args.bw/1000:.0f} kHz, CR=4/5")
    print(f"⚡ TX Gain Atten    : {args.tx_gain:.1f} dB")
    print("=" * 65)

    config = SDRLoRaConfig(
        serial_number=args.sat,
        custom_frequency_hz=freq_hz,
        spreading_factor=args.sf,
        bandwidth_hz=args.bw,
        sdr_uri=args.uri,
    )

    transmitter = PlutoSDRTransmitter(config=config, tx_gain_db=args.tx_gain)
    transmitter.connect()

    # Determine payload to send
    if args.ping:
        payload = bytes([HostPort.OBC_INFO, 0x01, 0x00])
        print(f"\n[Uplink TX] Sending OBC_INFO wake-up request (0x{payload.hex().upper()})...", flush=True)
        transmitter.transmit_bytes(payload)
        print("✅ Transmission complete.")

    elif args.rgb:
        r, g, b = [max(0, min(255, val)) for val in args.rgb]
        payload = bytes([HostPort.ARDUINO_RGB, 0x03, r, g, b])
        print(f"\n[Uplink TX] Setting RGB LED ({r}, {g}, {b}) -> 0x{payload.hex().upper()}...", flush=True)
        transmitter.transmit_bytes(payload)
        print("✅ Transmission complete.")

    elif args.blink:
        colors = [
            (255, 0, 0, "RED"),
            (0, 255, 0, "GREEN"),
            (0, 0, 255, "BLUE"),
            (255, 255, 0, "YELLOW"),
            (255, 0, 255, "MAGENTA"),
            (0, 255, 255, "CYAN"),
            (255, 255, 255, "WHITE"),
            (0, 0, 0, "OFF"),
        ]
        print("\n[Blink Mode] Transmitting RGB color cycle over the air (Ctrl+C to stop)...\n")
        try:
            while True:
                for r, g, b, name in colors:
                    payload = bytes([HostPort.ARDUINO_RGB, 0x03, r, g, b])
                    transmitter.transmit_bytes(payload)
                    t_str = time.strftime("%H:%M:%S")
                    print(f"[{t_str}] 💡 LED -> {name} (RGB: {r},{g},{b})", flush=True)
                    time.sleep(0.8)
        except KeyboardInterrupt:
            print("\nBlink mode stopped.")

    elif args.song:
        payload = bytes([HostPort.ARDUINO_STARTUP_SONG, 0x01, 0x00])
        print(f"\n[Uplink TX] Triggering startup song -> 0x{payload.hex().upper()}...", flush=True)
        transmitter.transmit_bytes(payload)
        print("✅ Transmission complete.")

    elif args.raw_hex:
        payload = bytes.fromhex(args.raw_hex.replace(" ", "").replace("0x", ""))
        print(f"\n[Uplink TX] Transmitting custom frame -> 0x{payload.hex().upper()}...", flush=True)
        transmitter.transmit_bytes(payload)
        print("✅ Transmission complete.")

    elif args.beacon is not None:
        interval = max(0.5, args.beacon)
        payload = bytes([HostPort.OBC_INFO, 0x01, 0x00])
        print(f"\n[Beacon Mode] Transmitting wake-up beacon every {interval:.1f}s (Ctrl+C to stop)...\n")
        count = 0
        try:
            while True:
                count += 1
                transmitter.transmit_bytes(payload)
                t_str = time.strftime("%H:%M:%S")
                print(f"[{t_str}] 📡 Beacon #{count} transmitted to Sat #{args.sat}", flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nBeacon stopped.")


if __name__ == "__main__":
    main()
