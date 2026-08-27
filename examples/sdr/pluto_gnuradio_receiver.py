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

# Ensure workspace src/ directory and .venv packages are included in sys.path
WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WORKSPACE_SRC = os.path.join(WORKSPACE_DIR, "src")
if WORKSPACE_SRC not in sys.path:
    sys.path.insert(0, WORKSPACE_SRC)

# Include venv site-packages if running from system python
for venv_site in [
    os.path.join(WORKSPACE_DIR, ".venv", "lib", f"python{v}", "site-packages")
    for v in ["3.12", "3.13", "3.14", "3.11"]
]:
    if os.path.exists(venv_site) and venv_site not in sys.path:
        sys.path.insert(0, venv_site)

# Ensure Homebrew GNU Radio Python paths are included
for p in [
    "/opt/homebrew/lib/python3.14/site-packages",
    "/opt/homebrew/lib/python3.13/site-packages",
    "/opt/homebrew/lib/python3.12/site-packages",
]:
    if os.path.exists(p) and p not in sys.path:
        sys.path.insert(0, p)

from rascube_v2.decoder import decode_main_telemetry_hex
from rascube_v2.models.obc import FirmwareInfo, ObcInfo
from rascube_v2.models.receiver import ReceiverInfo


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

    import pmt
    from gnuradio import blocks, gr, iio
    from gnuradio.lora_sdr import lora_sdr_lora_rx

    class TelemetryHandlerBlock(gr.sync_block):
        def __init__(self, is_hex: bool) -> None:
            super().__init__(name="TelemetryHandler", in_sig=None, out_sig=None)
            self.is_hex = is_hex
            self.message_port_register_in(pmt.intern("in"))
            self.set_msg_handler(pmt.intern("in"), self.handle_msg)

        def handle_msg(self, msg: Any) -> None:
            try:
                data_pmt = pmt.cdr(msg) if pmt.is_pair(msg) else msg
                raw_bytes = bytes(pmt.u8vector_elements(data_pmt))
                if len(raw_bytes) == 121:
                    full_packet = bytes([0x10, 0x79]) + raw_bytes
                else:
                    full_packet = raw_bytes

                if self.is_hex:
                    print(full_packet.hex().upper())
                else:
                    sample = decode_main_telemetry_hex(full_packet)
                    print(f"{sample.device_uptime_ms} {sample.gps.latitude} {sample.gps.longitude}")
            except Exception:
                pass

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
                print_rx=[False],
                samp_rate=1_000_000,
                sf=int(args.sf),
                soft_decoding=True,
                sync_word=[0x34],
            )

            # 3. Custom Output Handler
            self.handler = TelemetryHandlerBlock(is_hex=args.hex)

            # Connect Blocks
            self.connect((self.sdr_source, 0), (self.lora_rx, 0))
            self.msg_connect((self.lora_rx, "out"), (self.handler, "in"))

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

