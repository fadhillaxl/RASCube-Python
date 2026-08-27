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
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from serial.tools import list_ports

from rascube_v2 import SyncRASCube, decode_telemetry_to_dict
from rascube_v2.constants import USB_PID_V2, USB_VID
from rascube_v2.exceptions import (
    CameraAssemblyError,
    ProtocolDecodeError,
    RequestTimeoutError,
    SessionBusyError,
)

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
        self.camera_lock = threading.Lock()
        self.camera_thread: threading.Thread | None = None

        # SSE Subscribers
        self.subscribers: list[queue.Queue[dict[str, Any]]] = []
        self.worker_thread: threading.Thread | None = None
        self.stop_signal = threading.Event()

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


def trigger_camera_capture(timeout: float = 30.0) -> None:
    """Initiates a background thread to capture JPEG camera image from satellite."""
    global state
    with state.camera_lock:
        if state.camera_status == "capturing":
            raise SessionBusyError("A camera capture session is already in progress")
        if not state.is_connected or state.cube is None:
            raise ConnectionError("Receiver is not connected to a satellite. Connect first via /api/connect")

        state.camera_status = "capturing"
        state.camera_progress = {
            "blocks_received": 0,
            "total_bytes": 0,
            "started_at": time.time(),
            "elapsed_seconds": 0.0,
            "error": None,
        }

    def _worker() -> None:
        start_time = time.time()
        try:
            def on_block(block: Any) -> None:
                with state.lock:
                    state.camera_progress["blocks_received"] += 1
                    state.camera_progress["total_bytes"] += len(block.data)
                    state.camera_progress["elapsed_seconds"] = round(time.time() - start_time, 2)

            image = state.cube.camera.capture(timeout=timeout, on_block=on_block)
            with state.lock:
                state.latest_image = image.jpeg
                state.latest_image_metadata = {
                    "block_count": image.block_count,
                    "duplicate_blocks": image.duplicate_blocks,
                    "byte_length": len(image.jpeg),
                    "captured_at": time.time(),
                    "capture_duration_seconds": round(time.time() - start_time, 2),
                }
                state.camera_status = "completed"
            print(f"[API Backend] Camera capture completed: {len(image.jpeg)} bytes in {time.time() - start_time:.2f}s")
        except Exception as exc:
            print(f"[API Backend] Camera capture error: {exc}")
            with state.lock:
                state.camera_status = "failed"
                state.camera_progress["error"] = str(exc)

    t = threading.Thread(target=_worker, daemon=True, name="camera-worker")
    state.camera_thread = t
    t.start()


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
        <button id="tabClientBtn" class="tab-btn active" onclick="switchTab('client')">💻 Client Web Serial (Browser USB)</button>
        <button id="tabServerBtn" class="tab-btn" onclick="switchTab('server')">🖥️ Server COM Port (Host USB)</button>
      </div>

      <!-- Tab 1: Client Web Serial -->
      <div id="tabClient">
        <div class="note-box">
          ✨ <strong>Client Web Serial Mode</strong>: Receiver USB terhubung langsung ke laptop/komputer Anda (Chrome / Edge / Opera). Data dibaca langsung oleh browser dan otomatis dikirim ke backend API.
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
          🖥️ <strong>Server Port Mode</strong>: Receiver USB terhubung ke komputer yang menjalankan server Python.
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
          <button id="btnConnect" onclick="handleConnect()">⚡ Connect</button>
          <button class="btn-secondary" onclick="loadPorts()">🔄 Refresh Ports</button>
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

      <div id="cameraProgressContainer" style="display: none; margin-bottom: 1.25rem;">
        <div style="font-size: 0.85rem; color: var(--accent); margin-bottom: 0.4rem;" id="cameraProgressDetails">Receiving blocks...</div>
        <div style="background: rgba(255,255,255,0.08); border-radius: 6px; height: 8px; overflow: hidden;">
          <div id="cameraProgressBar" style="width: 100%; height: 100%; background: linear-gradient(90deg, #0284c7, #38bdf8); animation: pulse 1.5s infinite;"></div>
        </div>
      </div>

      <div id="cameraImageContainer" style="text-align: center; background: rgba(5, 8, 15, 0.6); border-radius: 12px; padding: 1.25rem; min-height: 200px; display: flex; flex-direction: column; align-items: center; justify-content: center; border: 1px dashed var(--card-border);">
        <img id="cameraImgPreview" src="" alt="Satellite Capture Preview" style="max-width: 100%; max-height: 400px; border-radius: 8px; display: none; box-shadow: 0 4px 20px rgba(0,0,0,0.5);" />
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

    function switchTab(mode) {
      if (mode === 'client') {
        document.getElementById('tabClient').style.display = 'block';
        document.getElementById('tabServer').style.display = 'none';
        document.getElementById('tabClientBtn').className = 'tab-btn active';
        document.getElementById('tabServerBtn').className = 'tab-btn';
      } else {
        document.getElementById('tabClient').style.display = 'none';
        document.getElementById('tabServer').style.display = 'block';
        document.getElementById('tabClientBtn').className = 'tab-btn';
        document.getElementById('tabServerBtn').className = 'tab-btn active';
      }
    }

    async function triggerCameraCapture() {
      const btn = document.getElementById('btnCameraCapture');
      btn.disabled = true;
      btn.innerText = '⏳ Requesting...';
      try {
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
        } else if (data.status === 'completed') {
          clearInterval(cameraPollingInterval);
          cameraPollingInterval = null;
          btn.disabled = false;
          btn.innerText = '📸 Trigger Camera Capture';
          statusText.innerText = 'Status: Capture Complete! 🎉';
          document.getElementById('cameraProgressContainer').style.display = 'none';
          loadLatestCameraImage();
        } else if (data.status === 'failed') {
          clearInterval(cameraPollingInterval);
          cameraPollingInterval = null;
          btn.disabled = false;
          btn.innerText = '📸 Trigger Camera Capture';
          statusText.innerText = `Status: Failed (${data.progress.error || 'Timeout'})`;
          document.getElementById('cameraProgressContainer').style.display = 'none';
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
            if (value) {
              const merged = new Uint8Array(buffer.length + value.length);
              merged.set(buffer);
              merged.set(value, buffer.length);
              buffer = merged;

              // Cari header 0x10 0x79 (panjang 123 bytes)
              while (buffer.length >= 123) {
                let headerIdx = -1;
                for (let i = 0; i <= buffer.length - 123; i++) {
                  if (buffer[i] === 0x10 && buffer[i + 1] === 0x79) {
                    headerIdx = i;
                    break;
                  }
                }
                if (headerIdx === -1) {
                  // Simpan 2 byte terakhir untuk mengantisipasi header terpotong
                  buffer = buffer.slice(-2);
                  break;
                }

                if (buffer.length >= headerIdx + 123) {
                  const frame = buffer.slice(headerIdx, headerIdx + 123);
                  buffer = buffer.slice(headerIdx + 123);

                  const hex = Array.from(frame)
                    .map(b => b.toString(16).padStart(2, '0'))
                    .join('')
                    .toUpperCase();

                  // Push ke backend via /api/telemetry/ingest
                  fetch('/api/telemetry/ingest', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ hex, source: 'client_web_serial' })
                  })
                  .then(r => r.json())
                  .then(res => {
                    if (res.telemetry) renderTelemetry(res.telemetry);
                  })
                  .catch(console.error);
                } else {
                  break;
                }
              }
            }
          }
        } catch (e) {
          console.warn('Web Serial stream error:', e);
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

      if (isConnected) {
        badge.className = 'status-badge status-connected';
        text.innerText = `Server: ${data.connected_port} (#${data.serial_number})`;
        btn.innerText = 'Disconnect';
        btn.className = 'btn-danger';
        if (!sseSource) initSSE();
      } else {
        badge.className = 'status-badge status-disconnected';
        text.innerText = data.error_message ? `Error: ${data.error_message}` : 'Disconnected';
        btn.innerText = '⚡ Connect';
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

    function initSSE() {
      sseSource = new EventSource('/api/telemetry/stream');
      sseSource.onmessage = (event) => {
        const data = JSON.parse(event.data);
        renderTelemetry(data);
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

    loadPorts();
    checkStatus();
    loadLatestCameraImage();
    setInterval(checkStatus, 3000);
  </script>
</body>
</html>
"""


class GroundStationAPIHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
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
        if path in ("/api/camera/latest.jpg", "/api/camera/image"):
            with state.lock:
                if state.latest_image is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "No camera image captured yet"})
                    return
                img_data = state.latest_image
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img_data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(img_data)
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

        # 3. Ingest Telemetry from Client Web Serial
        if path == "/api/telemetry/ingest":
            hex_data = body_json.get("hex") or body_json.get("payload") or raw_body.strip().strip('"')
            if not hex_data:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'hex' in request body"})
                return
            try:
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
            try:
                trigger_camera_capture(timeout=timeout_val)
                self._send_json(HTTPStatus.ACCEPTED, {
                    "status": "capturing",
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
