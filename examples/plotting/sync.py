from collections import deque
from contextlib import suppress

import matplotlib.pyplot as plt

from rascube_v2 import SyncRASCube, prompt_connection

HISTORY_SECONDS = 30.0


def main(port: str, serial_number: int) -> None:
    uptime_s: deque[float] = deque()
    acceleration_x: deque[float] = deque()
    acceleration_y: deque[float] = deque()
    acceleration_z: deque[float] = deque()

    plt.ion()
    figure, axes = plt.subplots()
    (line_x,) = axes.plot([], [], label="X")
    (line_y,) = axes.plot([], [], label="Y")
    (line_z,) = axes.plot([], [], label="Z")
    axes.set(title="Live RASCube Acceleration", xlabel="Device uptime (s)", ylabel="g")
    axes.legend()
    axes.grid()
    figure.show()

    with SyncRASCube(port, serial_number=serial_number) as cube:
        for sample in cube.telemetry.iter_samples(timeout=15):
            if not plt.fignum_exists(figure.number):
                break

            uptime_s.append(sample.device_uptime_ms / 1000)
            acceleration_x.append(sample.imu.accelerometer_g.x)
            acceleration_y.append(sample.imu.accelerometer_g.y)
            acceleration_z.append(sample.imu.accelerometer_g.z)

            cutoff_s = uptime_s[-1] - HISTORY_SECONDS
            while uptime_s[0] < cutoff_s:
                uptime_s.popleft()
                acceleration_x.popleft()
                acceleration_y.popleft()
                acceleration_z.popleft()

            line_x.set_data(uptime_s, acceleration_x)
            line_y.set_data(uptime_s, acceleration_y)
            line_z.set_data(uptime_s, acceleration_z)
            axes.relim()
            axes.autoscale_view()
            figure.canvas.draw_idle()
            figure.canvas.flush_events()


if __name__ == "__main__":
    selected_port, selected_serial_number = prompt_connection()
    with suppress(KeyboardInterrupt):
        main(selected_port, selected_serial_number)
