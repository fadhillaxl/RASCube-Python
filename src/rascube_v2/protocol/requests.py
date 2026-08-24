from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass

from rascube_v2.exceptions import ConnectionLostError, RequestTimeoutError
from rascube_v2.protocol.frame import UsbFrame

FrameMatcher = Callable[[UsbFrame], bool]


@dataclass(slots=True)
class _PendingRequest:
    matcher: FrameMatcher
    future: asyncio.Future[UsbFrame]


class RequestBroker:
    """Correlates responses without consuming unrelated asynchronous frames."""

    def __init__(self) -> None:
        self._pending: dict[Hashable, _PendingRequest] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._generation = 0

    async def request(
        self,
        key: Hashable,
        matcher: FrameMatcher,
        send: Callable[[], Awaitable[object]],
        *,
        timeout: float,
    ) -> UsbFrame:
        generation = self._generation
        lock = self._locks.setdefault(key, asyncio.Lock())
        pending: _PendingRequest | None = None
        try:
            async with asyncio.timeout(timeout):
                async with lock:
                    if generation != self._generation:
                        raise ConnectionLostError("connection changed before request submission")
                    loop = asyncio.get_running_loop()
                    future: asyncio.Future[UsbFrame] = loop.create_future()
                    pending = _PendingRequest(matcher, future)
                    self._pending[key] = pending
                    await send()
                    return await asyncio.shield(future)
        except TimeoutError as exc:
            if pending is not None:
                pending.future.cancel()
            raise RequestTimeoutError(
                f"request {key!r} did not complete within {timeout:.1f}s; "
                "remote delivery remains unknown"
            ) from exc
        finally:
            if pending is not None and self._pending.get(key) is pending:
                del self._pending[key]
            if pending is not None and not pending.future.done():
                pending.future.cancel()

    def handle(self, frame: UsbFrame) -> bool:
        for key, pending in tuple(self._pending.items()):
            if pending.future.done():
                continue
            try:
                matches = pending.matcher(frame)
            except Exception:
                matches = False
            if matches:
                del self._pending[key]
                pending.future.set_result(frame)
                return True
        return False

    def cancel_all(self, message: str = "connection closed") -> None:
        self._generation += 1
        error = ConnectionLostError(message)
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(error)
        self._pending.clear()
