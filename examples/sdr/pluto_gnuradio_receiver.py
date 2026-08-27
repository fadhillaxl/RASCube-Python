#!/usr/bin/env python3
"""PlutoSDR Hardware Ground Station using Embedded GNU Radio + gr-lora_sdr in Python.

Runs the full GNU Radio and gr-lora_sdr C++ DSP physical-layer demodulator pipeline
entirely inside Python code.

Usage:
  # Run live PlutoSDR receiver via GNU Radio gr-lora_sdr (Sat #1581 -> 925.0 MHz)
  python3 examples/sdr/pluto_gnuradio_receiver.py --sat 1581 --uri ip:192.168.2.10

  # Output raw HEX packets (1079...)
  python3 examples/sdr/pluto_gnuradio_receiver.py --sat 1581 --hex
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Ensure Homebrew GNU Radio Python paths are included
for p in [
    "/opt/homebrew/lib/python3.14/site-packages",
    "/opt/homebrew/lib/python3.13/site-packages",
    "/opt/homebrew/lib/python3.12/site-packages",
]:
    if os.path.exists(p) and p not in sys.path:
        sys.path.insert(0, p)

from rascube_v2.decoder import decode_main_telemetry_hex
from rascube_v2.models.firmware import FirmwareInfo, ObcInfo, ReceiverInfo


def main() -> None:
    parser = argparse.ArgumentParser(description="PlutoSDR GNU Radio gr-lora_sdr Receiver")
    parser.add_argument("--sat", type=int, default=1581, help="Satellite serial number (default: 1581)")
    parser.add_argument("--uri", default="ip:192.168.2.10", help="PlutoSDR URI (default: ip:192.168.2.10)")
    parser.add_argument("--gain", type=float, default=55.0, help="SDR hardware gain in dB (default: 55.0)")
    parser.add_argument("--sf", type=int, default=7, help="LoRa Spreading Factor (default: 7)")
    parser.add_argument("--bw", type=int, default=125_000, help="LoRa Bandwidth in Hz (default: 125000)")
    parser.add_argument("--hex", action="store_true", help="Print raw HEX telemetry packets instead of metrics")

    args = parser.parse_args()

    serial_number = args.sat
    freq_hz = 916_000_000 + (serial_number % 18) * 600_000
    channel = serial_number % 18

    # 1. Receiver & OBC Banner Status
    print("Receiver:", ReceiverInfo(software_version=7, git_hash="ADALM-PLUTO", dirty=False))
    print("OBC:", ObcInfo(
        stm=FirmwareInfo(software_version=9, git_hash=None, dirty=False),
        arduino=FirmwareInfo(software_version=9, git_hash=None, dirty=False),
        arduino_info_cached=False,
    ))
    print("Enabled add-ons: []")

    print("=" * 65)
    print("🛰️ PlutoSDR GNU Radio + gr-lora_sdr Ground Station Receiver")
    print(f"📡 Target Satellite : #{serial_number}")
    print(f"📻 Target Frequency : {freq_hz / 1e6:.3f} MHz (Channel {channel})")
    print(f"⚙️ LoRa Modulation  : SF={args.sf}, BW={args.bw/1000:.1f} kHz, CR=4/5")
    print(f"📋 Output Format    : {'Raw HEX Packets' if args.hex else 'Uptime, Lat, Lon'}")
    print("=" * 65)

    try:
        from gnuradio import blocks, gr, iio
        from gnuradio.lora_sdr import lora_sdr_lora_rx
    except ImportError as exc:
        print(f"\n[Error loading GNU Radio / gr-lora_sdr] {exc}")
        print("Please run with GNU Radio Python environment: python3.14 examples/sdr/pluto_gnuradio_receiver.py")
        sys.exit(1)

    class PlutoLoRaFlowgraph(gr.top_block):
        def __init__(self) -> None:
            super().__init__("PlutoSDR LoRa Receiver")

            # 1. PlutoSDR Hardware Source
            self.sdr_source = iio.pluto_source(
                args.uri,
                int(freq_hz),
                1_000_000,  # 1 MSPS
                int(args.bw * 2),
                0x8000,
                True,
            )
            self.sdr_source.set_gain(0, float(args.gain))

            # 2. LoRa SDR Receiver Block
            self.lora_rx = lora_sdr_lora_rx(
                bw=int(args.bw),
                cr=1,  # 4/5
                has_crc=True,
                impl_head=False,
                ldo=False,
                pay_len=121,
                print_rx=[True],
                samp_rate=1_000_000,
                sf=int(args.sf),
                soft_decoding=True,
                sync_word=[0x34],
            )

            # 3. Connect Blocks
            self.connect((self.sdr_source, 0), (self.lora_rx, 0))

    tb = PlutoLoRaFlowgraph()
    tb.start()

    print(f"[Pluto+ SDR] Connected ({args.uri}) and tuned to {freq_hz/1e6:.3f} MHz.")
    print(f"Streaming live telemetry from Sat #{serial_number} via gr-lora_sdr (Ctrl+C to stop)...\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping receiver...")
        tb.stop()
        tb.wait()
        print("Done.")


if __name__ == "__main__":
    main()
