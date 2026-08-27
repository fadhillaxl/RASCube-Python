from __future__ import annotations

import dataclasses
import math
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np

from rascube_v2.constants import BANDWIDTH_KHZ
from rascube_v2.decoder import decode_main_telemetry_hex, decode_telemetry_to_dict
from rascube_v2.models.telemetry import MainTelemetrySample


@dataclasses.dataclass
class SDRLoRaConfig:
    serial_number: int = 1581
    custom_frequency_hz: int | None = None
    sample_rate: int = 1_000_000
    rx_gain_db: float = 40.0
    spreading_factor: int = 7
    bandwidth_hz: int = 500_000
    coding_rate: str = "4/5"
    sdr_uri: str = "usb:"

    @property
    def frequency_hz(self) -> int:
        """Calculate exact LoRa downlink frequency for satellite serial number or custom frequency."""
        if self.custom_frequency_hz is not None:
            return self.custom_frequency_hz
        channel = self.serial_number % 18
        return 916_000_000 + channel * 600_000


class PlutoSDRReceiver:
    """Receiver adapter for ADALM-PLUTO and Pluto+ SDR devices.

    Supports:
    1. Direct Pluto+ connection via `pyadi-iio` / `adi.Pluto`.
    2. GNU Radio / external SDR demodulator UDP bridge stream.
    """

    def __init__(
        self,
        config: SDRLoRaConfig,
        on_sample: Callable[[MainTelemetrySample], None] | None = None,
        on_raw_hex: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.on_sample = on_sample
        self.on_raw_hex = on_raw_hex
        self._running = False
        self._thread: threading.Thread | None = None
        self._sdr_device: Any = None
        self.total_packets_received = 0
        self.last_rssi_dbm: float | None = None

    @classmethod
    def calculate_frequency_hz(cls, serial_number: int) -> int:
        channel = serial_number % 18
        return 916_000_000 + channel * 600_000

    def start_direct_sdr(self) -> None:
        """Initialize Pluto+ SDR device directly via pyadi-iio with auto-discovery fallback."""
        try:
            import adi  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "pyadi-iio library is required for direct Pluto+ connection. "
                "Install with: pip install pyadi-iio"
            ) from exc

        uris_to_try = ["usb:", "ip:192.168.2.1", "ip:pluto.local", self.config.sdr_uri]
        seen = set()
        candidates = [u for u in uris_to_try if not (u in seen or seen.add(u))]

        connected_device = None
        last_error = None

        for uri in candidates:
            try:
                print(f"[Pluto+ SDR] Trying to connect via '{uri}'...", flush=True)
                dev = adi.Pluto(uri)
                dev.sample_rate = int(self.config.sample_rate)
                dev.rx_lo = int(self.config.frequency_hz)
                dev.rx_rf_bandwidth = int(self.config.bandwidth_hz * 2)
                dev.gain_control_mode_chan0 = "manual"
                dev.rx_hardwaregain_chan0 = float(self.config.rx_gain_db)
                dev.rx_buffer_size = 16384

                # Verify actual buffer read succeeds
                test_buf = dev.rx()
                if test_buf is not None and len(test_buf) > 0:
                    connected_device = dev
                    self.config.sdr_uri = uri
                    break
            except Exception as exc:
                last_error = exc

        if connected_device is None:
            raise RuntimeError(
                f"Could not connect to Pluto+ SDR on any URI ({', '.join(uris_to_try)}).\n"
                f"Details: {last_error}\n"
                f"Checklist:\n"
                f"1. Is the USB cable connected to the 'MIDDLE' USB port on Pluto (marked Data/USB)?\n"
                f"2. If using IP 192.168.2.1, ensure your Mac Ethernet adapter is set to Static IP 192.168.2.10 (Subnet: 255.255.255.0)."
            )

        self._sdr_device = connected_device
        self._sdr_device.sample_rate = int(self.config.sample_rate)
        self._sdr_device.rx_lo = int(self.config.frequency_hz)
        self._sdr_device.rx_rf_bandwidth = int(self.config.bandwidth_hz * 2)
        self._sdr_device.gain_control_mode_chan0 = "manual"
        self._sdr_device.rx_hardwaregain_chan0 = float(self.config.rx_gain_db)
        self._sdr_device.rx_buffer_size = 16384

        # Setup TX if available
        try:
            self._sdr_device.tx_lo = int(self.config.frequency_hz)
            self._sdr_device.tx_rf_bandwidth = int(self.config.bandwidth_hz * 2)
            self._sdr_device.tx_hardwaregain_chan0 = 0.0
        except Exception:
            pass

        print(
            f"[Pluto+ SDR] Connected successfully ({self.config.sdr_uri})!\n"
            f"[Pluto+ SDR] Tuned to {self.config.frequency_hz / 1e6:.3f} MHz "
            f"(Channel {self.config.serial_number % 18}), Gain: {self.config.rx_gain_db} dB"
        )

        from rascube_v2.sdr.lora_dsp import SoftwareLoRaDSP
        self._dsp = SoftwareLoRaDSP(
            spreading_factor=self.config.spreading_factor,
            bandwidth_hz=self.config.bandwidth_hz,
            sample_rate=self.config.sample_rate,
        )

        # Start live SDR RX worker
        self._running = True
        self._thread = threading.Thread(target=self._sdr_rx_worker, daemon=True)
        self._thread.start()

    def _sdr_rx_worker(self) -> None:
        """Continuous background thread streaming Pluto+ SDR raw I/Q samples to GNU Radio via UDP."""
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
        except Exception:
            pass
        CHUNK = 1472  # bytes per UDP datagram = 184 complex64 samples
        while self._running and self._sdr_device is not None:
            try:
                iq_data = self._sdr_device.rx()
                if iq_data is None or len(iq_data) == 0:
                    time.sleep(0.001)
                    continue

                raw_c64 = (iq_data.astype(np.complex64) / 2048.0).tobytes()
                for i in range(0, len(raw_c64), CHUNK):
                    udp_sock.sendto(raw_c64[i : i + CHUNK], ("127.0.0.1", 9090))

            except Exception:
                if self._running:
                    time.sleep(0.01)
                else:
                    break


    def transmit_packet(self, payload: bytes) -> None:
        """Modulates and transmits a LoRa packet via Pluto+ SDR TX antenna."""
        if self._sdr_device is None or self._dsp is None:
            return
        try:
            iq_tx = self._dsp.modulate_bytes(payload)
            if hasattr(self._sdr_device, "tx_destroy_buffer"):
                self._sdr_device.tx_destroy_buffer()
            self._sdr_device.tx_cyclic_buffer = False
            self._sdr_device.tx(iq_tx)
        except Exception as exc:
            print(f"[Pluto+ SDR TX] Transmit error: {exc}", flush=True)

    def start_udp_listener(self, host: str = "127.0.0.1", port: int = 9090) -> None:
        """Listen for demodulated telemetry frames from GNU Radio / gr-lorasdr via UDP."""
        self._running = True
        self._udp_thread = threading.Thread(
            target=self._udp_listen_worker, args=(host, port), daemon=True
        )
        self._udp_thread.start()
        print(f"[SDR UDP Bridge] Listening for demodulated packets on {host}:{port}...")

    def _udp_listen_worker(self, host: str, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.settimeout(1.0)

        while self._running:
            try:
                data, addr = sock.recvfrom(2048)
                if not data:
                    continue

                # Format to full RASCube 123-byte packet (0x10 0x79 + 113 payload + 8 RSSI/SNR)
                import struct
                if len(data) == 121:
                    raw_packet = bytes([0x10, 0x79]) + data
                elif len(data) == 113:
                    rssi_bytes = struct.pack("<f", float(self.last_rssi_dbm or -60.0))
                    snr_bytes = struct.pack("<f", float(self.last_snr_db or 12.0))
                    raw_packet = bytes([0x10, 0x79]) + data + rssi_bytes + snr_bytes
                else:
                    raw_packet = data

                hex_str = raw_packet.hex().upper()
                self.total_packets_received += 1

                if self.on_raw_hex:
                    self.on_raw_hex(hex_str)

                if self.on_sample:
                    try:
                        sample = decode_main_telemetry_hex(raw_packet)
                        self.on_sample(sample)
                    except Exception as err:
                        pass
            except socket.timeout:
                continue
            except Exception as exc:
                if self._running:
                    print(f"[SDR UDP Bridge] Error: {exc}")
                break

        sock.close()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._sdr_device = None
        print("[Pluto+ SDR] Receiver stopped.")


def generate_lora_tx_waveform(
    payload: bytes,
    sf: int = 7,
    bw: int = 500_000,
    samp_rate: int = 1_000_000,
    sync_words: list[int] = [0x12],
) -> np.ndarray:
    """Generates standard-compliant LoRa IQ samples using GNU Radio DSP blocks with software fallback."""
    try:
        import sys
        for ver in ["3.14", "3.13", "3.12"]:
            p = f"/opt/homebrew/lib/python{ver}/site-packages"
            if os.path.exists(p) and p not in sys.path:
                sys.path.insert(0, p)

        import pmt
        from gnuradio import blocks, gr
        from gnuradio.lora_sdr import lora_sdr_python as lora_sdr

        tb = gr.top_block("LoRaTxGen")
        msg = pmt.intern(payload.hex())
        strobe = blocks.message_strobe(msg, 20)
        whitening = lora_sdr.whitening(True, False, ",", "packet_len")
        header = lora_sdr.header(False, True, 1)
        add_crc = lora_sdr.add_crc(True)
        hamming_enc = lora_sdr.hamming_enc(1, sf)
        interleaver = lora_sdr.interleaver(1, sf, 0, bw)
        gray_demap = lora_sdr.gray_demap(sf)
        zero_pad = int(20 * (2**sf) * samp_rate / bw)
        modulate = lora_sdr.modulate(sf, samp_rate, bw, sync_words, zero_pad, 8)
        sink = blocks.vector_sink_c()

        tb.msg_connect((strobe, "strobe"), (whitening, "msg"))
        tb.connect((whitening, 0), (header, 0))
        tb.connect((header, 0), (add_crc, 0))
        tb.connect((add_crc, 0), (hamming_enc, 0))
        tb.connect((hamming_enc, 0), (interleaver, 0))
        tb.connect((interleaver, 0), (gray_demap, 0))
        tb.connect((gray_demap, 0), (modulate, 0))
        tb.connect((modulate, 0), (sink, 0))

        tb.start()
        time.sleep(0.25)
        tb.stop()
        tb.wait()

        iq_data = np.array(sink.data(), dtype=np.complex64)
        max_val = np.max(np.abs(iq_data))
        if max_val > 0:
            return ((iq_data / max_val) * 0.9 * 32767.0).astype(np.complex64)
        return iq_data
    except Exception:
        from rascube_v2.sdr.lora_dsp import SoftwareLoRaDSP

        dsp = SoftwareLoRaDSP(spreading_factor=sf, bandwidth_hz=bw, sample_rate=samp_rate)
        iq_raw = dsp.modulate_bytes(payload)
        return (iq_raw * 30000.0).astype(np.complex64)


class PlutoSDRTransmitter:
    """Uplink ground station transmitter for ADALM-PLUTO / Pluto+ SDR devices."""

    def __init__(self, config: SDRLoRaConfig, tx_gain_db: float = 0.0) -> None:
        self.config = config
        self.tx_gain_db = tx_gain_db
        self._sdr_device: Any = None

    def connect(self) -> None:
        import adi

        uris_to_try = ["usb:", "ip:192.168.2.1", "ip:pluto.local", self.config.sdr_uri]
        seen = set()
        candidates = [u for u in uris_to_try if not (u in seen or seen.add(u))]

        last_error = None
        for uri in candidates:
            try:
                dev = adi.Pluto(uri)
                dev.sample_rate = int(self.config.sample_rate)
                dev.tx_lo = int(self.config.frequency_hz)
                dev.tx_rf_bandwidth = int(self.config.bandwidth_hz * 2)
                dev.tx_hardwaregain_chan0 = float(self.tx_gain_db)
                self._sdr_device = dev
                self.config.sdr_uri = uri
                print(
                    f"[Pluto+ SDR TX] Connected via '{uri}' @ {self.config.frequency_hz / 1e6:.3f} MHz",
                    flush=True,
                )
                return
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"Could not connect PlutoSDR TX on any URI: {last_error}")

    def transmit_bytes(self, payload: bytes) -> None:
        """Modulates payload bytes into LoRa CSS and transmits via PlutoSDR TX antenna."""
        if self._sdr_device is None:
            self.connect()
        iq_tx = generate_lora_tx_waveform(
            payload,
            sf=self.config.spreading_factor,
            bw=self.config.bandwidth_hz,
            samp_rate=self.config.sample_rate,
        )
        if hasattr(self._sdr_device, "tx_destroy_buffer"):
            self._sdr_device.tx_destroy_buffer()
        self._sdr_device.tx_cyclic_buffer = False
        self._sdr_device.tx(iq_tx)

