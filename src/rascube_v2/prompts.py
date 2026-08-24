from __future__ import annotations

import questionary
from serial.tools import list_ports

MAX_SERIAL_NUMBER = 0xFFFFFFFF


def prompt_connection() -> tuple[str, int]:
    """Prompt for a serial port and satellite serial number."""
    ports = list(list_ports.comports())
    if not ports:
        raise RuntimeError("no serial ports found")

    port = questionary.select(
        "Select a COM port:",
        choices=[
            questionary.Choice(
                title=f"{item.device} - {item.description}",
                value=item.device,
            )
            for item in ports
        ],
    ).ask()
    if port is None:
        raise KeyboardInterrupt

    serial_number = questionary.text(
        "Satellite serial number:",
        validate=_validate_serial_number,
    ).ask()
    if serial_number is None:
        raise KeyboardInterrupt

    return str(port), int(serial_number)


def _validate_serial_number(value: str) -> bool | str:
    try:
        serial_number = int(value)
    except ValueError:
        return "Enter a decimal serial number."
    if not 0 <= serial_number <= MAX_SERIAL_NUMBER:
        return "Serial number must be between 0 and 4294967295."
    return True
