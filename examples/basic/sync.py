from rascube_v2 import SyncRASCube, prompt_connection

port, serial_number = prompt_connection()

with SyncRASCube(port, serial_number=serial_number) as cube:
    print("Receiver:", cube.receiver.get_info())
    print("OBC:", cube.obc.get_info())

    for sample in cube.telemetry.iter_samples(timeout=15):
        print(sample.device_uptime_ms, sample.gps.latitude, sample.gps.longitude)
