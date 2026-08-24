from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from rascube_v2.models.common import ProtocolIssue
from rascube_v2.protocol.events import EventStream
from rascube_v2.protocol.frame import UsbFrame
from rascube_v2.protocol.requests import RequestBroker

FrameHandler = Callable[[UsbFrame], None]


class FrameRouter:
    def __init__(self, broker: RequestBroker) -> None:
        self._broker = broker
        self._handlers: dict[int, list[FrameHandler]] = defaultdict(list)
        self.raw_frames: EventStream[UsbFrame] = EventStream(default_queue_size=250)
        self.protocol_issues: EventStream[ProtocolIssue] = EventStream(default_queue_size=50)

    def register(self, port: int, handler: FrameHandler) -> None:
        self._handlers[port].append(handler)

    def dispatch(self, frame: UsbFrame) -> None:
        self._broker.handle(frame)
        self.raw_frames.publish(frame)
        for handler in tuple(self._handlers.get(frame.port, ())):
            try:
                handler(frame)
            except Exception as exc:
                self.protocol_issues.publish(
                    ProtocolIssue(
                        message=str(exc),
                        port=frame.port,
                        payload=frame.payload,
                        exception=exc,
                    )
                )

    def close(self) -> None:
        self.raw_frames.close()
        self.protocol_issues.close()

    def interrupt(self, error: BaseException) -> None:
        self.raw_frames.interrupt(error)
        self.protocol_issues.interrupt(error)
