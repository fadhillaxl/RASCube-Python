from __future__ import annotations

from time import monotonic

from rascube_v2.addons.advanced_sensor import AdvancedSensorModule
from rascube_v2.addons.environmental_sensor import EnvironmentalSensorModule
from rascube_v2.addons.reaction_wheel import ReactionWheelModule
from rascube_v2.constants import AddonId, HostPort, InboundPort
from rascube_v2.exceptions import AddonUnavailableError
from rascube_v2.models.addons import AddonPresence, AddonVersions
from rascube_v2.models.common import SubmissionReceipt
from rascube_v2.protocol.bus import CommandBus
from rascube_v2.protocol.codecs import (
    decode_addon_presence,
    decode_addon_versions,
    decode_advanced_sensor,
    decode_advanced_sensor_status,
    decode_environmental_sensor,
    decode_reaction_wheel,
)
from rascube_v2.protocol.events import EventStream
from rascube_v2.protocol.frame import UsbFrame


class AddonManager:
    def __init__(self, bus: CommandBus) -> None:
        self._bus = bus
        self.presence: AddonPresence | None = None
        self.versions: AddonVersions | None = None
        self.presence_events: EventStream[AddonPresence] = EventStream(default_queue_size=10)
        self.version_events: EventStream[AddonVersions] = EventStream(default_queue_size=10)

        self.reaction_wheel = ReactionWheelModule(self)
        self.advanced_sensor = AdvancedSensorModule(self)
        self.environmental_sensor = EnvironmentalSensorModule()

        bus.register_handler(InboundPort.ADDONS_ENABLED, self._handle_presence)
        bus.register_handler(InboundPort.ADDONS_INFO, self._handle_versions)
        bus.register_handler(InboundPort.ADDON_TELEMETRY, self._handle_telemetry)
        bus.register_handler(InboundPort.ADDON_STATUS, self._handle_status)

    async def refresh_enabled(self, *, timeout: float = 15.0) -> AddonPresence:
        frame = await self._bus.request(
            HostPort.ADDONS_ENABLED_GET,
            b"\x00",
            key="addons-enabled",
            matcher=lambda item: item.port == InboundPort.ADDONS_ENABLED,
            timeout=timeout,
        )
        return decode_addon_presence(frame)

    async def get_versions(self, *, timeout: float = 15.0) -> AddonVersions:
        deadline = monotonic() + timeout
        if self.presence is None:
            await self.refresh_enabled(timeout=timeout)
        if self.presence is not None and not self.presence.enabled_ids:
            empty = AddonVersions.create({})
            self.versions = empty
            return empty
        frame = await self._bus.request(
            HostPort.ADDONS_INFO_GET,
            b"\x00",
            key="addons-info",
            matcher=lambda item: item.port == InboundPort.ADDONS_INFO,
            timeout=max(0.001, deadline - monotonic()),
        )
        return decode_addon_versions(frame)

    async def request_status(self) -> SubmissionReceipt:
        return await self._bus.submit(HostPort.ADDONS_STATUS_GET, b"\x00")

    async def _send_addon_command(self, addon_id: int, command: bytes) -> SubmissionReceipt:
        if self.presence is not None and not self.presence.is_enabled(int(addon_id)):
            raise AddonUnavailableError(f"add-on {int(addon_id)} is not enabled")
        return await self._bus.submit(HostPort.ADDON_COMMAND, bytes((int(addon_id),)) + command)

    def _handle_presence(self, frame: UsbFrame) -> None:
        presence = decode_addon_presence(frame)
        self.presence = presence
        self.presence_events.publish(presence)

    def _handle_versions(self, frame: UsbFrame) -> None:
        versions = decode_addon_versions(frame)
        self.versions = versions
        self.version_events.publish(versions)

    def _handle_telemetry(self, frame: UsbFrame) -> None:
        if not frame.payload:
            return
        addon_id = frame.payload[0]
        if addon_id == AddonId.REACTION_WHEEL:
            self.reaction_wheel._publish(decode_reaction_wheel(frame))
        elif addon_id == AddonId.ADVANCED_SENSOR:
            self.advanced_sensor._publish(decode_advanced_sensor(frame))
        elif addon_id == AddonId.ENVIRONMENTAL_SENSOR:
            self.environmental_sensor._publish(decode_environmental_sensor(frame))

    def _handle_status(self, frame: UsbFrame) -> None:
        if frame.payload and frame.payload[0] == AddonId.ADVANCED_SENSOR:
            self.advanced_sensor._publish_status(decode_advanced_sensor_status(frame))

    def interrupt(self, error: BaseException) -> None:
        self.presence_events.interrupt(error)
        self.version_events.interrupt(error)
        self.reaction_wheel.samples.interrupt(error)
        self.advanced_sensor.samples.interrupt(error)
        self.advanced_sensor.statuses.interrupt(error)
        self.environmental_sensor.samples.interrupt(error)

    def reset_connection(self) -> None:
        self.presence = None
        self.versions = None
        self.reaction_wheel.latest = None
        self.advanced_sensor.latest = None
        self.advanced_sensor.latest_status = None
        self.environmental_sensor.latest = None
