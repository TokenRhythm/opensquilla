"""Backward-compatible imports for the wizard application boundary."""

from __future__ import annotations

from opensquilla.application.wizard import (
    WIZARD_DEFINITIONS,
    WizardField,
    WizardFieldType,
    WizardRegistry,
    WizardSession,
    WizardStep,
    get_wizard_registry,
    reset_wizard_registry,
)

__all__ = [
    "WIZARD_DEFINITIONS",
    "WizardField",
    "WizardFieldType",
    "WizardRegistry",
    "WizardSession",
    "WizardStep",
    "get_wizard_registry",
    "reset_wizard_registry",
]
