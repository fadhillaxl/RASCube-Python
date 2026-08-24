from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _Terminal:
    error: BaseException | None


class Subscription(AsyncIterator[T], Generic[T]):
    def __init__(self, stream: EventStream[T], max_queue: int) -> None:
        self._stream = stream
        self._queue: asyncio.Queue[T | _Terminal] = asyncio.Queue(maxsize=max_queue)
        self._terminal: _Terminal | None = None
        self._waiting = False
        self.dropped = 0

    def __aiter__(self) -> Subscription[T]:
        return self

    async def __anext__(self) -> T:
        self._stream._ensure_loop()
        if self._waiting:
            raise RuntimeError("a subscription supports only one pending anext() call")
        if self._terminal is not None and self._queue.empty():
            self._raise_terminal()
        self._waiting = True
        try:
            item = await self._queue.get()
        finally:
            self._waiting = False
        if isinstance(item, _Terminal):
            self._terminal = item
            self._raise_terminal()
        return cast(T, item)

    async def __aenter__(self) -> Subscription[T]:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._stream._ensure_loop()
        if self._terminal is not None:
            return
        self._stream._remove(self)
        self._terminate(None)

    def _publish(self, item: T) -> None:
        if self._terminal is not None:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(item)

    def _terminate(self, error: BaseException | None) -> None:
        if self._terminal is not None:
            return
        terminal = _Terminal(error)
        self._terminal = terminal
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(terminal)

    def _raise_terminal(self) -> None:
        if self._terminal is not None and self._terminal.error is not None:
            raise self._terminal.error
        raise StopAsyncIteration


class EventStream(Generic[T]):
    """A loop-affine broadcast stream with bounded subscriber queues."""

    def __init__(self, *, default_queue_size: int = 100) -> None:
        if default_queue_size < 1:
            raise ValueError("default_queue_size must be positive")
        self._default_queue_size = default_queue_size
        self._subscriptions: set[Subscription[T]] = set()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self, *, max_queue: int | None = None) -> Subscription[T]:
        self._ensure_loop()
        if self._closed:
            raise RuntimeError("event stream is closed")
        size = self._default_queue_size if max_queue is None else max_queue
        if size < 1:
            raise ValueError("max_queue must be positive")
        subscription = Subscription(self, size)
        self._subscriptions.add(subscription)
        return subscription

    def bind(self) -> None:
        """Bind the stream to the current event loop without creating a subscriber."""
        self._ensure_loop()

    def publish(self, item: T) -> None:
        self._ensure_loop()
        if self._closed:
            return
        for subscription in tuple(self._subscriptions):
            subscription._publish(item)

    def publish_threadsafe(self, item: T) -> None:
        if self._loop is None:
            raise RuntimeError("event stream has no owner loop")
        self._loop.call_soon_threadsafe(self.publish, item)

    async def next(self, *, timeout: float | None = None) -> T:
        subscription = self.subscribe(max_queue=1)
        try:
            if timeout is None:
                return await anext(subscription)
            return await asyncio.wait_for(anext(subscription), timeout)
        finally:
            subscription.close()

    def interrupt(self, error: BaseException) -> None:
        """Fail current subscribers while keeping the stream reusable after reconnect."""
        self._ensure_loop()
        for subscription in tuple(self._subscriptions):
            subscription._terminate(error)
        self._subscriptions.clear()

    def close(self) -> None:
        self._ensure_loop()
        if self._closed:
            return
        self._closed = True
        for subscription in tuple(self._subscriptions):
            subscription._terminate(None)
        self._subscriptions.clear()

    def _remove(self, subscription: Subscription[T]) -> None:
        self._subscriptions.discard(subscription)

    def _ensure_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("event streams may only be used from their owner event loop")
