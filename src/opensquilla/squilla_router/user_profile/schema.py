"""Provider-free value types and vocabularies for the offline producer."""

from __future__ import annotations

from dataclasses import dataclass

# The six capability axes the offline doc (§1.4) labels sessions with. The long
# tail outside this set is dropped by ``capability_prior``'s top-N truncation,
# so the weights may sum to less than 1.
SIX_AXIS_CAPABILITIES: tuple[str, ...] = (
    "code_generation",
    "reasoning",
    "writing",
    "tool_use",
    "long_context",
    "format_following",
)

# The label an LLM assigns when it cannot place a session on any axis. Excluded
# from ``capability_prior``'s denominator (§1.4: 63 read, 3 unknown -> /60).
UNKNOWN_CAPABILITY = "unknown"

# Quality/latency tradeoff enum. ``UNKNOWN_TRADEOFF`` is the batch-level "can't
# tell"; the builder maps a losing/tied vote to ``None`` (absent), which the
# default profile supplies ``balanced``.
QUALITY_FIRST = "quality_first"
BALANCED = "balanced"
LATENCY_FIRST = "latency_first"
UNKNOWN_TRADEOFF = "unknown"
TRADEOFF_VALUES: frozenset[str] = frozenset({QUALITY_FIRST, BALANCED, LATENCY_FIRST})

# Cost sensitivity enum. ``UNKNOWN_COST_SENSITIVITY`` is the batch-level "can't
# tell"; the builder maps a losing/tied vote to ``None`` (absent), which the
# default profile supplies ``medium``.
HIGH_COST_SENSITIVITY = "high"
MEDIUM_COST_SENSITIVITY = "medium"
LOW_COST_SENSITIVITY = "low"
UNKNOWN_COST_SENSITIVITY = "unknown"
COST_SENSITIVITY_VALUES: frozenset[str] = frozenset(
    {HIGH_COST_SENSITIVITY, MEDIUM_COST_SENSITIVITY, LOW_COST_SENSITIVITY}
)

# A model mention's direction. Praise feeds ``positive_model_ids``, blame feeds
# ``negative_model_ids``; a model seen in both lands in neither (§1.4 glm-5.2).
PRAISE = "praise"
BLAME = "blame"
MENTION_DIRECTIONS: frozenset[str] = frozenset({PRAISE, BLAME})


@dataclass(frozen=True)
class SessionTranscript:
    """One session rendered to plain text, ready to feed the LLM.

    ``text`` is already role-prefixed and truncated by ``extractor``; the raw
    transcript never leaves that module.
    """

    session_id: str
    text: str


@dataclass(frozen=True)
class SessionLabel:
    """One session's primary-capability label from the LLM."""

    session_id: str
    capability: str  # a member of SIX_AXIS_CAPABILITIES or UNKNOWN_CAPABILITY
    confidence: float = 0.0


@dataclass(frozen=True)
class ModelMention:
    """A named, directional model evaluation the LLM extracted from a batch."""

    model_id: str
    direction: str  # PRAISE | BLAME
    session_ids: tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True)
class BatchAnalysis:
    """The LLM's structured verdict on one batch of sessions.

    ``ok=False`` marks a batch that failed to parse or errored mid-stream; the
    builder skips it (best-effort) rather than aborting the whole run.
    """

    ok: bool
    session_labels: tuple[SessionLabel, ...] = ()
    tradeoff: str | None = None  # a TRADEOFF_VALUES member, UNKNOWN_TRADEOFF, or None
    tradeoff_confidence: float = 0.0
    tradeoff_session_ids: tuple[str, ...] = ()
    cost_sensitivity: str | None = (
        None  # a COST_SENSITIVITY_VALUES member, UNKNOWN_COST_SENSITIVITY, or None  # noqa: E501
    )
    cost_sensitivity_confidence: float = 0.0
    model_mentions: tuple[ModelMention, ...] = ()
    # Session ids actually sent in this batch — the honest denominator anchor
    # even when the LLM forgets to label one.
    session_ids: tuple[str, ...] = ()

    @classmethod
    def failed(cls, session_ids: tuple[str, ...] = ()) -> BatchAnalysis:
        return cls(ok=False, session_ids=session_ids)


__all__ = [
    "BALANCED",
    "BLAME",
    "COST_SENSITIVITY_VALUES",
    "HIGH_COST_SENSITIVITY",
    "LATENCY_FIRST",
    "LOW_COST_SENSITIVITY",
    "MEDIUM_COST_SENSITIVITY",
    "MENTION_DIRECTIONS",
    "PRAISE",
    "QUALITY_FIRST",
    "SIX_AXIS_CAPABILITIES",
    "TRADEOFF_VALUES",
    "UNKNOWN_CAPABILITY",
    "UNKNOWN_COST_SENSITIVITY",
    "UNKNOWN_TRADEOFF",
    "BatchAnalysis",
    "ModelMention",
    "SessionLabel",
    "SessionTranscript",
]
