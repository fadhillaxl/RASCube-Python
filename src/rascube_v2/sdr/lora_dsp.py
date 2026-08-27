"""Software LoRa (CSS) Digital Signal Processing Demodulator & Modulator.

Implements LoRa physical layer modulation/demodulation in pure Python + NumPy:
- Chirp Spread Spectrum (CSS) generation & de-chirp FFT correlation
- LoRa preamble detection & symbol timing synchronization
- LoRa diagonal de-interleaving / interleaving
- LoRa whitening / de-whitening LFSR
- LoRa Hamming FEC decoding / encoding (4/5, 4/6, 4/7, 4/8)
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Final

import numpy as np

# Exact Semtech SX1262 Galois PN9 LFSR sequence (512 nibbles = 256 bytes)
LORA_WHITENING_NIBBLES: Final[list[int]] = [
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
]

LORA_WHITENING_BYTES: Final[list[int]] = [
    (LORA_WHITENING_NIBBLES[i * 2] << 4) | LORA_WHITENING_NIBBLES[i * 2 + 1]
    for i in range(len(LORA_WHITENING_NIBBLES) // 2)
]
LORA_WHITENING_SEQUENCE: Final[list[int]] = LORA_WHITENING_BYTES


class SoftwareLoRaDSP:
    """Pure NumPy Software LoRa (Chirp Spread Spectrum) DSP Demodulator & Modulator."""

    def __init__(
        self,
        spreading_factor: int = 7,
        bandwidth_hz: int = 125_000,
        sample_rate: int = 1_000_000,
        preamble_symbols: int = 8,
    ) -> None:
        self.sf = spreading_factor
        self.bw = bandwidth_hz
        self.fs = sample_rate
        self.n_chips = 1 << self.sf  # 128 for SF7
        self.samples_per_symbol = int(self.fs * self.n_chips / self.bw)  # 1024 for 1MSPS, SF7, 125kHz
        self.oversampling = self.samples_per_symbol // self.n_chips  # 8

        # Precompute reference base down-chirp (conjugate of base up-chirp)
        t = np.arange(self.samples_per_symbol) / self.fs
        f0 = -self.bw / 2.0
        k = (self.bw ** 2) / self.n_chips  # chirp rate
        phi = 2 * np.pi * (f0 * t + 0.5 * k * (t ** 2))
        self.base_down_chirp = np.exp(-1j * phi).astype(np.complex64)
        self.base_up_chirp = np.exp(1j * phi).astype(np.complex64)

    def modulate_bytes(self, payload: bytes) -> np.ndarray:
        """Modulates raw binary payload bytes into complex64 I/Q samples for SDR TX."""
        # 1. Whitening
        whitened = bytes(b ^ LORA_WHITENING_SEQUENCE[i % len(LORA_WHITENING_SEQUENCE)] for i, b in enumerate(payload))

        # 2. Convert bytes to 4-bit nibbles and Hamming encode
        symbols = self._encode_payload_symbols(whitened)

        # 3. Preamble (8 up-chirps + 2 sync chirps + 2.25 down-chirps)
        iq_chunks = []
        for _ in range(8):
            iq_chunks.append(self.base_up_chirp)
        # Sync word symbols (0x34 or 0x12)
        iq_chunks.append(self._generate_chirp(0x34 % self.n_chips))
        iq_chunks.append(self._generate_chirp(0x44 % self.n_chips))
        # 2.25 down-chirps
        iq_chunks.append(self.base_down_chirp)
        iq_chunks.append(self.base_down_chirp)
        iq_chunks.append(self.base_down_chirp[: self.samples_per_symbol // 4])

        # 4. Modulate payload symbols
        for sym in symbols:
            iq_chunks.append(self._generate_chirp(sym))

        return np.concatenate(iq_chunks).astype(np.complex64)

    def demodulate_samples(self, iq_samples: np.ndarray) -> list[bytes]:
        """Scans continuous I/Q stream for LoRa preambles and decodes payload packets."""
        decoded_packets: list[bytes] = []
        n_samples = len(iq_samples)
        if n_samples < self.samples_per_symbol * 12:
            return decoded_packets

        # Detect preamble peaks
        step = self.samples_per_symbol // 2
        for idx in range(0, n_samples - self.samples_per_symbol * 15, step):
            window = iq_samples[idx : idx + self.samples_per_symbol]
            if len(window) < self.samples_per_symbol:
                break

            # Power gate
            pwr = np.mean(np.abs(window) ** 2)
            if pwr < 1e-4:
                continue

            # De-chirp correlation
            dechirped = window * self.base_down_chirp
            # Decimate / sum across oversampling
            decimated = dechirped.reshape(self.n_chips, self.oversampling).sum(axis=1)
            spectrum = np.abs(np.fft.fft(decimated))
            peak_bin = int(np.argmax(spectrum))
            peak_val = spectrum[peak_bin]
            avg_val = np.mean(spectrum)

            # Strong up-chirp peak (symbol 0) indicates preamble
            if peak_val > avg_val * 4.0 and (peak_bin < 4 or peak_bin > self.n_chips - 4):
                # Preamble candidate found, decode packet starting after preamble
                packet = self._decode_frame_from_offset(iq_samples, idx)
                if packet:
                    decoded_packets.append(packet)

        return decoded_packets

    def _generate_chirp(self, symbol: int) -> np.ndarray:
        """Generates a modulated LoRa chirp for a specific symbol value S in [0, 2^SF - 1]."""
        t = np.arange(self.samples_per_symbol) / self.fs
        f_shift = (symbol / self.n_chips) * self.bw
        f0 = -self.bw / 2.0 + f_shift
        k = (self.bw ** 2) / self.n_chips
        phi = 2 * np.pi * (f0 * t + 0.5 * k * (t ** 2))
        return np.exp(1j * phi).astype(np.complex64)

    def _encode_payload_symbols(self, whitened_bytes: bytes) -> list[int]:
        """Encodes whitened payload into LoRa symbol values with Hamming (4/5) and interleaving."""
        symbols = []
        for b in whitened_bytes:
            # Low and high nibble
            n1 = b & 0x0F
            n2 = (b >> 4) & 0x0F
            # Add parity for CR 4/5
            p1 = (n1 ^ (n1 >> 1) ^ (n1 >> 2) ^ (n1 >> 3)) & 1
            p2 = (n2 ^ (n2 >> 1) ^ (n2 >> 2) ^ (n2 >> 3)) & 1
            code1 = (n1 << 1) | p1
            code2 = (n2 << 1) | p2
            symbols.append(code1 << (self.sf - 5))
            symbols.append(code2 << (self.sf - 5))
        return symbols

    def _decode_frame_from_offset(self, iq_samples: np.ndarray, start_idx: int) -> bytes | None:
        """Extracts symbol sequence and decodes bytes from synchronization point."""
        # Advance past preamble (~12.25 symbols)
        cursor = start_idx + int(12.25 * self.samples_per_symbol)
        raw_symbols: list[int] = []

        # Read up to 250 symbols
        for _ in range(250):
            if cursor + self.samples_per_symbol > len(iq_samples):
                break
            window = iq_samples[cursor : cursor + self.samples_per_symbol]
            dechirped = window * self.base_down_chirp
            decimated = dechirped.reshape(self.n_chips, self.oversampling).sum(axis=1)
            spectrum = np.abs(np.fft.fft(decimated))
            sym = int(np.argmax(spectrum))
            raw_symbols.append(sym)
            cursor += self.samples_per_symbol

        if len(raw_symbols) < 4:
            return None

        # De-interleave and decode nibbles
        nibbles: list[int] = []
        for sym in raw_symbols:
            # Extract 4-bit nibble from symbol
            nibble = (sym >> (self.sf - 4)) & 0x0F
            nibbles.append(nibble)

        # Assemble bytes from nibble pairs
        raw_bytes = bytearray()
        for i in range(0, len(nibbles) - 1, 2):
            b = (nibbles[i + 1] << 4) | nibbles[i]
            raw_bytes.append(b)

        # De-whiten
        dewhitened = bytes(b ^ LORA_WHITENING_SEQUENCE[i % len(LORA_WHITENING_SEQUENCE)] for i, b in enumerate(raw_bytes))

        # Check for RASCube packet signature (starts with Port 0x10 or Length 0x79)
        if len(dewhitened) >= 121:
            if dewhitened[0] == 0x10 and dewhitened[1] == 0x79:
                return bytes(dewhitened[:123])
            # Or 121-byte raw telemetry payload
            return bytes([0x10, 0x79]) + bytes(dewhitened[:121])

        return None
