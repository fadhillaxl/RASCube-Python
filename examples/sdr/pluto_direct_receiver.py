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


# Exact Semtech SX1262 Galois PN9 LFSR sequence (512 nibbles = 256 bytes)
LFSR_NIBBLES = [
    15, 15, 14, 15, 12, 15, 8, 15, 0, 15, 1, 14, 2, 12, 5, 8, 11, 0, 7, 1, 15, 2, 14, 5, 12, 11, 8, 7, 1, 15, 3, 14,
    6, 12, 13, 8, 10, 1, 4, 3, 8, 6, 0, 13, 0, 10, 0, 4, 0, 8, 1, 0, 2, 0, 4, 0, 8, 0, 1, 1, 3, 2, 7, 4,
    14, 8, 13, 1, 11, 2, 6, 5, 13, 11, 10, 6, 4, 13, 9, 10, 3, 5, 6, 11, 12, 6, 9, 13, 2, 11, 5, 6, 10, 13, 5, 10,
    11, 5, 6, 10, 12, 5, 9, 10, 3, 5, 7, 10, 14, 4, 13, 9, 11, 2, 7, 5, 14, 11, 13, 6, 10, 13, 4, 11, 9, 6, 2, 13,
    5, 11, 11, 6, 6, 12, 13, 9, 10, 3, 4, 7, 9, 14, 2, 13, 4, 11, 8, 6, 1, 13, 2, 11, 5, 6, 10, 13, 4, 10, 9, 5,
    3, 10, 6, 5, 13, 10, 10, 5, 5, 10, 11, 5, 7, 10, 14, 5, 13, 10, 11, 5, 6, 10, 13, 5, 10, 11, 4, 6, 9, 13, 3, 10,
    6, 5, 12, 11, 9, 6, 3, 13, 6, 11, 12, 6, 8, 13, 1, 11, 3, 6, 6, 13, 13, 10, 11, 4, 6, 8, 13, 0, 10, 0, 5, 0,
    10, 0, 5, 1, 10, 2, 4, 5, 9, 10, 3, 4, 6, 9, 12, 2, 9, 4, 3, 9, 6, 2, 13, 4, 11, 9, 6, 2, 13, 5, 11, 10, 6,
    5, 13, 11, 10, 6, 4, 13, 9, 10, 3, 4, 7, 9, 15, 2, 14, 5, 13, 10, 10, 5, 4, 11, 9, 6, 3, 13, 7, 10, 14, 5, 13,
    11, 10, 6, 5, 13, 10, 10, 5, 4, 10, 8, 5, 1, 10, 3, 4, 6, 9, 13, 2, 10, 5, 5, 10, 10, 5, 4, 10, 8, 4, 0, 9,
    0, 2, 0, 5, 0, 10, 1, 5, 2, 10, 4, 5, 8, 11, 0, 6, 0, 13, 1, 10, 2, 5, 4, 10, 8, 5, 0, 11, 1, 6, 2, 13,
    4, 10, 9, 4, 3, 9, 7, 2, 14, 5, 13, 10, 10, 5, 5, 11, 11, 6, 6, 13, 12, 10, 9, 4, 2, 9, 5, 2, 11, 5, 6, 10,
    12, 5, 8, 11, 1, 6, 3, 12, 7, 9, 14, 2, 13, 5, 10, 11, 4, 6, 9, 12, 2, 9, 5, 2, 10, 5, 5, 11, 10, 6, 4, 12,
    9, 9, 2, 2, 5, 5, 10, 10, 5, 4, 10, 9, 4, 2, 9, 5, 3, 10, 6, 4, 12, 9, 8, 3, 0, 6, 0, 12, 1, 9, 2, 2,
    4, 5, 8, 10, 1, 5, 3, 10, 6, 5, 12, 11, 8, 6, 0, 13, 1, 10, 3, 5, 7, 10, 14, 4, 12, 9, 9, 2, 3, 5, 6, 10,
    13, 5, 10, 10, 4, 5, 9, 10, 2, 5, 5, 10, 11, 5, 6, 10, 13, 5, 10, 11, 4, 6, 8, 12, 1, 9, 3, 3, 6, 6, 12, 12
]


def dewhiten_nibbles(nibbles: list[int]) -> list[int]:
    """Semtech SX1262 PN9 Galois LFSR dewhitener."""
    return [n ^ LFSR_NIBBLES[i % len(LFSR_NIBBLES)] for i, n in enumerate(nibbles)]


def decode_hamming_5_4(codeword: int) -> int:
    """Decode Hamming(5,4): 4 data bits + 1 parity bit."""
    d0 = codeword & 1
    d1 = (codeword >> 1) & 1
    d2 = (codeword >> 2) & 1
    d3 = (codeword >> 3) & 1
    return (d3 << 3) | (d2 << 2) | (d1 << 1) | d0


def decode_lora_frame(symbols: list[int], sf: int = 7, cr: int = 1) -> bytes | None:
    """Semtech SX1262 DSP Demapper, Diagonal Deinterleaver, and Galois PN9 Dewhitener."""
    if len(symbols) < 10:
        return None

    # 1. Gray Mapping: codeword = (sym ^ (sym >> 1))
    mapped = [(s ^ (s >> 1)) for s in symbols]

    # 2. Diagonal Deinterleaving (CR=4/5 -> cw_len=5)
    cw_len = 4 + cr
    n_blocks = len(mapped) // cw_len
    nibbles = []

    for blk in range(n_blocks):
        block_syms = mapped[blk * cw_len : (blk + 1) * cw_len]
        for bit in range(sf):
            codeword = 0
            for i in range(cw_len):
                shift = (bit + i) % sf
                b = (block_syms[i] >> shift) & 1
                codeword |= (b << i)
            nibbles.append(decode_hamming_5_4(codeword))

    # 3. Dewhiten using Semtech SX1262 Galois LFSR
    unwhitened = dewhiten_nibbles(nibbles)

    # 4. Pack nibbles into bytes
    data_bytes = bytearray()
    for i in range(0, len(unwhitened) - 1, 2):
        data_bytes.append((unwhitened[i] << 4) | unwhitened[i + 1])

    return bytes(data_bytes)


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


def format_telemetry_summary(sample: MainTelemetrySample, meas_rssi: float, meas_snr: float, count: int) -> str:
    """Formats decoded MainTelemetrySample into human-readable ground station log."""
    return (
        f"[{count}] Seq #{sample.packet_sequence} | "
        f"Uptime: {sample.device_uptime_ms / 1000.0:.1f}s | "
        f"Vbat: {sample.eps.battery_charge.bus_voltage_v:.2f}V | "
        f"Temp: {sample.barometer.temperature_c:.1f}°C | "
        f"Alt: {sample.barometer.altitude_m:.1f}m | "
        f"GPS: ({sample.gps.latitude:.4f}°, {sample.gps.longitude:.4f}°, {sample.gps.satellites} sats) | "
        f"RSSI: {meas_rssi:.1f}dBm | SNR: {meas_snr:.1f}dB"
    )


def print_decoded_hex(hex_str: str) -> None:
    """Decodes and prints complete telemetry structure from HEX."""
    import json
    from rascube_v2 import decode_telemetry_to_dict

    print("=" * 65)
    print("🛰️ PlutoSDR Telemetry HEX Decoder")
    print("=" * 65)
    print(f"Raw HEX ({len(hex_str)//2} bytes):\n{hex_str}\n")

    sample = decode_main_telemetry_hex(hex_str)
    print("--- 📡 Decoded Satellite Metrics ---")
    print(f"Sequence Counter  : #{sample.packet_sequence}")
    print(f"System Uptime     : {sample.device_uptime_ms / 1000.0:.2f} s")
    print(f"Battery Voltage   : {sample.eps.battery_charge.bus_voltage_v:.3f} V")
    print(f"5V Rail Voltage   : {sample.eps.main_5v_v:.3f} V")
    print(f"3.3V Rail Voltage : {sample.eps.main_3v3_v:.3f} V")
    print(f"Barometer Temp    : {sample.barometer.temperature_c:.1f} °C")
    print(f"Barometer Altitude: {sample.barometer.altitude_m:.2f} m")
    print(f"Barometer Pressure: {sample.barometer.pressure_hpa:.2f} hPa")
    print(f"IMU Accel (g)     : X={sample.imu.accelerometer_g.x:.3f}, Y={sample.imu.accelerometer_g.y:.3f}, Z={sample.imu.accelerometer_g.z:.3f}")
    print(f"IMU Gyro (°/s)    : X={sample.imu.gyroscope_dps.x:.2f}, Y={sample.imu.gyroscope_dps.y:.2f}, Z={sample.imu.gyroscope_dps.z:.2f}")
    print(f"GPS Position      : Lat={sample.gps.latitude:.6f}°, Lon={sample.gps.longitude:.6f}°, Alt={sample.gps.altitude_m:.1f} m, Sats={sample.gps.satellites}")
    print(f"Radio Link        : RSSI={sample.receiver_rssi:.1f} dBm, SNR={sample.receiver_snr:.2f} dB")
    print("=" * 65)


def main() -> None:
    parser = argparse.ArgumentParser(description="PlutoSDR Direct Telemetry Receiver & HEX Decoder")
    parser.add_argument("--sat", type=int, default=1581, help="Satellite serial number (default: 1581)")
    parser.add_argument("--gain", type=float, default=40.0, help="SDR RX gain in dB (default: 40.0)")
    parser.add_argument("--hex", action="store_true", help="Print raw HEX telemetry packets instead of metrics")
    parser.add_argument("--json", action="store_true", help="Print decoded packets in JSON format")
    parser.add_argument("--decode", type=str, default=None, help="Decode a specific raw HEX packet and exit")
    args = parser.parse_args()

    if args.decode:
        print_decoded_hex(args.decode)
        return

    freq_hz = 925_000_000
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
    print(f"📋 Output Mode    : {'Raw HEX Stream' if args.hex else 'Live Decoded Telemetry'}")
    print("=" * 65)
    print("Streaming live telemetry directly from SDR (Ctrl+C to stop)...\n")

    dev.rx()  # warm up
    buf_accum = np.array([], dtype=np.complex64)
    total_decoded = 0

    try:
        while True:
            raw_buf = dev.rx()
            if raw_buf is None or len(raw_buf) == 0:
                time.sleep(0.005)
                continue

            c64_buf = (raw_buf.astype(np.complex64) / 2048.0)
            buf_accum = np.concatenate((buf_accum, c64_buf))

            # Process in windows of 200,000 samples (~0.2s)
            if len(buf_accum) >= 200_000:
                iq_proc = buf_accum[:200_000]
                buf_accum = buf_accum[180_000:]

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
                            elif args.json:
                                import json
                                from rascube_v2 import decode_telemetry_to_dict
                                try:
                                    d = decode_telemetry_to_dict(rascube_pkt)
                                    print(json.dumps(d), flush=True)
                                except Exception:
                                    print(f"[{total_decoded}] {rascube_pkt.hex().upper()}", flush=True)
                            else:
                                try:
                                    sample = decode_main_telemetry_hex(rascube_pkt)
                                    print(format_telemetry_summary(sample, meas_rssi, meas_snr, total_decoded), flush=True)
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
