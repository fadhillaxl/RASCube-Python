#!/usr/bin/env python3
"""Diagnostic: Test if gr-lora_sdr can decode a locally-generated LoRa signal.

This script:
1. Uses our SoftwareLoRaDSP to generate a valid LoRa I/Q waveform
2. Sends it as UDP to GNU Radio gr-lora_sdr
3. Reports whether gr-lora_sdr decoded anything

If gr-lora_sdr successfully decodes the signal, the GR pipeline is correct.
If not, we have a sync word / parameter mismatch.

Usage:
  python examples/sdr/pluto_loopback_test.py
"""
from __future__ import annotations
import socket
import subprocess
import sys
import time
import struct

sys.path.insert(0, "src")
from rascube_v2.sdr.lora_dsp import SoftwareLoRaDSP

# LoRa parameters (must match gr-lora_sdr config)
SF = 7
BW = 125_000
SAMP_RATE = 1_000_000
FREQ_HZ = 925_000_000

# Test payload: 121 bytes (what satellite sends — fake ones for loopback)
test_payload = bytes(range(121))

print("=" * 60)
print("🔬 gr-lora_sdr Loopback Diagnostic Test")
print(f"   SF={SF}, BW={BW/1000:.0f}kHz, Freq={FREQ_HZ/1e6:.3f}MHz")
print("=" * 60)

# -----------------------------------------------------------------------
# Try multiple sync words
# -----------------------------------------------------------------------
SYNC_WORDS_TO_TEST = [
    ([0x12], "SX1262 private (0x12)"),
    ([0x34], "LoRaWAN public (0x34)"),
    ([0x12, 0x34], "Both"),
    ([0x14, 0x24], "SX1262 air-encoded private"),
]

def run_gr_test(sync_word: list[int], label: str) -> bool:
    """Run gr-lora_sdr with given sync_word and a loopback UDP signal source."""
    print(f"\n[Test] sync_word={[hex(x) for x in sync_word]} ({label})")

    gr_script = f"""\
import sys
sys.path.insert(0, '/opt/homebrew/lib/python3.14/site-packages')
import pmt, socket, signal
from gnuradio import gr, network, blocks
from gnuradio.lora_sdr import lora_sdr_lora_rx

received = []

class Sink(gr.sync_block):
    def __init__(self):
        super().__init__(name='Sink', in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.intern('in'))
        self.set_msg_handler(pmt.intern('in'), self.handle)
    def handle(self, msg):
        try:
            data = pmt.cdr(msg) if pmt.is_pair(msg) else msg
            raw = bytes(pmt.u8vector_elements(data))
            print(f'DECODED: {{len(raw)}} bytes :: {{raw[:16].hex().upper()}}', flush=True)
        except Exception as e:
            print(f'SINK ERROR: {{e}}', flush=True)

tb = gr.top_block('LoRaLoopback')
src = network.udp_source(8, 1, 9090, 0, 1472, False, False, False)
rx = lora_sdr_lora_rx(
    center_freq={FREQ_HZ},
    bw={BW},
    cr=1,
    has_crc=True,
    impl_head=False,
    pay_len=121,
    samp_rate={SAMP_RATE},
    sf={SF},
    sync_word={sync_word},
    soft_decoding=True,
    ldro_mode=0,
    print_rx=[True, True],
)
sink = Sink()
tb.connect((src, 0), (rx, 0))
tb.msg_connect((rx, 'out'), (sink, 'in'))
print('GR_READY', flush=True)
tb.start()
import time; time.sleep(10)
tb.stop(); tb.wait()
"""
    proc = subprocess.Popen(
        ["/opt/homebrew/opt/python@3.14/bin/python3.14", "-c", gr_script],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )

    # Wait for GR to be ready
    ready = False
    deadline = time.time() + 5
    while time.time() < deadline:
        line = proc.stdout.readline()
        if "GR_READY" in line or "Listening" in line:
            ready = True
            break

    if not ready:
        proc.terminate()
        print("  ❌ GNU Radio failed to start in time")
        return False

    print("  ✅ GNU Radio started, sending test signal...")

    # Generate LoRa I/Q using software DSP
    dsp = SoftwareLoRaDSP(spreading_factor=SF, bandwidth_hz=BW, sample_rate=SAMP_RATE)
    iq_samples = dsp.modulate_bytes(test_payload)
    print(f"  Generated {len(iq_samples)} I/Q samples ({len(iq_samples)*8/1024:.1f} KB)")

    # Send via UDP in chunks (repeat 5x to give GR more chances)
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    raw = iq_samples.astype("complex64").tobytes()
    CHUNK = 1472
    for repeat in range(5):
        for i in range(0, len(raw), CHUNK):
            udp_sock.sendto(raw[i : i + CHUNK], ("127.0.0.1", 9090))
        time.sleep(0.1)
    udp_sock.close()
    print("  📡 Signal sent. Waiting for GR decode output...")

    # Collect output for 8 seconds
    import threading
    output_lines = []
    def collect():
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            output_lines.append(line.strip())
            print(f"  [GR] {line.strip()}", flush=True)
    t = threading.Thread(target=collect, daemon=True)
    t.start()
    time.sleep(8)
    proc.terminate()
    t.join(timeout=2)

    decoded = any("DECODED" in l for l in output_lines)
    if decoded:
        print(f"  🎉 SUCCESS: gr-lora_sdr decoded with sync_word={[hex(x) for x in sync_word]}")
    else:
        print(f"  ❌ No decode with sync_word={[hex(x) for x in sync_word]}")
    return decoded


found_working = False
for sw, label in SYNC_WORDS_TO_TEST:
    if run_gr_test(sw, label):
        found_working = True
        print(f"\n✅ WORKING sync_word: {[hex(x) for x in sw]} ({label})")
        print(f"   Use this in pluto_receiver.py!")
        break

if not found_working:
    print("\n❌ No sync_word worked with our software-generated LoRa signal.")
    print("   This means the SoftwareLoRaDSP modulation format doesn't match gr-lora_sdr's expectation.")
    print("   → The satellite signal at 925 MHz needs a real LoRa signal (from hardware or loopback).")
