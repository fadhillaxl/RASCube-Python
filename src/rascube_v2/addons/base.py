from __future__ import annotations

from typing import Protocol

from rascube_v2.models.common import SubmissionReceipt


class AddonCommandSender(Protocol):
    async def _send_addon_command(self, addon_id: int, command: bytes) -> SubmissionReceipt: ...
