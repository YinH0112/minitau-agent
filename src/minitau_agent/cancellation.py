"""Cancellation contract shared by providers and tools."""

from typing import Protocol


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...
