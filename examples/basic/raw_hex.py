from rascube_v2 import SyncRASCube, prompt_connection


def main() -> None:
    port, serial_number = prompt_connection()

    with SyncRASCube(port, serial_number=serial_number) as cube:
        print(f"Connected to satellite {serial_number} on {port}")
        print("Streaming raw HEX telemetry packets (Ctrl+C to stop)...\n")

        for sample in cube.telemetry.iter_samples(timeout=15):
            # Port (1 byte) + Length (1 byte) + Payload (121 bytes)
            raw_packet = (
                bytes([sample.metadata.port, len(sample.metadata.raw_payload)])
                + sample.metadata.raw_payload
            )
            print(raw_packet.hex().upper())


if __name__ == "__main__":
    main()
