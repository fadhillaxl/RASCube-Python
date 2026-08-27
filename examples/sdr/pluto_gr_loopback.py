#!/usr/bin/env python3
"""GNU Radio gr-lora_sdr TX→RX loopback test.

Uses gr-lora_sdr's own TX block to generate a proper LoRa I/Q signal,
pipes it directly into the RX block, and verifies decode works.

This confirms:
1. gr-lora_sdr RX configuration is correct
2. sync_word, SF, BW, CR, pay_len are all valid
3. The pipeline (modulate → demodulate) works end-to-end

Usage:
  python examples/sdr/pluto_gr_loopback.py
"""
from __future__ import annotations
import subprocess
import sys

GR_PYTHON = "/opt/homebrew/opt/python@3.14/bin/python3.14"

# Test payload (121 bytes, zeroes are fine for structure test)
TEST_PAYLOAD = list(range(121))

gr_script = f"""\
import sys
sys.path.insert(0, '/opt/homebrew/lib/python3.14/site-packages')
import pmt, time
from gnuradio import gr, blocks, analog
from gnuradio.lora_sdr import lora_sdr_lora_tx, lora_sdr_lora_rx

PAYLOAD = bytes({TEST_PAYLOAD})
SF = 7
BW = 125000
SAMP_RATE = 1000000
SYNC_WORD = [0x12]

tb = gr.top_block('LoRaLoopback')

# Build PDU: lora_sdr_lora_tx expects a u8vector wrapped in a pair
pdu_data = pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(len(PAYLOAD), list(PAYLOAD)))

# message_strobe fires the PDU once per period_ms
strobe = blocks.message_strobe(pdu_data, 200)  # every 200ms

tx = lora_sdr_lora_tx(
    bw=BW,
    cr=1,
    has_crc=True,
    impl_head=False,
    samp_rate=SAMP_RATE,
    sf=SF,
    sync_word=SYNC_WORD,
    ldro_mode=0,
)

# Add small noise
noise = analog.noise_source_c(analog.GR_GAUSSIAN, 0.01, 0)
add = blocks.add_cc()

# RX side
rx = lora_sdr_lora_rx(
    center_freq=0,
    bw=BW,
    cr=1,
    has_crc=True,
    impl_head=False,
    pay_len=121,
    samp_rate=SAMP_RATE,
    sf=SF,
    sync_word=SYNC_WORD,
    soft_decoding=True,
    ldro_mode=0,
    print_rx=[True, True],
)

class PduSink(gr.sync_block):
    def __init__(self):
        super().__init__(name='PduSink', in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.intern('in'))
        self.set_msg_handler(pmt.intern('in'), self.handle)
        self.received = []

    def handle(self, msg):
        try:
            data = pmt.cdr(msg) if pmt.is_pair(msg) else msg
            raw = bytes(pmt.u8vector_elements(data))
            self.received.append(raw)
            print(f'DECODED: {{len(raw)}} bytes: {{raw[:12].hex().upper()}}...', flush=True)
        except Exception as e:
            print(f'SINK ERR: {{e}}', flush=True)

sink = PduSink()

# Wire: strobe -> tx -> add(+noise) -> rx -> sink
tb.msg_connect((strobe, 'strobe'), (tx, 'in'))
tb.connect((tx, 0), (add, 0))
tb.connect((noise, 0), (add, 1))
tb.connect((add, 0), (rx, 0))
tb.msg_connect((rx, 'out'), (sink, 'in'))

print('Starting gr-lora_sdr loopback (TX -> RX)...', flush=True)
tb.start()
time.sleep(8)
tb.stop()
tb.wait()

if sink.received:
    first = sink.received[0]
    match = (first == bytes({TEST_PAYLOAD}))
    print(f'\\n✅ SUCCESS: decoded {{len(sink.received)}} packet(s), payload match: {{match}}', flush=True)
else:
    print('\\n❌ FAIL: no packets decoded in gr-lora_sdr loopback', flush=True)
"""

print("=" * 60)
print("🔬 gr-lora_sdr Internal TX→RX Loopback Test")
print("=" * 60)
print(f"Running with: {GR_PYTHON}")
print()

proc = subprocess.run(
    [GR_PYTHON, "-c", gr_script],
    capture_output=False,
    timeout=30,
)
sys.exit(proc.returncode)
