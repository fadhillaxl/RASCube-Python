# RASCubeV2 Python Library

Typed Python access to a RASCubeV2 USB receiver and satellite. The library supports
receiver and OBC information, telemetry, Arduino controls, camera capture, calibration,
and add-on modules through asynchronous and blocking APIs.

## Requirements

- Python 3.11 or newer
- RASCubeV2 USB receiver (default USB VID/PID `0483:5740`)
- The numeric serial number of the satellite

## Installation

From a source checkout:

```bash
python -m pip install .
```

Alternatively, use `uv sync --no-dev`.

## API Conventions

- `AsyncRASCube` is the asynchronous client. Its operations must be awaited.
- `SyncRASCube` is the blocking client. It provides blocking equivalents of the main
  async operations.
- Use both clients as context managers so the serial connection is closed reliably.
- `RASCube` is an alias for `AsyncRASCube`.
- Typed return models can be imported from `rascube_v2.models`.
- A `SubmissionReceipt` confirms local submission to the USB receiver, not execution by
  the satellite.

## Connection Helpers

| Function | Returns | Description |
|---|---|---|
| `find_receivers(*, vid=0x0483, pid=0x5740)` | `list[SerialDevice]` | Finds serial ports matching the supplied USB VID/PID. |
| `prompt_connection()` | `tuple[str, int]` | Prompts for a serial port and satellite serial number. Raises `RuntimeError` when no ports are available and `KeyboardInterrupt` when cancelled. |

`SerialDevice` contains `device`, `description`, `vid`, `pid`, and `usb_serial_number`.
The USB serial number is not the satellite serial number used to create a client.

## Clients

Both clients accept:

`port`, required `serial_number`, optional `initialize=True`, and optional
`stale_after=20.0`. Initialization reads the receiver mode and selects the satellite when
the receiver is in application mode.

### Async Client

Constructor: `AsyncRASCube(port, *, serial_number, initialize=True, stale_after=20.0)`

| Member | Returns | Description |
|---|---|---|
| `await open()` | `AsyncRASCube` | Opens and initializes the receiver. |
| `await close()` | `None` | Stops pending work and closes the receiver. |
| `await select_satellite(serial_number)` | `SubmissionReceipt` | Changes the selected satellite. |
| `is_open` | `bool` | Whether the receiver connection is open. |
| `serial_number` | `int` | Currently selected satellite serial number. |
| `mode` | `ReceiverMode | None` | Receiver mode read during initialization. |
| `connection` | `ConnectionMonitor` | Connection state, freshness, statistics, and event streams. |
| `raw_frames` | `EventStream[UsbFrame]` | Async-only diagnostic stream of received frames. |
| `protocol_issues` | `EventStream[ProtocolIssue]` | Async-only stream of malformed or unsupported received data. |

The async client can be reopened on the same event loop after closing, but cannot be
moved between event loops.

### Blocking Client

Constructor: `SyncRASCube(port, *, serial_number, initialize=True, stale_after=20.0)`

| Member | Returns | Description |
|---|---|---|
| `close()` | `None` | Closes the receiver. Normally handled by the context manager. |
| `select_satellite(serial_number)` | `SubmissionReceipt` | Changes the selected satellite. |
| `is_open` | `bool` | Whether the receiver connection is open. |
| `serial_number` | `int` | Currently selected satellite serial number. |
| `mode` | `ReceiverMode | None` | Receiver mode read during initialization. |
| `transport_state` | `TransportState` | Current USB transport state. |
| `satellite_state` | `SatelliteLinkState` | Current satellite traffic state. |
| `statistics` | `LinkStatistics` | Inbound and outbound frame and byte counts. |

## Connection State

The async `cube.connection` object exposes the following read-only state and streams.

| Member | Type | Description |
|---|---|---|
| `transport_state` | `TransportState` | `DISCONNECTED`, `CONNECTING`, `CONNECTED`, or `FAILED`. |
| `satellite_state` | `SatelliteLinkState` | `NEVER_SEEN`, `ACTIVE`, or `STALE`. |
| `satellite_age` | `float | None` | Seconds since the last satellite frame. |
| `stale_after` | `float` | Age at which the satellite state becomes stale. |
| `statistics` | `LinkStatistics` | Inbound/outbound byte and frame counts. |
| `transport_events` | `EventStream[TransportState]` | Transport-state changes. |
| `satellite_events` | `EventStream[SatelliteLinkState]` | Satellite-state changes. |
| `statistics_events` | `EventStream[LinkStatistics]` | Updated link statistics. |

## Receiver

Available through `cube.receiver`.

| Method | Returns | Description |
|---|---|---|
| `get_info(*, timeout=2.0)` | `ReceiverInfo` | Reads receiver firmware information. |
| `get_mode(*, timeout=2.0)` | `ReceiverMode` | Reads the current receiver mode. |
| `set_satellite_serial_number(serial_number)` | `SubmissionReceipt` | Selects a satellite. `cube.select_satellite()` is equivalent. |
| `set_rf_config(spreading_factor, bandwidth_index, *, coding_rate_index=None)` | `SubmissionReceipt` | Changes receiver RF settings. Only coding-rate index `0` is accepted. |
| `enter_bootloader()` | `SubmissionReceipt` | Requests receiver bootloader mode. Firmware programming is not provided. |
| `next_status(*, timeout=None)` | `ReceiverStatus` | Blocking-client convenience for waiting for the next status. |

Async users receive statuses through `cube.receiver.statuses`, an
`EventStream[ReceiverStatus]`.

Status flags are `HARDWARE_GOOD`, `USB_PACKET_DROPPED`, `RADIO_PACKET_DROPPED`,
`RADIO_WAITING_FOR_RX`, `RADIO_PACKET_RECEIVED`, and `RADIO_PACKET_TRANSMITTING`.

The async receiver module also provides calculation helpers:

| Method | Returns | Description |
|---|---|---|
| `wireless_channel(serial_number)` | `int` | Calculates the satellite radio channel. |
| `frequency_hz(serial_number)` | `int` | Calculates the satellite frequency in hertz. |
| `radio_address(serial_number)` | `int` | Calculates the 16-bit radio address. |
| `bandwidth_khz(index)` | `int` | Returns the bandwidth represented by an index from 0 through 9. |

## OBC

Available through `cube.obc`.

| Method | Returns | Description |
|---|---|---|
| `get_info(*, timeout=15.0)` | `ObcInfo` | Reads STM and Arduino firmware information. Current and legacy version-only responses are supported. |
| `get_flash_settings(*, timeout=15.0)` | `FlashSettings` | Reads OBC flash-setting flags. |
| `set_flash_settings(settings)` | `SubmissionReceipt` | Writes an updated `FlashSettings` value. |
| `set_startup_sound(enabled, *, read_timeout=15.0)` | `SubmissionReceipt` | Preserves current flags while enabling or disabling startup sound. The blocking argument is named `timeout`. |
| `set_telemetry_during_camera(enabled)` | `SubmissionReceipt` | Enables or disables telemetry during camera capture. |
| `set_rf_config(spreading_factor, bandwidth_index)` | `SubmissionReceipt` | Changes OBC RF settings. This is an advanced operation. |

## Arduino

Available through `cube.arduino`.

| Method | Returns | Description |
|---|---|---|
| `set_rgb(red, green, blue)` | `SubmissionReceipt` | Sets RGB values from 0 through 255. |
| `play_startup_song()` | `SubmissionReceipt` | Plays the startup song immediately. |

## Telemetry

Available through `cube.telemetry`.

| Member | Type | Description |
|---|---|---|
| `next_sample(*, timeout=None)` | `MainTelemetrySample` | Waits for the next main telemetry sample. |
| `latest` | `MainTelemetrySample | None` | Most recently received main sample. |
| `latest_user` | `UserTelemetrySample | None` | Most recently received user sample. |
| `samples` | `EventStream[MainTelemetrySample]` | Async stream of main telemetry. |
| `user_samples` | `EventStream[UserTelemetrySample]` | Async stream of user telemetry. |
| `user_names` | `EventStream[UserDataName]` | Async stream of user-data name updates. |
| `user_name_by_index` | `dict[int, str]` | Async client's current user-data names. |
| `next_user_sample(*, timeout=None)` | `UserTelemetrySample` | Blocking-client convenience for the next user sample. |
| `next_user_name(*, timeout=None)` | `UserDataName` | Blocking-client convenience for the next name update. |
| `iter_samples(*, timeout=None)` | `Iterator[MainTelemetrySample]` | Blocking iterator over main telemetry. |

The blocking client exposes the user-name mapping as `cube.telemetry.user_names`.

`MainTelemetrySample` includes EPS, barometer, IMU, GPS, receiver RSSI/SNR, uptime,
packet sequence, firmware version, and error code data.

## Camera

Available through `cube.camera`.

| Member | Type | Description |
|---|---|---|
| `capture(*, timeout=30.0)` | `CameraImage` | Requests and assembles a JPEG image. The blocking API also accepts `on_block(CameraBlock)` for progress. Use `CameraImage.jpeg` for the final image bytes. |
| `poisoned` | `bool` | Async-only indication that a prior incomplete capture makes another capture unsafe. |
| `blocks` | `EventStream[CameraBlock]` | Async-only stream of received camera blocks. |
| `images` | `EventStream[CameraImage]` | Async-only stream of completed images. |

Only one capture can run at a time. A timeout can block another capture until the prior
transfer finishes or the satellite is restarted.

The blocking `on_block` callback runs on the client's internal event-loop thread and must
return quickly. Queue blocks to the UI thread rather than rendering inside the callback.

## Calibration

Available through `cube.calibration`.

| Member | Type | Description |
|---|---|---|
| `run(*, gyroscope_timeout=60.0, magnetometer_timeout=120.0)` | `CalibrationResult` | Starts calibration and waits for gyroscope and magnetometer completion events. |
| `apply_runtime(coefficients)` | `SubmissionReceipt` | Applies finite `CalibrationCoefficients` until the Arduino restarts. |
| `events` | `EventStream[CalibrationEvent]` | Async-only stream of calibration-stage events. |

`run()` reports stage completion; it does not return calculated coefficients or confirm a
persistent flash write. A timed-out calibration can block further calibration operations
until its late completion event arrives or the satellite is restarted.

## Add-on Manager

Available through `cube.addons`.

| Member | Type | Description |
|---|---|---|
| `refresh_enabled(*, timeout=15.0)` | `AddonPresence` | Reads the enabled add-on bitmap. |
| `get_versions(*, timeout=15.0)` | `AddonVersions` | Reads enabled add-on firmware versions, fetching presence first when needed. |
| `request_status()` | `SubmissionReceipt` | Requests status updates from enabled add-ons. |
| `presence` | `AddonPresence | None` | Async client's most recently received presence information. |
| `versions` | `AddonVersions | None` | Async client's most recently received versions. |
| `presence_events` | `EventStream[AddonPresence]` | Async-only presence updates. |
| `version_events` | `EventStream[AddonVersions]` | Async-only version updates. |

Add-on IDs are available through `AddonId.REACTION_WHEEL`,
`AddonId.ADVANCED_SENSOR`, and `AddonId.ENVIRONMENTAL_SENSOR`.

### Reaction Wheel

Available through `cube.addons.reaction_wheel`.

| Member | Type | Description |
|---|---|---|
| `enable()` | `SubmissionReceipt` | Enables wheel control. |
| `disable()` | `SubmissionReceipt` | Disables wheel control. |
| `set_target(target)` | `SubmissionReceipt` | Sets a signed 32-bit target. |
| `set_pid_gains(p, i, d)` | `SubmissionReceipt` | Sets finite PID gains. The integral gain must be nonzero. |
| `next_sample(*, timeout=None)` | `ReactionWheelSample` | Waits for the next wheel sample. |
| `latest` | `ReactionWheelSample | None` | Most recently received wheel sample. |
| `samples` | `EventStream[ReactionWheelSample]` | Async-only wheel sample stream. |

### Advanced Sensor

Available through `cube.addons.advanced_sensor`.

| Member | Type | Description |
|---|---|---|
| `start_recording()` | `SubmissionReceipt` | Starts SD-card recording. |
| `stop_recording()` | `SubmissionReceipt` | Stops SD-card recording. |
| `next_sample(*, timeout=None)` | `AdvancedSensorSample` | Waits for the next sensor sample. |
| `next_status(*, timeout=None)` | `AdvancedSensorStatus` | Waits for the next sensor status. |
| `latest` | `AdvancedSensorSample | None` | Most recently received sensor sample. |
| `latest_status` | `AdvancedSensorStatus | None` | Most recently received status. |
| `samples` | `EventStream[AdvancedSensorSample]` | Async-only sample stream. |
| `statuses` | `EventStream[AdvancedSensorStatus]` | Async-only status stream. |

### Environmental Sensor

Available through `cube.addons.environmental_sensor`.

| Member | Type | Description |
|---|---|---|
| `next_sample(*, timeout=None)` | `EnvironmentalSensorSample` | Waits for the next environmental sample. |
| `latest` | `EnvironmentalSensorSample | None` | Most recently received sample. |
| `samples` | `EventStream[EnvironmentalSensorSample]` | Async-only sample stream. |

## Async Event Streams

Async stream properties use `EventStream[T]`.

| Member | Returns | Description |
|---|---|---|
| `await stream.next(*, timeout=None)` | `T` | Waits for one future event. A deadline raises built-in `TimeoutError`. |
| `stream.subscribe(*, max_queue=None)` | `Subscription[T]` | Creates an independent async subscription. Older events are dropped when its queue is full. |
| `subscription.close()` | `None` | Stops the subscription. |
| `subscription.dropped` | `int` | Number of events discarded because the queue was full. |

`Subscription` is an async iterator and async context manager. A subscription supports
one pending iteration call at a time. Current subscribers receive `ConnectionLostError`
if the connection fails.

## Model Helpers

| Method or property | Description |
|---|---|
| `FlashSettings(raw_flags)` | Creates OBC settings from an unsigned byte of flags. |
| `CalibrationCoefficients(gyroscope_offsets, magnetometer_offsets, magnetometer_scales)` | Creates the three coefficient vectors accepted by `apply_runtime()`. |
| `ReceiverStatus.has(flag)` | Tests a `ReceiverStatusFlag`. |
| `FlashSettings.startup_sound_enabled` | Reports the startup-sound flag. |
| `FlashSettings.with_startup_sound(enabled)` | Returns a new settings value with that flag changed. |
| `AddonPresence.is_enabled(addon_id)` | Tests whether an add-on is enabled. |
| `AddonVersions.create(versions)` | Creates an immutable add-on version mapping. |
| `CalibrationCoefficients.values()` | Returns all nine coefficients in transmission order. |
| `AdvancedSensorStatus.recording` | Reports whether recording is active. |
| `AdvancedSensorStatus.sd_ready` | Reports whether the SD subsystem is ready. |
| `AdvancedSensorStatus.microphone_initialized` | Reports microphone initialization. |
| `AdvancedSensorStatus.radiation_initialized` | Reports radiation-sensor initialization. |

## Exceptions

All library exceptions derive from `RASCubeError`.

| Exception | Meaning |
|---|---|
| `ConnectionClosedError` | An operation requires an open connection. |
| `ConnectionLostError` | The receiver connection failed while work was pending. |
| `RequestTimeoutError` | A command or workflow exceeded its deadline. |
| `ProtocolDecodeError` | Received data does not match a supported format. |
| `AddonUnavailableError` | Known presence information says the target add-on is disabled. |
| `SessionBusyError` | A camera or calibration workflow is already active or ambiguous. |
| `CameraAssemblyError` | Camera blocks could not be assembled into a valid JPEG. |

Invalid arguments can also raise `ValueError`. Event-stream deadlines raise built-in
`TimeoutError` rather than `RequestTimeoutError`.

## Usage Notes

- Requests are not automatically retried.
- Runtime calibration coefficients are not persistent.
- RF changes are not atomic; incompatible settings can prevent further communication.
- Receiver firmware programming, legacy telemetry, and legacy RGB565 camera capture are
  not supported.
- Environmental telemetry and reaction-wheel speed units depend on deployed firmware.

## Examples

- **REST API Server & Swagger Docs**: [`examples/api/server.py`](examples/api/server.py) (See [API Tutorial](examples/api/README.md))
- **Raw Telemetry HEX Ingest & Decoder**: [`examples/basic/raw_hex.py`](examples/basic/raw_hex.py) & [`examples/basic/decode_hex.py`](examples/basic/decode_hex.py)
- **Pluto+ SDR Satellite Receiver**: [`examples/sdr/pluto_receiver.py`](examples/sdr/pluto_receiver.py)
- **Basic CLI Clients**: [`examples/basic/sync.py`](examples/basic/sync.py) & [`examples/basic/async.py`](examples/basic/async.py)
- **Realtime Plotting**: [`examples/plotting/sync.py`](examples/plotting/sync.py) & [`examples/plotting/async.py`](examples/plotting/async.py)
- **Reaction Wheel**: [`examples/reaction_wheel/sync.py`](examples/reaction_wheel/sync.py) & [`examples/reaction_wheel/async.py`](examples/reaction_wheel/async.py)
- **Camera Capture**: [`examples/camera/sync.py`](examples/camera/sync.py) & [`examples/camera/async.py`](examples/camera/async.py)

The plotting examples show a rolling 30-second accelerometer history. They require
Matplotlib, which is included in the development dependency group installed by `uv sync`.
The reaction-wheel examples add enable, target, and disable controls while plotting a
rolling 30-second history of wheel orientation and speed.
The camera examples progressively decode contiguous JPEG blocks and refresh the preview
while each capture is in progress. They do not automatically retry a timed-out capture
because the late camera stream is unlabelled.
