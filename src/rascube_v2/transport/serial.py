from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar, cast

from rascube_v2.constants import DEFAULT_BAUDRATE, USB_PID_V2, USB_VID
from rascube_v2.exceptions import ConnectionClosedError, ConnectionLostError

T = TypeVar("T")


class _SerialLike(Protocol):
    is_open: bool
    dtr: bool

    def read(self, size: int) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SerialDevice:
    device: str
    description: str
    vid: int | None
    pid: int | None
    usb_serial_number: str | None


def find_receivers(*, vid: int = USB_VID, pid: int = USB_PID_V2) -> list[SerialDevice]:
    from serial.tools import list_ports

    return [
        SerialDevice(port.device, port.description, port.vid, port.pid, port.serial_number)
        for port in list_ports.comports()
        if port.vid == vid and port.pid == pid
    ]


class SerialTransport:
    """Cancellation-safe async adapter around pyserial's blocking USB CDC interface."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        read_timeout: float = 0.1,
        write_timeout: float = 2.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self._serial: _SerialLike | None = None
        self._closing = False
        self._lifecycle_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return bool(self._serial is not None and self._serial.is_open)

    async def open(self) -> None:
        async with self._lifecycle_lock:
            if self.is_open:
                return
            task = asyncio.create_task(asyncio.to_thread(self._create_serial))
            serial_port, cancelled = await self._drain_worker(task)
            if cancelled:
                try:
                    await self._blocking(serial_port.close)
                finally:
                    if serial_port.is_open:
                        self._serial = serial_port
                raise asyncio.CancelledError
            self._serial = serial_port

    def _create_serial(self) -> _SerialLike:
        import serial

        return cast(
            _SerialLike,
            serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.read_timeout,
                write_timeout=self.write_timeout,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            ),
        )

    async def close(self) -> None:
        async with self._lifecycle_lock:
            serial_port = self._serial
            if serial_port is None:
                return
            self._closing = True
            try:
                async with self._write_lock, self._read_lock:
                    await self._blocking(serial_port.close)
            finally:
                if not serial_port.is_open:
                    self._serial = None
                self._closing = False

    async def read(self, max_bytes: int = 4096) -> bytes:
        async with self._read_lock:
            serial_port = self._require_open()
            try:
                return bytes(await self._blocking(serial_port.read, max_bytes))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ConnectionLostError(f"serial read failed: {exc}") from exc

    async def write(self, data: bytes) -> None:
        if not data:
            return
        async with self._write_lock:
            serial_port = self._require_open()
            try:
                await self._blocking(self._write_and_flush, serial_port, data)
            except asyncio.CancelledError:
                raise
            except ConnectionLostError:
                raise
            except Exception as exc:
                raise ConnectionLostError(f"serial write failed: {exc}") from exc

    @staticmethod
    def _write_and_flush(serial_port: _SerialLike, data: bytes) -> None:
        written = serial_port.write(data)
        if written != len(data):
            raise ConnectionLostError(f"serial write accepted {written} of {len(data)} bytes")
        serial_port.flush()

    async def set_dtr(self, enabled: bool) -> None:
        async with self._lifecycle_lock:
            serial_port = self._require_open()
            try:
                await self._blocking(setattr, serial_port, "dtr", enabled)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ConnectionLostError(f"failed to set DTR: {exc}") from exc

    def _require_open(self) -> _SerialLike:
        if self._closing or not self.is_open or self._serial is None:
            raise ConnectionClosedError("serial transport is closed")
        return self._serial

    @staticmethod
    async def _blocking(function: Callable[..., T], *args: object) -> T:
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        result, cancelled = await SerialTransport._drain_worker(task)
        if cancelled:
            raise asyncio.CancelledError
        return result

    @staticmethod
    async def _drain_worker(task: asyncio.Task[T]) -> tuple[T, bool]:
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        return task.result(), cancelled
