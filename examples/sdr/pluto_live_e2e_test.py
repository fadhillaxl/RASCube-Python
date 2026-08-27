#!/usr/bin/env python3
"""End-to-End PlutoSDR LoRa Telemetry Test.

1. Wakes up the satellite using the USB dongle on a background thread.
2. Connects to PlutoSDR and streams raw I/Q to GNU Radio demodulator.
3. Decodes and prints the live telemetry packets received over RF by PlutoSDR!
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

sys.path.insert(0, "src")


def dongle_poller() -> None:
    """Keep satellite alive and transmitting via dongle."""
    try:
        from rascube_v2.sync import RASCube

        port = "/dev/cu.usbmodem20623154594D1"
        print(f"[Dongle] Connecting to {port}...", flush=True)
        with RASCube(port=port, serial_number=1581) as cube:
            print("[Dongle] Connected! Satellite woken up. Polling samples...", flush=True)
            for i, sample in enumerate(cube.telemetry.iter_samples(timeout=10)):
                if i % 5 == 0:
                    print(f"[Dongle TX-Beacon] Sat Uptime: {sample.device_uptime_ms} ms", flush=True)
                time.sleep(0.5)
    except Exception as exc:
        print(f"[Dongle Notice] Poller stopped: {exc}", flush=True)


def main() -> None:
    print("=" * 65, flush=True)
    print("🛰️ PlutoSDR Live End-to-End Ground Station Test", flush=True)
    print("=" * 65, flush=True)

    # 1. Start dongle poller in background thread to wake satellite
    t_dongle = threading.Thread(target=dongle_poller, daemon=True)
    t_dongle.start()
    time.sleep(2.0)  # Wait for satellite to start transmitting

    # 2. Run PlutoSDR receiver
    print("\n[PlutoSDR] Starting PlutoSDR Receiver...", flush=True)
    subprocess.run(
        [
            ".venv/bin/python",
            "examples/sdr/pluto_receiver.py",
            "--sat",
            "1581",
            "--gain",
            "40",
            "--bw",
            "500000",
        ],
        check=False,
    )


if __name__ == "__main__":
    main()
