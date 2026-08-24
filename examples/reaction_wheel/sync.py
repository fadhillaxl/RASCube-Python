from collections import deque
from contextlib import suppress

import matplotlib.pyplot as plt
from matplotlib.widgets import Button, Slider

from rascube_v2 import AddonId, AddonUnavailableError, SyncRASCube, prompt_connection

HISTORY_SECONDS = 30.0
POLL_SECONDS = 0.05


def main(port: str, serial_number: int) -> None:
    actions: deque[tuple[str, int | None]] = deque()
    uptime_s: deque[float] = deque()
    measured_orientation: deque[int] = deque()
    target_orientation: deque[int] = deque()
    measured_speed: deque[int] = deque()
    target_speed: deque[int] = deque()

    with SyncRASCube(port, serial_number=serial_number) as cube:
        presence = cube.addons.refresh_enabled()
        if not presence.is_enabled(AddonId.REACTION_WHEEL):
            raise AddonUnavailableError("the reaction-wheel add-on is not enabled")
        wheel = cube.addons.reaction_wheel

        plt.ion()
        figure, (orientation_axes, speed_axes) = plt.subplots(2, 1, sharex=True)
        figure.subplots_adjust(bottom=0.25, hspace=0.35)
        (measured_orientation_line,) = orientation_axes.plot([], [], label="Measured")
        (target_orientation_line,) = orientation_axes.plot([], [], label="Target")
        (measured_speed_line,) = speed_axes.plot([], [], label="Measured")
        (target_speed_line,) = speed_axes.plot([], [], label="Target")

        orientation_axes.set(title="Reaction-Wheel Orientation", ylabel="Orientation")
        speed_axes.set(title="Reaction-Wheel Speed", xlabel="Device uptime (s)", ylabel="Units")
        for axes in (orientation_axes, speed_axes):
            axes.legend()
            axes.grid()

        target_slider = Slider(
            figure.add_axes((0.17, 0.12, 0.66, 0.03)),
            "Target",
            -180,
            180,
            valinit=0,
            valstep=1,
        )
        enable_button = Button(figure.add_axes((0.17, 0.04, 0.16, 0.05)), "Enable")
        apply_button = Button(figure.add_axes((0.42, 0.04, 0.16, 0.05)), "Apply target")
        disable_button = Button(figure.add_axes((0.67, 0.04, 0.16, 0.05)), "Disable")
        status = figure.text(0.5, 0.01, "Wheel disabled", ha="center")

        enable_button.on_clicked(lambda _event: actions.append(("enable", None)))
        apply_button.on_clicked(lambda _event: actions.append(("target", round(target_slider.val))))
        disable_button.on_clicked(lambda _event: actions.append(("disable", None)))
        figure.show()

        try:
            while plt.fignum_exists(figure.number):
                try:
                    sample = wheel.next_sample(timeout=POLL_SECONDS)
                except TimeoutError:
                    sample = None

                if sample is not None:
                    uptime_s.append(sample.device_uptime_ms / 1000)
                    measured_orientation.append(sample.measured_orientation)
                    target_orientation.append(sample.target_orientation)
                    measured_speed.append(sample.measured_speed)
                    target_speed.append(sample.target_speed)

                    cutoff_s = uptime_s[-1] - HISTORY_SECONDS
                    while uptime_s[0] < cutoff_s:
                        uptime_s.popleft()
                        measured_orientation.popleft()
                        target_orientation.popleft()
                        measured_speed.popleft()
                        target_speed.popleft()

                    measured_orientation_line.set_data(uptime_s, measured_orientation)
                    target_orientation_line.set_data(uptime_s, target_orientation)
                    measured_speed_line.set_data(uptime_s, measured_speed)
                    target_speed_line.set_data(uptime_s, target_speed)
                    for axes in (orientation_axes, speed_axes):
                        axes.relim()
                        axes.autoscale_view()
                    figure.canvas.draw_idle()

                figure.canvas.flush_events()

                while actions:
                    action, target = actions.popleft()
                    if action == "enable":
                        wheel.enable()
                        status.set_text("Enable submitted")
                    elif action == "disable":
                        wheel.disable()
                        status.set_text("Disable submitted")
                    elif target is not None:
                        wheel.set_target(target)
                        status.set_text(f"Target {target} submitted")
                    figure.canvas.draw_idle()
        finally:
            with suppress(Exception):
                wheel.disable()


if __name__ == "__main__":
    selected_port, selected_serial_number = prompt_connection()
    with suppress(KeyboardInterrupt):
        main(selected_port, selected_serial_number)
