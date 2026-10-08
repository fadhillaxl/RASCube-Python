# RASCube Ground Station REST API & Realtime Streaming Tutorial

This tutorial explains how to run, test, and integrate the **RASCube Ground Station Server** (`server.py`). The server uses an exclusive **Client Web USB / Web Serial** architecture, allowing users to connect their USB receiver dongle directly to the browser (Chrome, Edge, Opera) without needing any USB device passthrough or privileged permissions on the backend or Docker host.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites & Installation](#prerequisites--installation)
3. [Running the API Server](#running-the-api-server)
4. [Interactive Swagger UI & Web Dashboard](#interactive-swagger-ui--web-dashboard)
5. [Client Web Serial Connection Guide](#client-web-serial-connection-guide)
6. [Satellite Radio Uplink Commands](#satellite-radio-uplink-commands)
7. [API Endpoint Reference](#api-endpoint-reference)
   - [1. Check Ground Station Status](#1-check-ground-station-status)
   - [2. Ingest Telemetry from Client Web Serial](#2-ingest-telemetry-from-client-web-serial)
   - [3. Get Latest Telemetry Snapshot](#3-get-latest-telemetry-snapshot)
   - [4. Get Telemetry History Buffer](#4-get-telemetry-history-buffer)
   - [5. Realtime Live Stream (SSE)](#5-realtime-live-stream-sse)
   - [6. Trigger Camera Capture Session](#6-trigger-camera-capture-session)
   - [7. Ingest Camera Chunk from Web Serial](#7-ingest-camera-chunk-from-web-serial)
   - [8. Get Camera Capture Status](#8-get-camera-capture-status)
   - [9. Get Latest Camera Image](#9-get-latest-camera-image)
   - [10. Decode Raw HEX Telemetry](#10-decode-raw-hex-telemetry)
8. [Frontend Integration Guide (React / Vite / JavaScript)](#frontend-integration-guide-react--vite--javascript)
9. [Secure Context & Remote IP Access](#secure-context--remote-ip-access)

---

## Architecture Overview

In this architecture:
- **Client (Browser)**: Connects directly to the RASCube USB Dongle via Web Serial (`navigator.serial`). It filters by satellite number, decodes incoming frames in real-time for zero-latency local rendering, and sends uplink commands (Blink LED, Song, Ping, Camera Capture).
- **Backend API (`server.py`)**: Acts as the central coordinator and data hub. It ingests decoded telemetry and camera blocks (`/api/telemetry/ingest`, `/api/camera/chunk/ingest`), persists history, provides REST endpoints, and broadcasts live events via Server-Sent Events (SSE).
- **Docker / Cloud Friendly**: Since all USB communications happen on the client machine, the Docker container does **not** need `/dev/bus/usb` access or root privileges.

---

## Prerequisites & Installation

Make sure your virtual environment is activated and the package is installed:

```bash
# 1. Activate virtual environment
source .venv/bin/activate

# 2. Ensure package is installed in editable mode
pip install -e .
```

---

## Running the API Server

Start the server using:

```bash
python examples/api/server.py --port 8080
```

### Docker Deployment
```bash
docker compose up -d
```

### Command Line Options

| Argument | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Host network interface to bind to |
| `--port` | `8080` | Port number to listen on |
| `--ssl` | `False` | Enable HTTPS with automatic self-signed certificate |
| `--ssl-cert` | `None` | Path to custom SSL certificate (.pem / .crt) |
| `--ssl-key` | `None` | Path to custom SSL private key (.key) |

---

## Interactive Swagger UI & Web Dashboard

Open the following URLs in your browser:

- **Swagger UI Interactive Docs**: [http://localhost:8080/docs](http://localhost:8080/docs)
- **OpenAPI 3.0 JSON Schema**: [http://localhost:8080/openapi.json](http://localhost:8080/openapi.json)
- **Ground Station Web UI**: [http://localhost:8080/](http://localhost:8080/)

---

## Client Web Serial Connection Guide

1. Open the dashboard at `http://localhost:8080/` (or your remote server IP/domain).
2. Plug the **RASCube USB Receiver Dongle** into your computer's USB port.
3. Enter your **Satellite Serial Number** (e.g. `1581`).
4. Click **"🔌 Connect Browser USB"**.
5. Select the **RASCube Receiver** (`USB VID: 0x0483, PID: 0x5740`) in the browser permission prompt.
6. The browser opens the serial port at 1,000,000 baud, sets the satellite filter header, reads frames in real-time, and synchronizes data with the backend API.

---

## Satellite Radio Uplink Commands

When connected via Web Serial, the dashboard provides direct uplink buttons:

- **💡 Blink RGB LED**: Sends `[0x80, 0x03, 0xFF, 0x00, 0x00]` to the satellite.
- **🎵 Play Startup Song**: Sends `[0x84, 0x01, 0x00]` to sound the onboard buzzer.
- **📡 Ping Satellite (OBC Info)**: Sends `[0x12, 0x01, 0x00]` to poll onboard computer info.
- **📸 Capture Satellite Photo**: Sends `[0x13, 0x01, 0x00]` to trigger camera photo acquisition and stream 242-byte blocks.

---

## API Endpoint Reference

### 1. Check Ground Station Status

- **Method**: `GET`
- **URL**: `/api/status`
- **Example Response (200 OK)**:
  ```json
  {
    "is_connected": true,
    "mode": "client_web_serial",
    "connected_port": "Client Web USB/Serial (Browser)",
    "serial_number": 1581,
    "total_samples_received": 142,
    "last_received_time": 1729000000.0,
    "camera_status": "idle",
    "error_message": null
  }
  ```

---

### 2. Ingest Telemetry from Client Web Serial

- **Method**: `POST`
- **URL**: `/api/telemetry/ingest`
- **Headers**: `Content-Type: application/json`
- **Body**:
  ```json
  {
    "hex": "10796C3100008E13EC0CFB0FFA0FFB0FD80F000090070000D00FFA0020010000F0000000A001000000000D591A00ABFF2E0B0700360139A6C4474049A43F000014010DBFFFFF000005009670C8C0EE9DD5429A99154200000000000000009A99993F0801D36237C2636FAD41C4981B440000090000F8C100005441",
    "source": "client_web_serial"
  }
  ```
- **Example Response (200 OK)**:
  ```json
  {
    "status": "ingested",
    "telemetry": {
      "packet_sequence": 12652,
      "device_uptime_ms": 1726733,
      "environment": { "temperature_c": 31.0, "pressure_hpa": 1006.84 },
      "eps": { "...": "..." },
      "imu": { "...": "..." },
      "gps": { "...": "..." },
      "timestamp": 1787574341.26
    }
  }
  ```

---

### 3. Get Latest Telemetry Snapshot

- **Method**: `GET`
- **URL**: `/api/telemetry/latest`

---

### 4. Get Telemetry History Buffer

- **Method**: `GET`
- **URL**: `/api/telemetry/history?limit=50`

---

### 5. Realtime Live Stream (SSE)

- **Method**: `GET`
- **URL**: `/api/telemetry/stream`
- **Response Format**: `text/event-stream`

---

### 6. Trigger Camera Capture Session

- **Method**: `POST`
- **URL**: `/api/camera/capture`
- **Body**: `{"timeout": 35.0, "source": "client_web_serial"}`

---

### 7. Ingest Camera Chunk from Web Serial

- **Method**: `POST`
- **URL**: `/api/camera/chunk/ingest`
- **Body**: `{"hex": "<raw 242-byte block hex>", "source": "client_web_serial"}`

---

### 8. Get Camera Capture Status

- **Method**: `GET`
- **URL**: `/api/camera/status`

---

### 9. Get Latest Camera Image

- **JSON & Base64**: `GET /api/camera/latest`
- **Raw Binary JPEG**: `GET /api/camera/latest.jpg`

---

### 10. Decode Raw HEX Telemetry

- **Method**: `POST` (or `GET /api/decode?hex=...`)
- **URL**: `/api/decode`
- **Body**: `{"hex": "10796C3100008E13EC0C..."}`

---

## Frontend Integration Guide (React / Vite / JavaScript)

### Realtime Dashboard Hook (React Example)

```jsx
import { useEffect, useState } from "react";

export function useSatelliteTelemetry() {
  const [telemetry, setTelemetry] = useState(null);
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    // 1. Check status
    fetch("/api/status")
      .then((res) => res.json())
      .then((data) => setIsConnected(data.is_connected));

    // 2. Open Realtime SSE Stream
    const eventSource = new EventSource("/api/telemetry/stream");

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type !== "camera_chunk") {
          setTelemetry(data);
          setIsConnected(true);
        }
      } catch (err) {
        console.error("Failed to parse telemetry event", err);
      }
    };

    return () => {
      eventSource.close();
    };
  }, []);

  return { telemetry, isConnected };
}
```

---

## Secure Context & Remote IP Access

The Web Serial API requires a **Secure Context** (HTTPS or localhost) in Chromium browsers.

When accessing from a remote IP address (e.g. `http://192.168.123.176:8080`):
1. Open `chrome://flags/#unsafely-treat-insecure-origin-as-secure` in Chrome.
2. Enable the flag and add your server URL (e.g. `http://192.168.123.176:8080`).
3. Click **Relaunch**.
4. Or enable HTTPS directly using `ENABLE_SSL=1` in `docker-compose.yml`.
