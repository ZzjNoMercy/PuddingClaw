"""Platform-neutral Agent evaluation domain.

This package deliberately does not enable tracing or import provider SDKs at
module import time. Provider integrations live behind adapters.
"""

from .contracts import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]
