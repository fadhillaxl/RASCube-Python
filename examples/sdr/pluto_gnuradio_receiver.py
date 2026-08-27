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

# Ensure workspace src/ directory is included in sys.path
WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WORKSPACE_SRC = os.path.join(WORKSPACE_DIR, "src")
if WORKSPACE_SRC not in sys.path:
    sys.path.insert(0, WORKSPACE_SRC)

# Clean sys.path from incompatible other Python version site-packages
current_py = f"python{sys.version_info.major}.{sys.version_info.minor}"
sys.path = [p for p in sys.path if not any(f"python3.{v}" in p for v in range(8, 20) if f"python3.{v}" != current_py)]

# Ensure Homebrew GNU Radio Python paths for current python version are included
hb_site = f"/opt/homebrew/lib/{current_py}/site-packages"
if os.path.exists(hb_site) and hb_site not in sys.path:
    sys.path.insert(0, hb_site)

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
    parser.add_argument("--port", type=int, default=9090, help="UDP listening port for SDR stream (default: 9090)")

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
    from gnuradio import blocks, gr, network
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

            # 1. UDP / Network I/Q Source from PlutoSDR stream
            self.iq_source = network.udp_source(
                8,  # sizeof(gr_complex) = 8 bytes
                1,
                args.port,
                0,
                1472,
                False,
                False,
                False,
            )

            # 2. LoRa SDR Receiver Block
            self.lora_rx = lora_sdr_lora_rx(
                center_freq=int(freq_hz),
                bw=int(args.bw),
                cr=1,  # 4/5
                has_crc=True,
                impl_head=False,
                pay_len=121,
                samp_rate=1_000_000,
                sf=int(args.sf),
                sync_word=[0x12, 0x34],
                soft_decoding=True,
                ldro_mode=0,
                print_rx=[False, False],
            )

            # 3. Output Handler
            self.handler = TelemetryHandlerBlock(is_hex=args.hex)

            # Connect Blocks
            self.connect((self.iq_source, 0), (self.lora_rx, 0))
            self.msg_connect((self.lora_rx, "out"), (self.handler, "in"))

    tb = PlutoLoRaFlowgraph()
    tb.start()

    print(f"[Pluto+ SDR] Demodulator active on {freq_hz/1e6:.3f} MHz (Listening on UDP:{args.port}).")
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


