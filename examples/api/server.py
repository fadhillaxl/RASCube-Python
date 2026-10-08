#!/usr/bin/env python3
"""RASCube Ground Station REST & Realtime Streaming API Server (Client Web USB/Serial Architecture).

Architecture:
- Client Web USB/Serial: The USB Receiver Dongle is connected directly to the user's browser (Chrome/Edge/Opera).
- Live Telemetry & Camera frames are read by the browser and ingested into the server via `/api/telemetry/ingest` and `/api/camera/chunk/ingest`.
- Real-time Server-Sent Events (SSE) stream at `/api/telemetry/stream`.
- Swagger UI Documentation at `/docs` and `/swagger`.
- OpenAPI Specification at `/openapi.json`.
- Ground Station Web Dashboard at `/`.
"""

from __future__ import annotations

import argparse
import base64
import collections
import dataclasses
import json
import os
import queue
import ssl
import struct
import subprocess
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from rascube_v2 import decode_telemetry_to_dict
from rascube_v2.exceptions import (
    ProtocolDecodeError,
    SessionBusyError,
)
from rascube_v2.models.camera import CameraBlock
from rascube_v2.protocol.camera import CameraAssembler


# --- Global State ---
class GroundStationState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.is_connected = False
        self.connected_source: str | None = None
        self.connected_port: str | None = None
        self.serial_number: int = 1581
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
            "transfer_speed_bps": 0.0,
            "latest_block_index": 0,
            "error": None,
        }
        self.latest_image: bytes | None = None
        self.latest_image_metadata: dict[str, Any] | None = None
        self.partial_image: bytes | None = None
        self.camera_blocks: dict[int, bytes] = {}
        self.camera_chunks: list[dict[str, Any]] = []
        self.camera_assembler = CameraAssembler()
        self.camera_lock = threading.Lock()

        # SSE Subscribers
        self.subscribers: list[queue.Queue[dict[str, Any]]] = []

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


def trigger_camera_capture(timeout: float = 35.0, source: str = "client_web_serial") -> None:
    """Initializes camera capture session waiting for chunks from Client Web Serial."""
    global state
    with state.camera_lock:
        if state.camera_status == "capturing":
            raise SessionBusyError("A camera capture session is already in progress")

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

    def _timeout_guard() -> None:
        time.sleep(timeout)
        with state.camera_lock:
            if state.camera_status == "capturing":
                state.camera_status = "failed"
                state.camera_progress["error"] = f"Camera capture timed out after {timeout:.1f}s"
                print(f"[API WebUSB] Camera capture timed out after {timeout:.1f}s")

    threading.Thread(target=_timeout_guard, daemon=True, name="camera-timeout-guard").start()


# --- OpenAPI Specification Schema ---
OPENAPI_SCHEMA: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {
        "title": "RASCube Ground Station API",
        "description": "Client Web USB/Serial Ground Station REST API, Telemetry Ingestion & Realtime Streaming",
        "version": "2.0.0",
        "contact": {
            "name": "RASCube Ground Station Team",
        },
    },
    "servers": [
        {"url": "/", "description": "Ground Station Server"}
    ],
    "tags": [
        {"name": "Status", "description": "Ground station status & client connection monitor"},
        {"name": "Telemetry", "description": "Telemetry ingestion, history, snapshot & SSE real-time stream"},
        {"name": "Camera", "description": "Satellite camera capture, chunk ingestion & JPEG preview"},
        {"name": "Decoder", "description": "Standalone telemetry hex decoding utility"},
    ],
    "paths": {
        "/api/status": {
            "get": {
                "tags": ["Status"],
                "summary": "Get Ground Station Status",
                "description": "Returns client Web Serial connection status, target satellite ID, received sample counters, and camera state.",
                "responses": {
                    "200": {
                        "description": "Ground station status object",
                        "content": {
                            "application/json": {
                                "example": {
                                    "is_connected": True,
                                    "mode": "client_web_serial",
                                    "connected_port": "Client Web USB/Serial (Browser)",
                                    "serial_number": 1581,
                                    "total_samples_received": 142,
                                    "last_received_time": 1729000000.0,
                                    "camera_status": "idle",
                                    "error_message": None,
                                }
                            }
                        },
                    }
                },
            },
            "post": {
                "tags": ["Status"],
                "summary": "Update Client Connection State",
                "description": "Announces client browser Web Serial connection or disconnection to the ground station backend.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "is_connected": {"type": "boolean"},
                                    "serial_number": {"type": "integer"},
                                    "source": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Updated status object"}
                },
            },
        },
        "/api/telemetry/ingest": {
            "post": {
                "tags": ["Telemetry"],
                "summary": "Ingest Telemetry Frame from Web Serial",
                "description": "Accepts raw telemetry hex frame received by browser via Web Serial, decodes it, saves to history, and broadcasts to SSE clients.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["hex"],
                                "properties": {
                                    "hex": {"type": "string", "description": "Raw 121, 122, or 123 byte HEX telemetry packet"},
                                    "source": {"type": "string", "example": "client_web_serial"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Decoded telemetry payload"},
                    "422": {"description": "Invalid hex or decode error"},
                },
            }
        },
        "/api/telemetry/latest": {
            "get": {
                "tags": ["Telemetry"],
                "summary": "Get Latest Telemetry Snapshot",
                "description": "Returns the most recently decoded telemetry packet.",
                "responses": {
                    "200": {"description": "Latest telemetry sample"},
                    "503": {"description": "No telemetry data received yet"},
                },
            }
        },
        "/api/telemetry/history": {
            "get": {
                "tags": ["Telemetry"],
                "summary": "Get Telemetry History Buffer",
                "description": "Returns up to the last 200 telemetry samples.",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "default": 50},
                        "description": "Maximum number of samples to retrieve",
                    }
                ],
                "responses": {
                    "200": {"description": "List of telemetry samples"}
                },
            }
        },
        "/api/telemetry/stream": {
            "get": {
                "tags": ["Telemetry"],
                "summary": "Real-time Telemetry Stream (SSE)",
                "description": "Subscribes to live Server-Sent Events (SSE) telemetry data stream.",
                "responses": {
                    "200": {
                        "description": "Event stream of telemetry JSON packets",
                        "content": {"text/event-stream": {}},
                    }
                },
            }
        },
        "/api/camera/capture": {
            "post": {
                "tags": ["Camera"],
                "summary": "Initiate Camera Capture Session",
                "description": "Signals backend that a camera capture session is starting (awaits incoming chunks from Web Serial).",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "timeout": {"type": "number", "default": 35.0},
                                    "source": {"type": "string", "default": "client_web_serial"},
                                },
                            }
                        }
                    }
                },
                "responses": {
                    "202": {"description": "Camera capture initiated"}
                },
            }
        },
        "/api/camera/chunk/ingest": {
            "post": {
                "tags": ["Camera"],
                "summary": "Ingest Camera Chunk from Web Serial",
                "description": "Accepts 242-byte raw camera block from Web Serial, parses block index, updates progressive JPEG preview, and broadcasts chunk.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["hex"],
                                "properties": {
                                    "hex": {"type": "string"},
                                    "source": {"type": "string", "example": "client_web_serial"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Chunk ingested and partial JPEG updated"}
                },
            }
        },
        "/api/camera/status": {
            "get": {
                "tags": ["Camera"],
                "summary": "Get Camera Capture Status & Progress",
                "description": "Returns current camera transfer state, total blocks received, bytes, elapsed seconds, and recent chunks.",
                "responses": {
                    "200": {"description": "Camera progress information"}
                },
            }
        },
        "/api/camera/latest": {
            "get": {
                "tags": ["Camera"],
                "summary": "Get Latest Camera Image (JSON & Base64)",
                "description": "Returns the completed JPEG image encoded as base64 alongside capture metadata.",
                "responses": {
                    "200": {"description": "Image metadata and base64 string"},
                    "404": {"description": "No camera image available"},
                },
            }
        },
        "/api/camera/latest.jpg": {
            "get": {
                "tags": ["Camera"],
                "summary": "Download Latest Camera Image (Raw Binary JPEG)",
                "description": "Returns the raw binary JPEG image.",
                "responses": {
                    "200": {
                        "description": "JPEG binary data",
                        "content": {"image/jpeg": {}},
                    },
                    "404": {"description": "No camera image available"},
                },
            }
        },
        "/api/decode": {
            "get": {
                "tags": ["Decoder"],
                "summary": "Decode HEX via Query Parameter",
                "parameters": [
                    {
                        "name": "hex",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Raw hex telemetry string",
                    }
                ],
                "responses": {
                    "200": {"description": "Decoded telemetry JSON object"},
                    "422": {"description": "Invalid hex or decode error"},
                },
            },
            "post": {
                "tags": ["Decoder"],
                "summary": "Decode HEX via Request Body",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["hex"],
                                "properties": {
                                    "hex": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Decoded telemetry JSON object"},
                    "422": {"description": "Invalid hex or decode error"},
                },
            },
        },
    },
}


# --- Swagger UI HTML Template ---
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


# --- HTML Dashboard Template (Client Web USB / Web Serial Exclusive) ---
HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RASCube Ground Station - Client Web USB/Serial Dashboard</title>
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
    .status-badge { display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.35rem 0.85rem; border-radius: 9999px; font-size: 0.85rem; font-weight: 700; transition: all 0.3s ease; }
    .status-connected { background: rgba(16, 185, 129, 0.15); border: 1px solid var(--success); color: var(--success); }
    .status-disconnected { background: rgba(239, 68, 68, 0.15); border: 1px solid var(--danger); color: var(--danger); }
    .status-client { background: rgba(56, 189, 248, 0.15); border: 1px solid var(--accent); color: var(--accent); }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
    .card { background: var(--card-bg); backdrop-filter: blur(12px); border: 1px solid var(--card-border); border-radius: 16px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 10px 30px rgba(0,0,0,0.3); }
    .card h2 { font-size: 1.15rem; font-weight: 700; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.5rem; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr auto; gap: 1rem; align-items: end; }
    label { display: block; font-size: 0.8rem; font-weight: 600; color: var(--text-muted); margin-bottom: 0.4rem; }
    select, input { width: 100%; background: rgba(10, 15, 26, 0.8); border: 1px solid var(--card-border); border-radius: 8px; color: #fff; font-size: 0.9rem; padding: 0.65rem 0.85rem; outline: none; }
    select:focus, input:focus { border-color: var(--accent); }
    button { background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%); color: #fff; border: none; padding: 0.65rem 1.25rem; border-radius: 8px; font-weight: 600; font-size: 0.9rem; cursor: pointer; transition: all 0.2s; white-space: nowrap; }
    button:hover { filter: brightness(1.15); }
    .btn-danger { background: linear-gradient(135deg, #dc2626 0%, #991b1b 100%); }
    .btn-secondary { background: rgba(255,255,255,0.08); border: 1px solid var(--card-border); }
    .btn-secondary:hover { background: rgba(255,255,255,0.15); }
    .grid-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-top: 1rem; }
    .metric-box { background: rgba(15, 23, 42, 0.6); border: 1px solid var(--card-border); border-radius: 12px; padding: 1rem; }
    .metric-label { font-size: 0.72rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600; }
    .metric-value { font-size: 1.35rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; color: #fff; margin-top: 0.25rem; }
    .metric-sub { font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem; font-family: 'JetBrains Mono', monospace; }
    pre { background: rgba(5, 8, 15, 0.95); border: 1px solid var(--card-border); border-radius: 10px; padding: 1rem; font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; color: #a5f3fc; overflow-x: auto; max-height: 280px; }
    .note-box { background: rgba(56, 189, 248, 0.08); border-left: 3px solid var(--accent); padding: 0.75rem 1rem; border-radius: 6px; font-size: 0.85rem; color: #cbd5e1; margin-bottom: 1rem; line-height: 1.5; }
    .warning-box { background: rgba(245, 158, 11, 0.1); border-left: 3px solid var(--warning); padding: 0.75rem 1rem; border-radius: 6px; font-size: 0.85rem; color: #fde68a; margin-bottom: 1rem; line-height: 1.5; display: none; }
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
    @keyframes chunkPop {
      0% { transform: scale(0.4); opacity: 0; }
      70% { transform: scale(1.15); opacity: 1; box-shadow: 0 0 12px rgba(56, 189, 248, 0.8); }
      100% { transform: scale(1); }
    }
    #toast {
      position: fixed;
      bottom: 2rem;
      right: 2rem;
      background: #0284c7;
      color: #fff;
      padding: 0.75rem 1.25rem;
      border-radius: 8px;
      font-size: 0.88rem;
      font-weight: 600;
      box-shadow: 0 8px 24px rgba(0,0,0,0.5);
      display: none;
      z-index: 9999;
      animation: fadeIn 0.3s ease;
    }
    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1 style="font-size: 1.6rem; font-weight: 800;">🛰️ RASCube Ground Station</h1>
        <div style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.2rem;">Client Web USB / Web Serial Dashboard & Realtime API</div>
      </div>
      <div class="header-links">
        <a href="/docs" target="_blank" class="swagger-btn">📖 Swagger UI Docs</a>
        <div id="statusBadge" class="status-badge status-disconnected">
          <span class="status-dot"></span> <span id="statusText">Disconnected</span>
        </div>
      </div>
    </header>

    <!-- Client Web USB / Serial Connection Card -->
    <div class="card">
      <h2>🔌 Client Web USB / Web Serial Interface</h2>
      <div class="note-box">
        💻 <strong>Direct Browser Connection</strong>: Receiver USB Dongle terhubung langsung ke port USB laptop/komputer Anda. Browser (Chrome / Edge / Opera) membaca paket satelit secara real-time via Web Serial API dan otomatis menyinkronkannya ke Ground Station API & SSE Stream.
      </div>
      <div id="secureNotice" class="warning-box">
        🔒 <strong>Secure Context Required</strong>: Web Serial mewajibkan akses via HTTPS atau localhost. Jika Anda membuka dashboard ini dari IP server LAN (misal: <code>http://192.168.x.x:8080</code>), aktifkan flag Chrome: <br>
        <code>chrome://flags/#unsafely-treat-insecure-origin-as-secure</code> &rarr; Enabled &rarr; Tambahkan origin URL Anda &rarr; Relaunch browser. Atau gunakan mode HTTPS (ENABLE_SSL=1).
      </div>
      <div class="form-grid">
        <div>
          <label>Target Satellite Serial Number</label>
          <input type="number" id="clientSerialInput" value="1581" placeholder="e.g. 1581" />
        </div>
        <div>
          <label>Baud Rate</label>
          <input type="number" id="clientBaudInput" value="1000000" />
        </div>
        <button id="btnClientConnect" onclick="handleClientWebSerial()">🔌 Connect Browser USB</button>
      </div>

      <!-- Satellite Radio Uplink Controls (Active when connected) -->
      <div style="margin-top: 1.25rem; padding-top: 1rem; border-top: 1px solid var(--card-border);">
        <label style="margin-bottom: 0.6rem; color: #fff;">📡 Direct Satellite Radio Uplink Commands (via Browser Web Serial):</label>
        <div style="display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center;">
          <button class="btn-secondary" onclick="sendBlinkLed()" id="btnUplinkBlink">💡 Blink RGB LED</button>
          <button class="btn-secondary" onclick="sendStartupSong()" id="btnUplinkSong">🎵 Play Startup Song</button>
          <button class="btn-secondary" onclick="sendPing()" id="btnUplinkPing">📡 Ping Satellite (OBC Info)</button>
          <button class="btn-secondary" onclick="triggerCameraCapture()" id="btnUplinkCamera">📸 Capture Satellite Photo</button>
        </div>
      </div>
    </div>

    <!-- Live Telemetry Stream Section -->
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
      <pre id="jsonDisplay">// Waiting for telemetry data from Web Serial...</pre>
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

  <div id="toast"></div>

  <script>
    let isClientConnected = false;
    let clientPort = null;
    let clientReader = null;
    let sseSource = null;
    let cameraPollingInterval = null;
    let clientCaptureStartTime = null;
    const receivedChunks = new Set();

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 3000);
    }

    // Check secure context on load
    window.addEventListener('DOMContentLoaded', () => {
      if (!('serial' in navigator)) {
        if (location.protocol !== 'https:' && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
          document.getElementById('secureNotice').style.display = 'block';
        }
      }
      initSSE();
      checkBackendStatus();
      setInterval(checkBackendStatus, 3000);
    });

    function updateStatusBadge(connected, satNum) {
      const badge = document.getElementById('statusBadge');
      const text = document.getElementById('statusText');
      if (connected) {
        badge.className = 'status-badge status-connected';
        text.innerText = `Connected: Sat #${satNum || 1581}`;
      } else {
        badge.className = 'status-badge status-disconnected';
        text.innerText = 'Disconnected';
      }
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
        updateStatusBadge(false);
        fetch('/api/status', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ is_connected: false })
        }).catch(() => {});
        showToast('USB Receiver terputus.');
        return;
      }

      if (!('serial' in navigator)) {
        if (location.protocol !== 'https:' && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
          alert('🔒 Web Serial API mewajibkan "Secure Context" (HTTPS atau localhost) oleh standar keamanan browser.\\n\\nKarena Anda mengakses via IP (' + location.origin + '):\\n1. Buka tab baru di browser: chrome://flags/#unsafely-treat-insecure-origin-as-secure\\n2. Ubah menjadi "Enabled"\\n3. Masukkan URL: ' + location.origin + '\\n4. Klik tombol "Relaunch" di kanan bawah.\\n\\nSetelah itu, Web Serial akan aktif penuh!');
        } else {
          alert('Browser Anda belum mendukung Web Serial API. Silakan gunakan Google Chrome, Edge, atau Opera.');
        }
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
        updateStatusBadge(true, satNum);

        fetch('/api/status', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ is_connected: true, serial_number: satNum, source: 'client_web_serial' })
        }).catch(() => {});

        showToast(`Terhubung ke Satelit #${satNum} via Web Serial!`);
        readClientSerialLoop();
      } catch (err) {
        alert('Koneksi Web Serial gagal: ' + err.message);
      }
    }

    async function sendClientCommand(portByte, payloadBytes = []) {
      if (!isClientConnected || !clientPort || !clientPort.writable) {
        alert('Harap hubungkan Browser USB / Serial terlebih dahulu!');
        return false;
      }
      try {
        const frame = new Uint8Array(2 + payloadBytes.length);
        frame[0] = portByte;
        frame[1] = payloadBytes.length;
        if (payloadBytes.length > 0) {
          frame.set(payloadBytes, 2);
        }
        const writer = clientPort.writable.getWriter();
        await writer.write(frame);
        writer.releaseLock();
        return true;
      } catch (err) {
        alert('Gagal mengirim perintah: ' + err.message);
        return false;
      }
    }

    async function sendBlinkLed() {
      const ok = await sendClientCommand(0x80, [0xFF, 0x00, 0x00]); // Red LED
      if (ok) showToast('💡 Perintah Blink RGB LED terkirim ke satelit!');
    }

    async function sendStartupSong() {
      const ok = await sendClientCommand(0x84, [0x00]); // Startup Song
      if (ok) showToast('🎵 Perintah Play Startup Song terkirim ke satelit!');
    }

    async function sendPing() {
      const ok = await sendClientCommand(0x12, [0x00]); // OBC Info / Ping
      if (ok) showToast('📡 Perintah Ping (OBC Info) terkirim ke satelit!');
    }

    async function triggerCameraCapture() {
      if (!isClientConnected || !clientPort || !clientPort.writable) {
        alert('Harap hubungkan Browser USB / Serial terlebih dahulu untuk mengambil foto satelit!');
        return;
      }
      const btn = document.getElementById('btnCameraCapture');
      btn.disabled = true;
      btn.innerText = '⏳ Triggering...';

      // Reset visualizer
      receivedChunks.clear();
      document.getElementById('chunkMatrix').innerHTML = '';
      document.getElementById('chunkStreamLog').innerHTML = '';
      document.getElementById('chunkCountLabel').innerText = '0 chunks';
      document.getElementById('cameraProgressBar').style.width = '0%';
      document.getElementById('cameraSpeedMetric').innerText = 'Speed: 0 B/s | Rate: 0 blk/s';
      clientCaptureStartTime = Date.now();

      try {
        const ok = await sendClientCommand(0x13, [0x00]); // HostPort.OBC_CAMERA
        if (!ok) {
          btn.disabled = false;
          btn.innerText = '📸 Trigger Camera Capture';
          return;
        }

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
      } catch (err) {
        alert('Camera request error: ' + err.message);
        btn.disabled = false;
        btn.innerText = '📸 Trigger Camera Capture';
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
                const isControl = (port === 0x00 || port === 0x01 || port === 0x02 || port === 0x03 || port === 0x0A || port === 0x12 || port === 0x13 || port === 0x80 || port === 0x84);

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

                    fetch('/api/camera/chunk/ingest', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ hex, source: 'client_web_serial' })
                    })
                    .then(r => r.json())
                    .then(res => {
                      if (res.chunk && res.chunk.partial_jpeg_base64) {
                        const img = document.getElementById('cameraImgPreview');
                        if (img) {
                          img.src = 'data:image/jpeg;base64,' + res.chunk.partial_jpeg_base64;
                          img.style.display = 'block';
                          document.getElementById('cameraPlaceholder').style.display = 'none';
                        }
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

    function renderCameraChunk(chunk) {
      if (!receivedChunks.has(chunk.index)) {
        receivedChunks.add(chunk.index);
        const matrix = document.getElementById('chunkMatrix');
        const badge = document.createElement('span');
        badge.className = 'chunk-badge';
        badge.id = `chunk-blk-${chunk.index}`;
        badge.innerText = `#${chunk.index}`;
        badge.title = `Block ${chunk.index} (${chunk.size} bytes)`;
        matrix.appendChild(badge);
        document.getElementById('chunkCountLabel').innerText = `${receivedChunks.size} chunks`;
      }

      const streamLog = document.getElementById('chunkStreamLog');
      const logLine = document.createElement('div');
      logLine.innerText = `[${new Date().toLocaleTimeString()}] Block #${chunk.index} | Size: ${chunk.size}B | Hex: ${chunk.hex_preview}...`;
      streamLog.prepend(logLine);

      const estimatedTotal = 75;
      const pct = Math.min(98, Math.round((receivedChunks.size / estimatedTotal) * 100));
      document.getElementById('cameraProgressBar').style.width = pct + '%';
      document.getElementById('cameraProgressDetails').innerText = `Received ${receivedChunks.size} blocks (${chunk.total_bytes} bytes)`;
      if (chunk.elapsed_seconds > 0) {
        const bps = Math.round(chunk.total_bytes / chunk.elapsed_seconds);
        const rate = (receivedChunks.size / chunk.elapsed_seconds).toFixed(1);
        document.getElementById('cameraSpeedMetric').innerText = `Speed: ${bps} B/s | Rate: ${rate} blk/s | ${chunk.elapsed_seconds.toFixed(1)}s`;
      }
    }

    async function pollCameraStatus() {
      try {
        const res = await fetch('/api/camera/status');
        const data = await res.json();
        const statusText = document.getElementById('cameraStatusText');
        const progressBar = document.getElementById('cameraProgressBar');

        if (data.status === 'capturing') {
          const blocks = data.progress.blocks_received || receivedChunks.size || 0;
          statusText.innerText = `Status: Capturing (${blocks} blocks)...`;
        } else if (data.status === 'completed') {
          if (cameraPollingInterval) clearInterval(cameraPollingInterval);
          cameraPollingInterval = null;
          statusText.innerText = `Status: Complete (${data.metadata ? data.metadata.block_count : 0} blocks)`;
          progressBar.style.width = '100%';
          const btn = document.getElementById('btnCameraCapture');
          btn.disabled = false;
          btn.innerText = '📸 Trigger Camera Capture';
          fetchLatestCameraImage();
        } else if (data.status === 'failed') {
          if (cameraPollingInterval) clearInterval(cameraPollingInterval);
          cameraPollingInterval = null;
          statusText.innerText = `Status: Failed (${data.progress.error || 'Unknown error'})`;
          const btn = document.getElementById('btnCameraCapture');
          btn.disabled = false;
          btn.innerText = '📸 Trigger Camera Capture';
        }
      } catch (e) {}
    }

    async function fetchLatestCameraImage() {
      try {
        const res = await fetch('/api/camera/latest');
        if (!res.ok) return;
        const data = await res.json();
        const img = document.getElementById('cameraImgPreview');
        const meta = document.getElementById('cameraMetaInfo');
        const placeholder = document.getElementById('cameraPlaceholder');

        if (data.jpeg_base64) {
          img.src = 'data:image/jpeg;base64,' + data.jpeg_base64;
          img.style.display = 'block';
          placeholder.style.display = 'none';

          if (data.metadata) {
            meta.style.display = 'block';
            meta.innerText = `Size: ${data.metadata.byte_length} bytes | Blocks: ${data.metadata.block_count} | Duration: ${data.metadata.capture_duration_seconds}s`;
          }
        }
      } catch (e) {}
    }

    function renderTelemetry(data) {
      if (!data) return;
      document.getElementById('valSeq').innerText = '#' + (data.packet_sequence ?? '-');
      document.getElementById('valUptime').innerText = 'Uptime: ' + (data.uptime_seconds != null ? data.uptime_seconds + 's' : '-');
      document.getElementById('valTemp').innerText = (data.environment && data.environment.temperature_c != null) ? data.environment.temperature_c.toFixed(1) + ' °C' : '-';
      document.getElementById('valPres').innerText = (data.environment && data.environment.pressure_hpa != null) ? data.environment.pressure_hpa.toFixed(1) + ' hPa' : '-';
      document.getElementById('valBatt').innerText = (data.eps && data.eps.battery_charge) ? data.eps.battery_charge.bus_voltage_v.toFixed(2) + ' V' : '-';
      document.getElementById('valRails').innerText = (data.eps) ? `5V: ${data.eps.main_5v_v.toFixed(2)}V | 3.3V: ${data.eps.main_3v3_v.toFixed(2)}V` : '-';
      document.getElementById('valGpsCoords').innerText = (data.gps) ? `${data.gps.latitude.toFixed(4)}, ${data.gps.longitude.toFixed(4)}` : '-';
      document.getElementById('valGpsStatus').innerText = (data.gps) ? `${data.gps.fix ? 'Fix OK' : 'No Fix'} (${data.gps.satellites} sats)` : '-';
      document.getElementById('valAccel').innerText = (data.imu && data.imu.accelerometer_g) ? `${data.imu.accelerometer_g.x.toFixed(2)}, ${data.imu.accelerometer_g.y.toFixed(2)}, ${data.imu.accelerometer_g.z.toFixed(2)}` : '-';
      document.getElementById('valSignal').innerText = (data.receiver_rssi != null) ? data.receiver_rssi.toFixed(1) + ' dBm' : '-';
      document.getElementById('valSnr').innerText = (data.receiver_snr != null) ? 'SNR: ' + data.receiver_snr.toFixed(2) + ' dB' : '-';
      document.getElementById('jsonDisplay').innerText = JSON.stringify(data, null, 2);
    }

    function initSSE() {
      if (sseSource) sseSource.close();
      sseSource = new EventSource('/api/telemetry/stream');
      sseSource.onmessage = (e) => {
        try {
          const item = JSON.parse(e.data);
          if (item.type === 'camera_chunk') {
            renderCameraChunk(item);
          } else {
            renderTelemetry(item);
          }
        } catch (err) {}
      };
      sseSource.onerror = () => {
        // SSE reconnects automatically
      };
    }

    async function checkBackendStatus() {
      if (isClientConnected) return;
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        updateStatusBadge(data.is_connected, data.serial_number);
      } catch (e) {}
    }
  </script>
</body>
</html>
"""


# --- HTTP Request Handler ---
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

    def do_HEAD(self) -> None:
        self.do_GET()

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

        # Favicon
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
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

        # 4. Connection & Ground Station Status
        if path == "/api/status":
            with state.lock:
                status_data = {
                    "is_connected": state.is_connected,
                    "mode": "client_web_serial",
                    "connected_port": state.connected_port or ("Client Web USB/Serial (Browser)" if state.is_connected else None),
                    "serial_number": state.serial_number,
                    "total_samples_received": state.total_samples_received,
                    "last_received_time": state.last_received_time,
                    "camera_status": state.camera_status,
                    "error_message": state.error_message,
                }
            self._send_json(HTTPStatus.OK, status_data)
            return

        # 5. Latest Telemetry Snapshot
        if path == "/api/telemetry/latest":
            with state.lock:
                if state.latest_sample is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "No telemetry data received yet. Connect via Web Serial in dashboard."},
                    )
                    return
                self._send_json(HTTPStatus.OK, state.latest_sample)
            return

        # 6. Telemetry History Buffer
        if path == "/api/telemetry/history":
            limit = int(query.get("limit", [50])[0])
            with state.lock:
                items = list(state.history)[-limit:]
            self._send_json(HTTPStatus.OK, {"count": len(items), "samples": items})
            return

        # 7. Realtime Server-Sent Events (SSE) Stream
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
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                state.remove_subscriber(sub_queue)
            return

        # 8. Manual Decode via Query Parameter
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

        # 9. Camera Capture Status
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

        # 10. Latest Camera Image (JSON & Base64)
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

        # 11. Latest Camera Image (Raw JPEG Binary)
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

        # Legacy / Informational endpoints
        if path == "/api/ports":
            self._send_json(HTTPStatus.OK, {
                "ports": [],
                "notice": "Server COM scanning disabled. Ground station uses Client Web USB/Serial only."
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

        # 1. Update Connection Status from Client Browser
        if path == "/api/status":
            conn = bool(body_json.get("is_connected", True))
            sat = int(body_json.get("serial_number", state.serial_number))
            source = str(body_json.get("source", "client_web_serial"))
            with state.lock:
                state.is_connected = conn
                state.connected_source = source if conn else None
                state.serial_number = sat
                state.connected_port = "Client Web USB/Serial (Browser)" if conn else None
            self._send_json(HTTPStatus.OK, {
                "status": "updated",
                "is_connected": state.is_connected,
                "serial_number": state.serial_number,
                "source": state.connected_source,
            })
            return

        # 2. Ingest Telemetry or Camera Chunks from Client Web Serial
        if path in ("/api/telemetry/ingest", "/api/camera/chunk/ingest"):
            hex_data = body_json.get("hex") or body_json.get("payload") or raw_body.strip().strip('"')
            if not hex_data:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'hex' in request body"})
                return
            try:
                raw_bytes = bytes.fromhex(hex_data)
                port = raw_bytes[0] if len(raw_bytes) > 0 else None

                # Camera Block (InboundPort.JPEG_CAMERA = 0x15 or 0x20)
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

                # Standard Telemetry Frame (0x10)
                decoded = decode_telemetry_to_dict(hex_data)
                decoded["raw_hex"] = hex_data if isinstance(hex_data, str) else hex_data.hex().upper()
                decoded["timestamp"] = time.time()
                decoded["source"] = body_json.get("source", "client_web_serial")
                with state.lock:
                    state.is_connected = True
                    state.connected_source = "client_web_serial"
                state.broadcast_telemetry(decoded)
                self._send_json(HTTPStatus.OK, {"status": "ingested", "telemetry": decoded})
            except (ProtocolDecodeError, ValueError) as exc:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            return

        # 3. Trigger Camera Capture Session
        if path == "/api/camera/capture":
            timeout_val = float(body_json.get("timeout", 35.0))
            source_val = str(body_json.get("source", "client_web_serial"))
            try:
                trigger_camera_capture(timeout=timeout_val, source=source_val)
                self._send_json(HTTPStatus.ACCEPTED, {
                    "status": "capturing",
                    "source": source_val,
                    "message": f"Camera capture session initiated (awaiting chunks from Web Serial)",
                    "check_status_url": "/api/camera/status",
                })
            except SessionBusyError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        # 4. Standalone HEX Decode
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

        # Legacy / inform endpoints
        if path in ("/api/connect", "/api/disconnect"):
            self._send_json(HTTPStatus.OK, {
                "notice": "Server COM connection disabled. Ground station uses Client Web USB/Serial directly in the browser."
            })
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="RASCube Ground Station REST API Server (Client Web USB/Serial)")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS (auto-generates self-signed TLS cert if none provided)")
    parser.add_argument("--ssl-cert", default=None, help="Path to custom SSL certificate (.pem / .crt)")
    parser.add_argument("--ssl-key", default=None, help="Path to custom SSL private key (.key)")

    args = parser.parse_args()

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.host, args.port), GroundStationAPIHandler)

    proto = "http"
    enable_ssl = args.ssl or os.environ.get("ENABLE_SSL", "0").lower() in ("1", "true", "yes")
    if enable_ssl or args.ssl_cert:
        cert_file = args.ssl_cert
        key_file = args.ssl_key

        if not cert_file:
            # Auto-generate temporary self-signed certificate using OpenSSL
            cert_dir = os.path.expanduser("~/.rascube/ssl")
            os.makedirs(cert_dir, exist_ok=True)
            cert_file = os.path.join(cert_dir, "server.crt")
            key_file = os.path.join(cert_dir, "server.key")
            if not (os.path.exists(cert_file) and os.path.exists(key_file)):
                try:
                    subprocess.run([
                        "openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-keyout", key_file, "-out", cert_file,
                        "-days", "365", "-nodes",
                        "-subj", "/CN=rascube-groundstation"
                    ], check=True, capture_output=True)
                except Exception as e:
                    print(f"[Warning] Failed to auto-generate SSL cert: {e}")

        if cert_file and os.path.exists(cert_file) and key_file and os.path.exists(key_file):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            proto = "https"

    url = f"{proto}://localhost:{args.port}"
    print("=" * 70)
    print(f"🚀 RASCube Ground Station Running ({proto.upper()}) [Client Web USB/Serial Mode]")
    print(f"💻 Live Dashboard         : {url}/")
    print(f"📖 Swagger UI Docs        : {url}/docs")
    print(f"📄 OpenAPI Specification  : {url}/openapi.json")
    print("=" * 70)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down ground station server...")
        server.server_close()


if __name__ == "__main__":
    main()
