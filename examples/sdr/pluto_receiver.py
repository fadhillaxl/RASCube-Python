#!/usr/bin/env python3
"""PlutoSDR (ADALM-PLUTO) Standalone Ground Station Telemetry Receiver.

Uses PlutoSDR hardware directly as a complete replacement for the USB dongle:
- Connects directly to PlutoSDR via USB (or Network IP)
- Tunes RF front-end to target frequency (e.g. 925.000 MHz for Sat #1581)
- Runs GNU Radio gr-lora_sdr demodulator as subprocess
- Streams telemetry data samples continuously (or raw HEX with --hex)

Usage:
  python examples/sdr/pluto_receiver.py --sat 1581
  python examples/sdr/pluto_receiver.py --sat 1581 --hex
  python examples/sdr/pluto_receiver.py --freq 926.2
"""

from __future__ import annotations

import argparse
import subprocess
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

    freq_hz = int(freq_mhz * 1e6)

    # Print banner
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
        custom_frequency_hz=freq_hz,
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

    receiver = PlutoSDRReceiver(config=config, on_sample=on_sample, on_raw_hex=on_raw_hex)

    # -----------------------------------------------------------------------
    # Start GNU Radio gr-lora_sdr demodulator subprocess
    # It listens for raw I/Q UDP on port 9090 and forwards decoded LoRa
    # packets as raw bytes via UDP to port 9091.
    # -----------------------------------------------------------------------
    gr_proc = None
    gr_script = f"""\
import sys
sys.path.insert(0, '/opt/homebrew/lib/python3.14/site-packages')
import pmt, socket
from gnuradio import gr, network
from gnuradio.lora_sdr import lora_sdr_lora_rx

class FrameForwarder(gr.sync_block):
    def __init__(self):
        super().__init__(name='Forwarder', in_sig=None, out_sig=None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.message_port_register_in(pmt.intern('in'))
        self.set_msg_handler(pmt.intern('in'), self.handle)

    def handle(self, msg):
        try:
            data = pmt.cdr(msg) if pmt.is_pair(msg) else msg
            raw = bytes(pmt.u8vector_elements(data)) if pmt.is_u8vector(data) else pmt.symbol_to_string(data).encode('latin-1')
            self.sock.sendto(raw, ('127.0.0.1', 9091))
            print(f'[GR] Decoded LoRa frame: {{len(raw)}} bytes  {{raw[:8].hex().upper()}}...', flush=True)
        except Exception as exc:
            print(f'[GR] Forward error: {{exc}}', flush=True)

tb = gr.top_block('LoRaRX')
src = network.udp_source(8, 1, 9090, 0, 1472, False, False, False)
rx = lora_sdr_lora_rx(
    center_freq={freq_hz},
    bw={args.bw},
    cr=1,
    has_crc=True,
    impl_head=False,
    pay_len=121,
    samp_rate=1000000,
    sf={args.sf},
    sync_word=[0x12],
    soft_decoding=True,
    ldro_mode=0,
    print_rx=[True, True],
)
fwd = FrameForwarder()
char_sink = blocks.null_sink(gr.sizeof_char)

tb.connect((src, 0), (rx, 0))
tb.connect((rx, 0), char_sink)
tb.msg_connect((rx, 'out'), (fwd, 'in'))
print('[GR] LoRa demodulator running (UDP:9090 -> UDP:9091)...', flush=True)
tb.run()
"""
    try:
        gr_proc = subprocess.Popen(
            ["/opt/homebrew/opt/python@3.14/bin/python3.14", "-c", gr_script],
            stdout=sys.stdout,
            stderr=sys.stdout,
        )
        time.sleep(1.0)  # Wait for GR UDP server to bind
    except Exception as exc:
        print(f"[GR Error] {exc}", flush=True)

    # -----------------------------------------------------------------------
    # Connect PlutoSDR hardware
    # -----------------------------------------------------------------------
    try:
        receiver.start_direct_sdr()
        receiver.start_udp_listener(port=9091)
    except Exception as exc:
        print(f"\n[PlutoSDR Error] {exc}", flush=True)
        if gr_proc:
            gr_proc.terminate()
        return

    # -----------------------------------------------------------------------
    # Transmit wake-up beacon to satellite
    # -----------------------------------------------------------------------
    import struct

    def send_ping() -> None:
        try:
            # Port 1: set satellite serial filter
            receiver.transmit_packet(bytes([0x01, 0x04]) + struct.pack("<I", serial_number))
            time.sleep(0.05)
            # Port 12: OBC info request (triggers telemetry stream)
            receiver.transmit_packet(bytes([0x0C, 0x01, 0x00]))
        except Exception:
            pass

    print(f"[Pluto+ SDR TX] Transmitting wake-up beacon to Sat #{serial_number}...", flush=True)
    send_ping()

    print(f"\nStreaming live telemetry from Sat #{serial_number} via PlutoSDR (Ctrl+C to stop)...\n", flush=True)

    try:
        last_ping = time.time()
        last_count = 0
        while True:
            time.sleep(5)
            n = receiver.total_packets_received
            if n > last_count:
                print(f"[Stats] ✅ Received {n} packets total", flush=True)
                last_count = n
            else:
                print(f"[Stats] ⏳ Waiting for packets... (total={n})", flush=True)
            # Periodic keep-alive ping every 10 seconds
            if time.time() - last_ping > 10.0:
                print("[Pluto+ SDR TX] Sending keep-alive ping...", flush=True)
                send_ping()
                last_ping = time.time()
    except KeyboardInterrupt:
        print("\nStopping PlutoSDR receiver...", flush=True)
        if gr_proc:
            gr_proc.terminate()
        receiver.stop()
        print("Done.", flush=True)


if __name__ == "__main__":
    main()
