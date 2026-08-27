# RASCubeV2 Ground Station REST API & Realtime Streaming Tutorial

This tutorial explains how to run, test, and integrate the **RASCubeV2 Ground Station REST API Server** (`server.py`). The server provides HTTP endpoints for serial port discovery, satellite connection management, manual HEX packet decoding, and real-time telemetry streaming via **Server-Sent Events (SSE)**.

---

## Table of Contents

1. [Features](#features)
2. [Prerequisites & Installation](#prerequisites--installation)
3. [Running the API Server](#running-the-api-server)
4. [Interactive Swagger UI & Web Dashboard](#interactive-swagger-ui--web-dashboard)
5. [API Endpoint Reference](#api-endpoint-reference)
   - [1. List Available Ports](#1-list-available-ports)
   - [2. Connect to Satellite (Server Host Mode)](#2-connect-to-satellite-server-host-mode)
   - [3. Disconnect from Satellite](#3-disconnect-from-satellite)
   - [4. Check Connection Status](#4-check-connection-status)
   - [5. Get Latest Telemetry (Snapshot)](#5-get-latest-telemetry-snapshot)
   - [6. Get Telemetry History Buffer](#6-get-telemetry-history-buffer)
   - [7. Realtime Live Stream (SSE)](#7-realtime-live-stream-sse)
   - [8. Ingest Telemetry from Client Web Serial](#8-ingest-telemetry-from-client-web-serial)
   - [9. Decode Raw HEX Telemetry](#9-decode-raw-hex-telemetry)
6. [Client Web Serial Guide (Connecting USB from User Browser)](#client-web-serial-guide-connecting-usb-from-user-browser)
7. [Frontend Integration Guide (React / Vite / JavaScript)](#frontend-integration-guide-react--vite--javascript)
8. [Troubleshooting & FAQs](#troubleshooting--faqs)

---

## Features

- 🔌 **Zero External Web Dependencies**: Uses Python's built-in `http.server` and threading. No extra frameworks required.
- 💻 **Client Web Serial (Browser USB)**: Direct USB receiver reading via Chrome/Edge Web Serial API, with automatic ingestion to the backend.
- 📖 **Interactive Swagger UI**: Full OpenAPI 3.0 documentation available at `/docs`.
- ⚡ **Realtime Streaming (SSE)**: Native Server-Sent Events stream for instant updates in frontend dashboards without polling overhead.
- 🌐 **Cross-Origin Enabled (CORS)**: Pre-configured CORS headers allow any React/Vite/Vue frontend to communicate seamlessly.
- 🛠️ **Dual Mode Telemetry**: Supports live satellite stream from hardware USB receiver (Server or Client) and standalone offline HEX string decoding.

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

### Command Line Options

| Argument | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Host network interface to bind to |
| `--port` | `8080` | Port number to listen on |

---

## Interactive Swagger UI & Web Dashboard

Once the server is running, open the following URLs in your browser:

- **Swagger UI Interactive Docs**: [http://localhost:8080/docs](http://localhost:8080/docs)  
  *(Allows testing all endpoints directly with "Try it out" and "Execute" buttons)*
- **OpenAPI 3.0 JSON Schema**: [http://localhost:8080/openapi.json](http://localhost:8080/openapi.json)
- **Built-in Ground Station Web UI**: [http://localhost:8080/](http://localhost:8080/)  
  *(A ready-to-use monitor to select ports, connect, and view live graphs)*

---

## API Endpoint Reference

### 1. List Available Ports

Scans all serial/COM ports on the host and flags RASCube USB receivers (`is_rascube: true`).

- **Method**: `GET`
- **URL**: `/api/ports`
- **Example cURL**:
  ```bash
  curl -s http://localhost:8080/api/ports
  ```
- **Example Response (200 OK)**:
  ```json
  {
    "ports": [
      {
        "device": "/dev/cu.usbmodem20623154594D1",
        "description": "RASCubeV2 Receiver",
        "vid": 1155,
        "pid": 22336,
        "serial_number": "20623154594D",
        "is_rascube": true
      }
    ]
  }
  ```

---

### 2. Connect to Satellite

Connects to the specified serial port, sets the target satellite serial number, and starts background telemetry ingestion.

- **Method**: `POST`
- **URL**: `/api/connect`
- **Headers**: `Content-Type: application/json`
- **Body**:
  ```json
  {
    "port": "/dev/cu.usbmodem20623154594D1",
    "serial_number": 1581
  }
  ```
- **Example cURL**:
  ```bash
  curl -X POST http://localhost:8080/api/connect \
       -H "Content-Type: application/json" \
       -d '{"port": "/dev/cu.usbmodem20623154594D1", "serial_number": 1581}'
  ```
- **Example Response (200 OK)**:
  ```json
  {
    "status": "connected",
    "port": "/dev/cu.usbmodem20623154594D1",
    "serial_number": 1581,
    "receiver_info": {
      "software_version": 7,
      "git_hash": null,
      "dirty": false
    },
    "obc_info": {
      "stm": { "software_version": 9, "git_hash": null, "dirty": false },
      "arduino": { "software_version": 9, "git_hash": null, "dirty": false },
      "arduino_info_cached": false
    },
    "error": null
  }
  ```

---

### 3. Disconnect from Satellite

Closes the active serial connection and halts background streaming.

- **Method**: `POST`
- **URL**: `/api/disconnect`
- **Example cURL**:
  ```bash
  curl -X POST http://localhost:8080/api/disconnect
  ```
- **Example Response (200 OK)**:
  ```json
  {
    "status": "disconnected"
  }
  ```

---

### 4. Check Connection Status

Returns current connection status, port, satellite number, and total received sample counts.

- **Method**: `GET`
- **URL**: `/api/status`
- **Example cURL**:
  ```bash
  curl -s http://localhost:8080/api/status
  ```
- **Example Response (200 OK)**:
  ```json
  {
    "is_connected": true,
    "connected_port": "/dev/cu.usbmodem20623154594D1",
    "serial_number": 1581,
    "total_samples_received": 1420,
    "last_received_time": 1787574341.26,
    "error_message": null
  }
  ```

---

### 5. Get Latest Telemetry (Snapshot)

Fetches the most recently decoded telemetry sample from the satellite.

- **Method**: `GET`
- **URL**: `/api/telemetry/latest`
- **Example cURL**:
  ```bash
  curl -s http://localhost:8080/api/telemetry/latest
  ```
- **Example Response (200 OK)**:
  ```json
  {
    "packet_sequence": 12652,
    "device_uptime_ms": 1726733,
    "barometer": {
      "temperature_c": 31.0,
      "pressure_hpa": 1006.84,
      "altitude_m": 1.28
    },
    "eps": {
      "main_5v_v": 5.006,
      "main_3v3_v": 3.308,
      "solar_ldr_raw": [4091, 4090, 4091],
      "battery_charge": { "bus_voltage_v": 4.056, "current_a": 0.0 },
      "usb": { "bus_voltage_v": 1.936, "current_a": 0.0 },
      "battery_draw": { "bus_voltage_v": 4.048, "current_a": 0.25 },
      "solar": [
        { "bus_voltage_v": 0.288, "current_a": 0.0 },
        { "bus_voltage_v": 0.240, "current_a": 0.0 },
        { "bus_voltage_v": 0.416, "current_a": 0.0 }
      ],
      "charging_complete": false,
      "charge_power_good": false
    },
    "imu": {
      "magnetometer_gauss": { "x": -0.021, "y": 0.699, "z": 0.002 },
      "accelerometer_g": { "x": 0.000, "y": 0.017, "z": -1.014 },
      "gyroscope_dps": { "x": -0.018, "y": 0.000, "z": 0.088 },
      "orientation_degrees": { "x": -45.85, "y": 21.68, "z": 622.39 }
    },
    "gps": {
      "latitude": -6.263743,
      "longitude": 106.808456,
      "altitude_m": 37.4,
      "speed_raw": 0.0,
      "course_degrees": 0.0,
      "hdop": 1.2,
      "satellites": 8,
      "fix": true
    },
    "error_code": 0,
    "stm_version": 9,
    "receiver_rssi": -31.0,
    "receiver_snr": 13.25,
    "raw_hex": "10796C3100008E13EC0C...",
    "timestamp": 1787574341.26
  }
  ```

---

### 6. Get Telemetry History Buffer

Returns the last $N$ telemetry samples from memory for charting or graphing.

- **Method**: `GET`
- **URL**: `/api/telemetry/history?limit=50`
- **Query Parameter**: `limit` (optional, default: `50`, max: `200`)
- **Example cURL**:
  ```bash
  curl -s "http://localhost:8080/api/telemetry/history?limit=10"
  ```
- **Example Response (200 OK)**:
  ```json
  {
    "count": 10,
    "samples": [
      { "packet_sequence": 12643, "...": "..." },
      { "packet_sequence": 12652, "...": "..." }
    ]
  }
  ```

---

### 7. Realtime Live Stream (SSE)

Provides a persistent Server-Sent Events (SSE) stream pushing new telemetry samples as soon as they arrive from the satellite.

- **Method**: `GET`
- **URL**: `/api/telemetry/stream`
- **Header**: `Accept: text/event-stream`
- **Example cURL**:
  ```bash
  curl -N http://localhost:8080/api/telemetry/stream
  ```
- **Stream Output Format**:
  ```text
  data: {"packet_sequence": 12652, "device_uptime_ms": 1726733, "barometer": {...}, ...}

  data: {"packet_sequence": 12653, "device_uptime_ms": 1726866, "barometer": {...}, ...}
  ```

---

### 8. Ingest Telemetry from Client Web Serial

Allows a browser client reading local USB via the Web Serial API to push raw telemetry packets to the server. The server parses the packet, saves it to memory history, and broadcasts it to all connected SSE clients.

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
- **Example cURL**:
  ```bash
  curl -X POST http://localhost:8080/api/telemetry/ingest \
       -H "Content-Type: application/json" \
       -d '{"hex": "10796C3100008E13EC0C...", "source": "client_web_serial"}'
  ```
- **Example Response (200 OK)**:
  ```json
  {
    "status": "ingested",
    "telemetry": {
      "packet_sequence": 12652,
      "device_uptime_ms": 1726733,
      "barometer": { "temperature_c": 31.0, "pressure_hpa": 1006.84, "altitude_m": 1.28 },
      "eps": { "...": "..." },
      "imu": { "...": "..." },
      "gps": { "...": "..." },
      "timestamp": 1787574341.26
    }
  }
  ```

---

### 9. Decode Raw HEX Telemetry

Decodes any 121-byte raw telemetry payload or 123-byte (`10 79...`) frame string into structured JSON.

- **Method**: `POST` (or `GET /api/decode?hex=...`)
- **URL**: `/api/decode`
- **Headers**: `Content-Type: application/json`
- **Body**:
  ```json
  {
    "hex": "10796C3100008E13EC0CFB0FFA0FFB0FD80F000090070000D00FFA0020010000F0000000A001000000000D591A00ABFF2E0B0700360139A6C4474049A43F000014010DBFFFFF000005009670C8C0EE9DD5429A99154200000000000000009A99993F0801D36237C2636FAD41C4981B440000090000F8C100005441"
  }
  ```
- **Example cURL**:
  ```bash
  curl -X POST http://localhost:8080/api/decode \
       -H "Content-Type: application/json" \
       -d '{"hex": "10796C3100008E13EC0C..."}'
  ```

---

## Client Web Serial Guide (Connecting USB from User Browser)

You do **not** need the USB dongle plugged into the server machine. When using a Chromium browser (Chrome, Edge, Opera), users can connect their USB receiver directly to their own computer:

1. Open the dashboard at `http://localhost:8080/` (or your hosted domain).
2. Select the **"💻 Client Web Serial (Browser USB)"** tab.
3. Enter your **Satellite Serial Number** (e.g. `1581`).
4. Click **"🔌 Connect Browser USB"**.
5. Select the **RASCube Receiver** (`USB VID: 0x0483, PID: 0x5740`) in the browser popup prompt.
6. The browser will automatically establish serial communication at 1,000,000 baud, send the satellite filter header, stream packets, and push decoded telemetry via `/api/telemetry/ingest`.

---

## Frontend Integration Guide (React / Vite / JavaScript)

### Realtime Dashboard Hook (React Example)

```jsx
import { useEffect, useState } from "react";

export function useSatelliteTelemetry() {
  const [telemetry, setTelemetry] = useState(null);
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    // 1. Check connection status
    fetch("http://localhost:8080/api/status")
      .then((res) => res.json())
      .then((data) => setIsConnected(data.is_connected));

    // 2. Open Realtime SSE Stream
    const eventSource = new EventSource("http://localhost:8080/api/telemetry/stream");

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        setTelemetry(data);
        setIsConnected(true);
      } catch (err) {
        console.error("Failed to parse telemetry event", err);
      }
    };

    eventSource.onerror = (err) => {
      console.warn("SSE connection interrupted, retrying...", err);
    };

    return () => {
      eventSource.close();
    };
  }, []);

  return { telemetry, isConnected };
}
```

### Connect & Port Selection Component

```jsx
export async function connectSatellite(port, serialNumber) {
  const response = await fetch("http://localhost:8080/api/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      port: port,
      serial_number: parseInt(serialNumber, 10),
    }),
  });
  return await response.json();
}
```

---

## Troubleshooting & FAQs

#### Q: Error `OSError: [Errno 48] Address already in use`
**Cause**: Another instance of `server.py` is already running on port `8080`.  
**Solution**:
1. Check and terminate existing processes:
   ```bash
   lsof -ti :8080 | xargs kill -9
   ```
2. Or specify a different port:
   ```bash
   python examples/api/server.py --port 8085
   ```

#### Q: Error `Resource busy: '/dev/cu.usbmodem...'`
**Cause**: The serial COM port is already opened by another terminal script (`sync.py` or `async.py`).  
**Solution**: Stop any running CLI example scripts before connecting through the API.
