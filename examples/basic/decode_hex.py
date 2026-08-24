import json
import sys
from pprint import pprint

from rascube_v2 import decode_main_telemetry_hex, decode_telemetry_to_dict

# Example RAW hex string from RASCube telemetry (Port 0x10 + 121 bytes payload)
SAMPLE_HEX = (
    "10796C3100008E13EC0CFB0FFA0FFB0FD80F000090070000D00FFA0020010000F0000000A001000000000D591"
    "A00ABFF2E0B0700360139A6C4474049A43F000014010DBFFFFF000005009670C8C0EE9DD5429A991542000000"
    "00000000009A99993F0801D36237C2636FAD41C4981B440000090000F8C100005441"
)


def main() -> None:
    # Use hex string from command line argument if provided, otherwise use sample
    hex_input = sys.argv[1] if len(sys.argv) > 1 else SAMPLE_HEX

    print("=" * 60)
    print("RASCubeV2 Telemetry HEX Decoder")
    print("=" * 60)
    print(f"Raw HEX:\n{hex_input}\n")

    # 1. Decode into typed dataclass model
    sample = decode_main_telemetry_hex(hex_input)

    print("--- Summary Values ---")
    print(f"Sequence Number   : {sample.packet_sequence}")
    print(f"Uptime            : {sample.device_uptime_ms / 1000.0:.2f} s")
    print(f"Barometer Temp    : {sample.barometer.temperature_c:.1f} °C")
    print(f"Barometer Pressure: {sample.barometer.pressure_hpa:.2f} hPa")
    print(f"Barometer Altitude: {sample.barometer.altitude_m:.2f} m")
    print(f"Battery Voltage   : {sample.eps.battery_charge.bus_voltage_v:.3f} V")
    print(f"5V Rail           : {sample.eps.main_5v_v:.3f} V")
    print(f"3.3V Rail         : {sample.eps.main_3v3_v:.3f} V")
    print(
        f"Accelerometer (g) : X={sample.imu.accelerometer_g.x:.3f}, "
        f"Y={sample.imu.accelerometer_g.y:.3f}, Z={sample.imu.accelerometer_g.z:.3f}"
    )
    print(
        f"GPS Position      : Lat={sample.gps.latitude:.6f}°, "
        f"Lon={sample.gps.longitude:.6f}°, Alt={sample.gps.altitude_m:.1f} m, "
        f"Fix={sample.gps.fix}, Sats={sample.gps.satellites}"
    )
    print(f"Radio Signal      : RSSI={sample.receiver_rssi:.1f} dBm, SNR={sample.receiver_snr:.2f} dB")
    print("=" * 60)

    # 2. Decode directly into JSON format
    json_dict = decode_telemetry_to_dict(hex_input)
    print("\n--- Full JSON Output ---")
    print(json.dumps(json_dict, indent=2))


if __name__ == "__main__":
    main()
