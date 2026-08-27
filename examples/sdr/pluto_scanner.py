#!/usr/bin/env python3
"""PlutoSDR Frequency & Spectrum Scanner for RASCube Satellites.

Scans RF frequencies across the 915 - 928 MHz band (all 18 RASCube channels)
using PlutoSDR hardware to measure RSSI/power and identify active transmissions.

Usage:
  # Scan all 18 RASCube satellite channels:
  python examples/sdr/pluto_scanner.py

  # Continuous monitoring with ASCII live spectrum bar chart:
  python examples/sdr/pluto_scanner.py --watch

  # Scan custom frequency range (in MHz):
  python examples/sdr/pluto_scanner.py --start 915 --stop 928 --step 0.2
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

# RASCube channel spacing (18 channels)
CHANNELS = [
    (ch, 916_000_000 + ch * 600_000)
    for ch in range(18)
]


def render_power_bar(power_db: float, min_db: float = -60.0, max_db: float = 0.0, width: int = 25) -> str:
    """Generates an ASCII bar representing power level relative to 0 dBFS full scale."""
    clamped = max(min_db, min(max_db, power_db))
    fraction = (clamped - min_db) / (max_db - min_db)
    filled = int(round(fraction * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"


def scan_channels(sdr) -> list[dict]:
    """Sweeps through all 18 RASCube channels and calculates peak/average power in dBFS."""
    results = []
    for ch, freq_hz in CHANNELS:
        sdr.rx_lo = int(freq_hz)
        time.sleep(0.01)  # LO settling time

        # Collect I/Q buffer
        samples = sdr.rx()
        if samples is None or len(samples) == 0:
            power_db = -100.0
            peak_db = -100.0
        else:
            mag = np.abs(samples)
            mean_pwr = np.mean(mag ** 2) + 1e-12
            peak_pwr = np.percentile(mag ** 2, 99.5) + 1e-12
            power_db = 10.0 * np.log10(mean_pwr / (2048 ** 2))
            peak_db = 10.0 * np.log10(peak_pwr / (2048 ** 2))

        results.append({
            "channel": ch,
            "freq_mhz": freq_hz / 1e6,
            "avg_db": power_db,
            "peak_db": peak_db,
        })

    # Calculate relative SNR against noise floor
    min_floor = min(r["avg_db"] for r in results)
    for r in results:
        r["snr_db"] = r["peak_db"] - min_floor

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="PlutoSDR RASCube Frequency & Spectrum Scanner")
    parser.add_argument("--uri", default="usb:", help="PlutoSDR URI (default: usb:, or ip:192.168.2.1)")
    parser.add_argument("--gain", type=float, default=40.0, help="SDR RX gain in dB (default: 40.0)")
    parser.add_argument("--watch", action="store_true", help="Continuously sweep and refresh spectrum in real-time")
    parser.add_argument("--sat", type=int, default=1581, help="Highlight expected satellite serial channel (default: 1581)")

    args = parser.parse_args()
    target_channel = args.sat % 18
    target_freq = 916.0 + target_channel * 0.6

    print("=" * 75, flush=True)
    print("📡 PlutoSDR (ADALM-PLUTO) Frequency Spectrum Scanner", flush=True)
    print(f"🎯 Target Satellite : #{args.sat} -> Channel {target_channel} ({target_freq:.3f} MHz)", flush=True)
    print(f"📻 Scanning Range   : 916.000 MHz – 926.200 MHz (18 Channels, Gain: {args.gain} dB)", flush=True)
    print("=" * 75, flush=True)

    try:
        import adi
    except ImportError:
        print("[Error] pyadi-iio is required. Install with: pip install pyadi-iio", flush=True)
        sys.exit(1)

    sdr = None
    uris = [args.uri, "usb:", "ip:192.168.2.1", "ip:pluto.local", "ip:192.168.2.10"]
    seen = set()
    for uri in [u for u in uris if not (u in seen or seen.add(u))]:
        try:
            print(f"[Pluto+ SDR] Connecting via '{uri}'...", flush=True)
            dev = adi.Pluto(uri)
            dev.sample_rate = int(1_000_000)
            dev.rx_rf_bandwidth = int(500_000)
            dev.gain_control_mode_chan0 = "manual"
            dev.rx_hardwaregain_chan0 = float(args.gain)
            dev.rx_buffer_size = 16384
            # test read
            test = dev.rx()
            if test is not None and len(test) > 0:
                sdr = dev
                print(f"[Pluto+ SDR] Connected successfully on {uri}!\n", flush=True)
                break
        except Exception:
            continue

    if sdr is None:
        print("[Error] Could not connect to PlutoSDR. Check USB connection.", flush=True)
        sys.exit(1)

    try:
        while True:
            results = scan_channels(sdr)

            # Find strongest channel
            strongest = max(results, key=lambda x: x["snr_db"])

            # Clear screen if watch mode
            if args.watch:
                sys.stdout.write("\033[H\033[J")

            print(f"{'CH':<4} {'FREQ (MHz)':<12} {'PEAK (dBFS)':<13} {'SNR (dB)':<10} {'SPECTRUM POWER':<28} {'STATUS'}", flush=True)
            print("-" * 75, flush=True)

            for r in results:
                ch = r["channel"]
                freq = r["freq_mhz"]
                peak = r["peak_db"]
                snr = r["snr_db"]
                bar = render_power_bar(peak, min_db=-60.0, max_db=0.0)

                tags = []
                if ch == target_channel:
                    tags.append(f"★ SAT #{args.sat}")
                if r == strongest and snr > 3.0:
                    tags.append("🔥 PEAK SIGNAL")

                status_str = " ".join(tags)
                highlight = ">>>" if ch == target_channel else "   "
                print(f"{highlight} {ch:<2} {freq:>8.3f} MHz  {peak:>6.1f} dBFS   {snr:>+5.1f} dB   {bar}  {status_str}", flush=True)

            print("-" * 75, flush=True)
            print(f"🔥 Strongest Activity: Channel {strongest['channel']} ({strongest['freq_mhz']:.3f} MHz) with +{strongest['snr_db']:.1f} dB SNR", flush=True)

            if not args.watch:
                break
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nScanner stopped.", flush=True)


if __name__ == "__main__":
    main()
