"""Compatibility exports for the package-neutral silent-reply protocol."""

from opensquilla.silent_reply import (
    HEARTBEAT_ACK_TOKEN,
    NO_REPLY_TOKEN,
    SILENT_REPLY_NOT_ALLOWED_CODE,
    SILENT_REPLY_NOT_ALLOWED_MESSAGE,
    SILENT_REPLY_SENTINELS,
    HistoricalSilentReplySanitization,
    SilentReplyDelivery,
    SilentReplyNormalization,
    SilentReplySegmentsNormalization,
    SilentReplySuppressionReason,
    is_silent_reply_prefix,
    normalize_silent_reply,
    sanitize_historical_silent_reply,
    sanitize_silent_reply_segments,
)

__all__ = [
    "HEARTBEAT_ACK_TOKEN",
    "HistoricalSilentReplySanitization",
    "NO_REPLY_TOKEN",
    "SILENT_REPLY_SENTINELS",
    "SILENT_REPLY_NOT_ALLOWED_CODE",
    "SILENT_REPLY_NOT_ALLOWED_MESSAGE",
    "SilentReplyDelivery",
    "SilentReplyNormalization",
    "SilentReplySegmentsNormalization",
    "SilentReplySuppressionReason",
    "is_silent_reply_prefix",
    "normalize_silent_reply",
    "sanitize_historical_silent_reply",
    "sanitize_silent_reply_segments",
]
