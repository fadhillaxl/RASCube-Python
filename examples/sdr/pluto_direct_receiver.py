#!/usr/bin/env python3
"""PlutoSDR Direct Native Python Ground Station Telemetry Receiver.

Uses PlutoSDR hardware directly without GNU Radio:
- Tunes AD9363 RF frontend (925.0 MHz)
- Demodulates LoRa CSS chirps in real time using vector FFT
- Decodes telemetry frames and prints live data
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

import numpy as np

sys.path.insert(0, "src")
from rascube_v2.decoder import decode_main_telemetry_hex
from rascube_v2.models.telemetry import MainTelemetrySample


WHITENING_SEQ = [
    0xFF, 0xFE, 0xFC, 0xF8, 0xF0, 0xE1, 0xC2, 0x85, 0x0B, 0x17, 0x2F, 0x5E, 0xBC, 0x78, 0xF1, 0xE3,
    0xC6, 0x8D, 0x1B, 0x37, 0x6E, 0xDC, 0xB8, 0x71, 0xE2, 0xC4, 0x89, 0x13, 0x27, 0x4E, 0x9C, 0x38,
    0x70, 0xE0, 0xC0, 0x81, 0x03, 0x07, 0x0F, 0x1F, 0x3F, 0x7E, 0xFC, 0xF8, 0xF1, 0xE3, 0xC7, 0x8F,
    0x1F, 0x3E, 0x7C, 0xF8, 0xF0, 0xE1, 0xC2, 0x84, 0x09, 0x13, 0x26, 0x4C, 0x98, 0x30, 0x60, 0xC0,
    0x80, 0x01, 0x02, 0x04, 0x08, 0x11, 0x23, 0x47, 0x8E, 0x1D, 0x3B, 0x76, 0xEC, 0xD8, 0xB1, 0x62,
    0xC5, 0x8B, 0x17, 0x2E, 0x5C, 0xB8, 0x70, 0xE1, 0xC2, 0x85, 0x0A, 0x15, 0x2B, 0x56, 0xAC, 0x58,
    0xB0, 0x61, 0xC3, 0x87, 0x0F, 0x1E, 0x3C, 0x78, 0xF0, 0xE0, 0xC1, 0x83, 0x07, 0x0E, 0x1C, 0x38,
    0x71, 0xE3, 0xC6, 0x8C, 0x19, 0x33, 0x66, 0xCC, 0x98, 0x31, 0x62, 0xC4, 0x88, 0x10, 0x21, 0x43,
    0x86, 0x0D, 0x1B, 0x36, 0x6C, 0xD9, 0xB3, 0x66, 0xCD, 0x9A, 0x34, 0x69, 0xD3, 0xA7, 0x4E, 0x9D,
    0x3A, 0x74, 0xE8, 0xD0, 0xA1, 0x42, 0x85, 0x0B, 0x16, 0x2D, 0x5A, 0xB4, 0x68, 0xD1, 0xA3, 0x46,
    0x8C, 0x18, 0x31, 0x63, 0xC6, 0x8D, 0x1A, 0x35, 0x6A, 0xD5, 0xAB, 0x56, 0xAD, 0x5A, 0xB5, 0x6A,
    0xD4, 0xA9, 0x52, 0xA4, 0x48, 0x91, 0x23, 0x46, 0x8D, 0x1B, 0x36, 0x6D, 0xDA, 0xB5, 0x6B, 0xD6,
    0xAC, 0x59, 0xB2, 0x64, 0xC9, 0x93, 0x27, 0x4F, 0x9E, 0x3C, 0x79, 0xF2, 0xE5, 0xCB, 0x97, 0x2E,
    0x5D, 0xBA, 0x75, 0xEB, 0xD6, 0xAD, 0x5B, 0xB6, 0x6C, 0xD8, 0xB0, 0x60, 0xC1, 0x82, 0x05, 0x0B,
    0x17, 0x2E, 0x5D, 0xBB, 0x76, 0xED, 0xDA, 0xB4, 0x69, 0xD2, 0xA5, 0x4A, 0x95, 0x2B, 0x57, 0xAE,
    0x5C, 0xB9, 0x72, 0xE4, 0xC9, 0x92, 0x25, 0x4B, 0x97, 0x2F, 0x5E, 0xBD, 0x7A, 0xF5, 0xEB, 0xD7
]


def dewhiten(data: bytearray | bytes) -> bytes:
    return bytes(b ^ WHITENING_SEQ[i % len(WHITENING_SEQ)] for i, b in enumerate(data))


def decode_lora_frame(symbols: list[int], sf: int = 7, cr: int = 1) -> bytes | None:
    """Demaps, deinterleaves, and dewhitens a stream of LoRa symbols."""
    gray_demapped = []
    for s in symbols:
        val = s
        shift = 1
        while shift < sf:
            val ^= (val >> shift)
            shift <<= 1
        gray_demapped.append(val)

    cw_len = 4 + cr
    n_blocks = len(gray_demapped) // cw_len
    nibbles = []

    for blk in range(n_blocks):
        block_syms = gray_demapped[blk * cw_len : (blk + 1) * cw_len]
        for bit in range(sf):
            codeword = 0
            for i in range(cw_len):
                shift = (bit + i) % sf
                b = (block_syms[i] >> shift) & 1
                codeword |= (b << i)
            data_nibble = codeword & 0x0F
            nibbles.append(data_nibble)

    raw_bytes = bytearray()
    for i in range(0, len(nibbles) - 1, 2):
        byte_val = (nibbles[i + 1] << 4) | nibbles[i]
        raw_bytes.append(byte_val)

    return dewhiten(raw_bytes)


def verify_lora_crc16(data: bytes, received_crc: int) -> bool:
    """Calculates SX126x/SX127x standard LoRa CRC16-CCITT."""
    crc = 0x0000
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc == received_crc


def main() -> None:
    parser = argparse.ArgumentParser(description="PlutoSDR Direct Telemetry Receiver")
    parser.add_argument("--sat", type=int, default=1581)
    parser.add_argument("--gain", type=float, default=40.0)
    parser.add_argument("--hex", action="store_true")
    args = parser.parse_args()

    freq_hz = 916_000_000 + (args.sat % 18) * 600_000
    fs = 1_000_000
    bw = 500_000
    sf = 7
    n_chips = 128
    n_samples_per_sym = int(fs * n_chips / bw)  # 256
    os = 2

    # Precalculate down-chirp
    t = np.arange(n_samples_per_sym) / fs
    k = (bw**2) / n_chips
    phi = 2 * np.pi * (-bw/2.0 * t + 0.5 * k * (t**2))
    down_chirp = np.exp(-1j * phi).astype(np.complex64)

    import adi
    print(f"[PlutoSDR] Connecting to USB...")
    dev = adi.Pluto("usb:")
    dev.sample_rate = fs
    dev.rx_lo = freq_hz
    dev.rx_rf_bandwidth = 1_000_000
    dev.gain_control_mode_chan0 = "manual"
    dev.rx_hardwaregain_chan0 = args.gain
    dev.rx_buffer_size = 65536

    print("=" * 65)
    print(f"🛰️ PlutoSDR Native Real-Time Ground Station Receiver")
    print(f"📡 Sat #{args.sat} @ {freq_hz/1e6:.3f} MHz (Channel {args.sat % 18})")
    print(f"⚙️ LoRa Modulation: SF={sf}, BW={bw/1000:.0f} kHz, CR=4/5")
    print("=" * 65)
    print("Streaming live telemetry directly from SDR (Ctrl+C to stop)...\n")

    dev.rx()  # warm up
    buf_accum = np.array([], dtype=np.complex64)
    total_decoded = 0

    try:
        while True:
            raw_buf = dev.rx()
            if raw_buf is None:
                continue

            iq = (raw_buf / 2048.0).astype(np.complex64)
            buf_accum = np.concatenate([buf_accum, iq])

            # Process when we have > 1.0s of data
            if len(buf_accum) >= 1_000_000:
                iq_proc = buf_accum[:1_000_000]
                buf_accum = buf_accum[750_000:]  # 250ms overlap

                # Search for LoRa preambles
                step = n_samples_per_sym // 4
                n_steps = (len(iq_proc) - n_samples_per_sym) // step

                all_syms = []
                for s in range(n_steps):
                    idx = s * step
                    win = iq_proc[idx : idx + n_samples_per_sym] * down_chirp
                    dec = win.reshape(n_chips, os).sum(axis=1)
                    fft_mag = np.abs(np.fft.fft(dec))
                    sym = int(np.argmax(fft_mag))
                    snr = fft_mag[sym] / (np.mean(fft_mag) + 1e-10)
                    all_syms.append((idx, sym, snr))

                # Preamble detection: 8 consecutive symbols with SNR > 10.0 and constant bin (modulo jitter)
                idx = 0
                while idx < len(all_syms) - 200:
                    cands = [all_syms[idx + k * 4] for k in range(8)]
                    syms = [c[1] for c in cands]
                    snrs = [c[2] for c in cands]

                    if all(s > 10.0 for s in snrs) and (max(syms) - min(syms) <= 2):
                        start_sample = cands[0][0]
                        cfo = syms[0]

                        # Demodulate payload symbols starting after SFD (offset 12.25 symbols)
                        payload_start = start_sample + int(12.25 * n_samples_per_sym)
                        frame_symbols = []
                        for s_idx in range(250):
                            pos = payload_start + s_idx * n_samples_per_sym
                            if pos + n_samples_per_sym > len(iq_proc):
                                break
                            win = iq_proc[pos : pos + n_samples_per_sym] * down_chirp
                            dec = win.reshape(n_chips, os).sum(axis=1)
                            raw_sym = int(np.argmax(np.abs(np.fft.fft(dec))))
                            # Apply CFO correction
                            frame_symbols.append((raw_sym - cfo) % n_chips)

                        decoded = decode_lora_frame(frame_symbols, sf=sf, cr=1)
                        if decoded and len(decoded) >= 20:
                            total_decoded += 1
                            # Estimate RF power (RSSI) and SNR
                            p_sig = float(np.mean(np.abs(iq_proc[payload_start : payload_start + 1024]) ** 2) + 1e-12)
                            meas_rssi = float(-100.0 + 10.0 * np.log10(p_sig * 1000.0))
                            meas_snr = float(np.mean(snrs))

                            # Format to full RASCube Ground Station 123-byte Frame (0x10 0x79 + 113 payload + 8 RSSI/SNR)
                            payload_113 = decoded[:113].ljust(113, b"\x00")
                            rssi_bytes = struct.pack("<f", meas_rssi)
                            snr_bytes = struct.pack("<f", meas_snr)
                            rascube_pkt = bytes([0x10, 0x79]) + payload_113 + rssi_bytes + snr_bytes

                            if args.hex:
                                print(rascube_pkt.hex().upper(), flush=True)
                            else:
                                try:
                                    sample = decode_main_telemetry_hex(rascube_pkt)
                                    print(f"[{total_decoded}] Uptime: {sample.device_uptime_ms}ms | Bat: {sample.voltage_cell_1_mv}mV | RSSI: {sample.radio.rssi_dbm:.1f}dBm | SNR: {sample.radio.snr_db:.1f}dB", flush=True)
                                except Exception:
                                    print(f"[{total_decoded}] {rascube_pkt.hex().upper()}", flush=True)

                        # Skip forward past this packet
                        idx += 200
                    else:
                        idx += 1
    except KeyboardInterrupt:
        print(f"\nStopped. Total decoded: {total_decoded}")


if __name__ == "__main__":
    main()


