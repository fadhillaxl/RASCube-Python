#!/usr/bin/env python3
"""Step 1: Record raw I/Q from PlutoSDR to a .npy file.

Records 5 seconds of I/Q at 925 MHz (Sat #1581 channel).
Then runs gr-lora_sdr on the recorded file.

Usage:
  python examples/sdr/pluto_record_and_decode.py --freq 925.0
  python examples/sdr/pluto_record_and_decode.py --freq 926.2
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import os

import numpy as np

sys.path.insert(0, "src")


def record_iq(freq_hz: int, gain_db: float = 64.0, duration_s: float = 5.0,
              samp_rate: int = 1_000_000, uri: str = "usb:") -> np.ndarray:
    """Connect to PlutoSDR and record raw I/Q samples."""
    try:
        import adi  # type: ignore
    except ImportError:
        raise RuntimeError("pip install pyadi-iio")

    print(f"[Record] Connecting to PlutoSDR ({uri})...", flush=True)
    dev = adi.Pluto(uri)
    dev.sample_rate = samp_rate
    dev.rx_lo = freq_hz
    dev.rx_rf_bandwidth = 250_000  # wider than BW to capture signal edges
    dev.gain_control_mode_chan0 = "manual"
    dev.rx_hardwaregain_chan0 = gain_db
    dev.rx_buffer_size = 65536

    print(f"[Record] Tuned to {freq_hz/1e6:.3f} MHz, Gain={gain_db}dB, {duration_s:.0f}s", flush=True)

    # Warm up
    dev.rx()

    chunks = []
    n_chunks = int(duration_s * samp_rate / 65536) + 1
    print(f"[Record] Capturing {n_chunks} chunks × 65536 samples...", flush=True)
    for i in range(n_chunks):
        buf = dev.rx()
        if buf is not None:
            chunks.append(buf.astype(np.complex64))
        if i % 5 == 0:
            # Show true dBFS (PlutoSDR raw ADC is ±2048)
            raw_mean = np.mean(np.abs(chunks[-1]))
            dbfs = 20 * np.log10(raw_mean / 2048.0 + 1e-10)
            print(f"  chunk {i+1}/{n_chunks}  mean={raw_mean:.0f} ADC ({dbfs:.1f} dBFS)", flush=True)

    iq = np.concatenate(chunks).astype(np.complex64)
    # Normalize to ±1.0 (gr-lora_sdr expects normalized float, not raw ADC int16)
    iq_norm = (iq / 2048.0).astype(np.complex64)
    peak = float(np.max(np.abs(iq_norm)))
    mean_dbfs = 20 * np.log10(float(np.mean(np.abs(iq_norm))) + 1e-10)
    print(f"[Record] Captured {len(iq_norm)} samples | peak={peak:.3f} | mean={mean_dbfs:.1f} dBFS", flush=True)
    return iq_norm


def decode_file(iq_file: str, freq_hz: int, sf: int = 7, bw: int = 125_000,
                samp_rate: int = 1_000_000) -> None:
    """Run gr-lora_sdr on a recorded I/Q file and print decoded packets."""
    gr_script = f"""\
import sys
sys.path.insert(0, '/opt/homebrew/lib/python3.14/site-packages')
import pmt
import numpy as np
from gnuradio import gr, blocks
from gnuradio.lora_sdr import lora_sdr_lora_rx

class MsgSink(gr.sync_block):
    def __init__(self):
        super().__init__(name='MsgSink', in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.intern('in'))
        self.set_msg_handler(pmt.intern('in'), self.handle)
        self.n = 0
    def handle(self, msg):
        try:
            d = pmt.cdr(msg) if pmt.is_pair(msg) else msg
            if pmt.is_u8vector(d):
                raw = bytes(pmt.u8vector_elements(d))
            else:
                raw = pmt.symbol_to_string(d).encode('latin-1')
            self.n += 1
            print(f'[DECODE] Packet #{{self.n}}: {{len(raw)}} bytes  {{raw[:16].hex().upper()}}', flush=True)
        except Exception as e:
            print(f'[DECODE] Error: {{e}}', flush=True)

# Load I/Q from file
iq_raw = np.fromfile('{iq_file}', dtype=np.complex64)
# Normalize: PlutoSDR returns raw int16 ADC counts (±2048 for 12-bit)
# gr-lora_sdr expects normalized complex float (≈ ±1.0)
max_val = np.max(np.abs(iq_raw))
if max_val > 10.0:
    iq = (iq_raw / 2048.0).astype(np.complex64)
    print(f'[GR] Loaded {{len(iq)}} samples (normalized from ADC counts, peak={{max_val:.0f}} → {{np.max(np.abs(iq)):.3f}})', flush=True)
else:
    iq = iq_raw
    print(f'[GR] Loaded {{len(iq)}} samples (already normalized, peak={{max_val:.3f}})', flush=True)

print(f'[GR] Signal power: mean={{np.mean(np.abs(iq)):.4f}}, peak={{np.max(np.abs(iq)):.4f}}', flush=True)

# Try multiple sync words AND multiple SF values
tests = [
    ('SF7 sw=0x12', dict(sf={sf}, sync_word=[0x12])),
    ('SF7 sw=0x34', dict(sf={sf}, sync_word=[0x34])),
    ('SF8 sw=0x12', dict(sf=8, sync_word=[0x12])),
    ('SF9 sw=0x12', dict(sf=9, sync_word=[0x12])),
    ('SF12 sw=0x12', dict(sf=12, sync_word=[0x12])),
]
for label, params in tests:
    print(f'\\n[GR] Trying {{label}}...', flush=True)
    try:
        src = blocks.vector_source_c(iq.tolist(), False)
        rx = lora_sdr_lora_rx(
            center_freq={freq_hz},
            bw={bw},
            cr=1,
            has_crc=True,
            impl_head=False,
            pay_len=121,
            samp_rate={samp_rate},
            soft_decoding=True,
            ldro_mode=0,
            print_rx=[True, True],
            **params
        )
        char_sink = blocks.null_sink(gr.sizeof_char)
        sink = MsgSink(); sink.n = 0
        tb = gr.top_block()
        tb.connect((src, 0), (rx, 0))
        tb.connect((rx, 0), char_sink)
        tb.msg_connect((rx, 'out'), (sink, 'in'))
        tb.run()
        print(f'[GR] → {{sink.n}} packets decoded', flush=True)
        if sink.n > 0:
            print(f'[GR] ✅ SUCCESS with {{label}}!', flush=True)
            break
    except Exception as e:
        print(f'[GR] Error: {{e}}', flush=True)
"""
    print("\n[Decode] Running gr-lora_sdr on recorded I/Q...", flush=True)
    subprocess.run(
        ["/opt/homebrew/opt/python@3.14/bin/python3.14", "-c", gr_script],
        check=False
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freq", type=float, default=925.0, help="Frequency MHz")
    parser.add_argument("--gain", type=float, default=40.0, help="RX gain dB (default: 40 = non-clipping)")
    parser.add_argument("--uri", default="usb:", help="PlutoSDR URI")
    parser.add_argument("--sf", type=int, default=7)
    parser.add_argument("--bw", type=int, default=125_000)
    parser.add_argument("--duration", type=float, default=5.0, help="Recording duration seconds")
    parser.add_argument("--file", default=None, help="Use existing .npy file instead of recording")
    args = parser.parse_args()

    freq_hz = int(args.freq * 1e6)
    iq_file = args.file or f"/tmp/pluto_iq_{int(args.freq*10):05d}.f32"

    if args.file and os.path.exists(args.file):
        print(f"[Info] Using existing file: {args.file}", flush=True)
    else:
        iq = record_iq(freq_hz=freq_hz, gain_db=args.gain,
                       duration_s=args.duration, uri=args.uri)
        iq.astype(np.complex64).tofile(iq_file)
        size_kb = os.path.getsize(iq_file) / 1024
        print(f"[Info] Saved {size_kb:.0f} KB to {iq_file}", flush=True)

    decode_file(iq_file, freq_hz=freq_hz, sf=args.sf, bw=args.bw)


if __name__ == "__main__":
    main()
