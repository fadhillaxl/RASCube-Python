# 🛰️ PlutoSDR Ground Station Commands

## 1. Standalone CLI Receiver & Transmitter
```bash
# Direct SDR Live Receiver (Raw HEX & Decoded Telemetry)
python3 examples/sdr/pluto_direct_receiver.py --sat 1581 --gain 40 --hex

# PlutoSDR Transmitter Beacon
python3 examples/sdr/pluto_transmitter.py --wake --sat 1581 --bw 500000
```

---

## 2. Web API & Ground Station Dashboard (with Web USB + PlutoSDR DSP)
```bash
# Run Ground Station Server
python3 examples/api/server.py --port 8080
```
- Open **http://localhost:8080** in Chrome / Edge.
- **Tab 1 ("💻 Client Web USB/Serial")**: Connects directly to USB hardware from the browser.
- **Tab 3 ("🛰️ PlutoSDR Radio")**: Controls PlutoSDR DSP receiver & uplink transmitter with live telemetry stream, gauges, and graphs!

