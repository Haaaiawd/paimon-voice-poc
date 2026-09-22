"""Runtime entry points; audio devices are optional for the browser gateway."""

__all__ = ["MicCapture", "StreamingPlayer"]


def __getattr__(name: str):
    if name == "MicCapture":
        from .mic import MicCapture

        return MicCapture
    if name == "StreamingPlayer":
        from .playback import StreamingPlayer

        return StreamingPlayer
    raise AttributeError(name)
