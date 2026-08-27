import time
from rascube_v2 import SyncRASCube, prompt_connection


def main() -> None:
    port, serial_number = prompt_connection()

    with SyncRASCube(port, serial_number=serial_number) as cube:
        print(f"Connected to USB Receiver on {port} (Target Sat #{serial_number})")

        # 1. Transmit OBC Info Request (Uplink ping & status check)
        print("Transmitting OBC Info Request (HostPort.OBC_INFO 0x12) to satellite...")
        try:
            obc_info = cube.obc.get_info(timeout=3.0)
            stm_ver = obc_info.stm.software_version if obc_info.stm else "Unknown"
            ard_ver = obc_info.arduino.software_version if obc_info.arduino else "Unknown"
            print(f"📡 Sat #{serial_number} responded! (STM FW v{stm_ver}, Arduino FW v{ard_ver})")
        except Exception as exc:
            print(f"⚠️ Uplink request sent, proceeding to downlink stream ({exc})")

        # 2. Stream Downlink Telemetry Frames in Raw HEX
        print("\nStreaming raw HEX telemetry packets (Ctrl+C to stop)...\n")

        for sample in cube.telemetry.iter_samples(timeout=15):
            # Port (1 byte) + Length (1 byte) + Payload (121 bytes)
            raw_packet = (
                bytes([sample.metadata.port, len(sample.metadata.raw_payload)])
                + sample.metadata.raw_payload
            )
            print(raw_packet.hex().upper())


if __name__ == "__main__":
    main()

