from __future__ import annotations

from collections.abc import Hashable
from typing import Protocol

from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.protocol.frame import UsbFrame
from rascube_v2.protocol.requests import FrameMatcher
from rascube_v2.protocol.router import FrameHandler


class CommandBus(Protocol):
    async def submit(
        self, port: int, payload: bytes, *, radio_bound: bool = True
    ) -> SubmissionReceipt: ...

    async def request(
        self,
        port: int,
        payload: bytes,
        *,
        key: Hashable,
        matcher: FrameMatcher,
        timeout: float,
        radio_bound: bool = True,
    ) -> UsbFrame: ...

    def register_handler(self, port: int, handler: FrameHandler) -> None: ...
