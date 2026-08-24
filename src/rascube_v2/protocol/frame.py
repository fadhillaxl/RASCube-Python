from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from rascube_v2.constants import MAX_USB_PAYLOAD


@dataclass(frozen=True, slots=True)
class UsbFrame:
    port: int
    payload: bytes
    received_monotonic: float | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.port <= 0xFF:
            raise ValueError("port must fit in one byte")
        if len(self.payload) > MAX_USB_PAYLOAD:
            raise ValueError("payload exceeds the USB framing limit")


class UsbFrameCodec:
    def __init__(self) -> None:
        self._buffer = bytearray()

    @staticmethod
    def encode(frame: UsbFrame) -> bytes:
        return bytes((frame.port, len(frame.payload))) + frame.payload

    def feed(self, data: bytes) -> list[UsbFrame]:
        self._buffer.extend(data)
        frames: list[UsbFrame] = []
        received_at = monotonic()

        while len(self._buffer) >= 2:
            payload_length = self._buffer[1]
            frame_length = 2 + payload_length
            if len(self._buffer) < frame_length:
                break

            port = self._buffer[0]
            payload = bytes(self._buffer[2:frame_length])
            del self._buffer[:frame_length]
            frames.append(UsbFrame(port, payload, received_at))

        return frames

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()
