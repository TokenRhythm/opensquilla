"""Isolated server-side ingestion for the versioned telemetry protocol."""

from opensquilla.telemetry.server.collector import create_collector_app
from opensquilla.telemetry.server.settings import CollectorSettings
from opensquilla.telemetry.server.storage import (
    BatchConflictError,
    EventConflictError,
    IngestReceipt,
    StorageCompatibilityError,
    StorageStats,
    TelemetryIngestStorage,
)

__all__ = [
    "BatchConflictError",
    "CollectorSettings",
    "EventConflictError",
    "IngestReceipt",
    "StorageCompatibilityError",
    "StorageStats",
    "TelemetryIngestStorage",
    "create_collector_app",
]
