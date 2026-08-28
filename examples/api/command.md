# 🚀 RASCube Ground Station API & Web Dashboard

## 1. Run Server

### Option A: Local Python
```bash
python examples/api/server.py --port 8080
```
Open Dashboard at: **http://localhost:8080**  
Open Swagger UI at: **http://localhost:8080/docs**

### Option B: Docker Container (Port 7072)
```bash
# 1. Build and Run via Docker Compose
docker compose up -d

# OR run directly via Docker CLI
docker build -t rascube-api:latest .
docker run -d --name rascube-groundstation -p 7072:8080 --restart unless-stopped rascube-api:latest
```
Open Dashboard at: **http://localhost:7072**  
Open Swagger UI at: **http://localhost:7072/docs**  
Open OpenAPI JSON at: **http://localhost:7072/openapi.json**


---

## 2. PlutoSDR Hardware Direct DSP Mode (via API)

### Start PlutoSDR Live Demodulator (`--sat 1581 --gain 40`)
```bash
curl -X POST http://localhost:8080/api/sdr/receiver/start \
  -H "Content-Type: application/json" \
  -d '{"sat": 1581, "gain": 40.0, "sf": 7, "bw": 500000, "uri": "usb:"}'
```

### Stop PlutoSDR Receiver
```bash
curl -X POST http://localhost:8080/api/sdr/receiver/stop
```

### PlutoSDR Radio Uplink Commands
```bash
# Blink Satellite RGB LED
curl -X POST http://localhost:8080/api/sdr/transmit \
  -H "Content-Type: application/json" \
  -d '{"sat": 1581, "command": "blink", "bw": 500000}'

# Play Startup Song
curl -X POST http://localhost:8080/api/sdr/transmit \
  -H "Content-Type: application/json" \
  -d '{"sat": 1581, "command": "song", "bw": 500000}'

# Hardware Continuous Wake Beacon (FPGA DMA Cyclic)
curl -X POST http://localhost:8080/api/sdr/transmit \
  -H "Content-Type: application/json" \
  -d '{"sat": 1581, "command": "wake", "bw": 500000}'
```

---

## 3. Web USB / Web Serial Client Mode
1. Open **http://localhost:8080** in Chrome or Edge.
2. Select tab **"💻 Client Web USB/Serial"**.
3. Plug in the RASCube USB Receiver Dongle into your computer.
4. Click **"🔌 Connect Browser USB"** and select the RASCube Dongle.
5. The browser will read raw frames directly and stream live telemetry to the dashboard!