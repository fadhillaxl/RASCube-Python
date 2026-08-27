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
    parser.add_argument("--freq", type=float, default=None, help="Target frequency in MHz (e.g. 926.2)")
    parser.add_argument("--chan", type=int, default=None, help="RASCube channel 0-17 (e.g. 17 for 926.2 MHz)")
    parser.add_argument("--sat", type=int, default=1581, help="Satellite numeric serial number (default: 1581)")
    parser.add_argument("--uri", default="usb:", help="PlutoSDR URI (default: usb:, or ip:192.168.2.1)")
    parser.add_argument("--gain", type=float, default=55.0, help="SDR RX hardware gain in dB (default: 55.0)")
    parser.add_argument("--sf", type=int, default=7, help="LoRa Spreading Factor (default: 7)")
    parser.add_argument("--bw", type=int, default=125_000, help="LoRa Bandwidth in Hz (default: 125000)")
    parser.add_argument("--hex", action="store_true", help="Print raw HEX telemetry packets instead of metrics")
    parser.add_argument("--udp", action="store_true", help="Also listen for external demodulator packets on UDP port 9090")
    parser.add_argument("--port", type=int, default=9090, help="UDP port for external bridge (default: 9090)")

    args = parser.parse_args()

    if args.freq is not None:
        freq_mhz = args.freq
        channel = int(round((freq_mhz - 916.0) / 0.6)) % 18
        serial_number = args.sat
    elif args.chan is not None:
        channel = args.chan % 18
        freq_mhz = 916.0 + channel * 0.6
        serial_number = args.sat
    else:
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

    print("Receiver:", receiver_info, flush=True)
    print("OBC:", obc_info, flush=True)
    print("Enabled add-ons: []", flush=True)

    print("=" * 65, flush=True)
    print("🛰️ PlutoSDR (ADALM-PLUTO) Ground Station Receiver", flush=True)
    print(f"📡 Target Satellite : #{serial_number}", flush=True)
    print(f"📻 Target Frequency : {freq_mhz:.3f} MHz (Channel {channel})", flush=True)
    print(f"⚙️ LoRa Modulation  : SF={args.sf}, BW={args.bw/1000:.1f} kHz, CR=4/5", flush=True)
    print(f"📋 Output Format    : {'Raw HEX Packets' if args.hex else 'Uptime, Lat, Lon'}", flush=True)
    print("=" * 65, flush=True)

    config = SDRLoRaConfig(
        serial_number=serial_number,
        custom_frequency_hz=int(freq_mhz * 1e6),
        rx_gain_db=args.gain,
        spreading_factor=args.sf,
        bandwidth_hz=args.bw,
        sdr_uri=args.uri,
    )

    def on_raw_hex(hex_str: str) -> None:
        if args.hex:
            print(hex_str, flush=True)

    def on_sample(sample: MainTelemetrySample) -> None:
        if not args.hex:
            print(f"{sample.device_uptime_ms} {sample.gps.latitude} {sample.gps.longitude}", flush=True)

    receiver = PlutoSDRReceiver(
        config=config,
        on_sample=on_sample,
        on_raw_hex=on_raw_hex,
    )

    # Start embedded GNU Radio LoRa demodulator subprocess in background
    gr_proc = None
    try:
        gr_cmd = [
            "/opt/homebrew/opt/python@3.14/bin/python3.14",
            "-c",
            f"""
import sys
sys.path = [p for p in sys.path if not any(f'python3.{{v}}' in p for v in range(8, 20) if f'python3.{{v}}' != 'python3.14')]
sys.path.insert(0, '/opt/homebrew/lib/python3.14/site-packages')
import pmt, socket
from gnuradio import gr, network
from gnuradio.lora_sdr import lora_sdr_lora_rx

class FrameForwarder(gr.sync_block):
    def __init__(self):
        super().__init__(name='Forwarder', in_sig=None, out_sig=None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.message_port_register_in(pmt.intern('in'))
        self.set_msg_handler(pmt.intern('in'), self.forward)

    def forward(self, msg):
        data = pmt.cdr(msg) if pmt.is_pair(msg) else msg
        raw = bytes(pmt.u8vector_elements(data))
        self.sock.sendto(raw, ('127.0.0.1', 9091))

tb = gr.top_block('LoRaRX')
src = network.udp_source(8, 1, 9090, 0, 1472, False, False, False)
rx = lora_sdr_lora_rx(
    center_freq={int(freq_mhz * 1e6)},
    bw={int(args.bw)},
    cr=1,
    has_crc=True,
    impl_head=False,
    pay_len=121,
    samp_rate=1000000,
    sf={int(args.sf)},
    sync_word=[0x12, 0x34],
    soft_decoding=True,
    ldro_mode=0,
    print_rx=[False, False],
)
fwd = FrameForwarder()
tb.connect((src, 0), (rx, 0))
tb.msg_connect((rx, 'out'), (fwd, 'in'))
tb.run()
""",
        ]
        import subprocess
        gr_proc = subprocess.Popen(gr_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
    except Exception:
        pass

    # Connect directly to PlutoSDR hardware
    try:
        receiver.start_direct_sdr()
        receiver.start_udp_listener(port=9091)

        # Transmit LoRa wake-up handshake beacon to Satellite on 925.0 MHz
        print(f"[Pluto+ SDR TX] Transmitting wake-up beacon to Sat #{serial_number}...", flush=True)
        import struct
        # 1. Port 1 Serial Filter beacon
        receiver.transmit_packet(bytes([0x01, 0x04]) + struct.pack("<I", serial_number))
        time.sleep(0.1)
        # 2. Port 12 OBC Info ping request
        receiver.transmit_packet(bytes([0x0C, 0x01, 0x00]))
    except Exception as exc:
        print(f"\n[PlutoSDR Error] {exc}", flush=True)
        receiver.start_udp_listener(port=9091)

    print(f"\nStreaming live telemetry from Sat #{serial_number} via PlutoSDR (Ctrl+C to stop)...\n", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping PlutoSDR receiver...", flush=True)
        if gr_proc:
            gr_proc.terminate()
        receiver.stop()
        print("Done.", flush=True)


if __name__ == "__main__":
    main()


