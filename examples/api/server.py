#!/usr/bin/env python3
"""RASCubeV2 Ground Station REST & Realtime Streaming API Server.

Features:
- Swagger UI Documentation at `/docs` and `/swagger`
- OpenAPI Specification at `/openapi.json`
- Ground Station Dashboard UI at `/`
- Port Management: `GET /api/ports`
- Connection Controls: `POST /api/connect`, `POST /api/disconnect`, `GET /api/status`
- Telemetry: `GET /api/telemetry/latest`, `GET /api/telemetry/history`, `GET /api/telemetry/stream`, `POST /api/decode`
"""

from __future__ import annotations

import argparse
import base64
import collections
import dataclasses
import json
import queue
import struct
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np
from serial.tools import list_ports

from rascube_v2 import SyncRASCube, decode_telemetry_to_dict
from rascube_v2.constants import USB_PID_V2, USB_VID
from rascube_v2.exceptions import (
    CameraAssemblyError,
    ProtocolDecodeError,
    RequestTimeoutError,
    SessionBusyError,
)
from rascube_v2.models.camera import CameraBlock
from rascube_v2.protocol.camera import CameraAssembler


# --- Global State ---
class GroundStationState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cube: SyncRASCube | None = None
        self.connected_port: str | None = None
        self.serial_number: int | None = None
        self.receiver_info: dict[str, Any] | None = None
        self.obc_info: dict[str, Any] | None = None
        self.is_connected = False
        self.error_message: str | None = None

        # Telemetry storage
        self.latest_sample: dict[str, Any] | None = None
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=200)
        self.total_samples_received: int = 0
        self.last_received_time: float | None = None

        # Camera storage & state
        self.camera_status: str = "idle"  # "idle", "capturing", "completed", "failed"
        self.camera_progress: dict[str, Any] = {
            "blocks_received": 0,
            "total_bytes": 0,
            "started_at": None,
            "elapsed_seconds": 0.0,
            "error": None,
        }
        self.latest_image: bytes | None = None
        self.latest_image_metadata: dict[str, Any] | None = None
        self.partial_image: bytes | None = None
        self.camera_blocks: dict[int, bytes] = {}
        self.camera_chunks: list[dict[str, Any]] = []
        self.camera_assembler = CameraAssembler()
        self.camera_lock = threading.Lock()
        self.camera_thread: threading.Thread | None = None


        # SSE Subscribers
        self.subscribers: list[queue.Queue[dict[str, Any]]] = []
        self.worker_thread: threading.Thread | None = None
        self.stop_signal = threading.Event()

        # SDR Direct Receiver & Transmitter State
        self.sdr_active: bool = False
        self.sdr_sat: int = 1581
        self.sdr_gain: float = 40.0
        self.sdr_sf: int = 7
        self.sdr_bw: int = 500_000
        self.sdr_uri: str = "usb:"
        self.sdr_packets_count: int = 0
        self.sdr_last_rssi: float | None = None
        self.sdr_last_snr: float | None = None
        self.sdr_error: str | None = None
        self.sdr_thread: threading.Thread | None = None
        self.sdr_stop_event: threading.Event = threading.Event()
        self.sdr_transmitter: Any | None = None
        self.sdr_cyclic_active: bool = False


    def add_subscriber(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
        with self.lock:
            self.subscribers.append(q)
        return q

    def remove_subscriber(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast_telemetry(self, data: dict[str, Any]) -> None:
        with self.lock:
            self.latest_sample = data
            self.history.append(data)
            self.total_samples_received += 1
            self.last_received_time = time.time()
            subs = list(self.subscribers)

        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def broadcast_camera_chunk(self, chunk: dict[str, Any]) -> None:
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                pass



state = GroundStationState()


def background_telemetry_loop(port: str, serial_number: int) -> None:
    """Thread worker that manages the serial connection and reads telemetry packets."""
    global state
    try:
        with SyncRASCube(port, serial_number=serial_number) as cube:
            receiver_info = cube.receiver.get_info()
            obc_info = None
            try:
                obc_info = cube.obc.get_info(timeout=1.0)
            except Exception:
                pass

            with state.lock:
                state.cube = cube
                state.is_connected = True
                state.connected_port = port
                state.serial_number = serial_number
                state.receiver_info = dataclasses.asdict(receiver_info) if receiver_info else None
                state.obc_info = dataclasses.asdict(obc_info) if obc_info else None
                state.error_message = None

            print(f"[API Backend] Connected to {port}, Satellite #{serial_number}")

            for sample in cube.telemetry.iter_samples(timeout=2.0):
                if state.stop_signal.is_set():
                    break

                raw_bytes = (
                    bytes([sample.metadata.port, len(sample.metadata.raw_payload)])
                    + sample.metadata.raw_payload
                )
                raw_hex = raw_bytes.hex().upper()

                telemetry_dict = decode_telemetry_to_dict(raw_bytes)
                telemetry_dict["raw_hex"] = raw_hex
                telemetry_dict["timestamp"] = time.time()

                state.broadcast_telemetry(telemetry_dict)

    except Exception as exc:
        print(f"[API Backend] Connection error: {exc}")
        with state.lock:
            state.error_message = str(exc)
    finally:
        with state.lock:
            state.is_connected = False
            state.cube = None
            state.stop_signal.clear()
        print(f"[API Backend] Disconnected from {port}")


def trigger_camera_capture(timeout: float = 30.0, source: str = "server") -> None:
    """Spawns an asynchronous worker to handle camera capture."""
    global state
    with state.camera_lock:
        if state.camera_status == "capturing":
            raise SessionBusyError("A camera capture session is already in progress")
        if source != "client_web_serial" and not state.is_connected and not state.sdr_active and state.cube is None:
            raise ConnectionError(
                "Receiver is not connected to a satellite. Please connect via Client Web USB (Tab 1), "
                "Server COM Port (Tab 2), or PlutoSDR (Tab 3) first."
            )

        state.camera_status = "capturing"
        state.camera_assembler.reset()
        state.camera_chunks.clear()
        state.camera_blocks.clear()
        state.partial_image = None
        state.camera_progress = {
            "blocks_received": 0,
            "total_bytes": 0,
            "started_at": time.time(),
            "elapsed_seconds": 0.0,
            "transfer_speed_bps": 0.0,
            "latest_block_index": 0,
            "error": None,
        }

    def _worker() -> None:
        start_time = time.time()
        try:
            if source == "client_web_serial":
                print("[API WebUSB] Camera capture session started. Waiting for chunks from Browser Web USB...")
                while time.time() - start_time < timeout:
                    with state.camera_lock:
                        if state.camera_status == "completed":
                            print(f"[API WebUSB] Camera capture completed: {len(state.latest_image or b'')} bytes")
                            return
                        if state.camera_status == "failed":
                            return
                    time.sleep(0.2)

                with state.camera_lock:
                    if state.camera_status == "capturing":
                        state.camera_status = "failed"
                        state.camera_progress["error"] = f"Camera capture timed out after {timeout:.1f}s"
                return

            if state.cube is not None:
                def on_block(block: Any) -> None:
                    now = time.time()
                    elapsed = round(now - start_time, 2)
                    with state.lock:
                        state.camera_blocks[block.index] = block.data
                        state.camera_progress["blocks_received"] += 1
                        state.camera_progress["total_bytes"] += len(block.data)
                        state.camera_progress["elapsed_seconds"] = elapsed
                        state.camera_progress["latest_block_index"] = block.index
                        speed = round(state.camera_progress["total_bytes"] / max(0.01, elapsed), 1)
                        state.camera_progress["transfer_speed_bps"] = speed

                        # Assemble contiguous progressive blocks sorted strictly: 0, 1, 2, ...
                        contiguous = bytearray()
                        idx = 0
                        while idx in state.camera_blocks:
                            contiguous.extend(state.camera_blocks[idx])
                            idx += 1

                        partial_b64 = None
                        if len(contiguous) >= 2 and contiguous[:2] == b"\xff\xd8":
                            if contiguous.find(b"\xff\xd9") < 0:
                                partial_jpeg = bytes(contiguous) + b"\xff\xd9"
                            else:
                                partial_jpeg = bytes(contiguous)
                            state.partial_image = partial_jpeg
                            partial_b64 = base64.b64encode(partial_jpeg).decode("ascii")

                        chunk_record = {
                            "type": "camera_chunk",
                            "index": block.index,
                            "size": len(block.data),
                            "total_blocks": state.camera_progress["blocks_received"],
                            "contiguous_blocks": idx,
                            "total_bytes": state.camera_progress["total_bytes"],
                            "elapsed_seconds": elapsed,
                            "hex_preview": block.data[:16].hex().upper(),
                            "partial_jpeg_base64": partial_b64,
                            "timestamp": now,
                        }
                        state.camera_chunks.append(chunk_record)

                    state.broadcast_camera_chunk(chunk_record)

                image = state.cube.camera.capture(timeout=timeout, on_block=on_block)
                with state.lock:
                    state.latest_image = image.jpeg
                    state.latest_image_metadata = {
                        "block_count": image.block_count,
                        "duplicate_blocks": len(image.duplicate_blocks),
                        "byte_length": len(image.jpeg),
                        "captured_at": time.time(),
                        "capture_duration_seconds": round(time.time() - start_time, 2),
                    }
                    state.camera_status = "completed"
                print(f"[API Backend] Camera capture completed: {len(image.jpeg)} bytes in {time.time() - start_time:.2f}s")

            elif state.sdr_active:
                # Transmit camera capture trigger over PlutoSDR RF (HostPort.OBC_CAMERA = 0x13)
                print(f"[API SDR] Sending camera capture trigger to Satellite #{state.sdr_sat}...")
                transmit_sdr_command(
                    sat=state.sdr_sat,
                    cmd_type="raw_hex",
                    params={"hex": "130100"},
                    bw=state.sdr_bw,
                    sdr_uri=state.sdr_uri,
                )
                # Wait for camera blocks to be received and assembled in SDR background receiver loop
                while time.time() - start_time < timeout:
                    with state.camera_lock:
                        if state.camera_status == "completed":
                            print(f"[API SDR] Camera capture completed: {len(state.latest_image or b'')} bytes")
                            return
                        if state.camera_status == "failed":
                            return
                    time.sleep(0.2)

                with state.camera_lock:
                    if state.camera_status == "capturing":
                        state.camera_status = "failed"
                        state.camera_progress["error"] = f"Camera capture timed out after {timeout:.1f}s"
            else:
                raise ConnectionError("No active satellite connection to trigger camera")

        except Exception as exc:
            print(f"[API Backend] Camera capture error: {exc}")
            with state.lock:
                state.camera_status = "failed"
                state.camera_progress["error"] = str(exc)

    t = threading.Thread(target=_worker, daemon=True, name="camera-worker")
    state.camera_thread = t
    t.start()



def background_sdr_receiver_loop(
    sat: int = 1581,
    gain: float = 40.0,
    sf: int = 7,
    bw: int = 500_000,
    sdr_uri: str = "usb:",
) -> None:
    """Thread worker that tunes PlutoSDR and runs direct real-time DSP LoRa demodulation."""
    global state
    freq_hz = 916_000_000 + (sat % 18) * 600_000
    fs = 1_000_000
    n_chips = 1 << sf
    n_samples_per_sym = int(fs * n_chips / bw)
    os_factor = max(1, n_samples_per_sym // n_chips)

    # Precompute reference base down-chirp
    t = np.arange(n_samples_per_sym) / fs
    k = (bw**2) / n_chips
    phi = 2 * np.pi * (-bw / 2.0 * t + 0.5 * k * (t**2))
    down_chirp = np.exp(-1j * phi).astype(np.complex64)

    try:
        import adi

        print(f"[API SDR] Connecting PlutoSDR via '{sdr_uri}' @ {freq_hz/1e6:.3f} MHz...")
        dev = adi.Pluto(sdr_uri)
        dev.sample_rate = fs
        dev.rx_lo = freq_hz
        dev.rx_rf_bandwidth = 1_000_000
        dev.gain_control_mode_chan0 = "manual"
        dev.rx_hardwaregain_chan0 = float(gain)
        dev.rx_buffer_size = 65536

        with state.lock:
            state.sdr_active = True
            state.sdr_sat = sat
            state.sdr_gain = gain
            state.sdr_sf = sf
            state.sdr_bw = bw
            state.sdr_uri = sdr_uri
            state.sdr_error = None
            state.is_connected = True
            state.connected_port = f"PlutoSDR ({sdr_uri})"
            state.serial_number = sat

        print(f"[API SDR] PlutoSDR direct DSP receiver running on {freq_hz/1e6:.3f} MHz (Sat #{sat})")
        dev.rx()  # Warmup

        buf_accum = np.array([], dtype=np.complex64)
        from rascube_v2.sdr.lora_dsp import LORA_WHITENING_NIBBLES

        while not state.sdr_stop_event.is_set():
            raw_buf = dev.rx()
            if raw_buf is None or len(raw_buf) == 0:
                time.sleep(0.005)
                continue

            c64_buf = raw_buf.astype(np.complex64) / 2048.0
            buf_accum = np.concatenate((buf_accum, c64_buf))

            if len(buf_accum) >= 200_000:
                iq_proc = buf_accum[:200_000]
                buf_accum = buf_accum[180_000:]

                step = n_samples_per_sym // 4
                n_steps = (len(iq_proc) - n_samples_per_sym) // step

                all_syms = []
                for s in range(n_steps):
                    idx = s * step
                    win = iq_proc[idx : idx + n_samples_per_sym] * down_chirp
                    dec = win.reshape(n_chips, os_factor).sum(axis=1)
                    fft_mag = np.abs(np.fft.fft(dec))
                    sym = int(np.argmax(fft_mag))
                    snr = fft_mag[sym] / (np.mean(fft_mag) + 1e-10)
                    all_syms.append((idx, sym, snr))

                idx = 0
                while idx < len(all_syms) - 200:
                    if state.sdr_stop_event.is_set():
                        break
                    cands = [all_syms[idx + k * 4] for k in range(8)]
                    syms = [c[1] for c in cands]
                    snrs = [c[2] for c in cands]

                    if all(s > 10.0 for s in snrs) and (max(syms) - min(syms) <= 2):
                        start_sample = cands[0][0]
                        cfo = syms[0]

                        payload_start = start_sample + int(12.25 * n_samples_per_sym)
                        frame_symbols = []
                        for s_idx in range(250):
                            pos = payload_start + s_idx * n_samples_per_sym
                            if pos + n_samples_per_sym > len(iq_proc):
                                break
                            win = iq_proc[pos : pos + n_samples_per_sym] * down_chirp
                            dec = win.reshape(n_chips, os_factor).sum(axis=1)
                            raw_sym = int(np.argmax(np.abs(np.fft.fft(dec))))
                            frame_symbols.append((raw_sym - cfo) % n_chips)

                        # Demap, deinterleave & dewhiten
                        mapped = [(s ^ (s >> 1)) for s in frame_symbols]
                        cw_len = 5
                        n_blocks = len(mapped) // cw_len
                        nibbles = []
                        for blk in range(n_blocks):
                            block_syms = mapped[blk * cw_len : (blk + 1) * cw_len]
                            for bit in range(sf):
                                codeword = 0
                                for i in range(cw_len):
                                    shift = (bit + i) % sf
                                    b = (block_syms[i] >> shift) & 1
                                    codeword |= b << i
                                d0 = codeword & 1
                                d1 = (codeword >> 1) & 1
                                d2 = (codeword >> 2) & 1
                                d3 = (codeword >> 3) & 1
                                nibbles.append((d3 << 3) | (d2 << 2) | (d1 << 1) | d0)

                        unwhitened = [
                            n ^ LORA_WHITENING_NIBBLES[i % len(LORA_WHITENING_NIBBLES)]
                            for i, n in enumerate(nibbles)
                        ]
                        data_bytes = bytearray()
                        for i in range(0, len(unwhitened) - 1, 2):
                            data_bytes.append((unwhitened[i] << 4) | unwhitened[i + 1])
                        decoded = bytes(data_bytes)

                        if decoded and len(decoded) >= 2:
                            port = decoded[0]

                            # 1. Camera Block Packet (InboundPort.JPEG_CAMERA = 0x15 or 0x20)
                            if port in (0x15, 0x20):
                                raw_payload = decoded[2:] if len(decoded) > 2 else decoded
                                if len(raw_payload) >= 2:
                                    blk_idx = struct.unpack_from("<H", raw_payload, 0)[0]
                                    blk_data = raw_payload[2:]
                                    block = CameraBlock(index=blk_idx, data=blk_data, metadata=None)
                                    with state.camera_lock:
                                        if state.camera_status == "capturing":
                                            try:
                                                jpeg_res = state.camera_assembler.add(block)
                                                now = time.time()
                                                started_at = state.camera_progress.get("started_at") or now
                                                elapsed = round(now - started_at, 2)
                                                state.camera_progress["blocks_received"] += 1
                                                state.camera_progress["total_bytes"] += len(blk_data)
                                                state.camera_progress["elapsed_seconds"] = elapsed
                                                state.camera_progress["latest_block_index"] = blk_idx
                                                speed = round(state.camera_progress["total_bytes"] / max(0.01, elapsed), 1)
                                                state.camera_progress["transfer_speed_bps"] = speed

                                                state.camera_blocks[blk_idx] = blk_data
                                                # Assemble contiguous progressive blocks sorted strictly: 0, 1, 2, ...
                                                contiguous = bytearray()
                                                idx = 0
                                                while idx in state.camera_blocks:
                                                    contiguous.extend(state.camera_blocks[idx])
                                                    idx += 1

                                                partial_b64 = None
                                                if len(contiguous) >= 2 and contiguous[:2] == b"\xff\xd8":
                                                    if contiguous.find(b"\xff\xd9") < 0:
                                                        partial_jpeg = bytes(contiguous) + b"\xff\xd9"
                                                    else:
                                                        partial_jpeg = bytes(contiguous)
                                                    state.partial_image = partial_jpeg
                                                    partial_b64 = base64.b64encode(partial_jpeg).decode("ascii")

                                                chunk_record = {
                                                    "type": "camera_chunk",
                                                    "index": blk_idx,
                                                    "size": len(blk_data),
                                                    "total_blocks": state.camera_progress["blocks_received"],
                                                    "contiguous_blocks": idx,
                                                    "total_bytes": state.camera_progress["total_bytes"],
                                                    "elapsed_seconds": elapsed,
                                                    "hex_preview": blk_data[:16].hex().upper(),
                                                    "partial_jpeg_base64": partial_b64,
                                                    "timestamp": now,
                                                }
                                                with state.lock:
                                                    state.camera_chunks.append(chunk_record)

                                                state.broadcast_camera_chunk(chunk_record)

                                                if jpeg_res is not None:
                                                    state.latest_image = jpeg_res
                                                    state.latest_image_metadata = {
                                                        "block_count": state.camera_assembler.block_count,
                                                        "duplicate_blocks": len(state.camera_assembler.duplicates),
                                                        "byte_length": len(jpeg_res),
                                                        "captured_at": time.time(),
                                                        "capture_duration_seconds": state.camera_progress["elapsed_seconds"],
                                                    }
                                                    state.camera_status = "completed"
                                                    print(f"[API SDR] Camera JPEG complete: {len(jpeg_res)} bytes ({state.camera_assembler.block_count} blocks)")
                                            except Exception as err:
                                                print(f"[API SDR] Camera assembly error: {err}")

                            # 2. Main Telemetry Packet (InboundPort.MAIN_TELEMETRY = 0x10 or raw payload)
                            elif len(decoded) >= 20:
                                p_sig = float(
                                    np.mean(np.abs(iq_proc[payload_start : payload_start + 1024]) ** 2) + 1e-12
                                )
                                meas_rssi = float(-100.0 + 10.0 * np.log10(p_sig * 1000.0))
                                meas_snr = float(np.mean(snrs))

                                payload_113 = decoded[:113].ljust(113, b"\x00")
                                rssi_bytes = struct.pack("<f", meas_rssi)
                                snr_bytes = struct.pack("<f", meas_snr)
                                rascube_pkt = bytes([0x10, 0x79]) + payload_113 + rssi_bytes + snr_bytes

                                with state.lock:
                                    state.sdr_packets_count += 1
                                    state.sdr_last_rssi = meas_rssi
                                    state.sdr_last_snr = meas_snr

                                try:
                                    t_dict = decode_telemetry_to_dict(rascube_pkt)
                                    t_dict["raw_hex"] = rascube_pkt.hex().upper()
                                    t_dict["timestamp"] = time.time()
                                    t_dict["source"] = "PlutoSDR"
                                    state.broadcast_telemetry(t_dict)
                                except Exception:
                                    pass

                        idx += 200
                    else:
                        idx += 1


    except Exception as exc:
        print(f"[API SDR] PlutoSDR error: {exc}")
        with state.lock:
            state.sdr_error = str(exc)
    finally:
        with state.lock:
            state.sdr_active = False
            state.sdr_stop_event.clear()
            if state.connected_port and "PlutoSDR" in state.connected_port:
                state.is_connected = False
                state.connected_port = None
        print("[API SDR] PlutoSDR direct receiver stopped.")


def transmit_sdr_command(
    sat: int = 1581,
    cmd_type: str = "ping",
    params: dict[str, Any] | None = None,
    bw: int = 500_000,
    sdr_uri: str = "usb:",
) -> dict[str, Any]:
    """Transmits radio command over PlutoSDR to the satellite."""
    from rascube_v2.constants import HostPort
    from rascube_v2.sdr.pluto import PlutoSDRTransmitter, SDRLoRaConfig

    if params is None:
        params = {}

    freq_hz = 916_000_000 + (sat % 18) * 600_000
    config = SDRLoRaConfig(
        serial_number=sat,
        custom_frequency_hz=freq_hz,
        spreading_factor=7,
        bandwidth_hz=bw,
        sdr_uri=sdr_uri,
    )
    tx = PlutoSDRTransmitter(config=config, tx_gain_db=0.0)

    if cmd_type == "wake":
        payload = bytes([HostPort.OBC_INFO, 0x01, 0x00])
        tx.transmit_cyclic_beacon(payload, gap_seconds=0.15)
        with state.lock:
            state.sdr_cyclic_active = True
            state.sdr_transmitter = tx
        return {"status": "started", "message": "Hardware FPGA cyclic wake beacon active"}

    elif cmd_type == "stop_wake":
        with state.lock:
            if state.sdr_transmitter:
                state.sdr_transmitter.stop_cyclic_beacon()
                state.sdr_transmitter = None
            state.sdr_cyclic_active = False
        return {"status": "stopped", "message": "Hardware FPGA cyclic wake beacon stopped"}

    elif cmd_type == "ping":
        payload = bytes([HostPort.OBC_INFO, 0x01, 0x00])
        tx.transmit_bytes(payload, repeat=3)
        return {"status": "transmitted", "command": "ping", "hex": payload.hex().upper()}

    elif cmd_type == "rgb":
        r = int(params.get("r", 255))
        g = int(params.get("g", 0))
        b = int(params.get("b", 0))
        payload = bytes([HostPort.ARDUINO_RGB, 0x03, r, g, b])
        tx.transmit_bytes(payload, repeat=3)
        return {"status": "transmitted", "command": "rgb", "r": r, "g": g, "b": b}

    elif cmd_type == "song":
        payload = bytes([HostPort.ARDUINO_STARTUP_SONG, 0x01, 0x00])
        tx.transmit_bytes(payload, repeat=3)
        return {"status": "transmitted", "command": "song"}

    elif cmd_type == "raw_hex":
        hex_str = params.get("hex", "120100").replace(" ", "").replace("0x", "")
        payload = bytes.fromhex(hex_str)
        tx.transmit_bytes(payload, repeat=3)
        return {"status": "transmitted", "command": "raw_hex", "hex": payload.hex().upper()}

    raise ValueError(f"Unknown command type: {cmd_type}")



# OpenAPI 3.0 Specification
OPENAPI_SCHEMA: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {
        "title": "RASCubeV2 Ground Station & Telemetry API",
        "description": "REST API and Realtime Streaming API for RASCubeV2 Satellite USB Receiver, Telemetry & Camera Capture",
        "version": "1.1.0",
        "contact": {
            "name": "RASCube Ground Station Team",
        },
    },
    "servers": [
        {"url": "/", "description": "Local Ground Station Server"}
    ],
    "tags": [
        {"name": "Connection", "description": "Serial Port & Satellite Connection Management"},
        {"name": "PlutoSDR", "description": "ADALM-PLUTO Radio Ground Station, DSP Demodulator & Uplink Transmitter"},
        {"name": "Telemetry", "description": "Real-time Telemetry, History & HEX Decoding"},
        {"name": "Camera", "description": "Satellite Camera Capture & Image Preview"},
    ],
    "paths": {
        "/api/ports": {
            "get": {
                "tags": ["Connection"],
                "summary": "List Available Serial Ports",
                "description": "Scans and lists all serial/COM ports available on the host machine, identifying connected RASCubeV2 receivers.",
                "responses": {
                    "200": {
                        "description": "List of serial ports",
                        "content": {
                            "application/json": {
                                "example": {
                                    "ports": [
                                        {
                                            "device": "/dev/cu.usbmodem20623154594D1",
                                            "description": "RASCubeV2 Receiver",
                                            "vid": 1155,
                                            "pid": 22336,
                                            "serial_number": "20623154594D",
                                            "is_rascube": True,
                                        }
                                    ]
                                }
                            }
                        },
                    }
                },
            }
        },
        "/api/connect": {
            "post": {
                "tags": ["Connection"],
                "summary": "Connect to Port & Select Satellite",
                "description": "Opens serial port connection to RASCube receiver, binds to specified satellite serial number, and begins telemetry ingestion.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["port", "serial_number"],
                                "properties": {
                                    "port": {
                                        "type": "string",
                                        "description": "Serial port device path",
                                        "example": "/dev/cu.usbmodem20623154594D1",
                                    },
                                    "serial_number": {
                                        "type": "integer",
                                        "description": "Numeric satellite serial number",
                                        "example": 1581,
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Connection status",
                        "content": {
                            "application/json": {
                                "example": {
                                    "status": "connected",
                                    "port": "/dev/cu.usbmodem20623154594D1",
                                    "serial_number": 1581,
                                    "receiver_info": {
                                        "software_version": 7,
                                        "git_hash": None,
                                        "dirty": False,
                                    },
                                    "error": None,
                                }
                            }
                        },
                    },
                    "400": {"description": "Invalid parameters"},
                },
            }
        },
        "/api/disconnect": {
            "post": {
                "tags": ["Connection"],
                "summary": "Disconnect from Receiver",
                "description": "Terminates the serial connection and halts background telemetry streaming.",
                "responses": {
                    "200": {
                        "description": "Disconnection status",
                        "content": {
                            "application/json": {
                                "example": {"status": "disconnected"}
                            }
                        },
                    }
                },
            }
        },
        "/api/status": {
            "get": {
                "tags": ["Connection"],
                "summary": "Get Connection & Hardware Status",
                "description": "Returns current connection state, satellite serial number, and firmware info.",
                "responses": {
                    "200": {
                        "description": "Ground station status",
                        "content": {
                            "application/json": {
                                "example": {
                                    "is_connected": True,
                                    "connected_port": "/dev/cu.usbmodem20623154594D1",
                                    "serial_number": 1581,
                                    "total_samples_received": 250,
                                    "last_received_time": 1787569800.0,
                                    "error_message": None,
                                }
                            }
                        },
                    }
                },
            }
        },
        "/api/telemetry/latest": {
            "get": {
                "tags": ["Telemetry"],
                "summary": "Get Latest Telemetry Sample",
                "description": "Fetches the most recently decoded 121-byte telemetry sample with raw HEX.",
                "responses": {
                    "200": {
                        "description": "Latest telemetry data",
                        "content": {
                            "application/json": {
                                "example": {
                                    "packet_sequence": 12652,
                                    "device_uptime_ms": 1726733,
                                    "barometer": {
                                        "temperature_c": 31.0,
                                        "pressure_hpa": 1006.84,
                                        "altitude_m": 1.28,
                                    },
                                    "eps": {
                                        "main_5v_v": 5.006,
                                        "main_3v3_v": 3.308,
                                        "battery_charge": {"bus_voltage_v": 4.056, "current_a": 0.0},
                                    },
                                    "imu": {
                                        "accelerometer_g": {"x": 0.0, "y": 0.016, "z": -1.014},
                                        "gyroscope_dps": {"x": -0.0175, "y": 0.0, "z": 0.0875},
                                    },
                                    "gps": {
                                        "latitude": -6.263743,
                                        "longitude": 106.808456,
                                        "altitude_m": 37.4,
                                        "satellites": 8,
                                        "fix": True,
                                    },
                                    "receiver_rssi": -31.0,
                                    "receiver_snr": 13.25,
                                    "raw_hex": "10796C3100008E13EC0C...",
                                }
                            }
                        },
                    },
                    "503": {"description": "No telemetry received yet"},
                },
            }
        },
        "/api/telemetry/history": {
            "get": {
                "tags": ["Telemetry"],
                "summary": "Get Telemetry History Buffer",
                "description": "Fetches a circular buffer of recent telemetry packets for charting and graphs.",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "description": "Maximum number of historical samples to retrieve (default: 50, max: 200)",
                        "schema": {"type": "integer", "default": 50},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "List of historical telemetry packets",
                        "content": {
                            "application/json": {
                                "example": {
                                    "count": 50,
                                    "samples": [],
                                }
                            }
                        },
                    }
                },
            }
        },
        "/api/telemetry/stream": {
            "get": {
                "tags": ["Telemetry"],
                "summary": "Realtime SSE Telemetry Stream",
                "description": "Server-Sent Events (SSE) stream pushing decoded telemetry JSON messages in real-time.",
                "responses": {
                    "200": {
                        "description": "Realtime event stream",
                        "content": {"text/event-stream": {}},
                    }
                },
            }
        },
        "/api/telemetry/ingest": {
            "post": {
                "tags": ["Telemetry"],
                "summary": "Ingest Telemetry from Client Web Serial",
                "description": "Allows a browser client reading local USB via Web Serial API to push raw telemetry packets to the server, decoding and broadcasting to all SSE dashboard clients.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["hex"],
                                "properties": {
                                    "hex": {
                                        "type": "string",
                                        "description": "Raw 121-byte payload or 123-byte 10 79... HEX frame",
                                        "example": "10796C3100008E13EC0CFB0FFA0FFB0FD80F000090070000D00FFA0020010000F0000000A001000000000D591A00ABFF2E0B0700360139A6C4474049A43F000014010DBFFFFF000005009670C8C0EE9DD5429A99154200000000000000009A99993F0801D36237C2636FAD41C4981B440000090000F8C100005441",
                                    },
                                    "source": {
                                        "type": "string",
                                        "description": "Optional source label",
                                        "example": "browser_web_serial",
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Telemetry ingested and broadcasted successfully",
                        "content": {"application/json": {}},
                    },
                    "422": {"description": "Invalid packet payload"},
                },
            }
        },
        "/api/decode": {
            "post": {
                "tags": ["Telemetry"],
                "summary": "Decode Raw Telemetry HEX",
                "description": "Decodes any raw hex packet string (121-byte payload or 123-byte 10 79... packet) according to telemetry.md into structured JSON.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["hex"],
                                "properties": {
                                    "hex": {
                                        "type": "string",
                                        "description": "Hexadecimal telemetry packet string",
                                        "example": "10796C3100008E13EC0CFB0FFA0FFB0FD80F000090070000D00FFA0020010000F0000000A001000000000D591A00ABFF2E0B0700360139A6C4474049A43F000014010DBFFFFF000005009670C8C0EE9DD5429A99154200000000000000009A99993F0801D36237C2636FAD41C4981B440000090000F8C100005441",
                                    }
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Decoded telemetry payload",
                        "content": {"application/json": {}},
                    },
                    "422": {"description": "Decode or payload length error"},
                },
            },
            "get": {
                "tags": ["Telemetry"],
                "summary": "Decode Raw Telemetry HEX via Query Param",
                "parameters": [
                    {
                        "name": "hex",
                        "in": "query",
                        "required": True,
                        "description": "Hex string to decode",
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {"description": "Decoded JSON"},
                    "422": {"description": "Invalid hex format"},
                },
            },
        },
        "/api/camera/capture": {
            "post": {
                "tags": ["Camera"],
                "summary": "Trigger Camera Capture",
                "description": "Sends command to satellite to capture a JPEG image with progressive block transfer.",
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "timeout": {
                                        "type": "number",
                                        "description": "Capture timeout in seconds",
                                        "example": 35.0,
                                        "default": 30.0,
                                    }
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "202": {
                        "description": "Camera capture initiated",
                        "content": {
                            "application/json": {
                                "example": {
                                    "status": "capturing",
                                    "message": "Camera capture initiated with 35.0s timeout",
                                    "check_status_url": "/api/camera/status",
                                }
                            }
                        },
                    },
                    "409": {"description": "Capture already in progress"},
                    "503": {"description": "Satellite not connected"},
                },
            }
        },
        "/api/camera/status": {
            "get": {
                "tags": ["Camera"],
                "summary": "Get Camera Capture Status & Progress",
                "description": "Returns current camera session status ('idle', 'capturing', 'completed', 'failed') and received block metrics.",
                "responses": {
                    "200": {
                        "description": "Camera capture progress status",
                        "content": {
                            "application/json": {
                                "example": {
                                    "status": "completed",
                                    "progress": {
                                        "blocks_received": 32,
                                        "total_bytes": 8192,
                                        "started_at": 1787579100.0,
                                        "elapsed_seconds": 8.4,
                                        "error": None,
                                    },
                                    "has_image": True,
                                    "metadata": {
                                        "block_count": 32,
                                        "duplicate_blocks": 0,
                                        "byte_length": 8192,
                                        "captured_at": 1787579108.4,
                                        "capture_duration_seconds": 8.4,
                                    },
                                    "image_url": "/api/camera/latest.jpg",
                                }
                            }
                        },
                    }
                },
            }
        },
        "/api/camera/latest": {
            "get": {
                "tags": ["Camera"],
                "summary": "Get Latest Captured Image (JSON & Base64)",
                "description": "Returns latest captured camera image metadata and base64 encoded JPEG payload.",
                "responses": {
                    "200": {
                        "description": "Latest camera image metadata and base64",
                        "content": {"application/json": {}},
                    },
                    "404": {"description": "No camera image captured yet"},
                },
            }
        },
        "/api/camera/latest.jpg": {
            "get": {
                "tags": ["Camera"],
                "summary": "Get Latest Captured Image (Raw JPEG Binary)",
                "description": "Serves the latest captured satellite image as raw image/jpeg binary stream.",
                "responses": {
                    "200": {
                        "description": "Raw JPEG image binary",
                        "content": {"image/jpeg": {}},
                    },
                    "404": {"description": "No camera image captured yet"},
                },
            }
        },
        "/api/sdr/status": {
            "get": {
                "tags": ["PlutoSDR"],
                "summary": "Get PlutoSDR Ground Station Status",
                "description": "Returns active status, tuned frequency, measured RSSI/SNR, and packet count of the PlutoSDR radio.",
                "responses": {
                    "200": {
                        "description": "PlutoSDR hardware status",
                        "content": {"application/json": {}},
                    }
                },
            }
        },
        "/api/sdr/receiver/start": {
            "post": {
                "tags": ["PlutoSDR"],
                "summary": "Start PlutoSDR Real-Time DSP LoRa Receiver",
                "description": "Starts the hardware SDR receiver thread on specified satellite serial channel with real-time FFT demodulation.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "sat": {"type": "integer", "example": 1581},
                                    "gain": {"type": "number", "example": 40.0},
                                    "sf": {"type": "integer", "example": 7},
                                    "bw": {"type": "integer", "example": 500000},
                                    "uri": {"type": "string", "example": "usb:"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "PlutoSDR receiver started successfully"},
                },
            }
        },
        "/api/sdr/receiver/stop": {
            "post": {
                "tags": ["PlutoSDR"],
                "summary": "Stop PlutoSDR Receiver",
                "description": "Stops the real-time SDR receiver worker thread.",
                "responses": {
                    "200": {"description": "PlutoSDR receiver stopped"},
                },
            }
        },
        "/api/sdr/transmit": {
            "post": {
                "tags": ["PlutoSDR"],
                "summary": "Transmit LoRa Uplink Command via PlutoSDR",
                "description": "Sends radio commands (wake beacon, RGB LED blink, startup song melody, ping) using PlutoSDR RF transmitter.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["sat", "command"],
                                "properties": {
                                    "sat": {"type": "integer", "example": 1581},
                                    "command": {"type": "string", "enum": ["ping", "blink", "rgb", "song", "wake", "stop_wake", "raw_hex"], "example": "blink"},
                                    "bw": {"type": "integer", "example": 500000},
                                    "params": {"type": "object"},
                                    "uri": {"type": "string", "example": "usb:"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Radio command transmitted"},
                },
            }
        },
    },
}

SWAGGER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>RASCube Ground Station - Swagger UI</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    * { font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
    body { margin: 0; padding: 0; background: #f8fafc; color: #0f172a; }
    .custom-navbar {
      background: #090d16;
      color: #fff;
      padding: 0.9rem 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid rgba(255,255,255,0.1);
    }
    .custom-navbar .brand {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      font-size: 1.1rem;
      font-weight: 800;
      color: #fff;
      text-decoration: none;
    }
    .custom-navbar .badge {
      background: #0284c7;
      color: #fff;
      padding: 0.2rem 0.6rem;
      border-radius: 9999px;
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .custom-navbar .nav-links {
      display: flex;
      gap: 1.25rem;
      align-items: center;
    }
    .custom-navbar .nav-links a {
      color: #94a3b8;
      text-decoration: none;
      font-size: 0.88rem;
      font-weight: 600;
      transition: color 0.2s;
    }
    .custom-navbar .nav-links a:hover {
      color: #38bdf8;
    }
    .swagger-ui .topbar { display: none !important; }
    .swagger-ui .wrapper { max-width: 1200px; padding: 0 1.5rem; }
    .swagger-ui .info { margin: 2rem 0 1.5rem; }
    .swagger-ui .info .title { font-size: 2rem; font-weight: 800; color: #0f172a; }
    .swagger-ui .info .title small { background: #0284c7; border-radius: 6px; }
    .swagger-ui code, .swagger-ui pre { font-family: 'JetBrains Mono', monospace !important; }
    .swagger-ui .opblock { border-radius: 10px !important; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }
    .swagger-ui .btn { border-radius: 8px !important; }
    .swagger-ui select { border-radius: 6px !important; }
  </style>
</head>
<body>
  <div class="custom-navbar">
    <a href="/" class="brand">
      <span>🛰️ RASCube Ground Station</span>
      <span class="badge">OpenAPI 3.0</span>
    </a>
    <div class="nav-links">
      <a href="/">🖥️ Live Dashboard</a>
      <a href="/openapi.json" target="_blank">📄 openapi.json</a>
    </div>
  </div>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
  <script>
    window.onload = () => {
      window.ui = SwaggerUIBundle({
        url: '/openapi.json',
        dom_id: '#swagger-ui',
        deepLinking: true,
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIStandalonePreset
        ],
        layout: "BaseLayout"
      });
    };
  </script>
</body>
</html>
"""

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RASCube Ground Station API & Dashboard</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d16;
      --card-bg: rgba(18, 24, 38, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.25);
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
    body { background-color: var(--bg); color: var(--text); min-height: 100vh; padding: 2rem 1.5rem; }
    .container { max-width: 1100px; margin: 0 auto; }
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2rem; border-bottom: 1px solid var(--card-border); padding-bottom: 1.5rem; }
    .header-links { display: flex; align-items: center; gap: 1rem; }
    .swagger-btn { background: #10b981; color: #022c22; font-weight: 700; text-decoration: none; padding: 0.45rem 1rem; border-radius: 8px; font-size: 0.85rem; display: inline-flex; align-items: center; gap: 0.4rem; }
    .swagger-btn:hover { filter: brightness(1.15); }
    .status-badge { display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.35rem 0.85rem; border-radius: 9999px; font-size: 0.85rem; font-weight: 700; }
    .status-connected { background: rgba(16, 185, 129, 0.15); border: 1px solid var(--success); color: var(--success); }
    .status-disconnected { background: rgba(239, 68, 68, 0.15); border: 1px solid var(--danger); color: var(--danger); }
    .status-client { background: rgba(56, 189, 248, 0.15); border: 1px solid var(--accent); color: var(--accent); }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
    .tabs { display: flex; gap: 0.5rem; margin-bottom: 1.25rem; border-bottom: 1px solid var(--card-border); padding-bottom: 0.5rem; }
    .tab-btn { background: transparent; color: var(--text-muted); border: none; padding: 0.6rem 1.2rem; font-size: 0.9rem; font-weight: 700; border-radius: 8px; cursor: pointer; transition: all 0.2s; }
    .tab-btn.active { background: rgba(56, 189, 248, 0.15); color: var(--accent); border: 1px solid var(--accent); }
    .card { background: var(--card-bg); backdrop-filter: blur(12px); border: 1px solid var(--card-border); border-radius: 16px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 10px 30px rgba(0,0,0,0.3); }
    .card h2 { font-size: 1.15rem; font-weight: 700; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.5rem; }
    .form-grid { display: grid; grid-template-columns: 2fr 1fr auto auto; gap: 1rem; align-items: end; }
    label { display: block; font-size: 0.8rem; font-weight: 600; color: var(--text-muted); margin-bottom: 0.4rem; }
    select, input { width: 100%; background: rgba(10, 15, 26, 0.8); border: 1px solid var(--card-border); border-radius: 8px; color: #fff; font-size: 0.9rem; padding: 0.65rem 0.85rem; outline: none; }
    select:focus, input:focus { border-color: var(--accent); }
    button { background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%); color: #fff; border: none; padding: 0.65rem 1.25rem; border-radius: 8px; font-weight: 600; font-size: 0.9rem; cursor: pointer; transition: all 0.2s; white-space: nowrap; }
    button:hover { filter: brightness(1.15); }
    .btn-danger { background: linear-gradient(135deg, #dc2626 0%, #991b1b 100%); }
    .btn-secondary { background: rgba(255,255,255,0.08); border: 1px solid var(--card-border); }
    .grid-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-top: 1rem; }
    .metric-box { background: rgba(15, 23, 42, 0.6); border: 1px solid var(--card-border); border-radius: 12px; padding: 1rem; }
    .metric-label { font-size: 0.72rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600; }
    .metric-value { font-size: 1.35rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; color: #fff; margin-top: 0.25rem; }
    .metric-sub { font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem; font-family: 'JetBrains Mono', monospace; }
    pre { background: rgba(5, 8, 15, 0.95); border: 1px solid var(--card-border); border-radius: 10px; padding: 1rem; font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; color: #a5f3fc; overflow-x: auto; max-height: 280px; }
    .note-box { background: rgba(56, 189, 248, 0.08); border-left: 3px solid var(--accent); padding: 0.75rem 1rem; border-radius: 6px; font-size: 0.85rem; color: #cbd5e1; margin-bottom: 1rem; }
    .chunk-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: rgba(56, 189, 248, 0.15);
      border: 1px solid rgba(56, 189, 248, 0.5);
      color: #38bdf8;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.72rem;
      font-weight: 700;
      padding: 3px 6px;
      border-radius: 5px;
      animation: chunkPop 0.35s cubic-bezier(0.175, 0.885, 0.32, 1.275);
    }
    .chunk-badge.duplicate {
      background: rgba(245, 158, 11, 0.2);
      border-color: #f59e0b;
      color: #f59e0b;
    }
    @keyframes chunkPop {
      0% { transform: scale(0.4); opacity: 0; }
      70% { transform: scale(1.15); opacity: 1; box-shadow: 0 0 12px rgba(56, 189, 248, 0.8); }
      100% { transform: scale(1); }
    }
  </style>

</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1 style="font-size: 1.6rem; font-weight: 800;">🛰️ RASCube Ground Station</h1>
        <div style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.2rem;">Live Satellite Telemetry Server & Realtime API</div>
      </div>
      <div class="header-links">
        <a href="/docs" target="_blank" class="swagger-btn">📖 Swagger UI Docs</a>
        <div id="statusBadge" class="status-badge status-disconnected">
          <span class="status-dot"></span> <span id="statusText">Disconnected</span>
        </div>
      </div>
    </header>

    <div class="card">
      <div class="tabs">
        <button id="tabClientBtn" class="tab-btn active" onclick="switchTab('client')">💻 Client Web USB/Serial</button>
        <button id="tabServerBtn" class="tab-btn" onclick="switchTab('server')">🖥️ Server COM Port (Dongle)</button>
        <button id="tabPlutoBtn" class="tab-btn" onclick="switchTab('pluto')">🛰️ PlutoSDR Radio (Direct DSP)</button>
      </div>

      <!-- Tab 1: Client Web Serial / Web USB -->
      <div id="tabClient">
        <div class="note-box">
          ✨ <strong>Client Web USB / Web Serial Mode</strong>: Receiver USB Dongle terhubung langsung ke browser laptop/komputer Anda (Chrome / Edge / Opera). Data dibaca langsung oleh browser dan otomatis di-ingest ke backend API.
        </div>
        <div class="form-grid" style="grid-template-columns: 1fr 1fr auto;">
          <div>
            <label>Satellite Serial Number</label>
            <input type="number" id="clientSerialInput" value="1581" placeholder="e.g. 1581" />
          </div>
          <div>
            <label>Baud Rate</label>
            <input type="number" id="clientBaudInput" value="1000000" />
          </div>
          <button id="btnClientConnect" onclick="handleClientWebSerial()">🔌 Connect Browser USB</button>
        </div>
      </div>

      <!-- Tab 2: Server COM Port -->
      <div id="tabServer" style="display: none;">
        <div class="note-box">
          🖥️ <strong>Server Port Mode</strong>: Receiver USB Dongle terhubung ke komputer yang menjalankan server Python backend.
        </div>
        <div class="form-grid">
          <div>
            <label>Select COM Port</label>
            <select id="portSelect"></select>
          </div>
          <div>
            <label>Satellite Serial Number</label>
            <input type="number" id="serialInput" value="1581" placeholder="e.g. 1581" />
          </div>
          <button id="btnConnect" onclick="handleConnect()">⚡ Connect Dongle</button>
          <button class="btn-secondary" onclick="loadPorts()">🔄 Refresh Ports</button>
        </div>
      </div>

      <!-- Tab 3: PlutoSDR Radio Direct Hardware DSP -->
      <div id="tabPluto" style="display: none;">
        <div class="note-box">
          🛰️ <strong>PlutoSDR Hardware Real-Time DSP Mode</strong>: Menjalankan demodulator LoRa CSS (SF7, BW 500k/125k, CR 4/5) langsung di hardware ADALM-PLUTO secara real-time.
        </div>
        <div class="form-grid" style="grid-template-columns: 1fr 1fr 1fr auto;">
          <div>
            <label>Satellite Serial</label>
            <input type="number" id="sdrSatInput" value="1581" oninput="updateSdrFreq()" />
            <small id="sdrFreqLabel" style="color: var(--accent); font-size: 0.75rem; font-family: monospace;">Freq: 925.000 MHz (Ch 15)</small>
          </div>
          <div>
            <label>Bandwidth</label>
            <select id="sdrBwInput">
              <option value="500000" selected>500 kHz (High Speed)</option>
              <option value="125000">125 kHz (Standard V2)</option>
              <option value="250000">250 kHz</option>
            </select>
          </div>
          <div>
            <label>RX Hardware Gain (dB)</label>
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <input type="range" id="sdrGainSlider" min="0" max="70" value="40" oninput="document.getElementById('sdrGainVal').innerText = this.value + ' dB'" />
              <span id="sdrGainVal" style="font-family: monospace; font-size: 0.85rem; width: 50px;">40 dB</span>
            </div>
          </div>
          <div style="display: flex; gap: 0.5rem;">
            <button id="btnSdrStart" onclick="handleSdrToggle()">⚡ Start PlutoSDR RX</button>
          </div>
        </div>

        <!-- PlutoSDR Uplink Transmitter Controls -->
        <div style="margin-top: 1.25rem; padding-top: 1rem; border-top: 1px solid var(--card-border);">
          <label style="margin-bottom: 0.6rem; color: #fff;">📡 PlutoSDR Radio Uplink Commands:</label>
          <div style="display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center;">
            <button class="btn-secondary" onclick="handleSdrTransmit('blink')">💡 Blink RGB LED</button>
            <button class="btn-secondary" onclick="handleSdrTransmit('song')">🎵 Play Startup Song</button>
            <button class="btn-secondary" onclick="handleSdrTransmit('ping')">📡 Send Ping (0x120100)</button>
            <button id="btnSdrWake" class="btn-secondary" onclick="handleSdrWakeToggle()">⚡ Hardware Wake Beacon (DMA Loop)</button>
          </div>
        </div>
      </div>
    </div>


    <div class="card">
      <h2>📊 Live Telemetry Stream</h2>
      <div class="grid-metrics">
        <div class="metric-box">
          <div class="metric-label">Packet / Uptime</div>
          <div class="metric-value" id="valSeq">-</div>
          <div class="metric-sub" id="valUptime">-</div>
        </div>
        <div class="metric-box">
          <div class="metric-label">Temperature / Pressure</div>
          <div class="metric-value" id="valTemp">-</div>
          <div class="metric-sub" id="valPres">-</div>
        </div>
        <div class="metric-box">
          <div class="metric-label">Battery Voltage</div>
          <div class="metric-value" id="valBatt">-</div>
          <div class="metric-sub" id="valRails">-</div>
        </div>
        <div class="metric-box">
          <div class="metric-label">GPS Position</div>
          <div class="metric-value" id="valGpsCoords">-</div>
          <div class="metric-sub" id="valGpsStatus">-</div>
        </div>
        <div class="metric-box">
          <div class="metric-label">Accelerometer (X,Y,Z)</div>
          <div class="metric-value" id="valAccel">-</div>
          <div class="metric-sub">Unit: g</div>
        </div>
        <div class="metric-box">
          <div class="metric-label">Radio Signal (RSSI / SNR)</div>
          <div class="metric-value" id="valSignal">-</div>
          <div class="metric-sub" id="valSnr">-</div>
        </div>
      </div>

      <h2 style="margin-top: 1.5rem;">📜 Latest Telemetry JSON & Raw HEX</h2>
      <pre id="jsonDisplay">// Waiting for telemetry data...</pre>
    </div>

    <!-- Satellite Camera Section -->
    <div class="card">
      <h2>📷 Satellite Camera Capture</h2>
      <div style="display: flex; gap: 1rem; align-items: center; margin-bottom: 1rem; flex-wrap: wrap;">
        <button id="btnCameraCapture" onclick="triggerCameraCapture()">📸 Trigger Camera Capture</button>
        <span id="cameraStatusText" style="font-size: 0.9rem; color: var(--text-muted);">Status: Idle</span>
      </div>

      <!-- Realtime Chunk Progress & Transfer Metrics -->
      <div id="cameraProgressContainer" style="display: none; margin-bottom: 1.25rem;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.4rem;">
          <div style="font-size: 0.88rem; color: var(--accent); font-weight: 700;" id="cameraProgressDetails">Receiving blocks...</div>
          <div style="font-size: 0.8rem; color: var(--text-muted); font-family: monospace;" id="cameraSpeedMetric">Speed: 0 B/s | Rate: 0 blk/s</div>
        </div>
        
        <div style="background: rgba(255,255,255,0.08); border-radius: 6px; height: 10px; overflow: hidden; margin-bottom: 1rem;">
          <div id="cameraProgressBar" style="width: 0%; height: 100%; background: linear-gradient(90deg, #0284c7, #38bdf8); transition: width 0.3s ease;"></div>
        </div>

        <!-- Live Interactive Chunk Matrix / Grid -->
        <div style="margin-bottom: 1rem; background: rgba(5, 8, 15, 0.7); border: 1px solid var(--card-border); border-radius: 10px; padding: 0.85rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
            <div style="font-size: 0.75rem; text-transform: uppercase; font-weight: 700; color: var(--text-muted); letter-spacing: 0.05em;">📦 Live Received Chunk Matrix:</div>
            <div style="font-size: 0.75rem; color: #38bdf8; font-family: 'JetBrains Mono', monospace; font-weight: 700;" id="chunkCountLabel">0 chunks</div>
          </div>
          <div id="chunkMatrix" style="display: flex; flex-wrap: wrap; gap: 6px; max-height: 140px; overflow-y: auto; padding: 6px; background: rgba(0,0,0,0.3); border-radius: 6px;"></div>
        </div>

        <!-- Live Chunk Stream Log -->
        <div style="background: rgba(5, 8, 15, 0.9); border: 1px solid var(--card-border); border-radius: 8px; padding: 0.6rem 0.85rem; max-height: 90px; overflow-y: auto; font-family: 'JetBrains Mono', monospace; font-size: 0.72rem; color: #a5f3fc;" id="chunkStreamLog">
          <div style="color: var(--text-muted);">// Real-time chunk packets will stream here...</div>
        </div>
      </div>

      <div id="cameraImageContainer" style="text-align: center; background: rgba(5, 8, 15, 0.6); border-radius: 12px; padding: 1.25rem; min-height: 200px; display: flex; flex-direction: column; align-items: center; justify-content: center; border: 1px dashed var(--card-border);">
        <img id="cameraImgPreview" src="" alt="Satellite Capture Preview" style="max-width: 100%; max-height: 450px; border-radius: 8px; display: none; box-shadow: 0 4px 25px rgba(0,0,0,0.6);" />
        <div id="cameraPlaceholder" style="color: var(--text-muted); font-size: 0.88rem;">No camera image captured yet. Click "Trigger Camera Capture" to take a picture.</div>
        <div id="cameraMetaInfo" style="margin-top: 0.75rem; font-size: 0.82rem; color: #a5f3fc; font-family: 'JetBrains Mono', monospace; display: none;"></div>
      </div>
    </div>
  </div>


  <script>
    let isConnected = false;
    let sseSource = null;
    let clientPort = null;
    let clientReader = null;
    let isClientConnected = false;
    let cameraPollingInterval = null;
    let isSdrActive = false;
    let isSdrWakeActive = false;

    function switchTab(mode) {
      document.getElementById('tabClient').style.display = (mode === 'client') ? 'block' : 'none';
      document.getElementById('tabServer').style.display = (mode === 'server') ? 'block' : 'none';
      document.getElementById('tabPluto').style.display = (mode === 'pluto') ? 'block' : 'none';

      document.getElementById('tabClientBtn').className = (mode === 'client') ? 'tab-btn active' : 'tab-btn';
      document.getElementById('tabServerBtn').className = (mode === 'server') ? 'tab-btn active' : 'tab-btn';
      document.getElementById('tabPlutoBtn').className = (mode === 'pluto') ? 'tab-btn active' : 'tab-btn';
    }

    function updateSdrFreq() {
      const sat = parseInt(document.getElementById('sdrSatInput').value, 10) || 1581;
      const ch = sat % 18;
      const freq = (916000000 + ch * 600000) / 1e6;
      document.getElementById('sdrFreqLabel').innerText = `Freq: ${freq.toFixed(3)} MHz (Ch ${ch})`;
    }

    async function handleSdrToggle() {
      const btn = document.getElementById('btnSdrStart');
      if (isSdrActive) {
        btn.disabled = true;
        btn.innerText = '⏳ Stopping...';
        await fetch('/api/sdr/receiver/stop', { method: 'POST' });
        isSdrActive = false;
        btn.innerText = '⚡ Start PlutoSDR RX';
        btn.className = '';
        btn.disabled = false;
        checkStatus();
      } else {
        const sat = parseInt(document.getElementById('sdrSatInput').value, 10) || 1581;
        const gain = parseFloat(document.getElementById('sdrGainSlider').value) || 40.0;
        const bw = parseInt(document.getElementById('sdrBwInput').value, 10) || 500000;

        btn.disabled = true;
        btn.innerText = '⏳ Starting SDR...';

        const res = await fetch('/api/sdr/receiver/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sat, gain, bw, sf: 7, uri: 'usb:' })
        });
        const data = await res.json();
        btn.disabled = false;

        if (!res.ok) {
          alert(data.error || 'Failed to start PlutoSDR');
          btn.innerText = '⚡ Start PlutoSDR RX';
          btn.className = '';
        } else {
          isSdrActive = true;
          btn.innerText = '⏹️ Stop PlutoSDR RX';
          btn.className = 'btn-danger';
          if (!sseSource) initSSE();
          checkStatus();
        }
      }
    }

    async function handleSdrTransmit(cmd) {
      const sat = parseInt(document.getElementById('sdrSatInput').value, 10) || 1581;
      const bw = parseInt(document.getElementById('sdrBwInput').value, 10) || 500000;

      try {
        const res = await fetch('/api/sdr/transmit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sat, command: cmd, bw, uri: 'usb:' })
        });
        const data = await res.json();
        if (!res.ok) alert(data.error || 'PlutoSDR TX failed');
        else console.log('PlutoSDR TX Response:', data);
      } catch (e) {
        alert('PlutoSDR TX Error: ' + e.message);
      }
    }

    async function handleSdrWakeToggle() {
      const btn = document.getElementById('btnSdrWake');
      const sat = parseInt(document.getElementById('sdrSatInput').value, 10) || 1581;
      const bw = parseInt(document.getElementById('sdrBwInput').value, 10) || 500000;

      if (isSdrWakeActive) {
        await fetch('/api/sdr/transmit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sat, command: 'stop_wake', bw, uri: 'usb:' })
        });
        isSdrWakeActive = false;
        btn.innerText = '⚡ Hardware Wake Beacon (DMA Loop)';
        btn.className = 'btn-secondary';
      } else {
        await fetch('/api/sdr/transmit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sat, command: 'wake', bw, uri: 'usb:' })
        });
        isSdrWakeActive = true;
        btn.innerText = '⏹️ Stop Hardware Wake Beacon';
        btn.className = 'btn-danger';
      }
    }


    const receivedChunks = new Set();

    function renderCameraChunk(chunk) {
      document.getElementById('cameraProgressContainer').style.display = 'block';
      const statusText = document.getElementById('cameraStatusText');
      statusText.innerText = `Status: Receiving Chunk #${chunk.index}...`;

      const details = document.getElementById('cameraProgressDetails');
      details.innerText = `Block #${chunk.index} received (${(chunk.total_bytes / 1024).toFixed(1)} KB)`;

      const speedMetric = document.getElementById('cameraSpeedMetric');
      const rate = chunk.elapsed_seconds > 0 ? (chunk.total_blocks / chunk.elapsed_seconds).toFixed(1) : '0';
      const speed = chunk.elapsed_seconds > 0 ? (chunk.total_bytes / chunk.elapsed_seconds).toFixed(0) : '0';
      speedMetric.innerText = `Speed: ${speed} B/s | Rate: ${rate} blk/s | Elapsed: ${chunk.elapsed_seconds}s`;

      // Interactive Matrix Grid (Strictly Sorted in Numerical Order)
      const matrix = document.getElementById('chunkMatrix');
      const countLabel = document.getElementById('chunkCountLabel');
      
      const badgeId = `chunk_blk_${chunk.index}`;
      let badge = document.getElementById(badgeId);
      if (!badge) {
        badge = document.createElement('span');
        badge.id = badgeId;
        badge.setAttribute('data-index', chunk.index);
        badge.className = 'chunk-badge';
        badge.innerText = '#' + String(chunk.index).padStart(2, '0');
        badge.title = `Block #${chunk.index} (${chunk.size} bytes)\nOffset: 0x${(chunk.index * 240).toString(16).toUpperCase()}\nHex: ${chunk.hex_preview}...`;
        
        // Insert in ascending numerical order
        const children = Array.from(matrix.children);
        let inserted = false;
        for (let child of children) {
          const childIdx = parseInt(child.getAttribute('data-index') || '-1', 10);
          if (chunk.index < childIdx) {
            matrix.insertBefore(badge, child);
            inserted = true;
            break;
          }
        }
        if (!inserted) matrix.appendChild(badge);
        receivedChunks.add(chunk.index);
      } else {
        badge.className = 'chunk-badge duplicate';
      }
      countLabel.innerText = `${receivedChunks.size} blocks received`;

      // Progressive Live Image Rendering per block
      if (chunk.partial_jpeg_base64) {
        const img = document.getElementById('cameraImgPreview');
        const placeholder = document.getElementById('cameraPlaceholder');
        const meta = document.getElementById('cameraMetaInfo');

        img.src = 'data:image/jpeg;base64,' + chunk.partial_jpeg_base64;
        img.style.display = 'block';
        placeholder.style.display = 'none';
        meta.style.display = 'block';
        meta.innerText = `[Progressive Reconstruction] Contiguous Blocks: 0..${(chunk.contiguous_blocks || chunk.total_blocks) - 1} | Buffer: ${(chunk.total_bytes / 1024).toFixed(1)} KB`;
      }

      // Progress bar estimation
      const estTotalBlocks = Math.max(35, chunk.index + 5);
      const pct = Math.min(95, Math.round((chunk.total_blocks / estTotalBlocks) * 100));
      document.getElementById('cameraProgressBar').style.width = pct + '%';

      // Log Stream
      const log = document.getElementById('chunkStreamLog');
      const logLine = document.createElement('div');
      logLine.innerText = `[${chunk.elapsed_seconds.toFixed(2)}s] 📥 Block #${String(chunk.index).padStart(4, '0')} | ${chunk.size}B | Offset 0x${(chunk.index * 240).toString(16).toUpperCase()} | ${chunk.hex_preview}...`;
      log.appendChild(logLine);
      log.scrollTop = log.scrollHeight;
    }

    let clientCaptureStartTime = null;

    async function triggerCameraCapture() {
      const btn = document.getElementById('btnCameraCapture');
      btn.disabled = true;
      btn.innerText = '⏳ Triggering...';

      // Reset chunks visualizer
      receivedChunks.clear();
      document.getElementById('chunkMatrix').innerHTML = '';
      document.getElementById('chunkStreamLog').innerHTML = '';
      document.getElementById('chunkCountLabel').innerText = '0 chunks';
      document.getElementById('cameraProgressBar').style.width = '0%';
      document.getElementById('cameraSpeedMetric').innerText = 'Speed: 0 B/s | Rate: 0 blk/s';
      clientCaptureStartTime = Date.now();

      try {
        if (isClientConnected && clientPort && clientPort.writable) {
          // Send camera trigger command to USB dongle: Port 0x13, Len 0x01, Payload 0x00
          const writer = clientPort.writable.getWriter();
          const cmd = new Uint8Array([0x13, 0x01, 0x00]);
          await writer.write(cmd);
          writer.releaseLock();

          // Inform backend of client web serial capture session
          await fetch('/api/camera/capture', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ timeout: 35.0, source: 'client_web_serial' })
          }).catch(console.warn);

          document.getElementById('cameraProgressContainer').style.display = 'block';
          document.getElementById('cameraStatusText').innerText = 'Status: Capturing via Web USB...';
          btn.innerText = '📸 Capturing (Web USB)...';
          if (cameraPollingInterval) clearInterval(cameraPollingInterval);
          cameraPollingInterval = setInterval(pollCameraStatus, 800);
          return;
        }

        const res = await fetch('/api/camera/capture', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ timeout: 35.0 })
        });
        const data = await res.json();
        if (!res.ok) {
          alert(data.error || 'Failed to trigger camera capture');
          btn.disabled = false;
          btn.innerText = '📸 Trigger Camera Capture';
          return;
        }
        document.getElementById('cameraProgressContainer').style.display = 'block';
        document.getElementById('cameraStatusText').innerText = 'Status: Capturing image...';
        btn.innerText = '📸 Capturing...';
        if (cameraPollingInterval) clearInterval(cameraPollingInterval);
        cameraPollingInterval = setInterval(pollCameraStatus, 800);
      } catch (err) {
        alert('Camera request error: ' + err.message);
        btn.disabled = false;
        btn.innerText = '📸 Trigger Camera Capture';
      }
    }

    async function pollCameraStatus() {
      try {
        const res = await fetch('/api/camera/status');
        const data = await res.json();
        const btn = document.getElementById('btnCameraCapture');
        const statusText = document.getElementById('cameraStatusText');
        const progDetails = document.getElementById('cameraProgressDetails');

        if (data.status === 'capturing') {
          statusText.innerText = `Status: Capturing (${data.progress.elapsed_seconds}s)`;
          progDetails.innerText = `Blocks received: ${data.progress.blocks_received} (${(data.progress.total_bytes / 1024).toFixed(1)} KB)`;
          if (data.progress.transfer_speed_bps) {
            document.getElementById('cameraSpeedMetric').innerText = `Speed: ${data.progress.transfer_speed_bps} B/s | Elapsed: ${data.progress.elapsed_seconds}s`;
          }
          if (data.chunks && data.chunks.length > 0) {
            data.chunks.forEach(chunk => renderCameraChunk(chunk));
          }
        } else if (data.status === 'completed') {
          clearInterval(cameraPollingInterval);
          cameraPollingInterval = null;
          btn.disabled = false;
          btn.innerText = '📸 Trigger Camera Capture';
          statusText.innerText = 'Status: Capture Complete! 🎉';
          document.getElementById('cameraProgressBar').style.width = '100%';
          loadLatestCameraImage();
        } else if (data.status === 'failed') {
          clearInterval(cameraPollingInterval);
          cameraPollingInterval = null;
          btn.disabled = false;
          btn.innerText = '📸 Trigger Camera Capture';
          statusText.innerText = `Status: Failed (${data.progress.error || 'Timeout'})`;
        }
      } catch (e) {}
    }

    async function loadLatestCameraImage() {
      try {
        const res = await fetch('/api/camera/latest');
        if (!res.ok) return;
        const data = await res.json();
        const img = document.getElementById('cameraImgPreview');
        const placeholder = document.getElementById('cameraPlaceholder');
        const meta = document.getElementById('cameraMetaInfo');

        img.src = `/api/camera/latest.jpg?t=${Date.now()}`;
        img.style.display = 'block';
        placeholder.style.display = 'none';
        meta.style.display = 'block';
        meta.innerText = `Size: ${(data.metadata.byte_length / 1024).toFixed(1)} KB | Blocks: ${data.metadata.block_count} | Duration: ${data.metadata.capture_duration_seconds}s`;
      } catch (e) {}
    }

    function initSSE() {
      sseSource = new EventSource('/api/telemetry/stream');
      sseSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'camera_chunk') {
            renderCameraChunk(data);
          } else {
            renderTelemetry(data);
          }
        } catch (e) {}
      };
      sseSource.onerror = () => {
        if (sseSource) { sseSource.close(); sseSource = null; }
      };
    }


    function renderTelemetry(data) {
      document.getElementById('valSeq').innerText = '#' + data.packet_sequence;
      document.getElementById('valUptime').innerText = (data.device_uptime_ms / 1000).toFixed(1) + 's uptime';
      document.getElementById('valTemp').innerText = data.barometer.temperature_c.toFixed(1) + ' °C';
      document.getElementById('valPres').innerText = data.barometer.pressure_hpa.toFixed(1) + ' hPa (' + data.barometer.altitude_m.toFixed(1) + 'm)';
      document.getElementById('valBatt').innerText = data.eps.battery_charge.bus_voltage_v.toFixed(2) + ' V';
      document.getElementById('valRails').innerText = '5V: ' + data.eps.main_5v_v.toFixed(2) + 'V | 3.3V: ' + data.eps.main_3v3_v.toFixed(2) + 'V';
      document.getElementById('valGpsCoords').innerText = data.gps.latitude.toFixed(4) + ', ' + data.gps.longitude.toFixed(4);
      document.getElementById('valGpsStatus').innerText = (data.gps.fix ? 'Fix OK' : 'No Fix') + ' (' + data.gps.satellites + ' sats)';
      document.getElementById('valAccel').innerText = data.imu.accelerometer_g.x.toFixed(2) + ', ' + data.imu.accelerometer_g.y.toFixed(2) + ', ' + data.imu.accelerometer_g.z.toFixed(2);
      document.getElementById('valSignal').innerText = data.receiver_rssi.toFixed(1) + ' dBm';
      document.getElementById('valSnr').innerText = 'SNR: ' + data.receiver_snr.toFixed(2) + ' dB';
      document.getElementById('jsonDisplay').innerText = JSON.stringify(data, null, 2);
    }

    async function handleClientWebSerial() {
      if (isClientConnected) {
        // Disconnect
        try {
          if (clientReader) await clientReader.cancel();
          if (clientPort) await clientPort.close();
        } catch (e) {}
        isClientConnected = false;
        clientPort = null;
        clientReader = null;
        document.getElementById('btnClientConnect').innerText = '🔌 Connect Browser USB';
        document.getElementById('btnClientConnect').className = '';
        checkStatus();
        return;
      }

      if (!('serial' in navigator)) {
        alert('Browser Anda belum mendukung Web Serial API. Silakan gunakan Google Chrome, Edge, atau Opera.');
        return;
      }

      try {
        clientPort = await navigator.serial.requestPort({
          filters: [{ usbVendorId: 0x0483, usbProductId: 0x5740 }]
        });
        const baudRate = parseInt(document.getElementById('clientBaudInput').value, 10) || 1000000;
        await clientPort.open({ baudRate });

        const satNum = parseInt(document.getElementById('clientSerialInput').value, 10) || 1581;

        // Kirim filter serial number ke receiver: port 0x01, len 4, uint32 little-endian
        const writer = clientPort.writable.getWriter();
        const cmd = new Uint8Array(6);
        cmd[0] = 0x01; // HostPort.USB_SERIAL_FILTER
        cmd[1] = 0x04; // Length 4 bytes
        const view = new DataView(cmd.buffer);
        view.setUint32(2, satNum, true);
        await writer.write(cmd);
        writer.releaseLock();

        isClientConnected = true;
        document.getElementById('btnClientConnect').innerText = 'Disconnect Browser USB';
        document.getElementById('btnClientConnect').className = 'btn-danger';

        const badge = document.getElementById('statusBadge');
        const text = document.getElementById('statusText');
        badge.className = 'status-badge status-client';
        text.innerText = `Client USB: Sat #${satNum}`;

        readClientSerialLoop();
      } catch (err) {
        alert('Koneksi Web Serial gagal: ' + err.message);
      }
    }

    async function readClientSerialLoop() {
      let buffer = new Uint8Array();
      while (clientPort && clientPort.readable && isClientConnected) {
        try {
          clientReader = clientPort.readable.getReader();
          while (true) {
            const { value, done } = await clientReader.read();
            if (done) break;
            if (value && value.length > 0) {
              const merged = new Uint8Array(buffer.length + value.length);
              merged.set(buffer);
              merged.set(value, buffer.length);
              buffer = merged;

              // Parse frames according to standard RASCube framing: [port, len, payload...]
              while (buffer.length >= 2) {
                const port = buffer[0];
                const len = buffer[1];
                const frameLen = 2 + len;

                const isCameraBlock = (port === 0x15 || port === 0x20) && len === 242;
                const isTelemetry = (port === 0x10) && len === 121;
                const isControl = (port === 0x00 || port === 0x01 || port === 0x02 || port === 0x03 || port === 0x0A || port === 0x12 || port === 0x13 || port === 0x80);

                if (isCameraBlock || isTelemetry || isControl) {
                  if (buffer.length < frameLen) {
                    break;
                  }

                  const frame = buffer.slice(0, frameLen);
                  buffer = buffer.slice(frameLen);
                  const hex = Array.from(frame).map(b => b.toString(16).padStart(2, '0')).join('').toUpperCase();

                  if (isTelemetry) {
                    fetch('/api/telemetry/ingest', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ hex, source: 'client_web_serial' })
                    })
                    .then(r => r.json())
                    .then(res => { if (res.telemetry) renderTelemetry(res.telemetry); })
                    .catch(console.error);
                  } else if (isCameraBlock) {
                    // Extract block info immediately for instant zero-latency UI rendering
                    const blockIdx = frame[2] | (frame[3] << 8);
                    const chunkSize = frame.length - 4;
                    const hexPreview = Array.from(frame.slice(4, 12)).map(b => b.toString(16).padStart(2, '0')).join('').toUpperCase();
                    const elapsed = clientCaptureStartTime ? (Date.now() - clientCaptureStartTime) / 1000 : 0;

                    renderCameraChunk({
                      index: blockIdx,
                      size: chunkSize,
                      hex_preview: hexPreview,
                      total_bytes: (receivedChunks.size + 1) * chunkSize,
                      total_blocks: receivedChunks.size + 1,
                      elapsed_seconds: elapsed
                    });

                    // Ingest to backend in background and update progressive JPEG preview
                    fetch('/api/camera/chunk/ingest', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ hex, source: 'client_web_serial' })
                    })
                    .then(r => r.json())
                    .then(res => {
                      if (res.chunk && res.chunk.partial_jpeg_base64) {
                        const img = document.getElementById('cameraImgPreview');
                        if (img) img.src = 'data:image/jpeg;base64,' + res.chunk.partial_jpeg_base64;
                      }
                    })
                    .catch(console.error);
                  }
                } else {
                  buffer = buffer.slice(1);
                }
              }
            }
          }
        } catch (e) {
          if (isClientConnected) console.warn('Web Serial stream error:', e);
          break;
        } finally {
          if (clientReader) {
            try { clientReader.releaseLock(); } catch(e) {}
            clientReader = null;
          }
        }
      }
    }

    async function loadPorts() {
      const res = await fetch('/api/ports');
      const data = await res.json();
      const sel = document.getElementById('portSelect');
      sel.innerHTML = '';
      data.ports.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.device;
        opt.innerText = p.device + (p.description ? ' - ' + p.description : '');
        if (p.is_rascube) opt.selected = true;
        sel.appendChild(opt);
      });
    }

    async function checkStatus() {
      if (isClientConnected) return; // Do not overwrite client UI state

      const res = await fetch('/api/status');
      const data = await res.json();
      isConnected = data.is_connected;
      const badge = document.getElementById('statusBadge');
      const text = document.getElementById('statusText');
      const btn = document.getElementById('btnConnect');

      // Check SDR status
      try {
        const sdrRes = await fetch('/api/sdr/status');
        const sdrData = await sdrRes.json();
        isSdrActive = sdrData.active;
        isSdrWakeActive = sdrData.cyclic_beacon_active;
        const sdrBtn = document.getElementById('btnSdrStart');
        const wakeBtn = document.getElementById('btnSdrWake');

        if (isSdrActive) {
          sdrBtn.innerText = '⏹️ Stop PlutoSDR RX';
          sdrBtn.className = 'btn-danger';
        } else {
          sdrBtn.innerText = '⚡ Start PlutoSDR RX';
          sdrBtn.className = '';
        }

        if (isSdrWakeActive) {
          wakeBtn.innerText = '⏹️ Stop Hardware Wake Beacon';
          wakeBtn.className = 'btn-danger';
        } else {
          wakeBtn.innerText = '⚡ Hardware Wake Beacon (DMA Loop)';
          wakeBtn.className = 'btn-secondary';
        }
      } catch (e) {}

      if (isConnected) {
        badge.className = 'status-badge status-connected';
        text.innerText = `${data.connected_port} (#${data.serial_number})`;
        if (data.connected_port && data.connected_port.includes('PlutoSDR')) {
          btn.innerText = '⚡ Connect Dongle';
          btn.className = '';
        } else {
          btn.innerText = 'Disconnect';
          btn.className = 'btn-danger';
        }
        if (!sseSource) initSSE();
      } else {
        badge.className = 'status-badge status-disconnected';
        text.innerText = data.error_message ? `Error: ${data.error_message}` : 'Disconnected';
        btn.innerText = '⚡ Connect Dongle';
        btn.className = '';
      }
    }

    async function handleConnect() {
      if (isConnected) {
        await fetch('/api/disconnect', { method: 'POST' });
        if (sseSource) { sseSource.close(); sseSource = null; }
        checkStatus();
      } else {
        const port = document.getElementById('portSelect').value;
        const serial_number = parseInt(document.getElementById('serialInput').value);
        const res = await fetch('/api/connect', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ port, serial_number })
        });
        const data = await res.json();
        if (!res.ok) alert(data.error || 'Connection failed');
        setTimeout(checkStatus, 500);
      }
    }

    loadPorts();
    checkStatus();
    loadLatestCameraImage();
    setInterval(checkStatus, 3000);
  </script>
</body>
</html>
"""


class GroundStationAPIHandler(BaseHTTPRequestHandler):
    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True
        except Exception as exc:
            self.close_connection = True

    def log_error(self, format: str, *args: Any) -> None:
        # Ignore ConnectionResetError / BrokenPipeError in standard HTTP logger
        msg = format % args
        if "Connection reset by peer" in msg or "Broken pipe" in msg:
            return
        super().log_error(format, *args)

    def _send_json(self, status: int, data: Any) -> None:
        def _json_default(obj: Any) -> Any:
            if isinstance(obj, (set, frozenset)):
                return list(obj)
            if dataclasses.is_dataclass(obj):
                return dataclasses.asdict(obj)
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            if isinstance(obj, bytes):
                return obj.hex().upper()
            return str(obj)

        body = json.dumps(data, indent=2, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. Swagger UI Documentation
        if path in ("/docs", "/swagger"):
            body = SWAGGER_UI_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 2. OpenAPI JSON Specification
        if path in ("/openapi.json", "/swagger.json"):
            self._send_json(HTTPStatus.OK, OPENAPI_SCHEMA)
            return

        # 3. Ground Station Dashboard Web UI
        if path == "/":
            body = HTML_DASHBOARD.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 4. List Available COM Ports
        if path == "/api/ports":
            ports_list = []
            for p in list_ports.comports():
                is_ras = (p.vid == USB_VID and p.pid == USB_PID_V2)
                ports_list.append({
                    "device": p.device,
                    "description": p.description,
                    "vid": p.vid,
                    "pid": p.pid,
                    "serial_number": p.serial_number,
                    "is_rascube": is_ras,
                })
            self._send_json(HTTPStatus.OK, {"ports": ports_list})
            return

        # 5. Connection Status
        if path == "/api/status":
            with state.lock:
                status_data = {
                    "is_connected": state.is_connected,
                    "connected_port": state.connected_port,
                    "serial_number": state.serial_number,
                    "receiver_info": state.receiver_info,
                    "obc_info": state.obc_info,
                    "total_samples_received": state.total_samples_received,
                    "last_received_time": state.last_received_time,
                    "error_message": state.error_message,
                }
            self._send_json(HTTPStatus.OK, status_data)
            return

        # 6. Latest Telemetry
        if path == "/api/telemetry/latest":
            with state.lock:
                if state.latest_sample is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "No telemetry data received yet"},
                    )
                    return
                self._send_json(HTTPStatus.OK, state.latest_sample)
            return

        # 7. Telemetry History Buffer
        if path == "/api/telemetry/history":
            limit = int(query.get("limit", [50])[0])
            with state.lock:
                items = list(state.history)[-limit:]
            self._send_json(HTTPStatus.OK, {"count": len(items), "samples": items})
            return

        # 8. Realtime Server-Sent Events (SSE) Stream
        if path == "/api/telemetry/stream":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            sub_queue = state.add_subscriber()
            try:
                while True:
                    try:
                        data = sub_queue.get(timeout=1.0)
                        msg = f"data: {json.dumps(data)}\n\n".encode("utf-8")
                        self.wfile.write(msg)
                        self.wfile.flush()
                    except queue.Empty:
                        # Keep-alive comment
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                state.remove_subscriber(sub_queue)
            return

        # 9. Manual Decode via Query Parameter
        if path == "/api/decode":
            hex_data = query.get("hex", [""])[0]
            if not hex_data:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'hex' query parameter"})
                return
            try:
                decoded = decode_telemetry_to_dict(hex_data)
                self._send_json(HTTPStatus.OK, decoded)
            except (ProtocolDecodeError, ValueError) as exc:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            return

        # 10. Camera Capture Status
        if path == "/api/camera/status":
            with state.lock:
                self._send_json(HTTPStatus.OK, {
                    "status": state.camera_status,
                    "progress": state.camera_progress,
                    "chunks": list(state.camera_chunks)[-100:],
                    "has_image": state.latest_image is not None,
                    "metadata": state.latest_image_metadata,
                    "image_url": "/api/camera/latest.jpg" if state.latest_image is not None else None,
                })
            return

        # 11. Latest Camera Image (JSON & Base64)
        if path == "/api/camera/latest":
            with state.lock:
                if state.latest_image is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "No camera image captured yet"})
                    return
                b64_img = base64.b64encode(state.latest_image).decode("ascii")
                self._send_json(HTTPStatus.OK, {
                    "metadata": state.latest_image_metadata,
                    "image_url": "/api/camera/latest.jpg",
                    "jpeg_base64": b64_img,
                })
            return

        # 12. Latest Camera Image (Raw JPEG Binary)
        if path in ("/api/camera/latest.jpg", "/api/camera/image", "/api/camera/partial.jpg"):
            with state.lock:
                img_data = state.latest_image or state.partial_image
                if img_data is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "No camera image or partial buffer available yet"})
                    return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img_data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(img_data)
            return

        # 13. PlutoSDR Ground Station Status
        if path == "/api/sdr/status":
            with state.lock:
                self._send_json(HTTPStatus.OK, {
                    "active": state.sdr_active,
                    "sat": state.sdr_sat,
                    "frequency_hz": 916_000_000 + (state.sdr_sat % 18) * 600_000,
                    "gain_db": state.sdr_gain,
                    "sf": state.sdr_sf,
                    "bw_hz": state.sdr_bw,
                    "uri": state.sdr_uri,
                    "packets_decoded": state.sdr_packets_count,
                    "last_rssi_dbm": state.sdr_last_rssi,
                    "last_snr_db": state.sdr_last_snr,
                    "cyclic_beacon_active": state.sdr_cyclic_active,
                    "error": state.sdr_error,
                })
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"

        try:
            body_json = json.loads(raw_body)
        except json.JSONDecodeError:
            body_json = {}

        # 1. Connect to Port & Set Satellite Serial Number (Server Host USB)
        if path == "/api/connect":
            port = body_json.get("port")
            serial_number = body_json.get("serial_number")

            if not port or serial_number is None:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Missing 'port' or 'serial_number' in request body"},
                )
                return

            try:
                serial_number = int(serial_number)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "'serial_number' must be an integer"})
                return

            with state.lock:
                if state.is_connected:
                    state.stop_signal.set()
                    time.sleep(0.3)

                state.stop_signal.clear()
                state.error_message = None
                worker = threading.Thread(
                    target=background_telemetry_loop,
                    args=(port, serial_number),
                    daemon=True,
                )
                state.worker_thread = worker
                worker.start()

            # Wait briefly for connection handshake
            time.sleep(0.6)
            with state.lock:
                self._send_json(HTTPStatus.OK, {
                    "status": "connecting" if not state.is_connected else "connected",
                    "port": port,
                    "serial_number": serial_number,
                    "receiver_info": state.receiver_info,
                    "obc_info": state.obc_info,
                    "error": state.error_message,
                })
            return

        # 2. Disconnect (Server Host USB)
        if path == "/api/disconnect":
            with state.lock:
                state.stop_signal.set()
                state.is_connected = False
            self._send_json(HTTPStatus.OK, {"status": "disconnected"})
            return

        # 3. Ingest Telemetry or Camera Chunks from Client Web Serial
        if path in ("/api/telemetry/ingest", "/api/camera/chunk/ingest"):
            hex_data = body_json.get("hex") or body_json.get("payload") or raw_body.strip().strip('"')
            if not hex_data:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'hex' in request body"})
                return
            try:
                raw_bytes = bytes.fromhex(hex_data)
                port = raw_bytes[0] if len(raw_bytes) > 0 else None

                # Check if it's a Camera Block (InboundPort.JPEG_CAMERA = 0x15 or 0x20)
                if port in (0x15, 0x20):
                    payload = raw_bytes[2:] if len(raw_bytes) > 2 else raw_bytes
                    if len(payload) >= 2:
                        blk_idx = struct.unpack_from("<H", payload, 0)[0]
                        blk_data = payload[2:]
                        block = CameraBlock(index=blk_idx, data=blk_data, metadata=None)
                        with state.camera_lock:
                            jpeg_res = state.camera_assembler.add(block)
                            now = time.time()
                            started_at = state.camera_progress.get("started_at") or now
                            elapsed = round(now - started_at, 2)
                            state.camera_progress["blocks_received"] += 1
                            state.camera_progress["total_bytes"] += len(blk_data)
                            state.camera_progress["elapsed_seconds"] = elapsed
                            state.camera_progress["latest_block_index"] = blk_idx
                            speed = round(state.camera_progress["total_bytes"] / max(0.01, elapsed), 1)
                            state.camera_progress["transfer_speed_bps"] = speed

                            state.camera_blocks[blk_idx] = blk_data
                            # Assemble contiguous progressive blocks sorted strictly: 0, 1, 2, ...
                            contiguous = bytearray()
                            idx = 0
                            while idx in state.camera_blocks:
                                contiguous.extend(state.camera_blocks[idx])
                                idx += 1

                            partial_b64 = None
                            if len(contiguous) >= 2 and contiguous[:2] == b"\xff\xd8":
                                if contiguous.find(b"\xff\xd9") < 0:
                                    partial_jpeg = bytes(contiguous) + b"\xff\xd9"
                                else:
                                    partial_jpeg = bytes(contiguous)
                                state.partial_image = partial_jpeg
                                partial_b64 = base64.b64encode(partial_jpeg).decode("ascii")

                            chunk_record = {
                                "type": "camera_chunk",
                                "index": blk_idx,
                                "size": len(blk_data),
                                "total_blocks": state.camera_progress["blocks_received"],
                                "contiguous_blocks": idx,
                                "total_bytes": state.camera_progress["total_bytes"],
                                "elapsed_seconds": elapsed,
                                "hex_preview": blk_data[:16].hex().upper(),
                                "partial_jpeg_base64": partial_b64,
                                "timestamp": now,
                            }
                            with state.lock:
                                state.camera_chunks.append(chunk_record)

                            state.broadcast_camera_chunk(chunk_record)

                            if jpeg_res is not None:
                                state.latest_image = jpeg_res
                                state.latest_image_metadata = {
                                    "block_count": state.camera_assembler.block_count,
                                    "duplicate_blocks": len(state.camera_assembler.duplicates),
                                    "byte_length": len(jpeg_res),
                                    "captured_at": time.time(),
                                    "capture_duration_seconds": elapsed,
                                }
                                state.camera_status = "completed"
                                print(f"[API WebUSB] Camera JPEG complete: {len(jpeg_res)} bytes")
                        self._send_json(HTTPStatus.OK, {"status": "camera_chunk_ingested", "chunk": chunk_record})
                        return

                # Otherwise standard Telemetry (0x10)
                decoded = decode_telemetry_to_dict(hex_data)
                decoded["raw_hex"] = hex_data if isinstance(hex_data, str) else hex_data.hex().upper()
                decoded["timestamp"] = time.time()
                decoded["source"] = body_json.get("source", "client_web_serial")
                state.broadcast_telemetry(decoded)
                self._send_json(HTTPStatus.OK, {"status": "ingested", "telemetry": decoded})
            except (ProtocolDecodeError, ValueError) as exc:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            return

        # 4. Trigger Camera Capture
        if path == "/api/camera/capture":
            timeout_val = float(body_json.get("timeout", 35.0))
            source_val = str(body_json.get("source", "server"))
            try:
                trigger_camera_capture(timeout=timeout_val, source=source_val)
                self._send_json(HTTPStatus.ACCEPTED, {
                    "status": "capturing",
                    "source": source_val,
                    "message": f"Camera capture initiated with {timeout_val:.1f}s timeout",
                    "check_status_url": "/api/camera/status",
                })
            except SessionBusyError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (ConnectionError, RuntimeError) as exc:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        # 5. Decode HEX Body
        if path == "/api/decode":
            hex_data = body_json.get("hex") or body_json.get("payload") or raw_body.strip().strip('"')
            if not hex_data:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'hex' in request body"})
                return
            try:
                decoded = decode_telemetry_to_dict(hex_data)
                self._send_json(HTTPStatus.OK, decoded)
            except (ProtocolDecodeError, ValueError) as exc:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            return

        # 6. Start PlutoSDR Direct DSP Receiver
        if path == "/api/sdr/receiver/start":
            sat = int(body_json.get("sat", 1581))
            gain = float(body_json.get("gain", 40.0))
            sf = int(body_json.get("sf", 7))
            bw = int(body_json.get("bw", 500_000))
            uri = str(body_json.get("uri", "usb:"))

            with state.lock:
                if state.sdr_active:
                    state.sdr_stop_event.set()
                    time.sleep(0.3)

                state.sdr_stop_event.clear()
                state.sdr_error = None
                sdr_worker = threading.Thread(
                    target=background_sdr_receiver_loop,
                    args=(sat, gain, sf, bw, uri),
                    daemon=True,
                    name="pluto-sdr-rx",
                )
                state.sdr_thread = sdr_worker
                sdr_worker.start()

            time.sleep(0.6)
            with state.lock:
                self._send_json(HTTPStatus.OK, {
                    "status": "active" if state.sdr_active else "starting",
                    "sat": sat,
                    "frequency_hz": 916_000_000 + (sat % 18) * 600_000,
                    "gain": gain,
                    "sf": sf,
                    "bw": bw,
                    "uri": uri,
                    "error": state.sdr_error,
                })
            return

        # 7. Stop PlutoSDR Direct DSP Receiver
        if path == "/api/sdr/receiver/stop":
            with state.lock:
                state.sdr_stop_event.set()
                state.sdr_active = False
            self._send_json(HTTPStatus.OK, {"status": "stopped"})
            return

        # 8. PlutoSDR Transmit Commands
        if path == "/api/sdr/transmit":
            sat = int(body_json.get("sat", 1581))
            cmd_type = str(body_json.get("command", "ping"))
            params = body_json.get("params", {})
            bw = int(body_json.get("bw", 500_000))
            uri = str(body_json.get("uri", "usb:"))

            try:
                res = transmit_sdr_command(sat=sat, cmd_type=cmd_type, params=params, bw=bw, sdr_uri=uri)
                self._send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return


        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="RASCube Ground Station REST API Server with Swagger UI")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")

    args = parser.parse_args()

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.host, args.port), GroundStationAPIHandler)
    url = f"http://localhost:{args.port}"
    print("=" * 65)
    print("🚀 RASCubeV2 Ground Station API & Swagger UI Server Running")
    print(f"📖 Swagger UI Docs        : {url}/docs")
    print(f"📄 OpenAPI Specification  : {url}/openapi.json")
    print(f"🛰️ Ground Station Dashboard: {url}/")
    print("=" * 65)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        with state.lock:
            state.stop_signal.set()
        server.server_close()


if __name__ == "__main__":
    main()
