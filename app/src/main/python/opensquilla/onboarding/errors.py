"""Onboarding control-flow exceptions."""

from __future__ import annotations


class UserCancelledError(Exception):
    """Raised at the questionary input boundary when the user aborts a prompt.

    The ``section`` lets the surrounding flow runner record which optional
    step was abandoned and continue with the next one, instead of letting a
    ``None`` answer leak into downstream validation and resurface as a
    misleading ``ValueError``.
    """

    def __init__(self, section: str = "", message: str = "") -> None:
        self.section = section
        if not message:
            message = f"cancelled at {section}" if section else "cancelled"
        super().__init__(message)
