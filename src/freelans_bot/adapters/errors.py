from __future__ import annotations


class SessionExpiredError(RuntimeError):
    """Raised when platform feed requires re-authentication for existing session."""

