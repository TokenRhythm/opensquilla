"""Versioned feature extraction primitives for the local routing models."""

from .lightgbm380 import FEATURE_NAMES_380, LightGBM380Featurizer
from .numeric import (
    extract_context5,
    extract_numeric88,
    extract_previous_signals19,
    extract_route_history16,
    extract_rules48,
)
from .registry import NUMERIC88_REGISTRY, numeric88_contract
from .text import (
    SEGMENT_KINDS,
    SegmentKind,
    TextSegments,
    extract_text_segments,
    truncate_content_tokens,
)

__all__ = [
    "FEATURE_NAMES_380",
    "LightGBM380Featurizer",
    "NUMERIC88_REGISTRY",
    "SEGMENT_KINDS",
    "SegmentKind",
    "TextSegments",
    "extract_context5",
    "extract_numeric88",
    "extract_previous_signals19",
    "extract_route_history16",
    "extract_rules48",
    "extract_text_segments",
    "numeric88_contract",
    "truncate_content_tokens",
]
