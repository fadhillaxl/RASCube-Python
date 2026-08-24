class RASCubeError(Exception):
    """Base exception for the RASCubeV2 library."""


class ConnectionClosedError(RASCubeError):
    """The operation requires an open receiver connection."""


class ConnectionLostError(RASCubeError):
    """The receiver connection was lost while an operation was pending."""


class RequestTimeoutError(RASCubeError, TimeoutError):
    """A command-specific response did not arrive before its deadline."""


class ProtocolDecodeError(RASCubeError, ValueError):
    """A frame payload did not match its documented layout."""


class UnsupportedCommandError(RASCubeError):
    """The current firmware does not implement the requested operation."""


class AddonUnavailableError(RASCubeError):
    """The command targets an add-on not present in the last enabled bitmap."""


class SessionBusyError(RASCubeError):
    """An exclusive camera or calibration workflow is already active."""


class CameraAssemblyError(RASCubeError):
    """Camera blocks could not be assembled into a valid JPEG image."""
