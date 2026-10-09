"""Fixed four-tier single-model routing state machine.

The state machine accepts the legacy deterministic random mock, a hash-pinned
model set produced by routing-training-platform, or the remote Jev adapter. Both classifier
heads receive the same complete route-before input; policy rules decide which
head is authoritative for a turn.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, Protocol, cast

MODE: Final = "four_tier_mapping"
SCHEMA_VERSION: Final = "fixed-four-tier-v2-v4"
LEGACY_SCHEMA_VERSIONS: Final = frozenset({"fixed-four-tier-v2-mock-v2"})
READABLE_SCHEMA_VERSIONS: Final = frozenset(
    {SCHEMA_VERSION, "fixed-four-tier-v2-v3", *LEGACY_SCHEMA_VERSIONS}
)
RULE_VERSION: Final = "fixed-four-tier-v2-rules-v2"
MOCK_CLASSIFIER_VERSION: Final = "random-mock-v2"
FEATURE_SCHEMA_VERSION: Final = "fixed-four-tier-v2-features-mock-v2"
FEATURE_VECTOR_DIM: Final = 413
FEATURE_VECTOR_STATUS: Final = "mock_not_materialized"
_MAX_SEGMENT_CHARS: Final = 2_040
_HISTORY_USER_MESSAGES: Final = 4

type Intent = Literal["continue", "redo", "new_task"]
type Tier = Literal["c0", "c1", "c2", "c3"]
type ClassifierSource = Literal["rule", "classifier", "fallback", "not_run"]
type ClassifierRunStatus = Literal["ran", "not_run", "error"]
type ContextAction = Literal["keep", "reset"]
type QualityFailureReason = Literal["explicit_correction", "validation_failure", "no_progress"]
type FeatureAuditInputContract = Literal[
    "legacy_mock_snapshot",
    "canonical_router_input",
]
type AttachmentModality = Literal[
    "document",
    "image",
    "audio",
    "video",
    "archive",
    "other",
]

INTENTS: Final[tuple[Intent, ...]] = ("continue", "redo", "new_task")
TIERS: Final[tuple[Tier, ...]] = ("c0", "c1", "c2", "c3")
QUALITY_FAILURE_REASONS: Final = ("explicit_correction", "validation_failure", "no_progress")
FIXED_FOUR_TIER_DEPLOYMENT_SPECS: Final[
    tuple[tuple[Tier, str, str, Literal["thinking", "max"], str], ...]
] = (
    ("c0", "openrouter", "qwen/qwen3.7-flash", "thinking", "qwen3.7-flash-thinking"),
    ("c1", "openrouter", "deepseek/deepseek-v4-flash", "max", "deepseek-v4-flash-0731"),
    ("c2", "openrouter", "deepseek/deepseek-v4-pro", "max", "deepseek-v4-pro-0813"),
    ("c3", "openrouter", "z-ai/glm-5.3", "max", "glm-5.3"),
)
ATTACHMENT_MODALITIES: Final[tuple[AttachmentModality, ...]] = (
    "document",
    "image",
    "audio",
    "video",
    "archive",
    "other",
)

_PREVIOUS_USAGE_REQUIRED_KEYS: Final = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    }
)
_PREVIOUS_EXECUTION_KEYS: Final = frozenset(
    {
        "route_id",
        "execution_status",
        "error_code",
        "response_id",
        "attempt_ids",
        "retry_count",
    }
)
_REGISTERED_FEATURE_CONTRACTS: Final = {
    "lightgbm_380.v1": ("lightgbm", 380),
    "bert_text88.v1": ("bert", 88),
}
_REGISTERED_IDENTITY_HASH_FIELDS: Final = (
    "model_manifest_hash",
    "artifact_closure_hash",
    "runner_digest",
    "environment_digest",
)


def fixed_four_tier_semantic_policy_config(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the portable policy projection used by ``policy_hash``.

    Registered model filesystem locations select where one already-identified
    artifact is loaded; they do not change routing semantics.  Keeping those
    host-local paths in the policy fingerprint made the same Manifest produce
    different policy identities after an otherwise transparent deployment
    move.  The exact ``model_set_id`` and Manifest Hash remain part of the
    projection, as do authorization and the frozen ladder. Deprecated confidence
    thresholds no longer affect v4 decisions and are excluded from its identity.
    """

    payload = dict(value)
    for field in ("intent_min_confidence", "tier_min_confidence", "min_margin"):
        payload.pop(field, None)
    raw_classifier = payload.get("classifier")
    if isinstance(raw_classifier, Mapping):
        classifier = dict(raw_classifier)
        if classifier.get("backend") == "registered_model":
            classifier.pop("artifact_root", None)
            classifier.pop("metadata_db", None)
        payload["classifier"] = classifier
    return payload


def _argmax_label(probabilities: Mapping[str, float], labels: Sequence[str]) -> str:
    """Resolve ties by the fixed class order, independently of mapping order."""

    return max(labels, key=probabilities.__getitem__)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _is_tagged_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _is_sha256(value.removeprefix("sha256:"))
    )


def _validate_registered_classifier_contract(
    *,
    identity: Mapping[str, Any] | None,
    feature_schema_version: str,
    feature_vector_dim: int | None,
    feature_vector_status: str,
) -> None:
    contract = _REGISTERED_FEATURE_CONTRACTS.get(feature_schema_version)
    if contract is None:
        raise ValueError("registered model decision feature schema is incompatible")
    expected_model_type, expected_dimension = contract
    if feature_vector_dim != expected_dimension:
        raise ValueError("registered model decision feature dimension is incompatible")
    if feature_vector_status != "materialized":
        raise ValueError("registered model decision features must be materialized")
    if not isinstance(identity, Mapping) or not identity:
        raise ValueError("registered model decision requires a runtime identity")

    required_fields = {
        "schema_version",
        "model_set_id",
        *_REGISTERED_IDENTITY_HASH_FIELDS,
        "model_type",
        "execution_mode",
        "registry_status",
        "input_schema_version",
    }
    if not required_fields.issubset(identity):
        raise ValueError("registered model decision runtime identity is incomplete")
    if identity.get("schema_version") != "local_runner_identity.v2":
        raise ValueError("registered model decision runtime identity schema is incompatible")
    model_set_id = identity.get("model_set_id")
    if (
        not isinstance(model_set_id, str)
        or not model_set_id.strip()
        or model_set_id != model_set_id.strip()
    ):
        raise ValueError("registered model decision has an invalid model-set identity")
    for field_name in _REGISTERED_IDENTITY_HASH_FIELDS:
        if not _is_tagged_sha256(identity.get(field_name)):
            raise ValueError(f"registered model decision runtime identity has invalid {field_name}")
    if identity.get("model_type") != expected_model_type:
        raise ValueError("registered model decision model type conflicts with its feature schema")
    if identity.get("execution_mode") != "native_embedded":
        raise ValueError("registered model decision execution mode is incompatible")
    if identity.get("registry_status") not in {"VALIDATED", "CANDIDATE"}:
        raise ValueError("registered model decision registry status is incompatible")
    if identity.get("input_schema_version") != feature_schema_version:
        raise ValueError("registered model decision runtime input schema is inconsistent")


def _validate_jev_classifier_contract(
    *,
    identity: Mapping[str, Any] | None,
    feature_schema_version: str,
    feature_vector_dim: int | None,
    feature_vector_status: str,
) -> None:
    """Remote evidence must not masquerade as local materialized/native evidence."""
    if (
        feature_schema_version != "jev_router_input.v1"
        or feature_vector_dim is not None
        or feature_vector_status != "remote_evaluated"
    ):
        raise ValueError("Jev decision remote feature contract is incompatible")
    if not isinstance(identity, Mapping) or (
        identity.get("schema_version") != "jev_classifier_identity.v1"
        or identity.get("model_id") != "typesafe-ai/jev"
        or identity.get("endpoint") != "https://ai-gateway.vercel.sh/v1/evaluate"
        or identity.get("execution_mode") != "remote_gateway"
        or identity.get("input_schema_version") != feature_schema_version
        or identity.get("prompt_version") != "jev-four-tier-questions.v1"
        or not _is_tagged_sha256(identity.get("prompt_hash"))
        or identity.get("confidence_semantics")
        != "selected_class_probability; vendor confidence retained in raw receipt"
        or identity.get("tier_selection_mode") not in {None, "probability_argmax.v1"}
        or "model_revision" not in identity
        or identity.get("model_revision") is not None
    ):
        raise ValueError("Jev decision remote identity is unavailable or incompatible")


_NEW_TASK_CONTROL_EVENTS = frozenset(
    {
        "new",
        "new_task",
        "new-task",
        "create_task",
        "create-task",
    }
)
_REDO_CONTROL_EVENTS = frozenset(
    {
        "redo",
        "regenerate",
        "retry_response",
        "retry-response",
    }
)
_NEW_TASK_TEXT = re.compile(
    r"^\s*(?:请\s*)?(?:/new(?:\s|$)|新建(?:一个)?任务(?:\s|$|[：:，,])|"
    r"开始(?:一个)?新任务(?:\s|$|[：:，,])|创建(?:一个)?新任务(?:\s|$|[：:，,])|"
    r"(?:start|create)\s+(?:a\s+)?new\s+task(?:\s|$|[,:]))",
    re.IGNORECASE,
)
_REDO_TEXT = re.compile(
    r"^\s*(?:请\s*)?(?:/redo(?:\s|$)|/regenerate(?:\s|$)|"
    r"重新生成(?:一下|一次|上一个回答)?(?:\s|$|[：:，,])|"
    r"重新回答(?:一下|一次)?(?:\s|$|[：:，,])|"
    r"(?:redo|regenerate)(?:\s|$|[,:]))",
    re.IGNORECASE,
)


class FixedFourTierRoutingError(RuntimeError):
    """Raised when the isolated routing contract cannot be satisfied."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ClassifierPrediction:
    """Raw classifier result before state-machine fallback rules are applied."""

    label: str
    probabilities: Mapping[str, float] | None
    confidence: float | None
    version: str


class IntentClassifier(Protocol):
    """Production seam for the eventual local intent model."""

    version: str

    def predict(self, snapshot: Mapping[str, Any]) -> ClassifierPrediction: ...


class TierClassifier(Protocol):
    """Production seam for the eventual local tier model."""

    version: str

    def predict(
        self,
        snapshot: Mapping[str, Any],
        allowed_tiers: Sequence[Tier],
    ) -> ClassifierPrediction: ...


def _stable_mock_choice(
    snapshot: Mapping[str, Any],
    labels: Sequence[str],
    *,
    seed: int,
    namespace: str,
) -> str:
    """Return a pseudo-random label that is stable for one request snapshot.

    The first version used one shared mutable RNG.  Its output changed with
    cross-session scheduling order, which made an accepted request impossible
    to replay after a worker restart.  A request-local PRNG keeps this a real
    random mock while making ``mock_seed + input`` reproducible.
    """

    if not labels:
        raise ValueError("mock classifier labels must be non-empty")
    digest = hashlib.sha256(
        (
            namespace
            + "\0"
            + str(seed)
            + "\0"
            + json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        ).encode("utf-8")
    ).digest()
    return labels[random.Random(int.from_bytes(digest[:16], "big")).randrange(len(labels))]


class RandomMockIntentClassifier:
    """Request-stable random intent mock with the production output schema."""

    backend = "random_mock"
    version = MOCK_CLASSIFIER_VERSION
    feature_schema_version = FEATURE_SCHEMA_VERSION
    feature_vector_dim = FEATURE_VECTOR_DIM
    feature_vector_status = FEATURE_VECTOR_STATUS

    def __init__(self, seed: int | None = None) -> None:
        self._seed = seed if seed is not None else random.SystemRandom().getrandbits(64)

    def predict(self, snapshot: Mapping[str, Any]) -> ClassifierPrediction:
        selected = _stable_mock_choice(
            snapshot,
            INTENTS,
            seed=self._seed,
            namespace="intent",
        )
        probabilities: dict[str, float] = {
            label: 1.0 if label == selected else 0.0 for label in INTENTS
        }
        return ClassifierPrediction(
            label=selected,
            probabilities=probabilities,
            confidence=1.0,
            version=self.version,
        )


class RandomMockTierClassifier:
    """Request-stable random tier mock restricted to the allowed tier set."""

    backend = "random_mock"
    version = MOCK_CLASSIFIER_VERSION
    feature_schema_version = FEATURE_SCHEMA_VERSION
    feature_vector_dim = FEATURE_VECTOR_DIM
    feature_vector_status = FEATURE_VECTOR_STATUS

    def __init__(self, seed: int | None = None) -> None:
        self._seed = seed if seed is not None else random.SystemRandom().getrandbits(64)

    def predict(
        self,
        snapshot: Mapping[str, Any],
        allowed_tiers: Sequence[Tier],
    ) -> ClassifierPrediction:
        normalized = tuple(tier for tier in allowed_tiers if tier in TIERS)
        if not normalized:
            raise FixedFourTierRoutingError(
                "tier mock received an empty allowed tier set",
                reason="empty_allowed_tiers",
            )
        selected = _stable_mock_choice(
            snapshot,
            normalized,
            seed=self._seed,
            namespace="tier",
        )
        probabilities: dict[str, float] = {tier: 1.0 if tier == selected else 0.0 for tier in TIERS}
        return ClassifierPrediction(
            label=selected,
            probabilities=probabilities,
            confidence=1.0,
            version=self.version,
        )


@dataclass(frozen=True)
class ClassificationAudit:
    """Final classifier decision with explicit ran/not-run/null semantics."""

    source: ClassifierSource
    run_status: ClassifierRunStatus
    prediction: str | None
    probabilities: Mapping[str, float] | None
    confidence: float | None
    final: str
    reason: str
    version: str | None
    # Jev alone supplies independent tier probabilities rather than a normalized
    # distribution. The owning decision identity authorizes this exception.
    probability_argmax: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.probability_argmax, bool):
            raise ValueError("four_tier_mapping classifier audit has invalid selection mode")
        if not isinstance(self.source, str) or self.source not in {
            "rule",
            "classifier",
            "fallback",
            "not_run",
        }:
            raise ValueError("four_tier_mapping classifier audit has an invalid source")
        if not isinstance(self.run_status, str) or self.run_status not in {
            "ran",
            "not_run",
            "error",
        }:
            raise ValueError("four_tier_mapping classifier audit has an invalid run status")
        if not isinstance(self.final, str) or not self.final.strip():
            raise ValueError("four_tier_mapping classifier audit requires a final value")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("four_tier_mapping classifier audit requires a reason")
        if self.prediction is not None and (
            not isinstance(self.prediction, str) or not self.prediction.strip()
        ):
            raise ValueError("four_tier_mapping classifier audit has an empty prediction")
        if self.version is not None and (
            not isinstance(self.version, str) or not self.version.strip()
        ):
            raise ValueError("four_tier_mapping classifier audit has an empty version")
        if self.confidence is not None and (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("four_tier_mapping classifier audit has invalid confidence")
        if self.probabilities is not None:
            if not isinstance(self.probabilities, Mapping):
                raise ValueError("four_tier_mapping classifier audit has invalid probabilities")
            normalized: dict[str, float] = {}
            for raw_label, raw_probability in self.probabilities.items():
                label = str(raw_label)
                if (
                    not label
                    or isinstance(raw_probability, bool)
                    or not isinstance(raw_probability, (int, float))
                    or not math.isfinite(float(raw_probability))
                    or not 0.0 <= float(raw_probability) <= 1.0
                ):
                    raise ValueError("four_tier_mapping classifier audit has invalid probabilities")
                normalized[label] = float(raw_probability)
            if not normalized or (
                not self.probability_argmax
                and not math.isclose(sum(normalized.values()), 1.0, rel_tol=1e-6, abs_tol=1e-6)
            ):
                raise ValueError("four_tier_mapping classifier audit has invalid probabilities")
            if self.probability_argmax and set(normalized) != set(TIERS):
                raise ValueError("Jev argmax audit requires all four tier probabilities")
            object.__setattr__(self, "probabilities", normalized)
        if self.run_status == "not_run" and any(
            value is not None for value in (self.prediction, self.probabilities, self.confidence)
        ):
            raise ValueError("four_tier_mapping classifier audit cannot report output when not run")
        if self.source in {"rule", "not_run"} and self.run_status != "not_run":
            raise ValueError("four_tier_mapping classifier audit source conflicts with run status")
        complete_output_required = self.source == "classifier" or (
            self.source == "fallback" and self.run_status == "ran"
        )
        if complete_output_required:
            if (
                self.run_status != "ran"
                or self.prediction is None
                or self.probabilities is None
                or self.confidence is None
                or self.version is None
            ):
                raise ValueError("four_tier_mapping classifier audit is missing classifier output")
            if self.prediction not in self.probabilities:
                raise ValueError(
                    "four_tier_mapping classifier audit prediction is not in probabilities"
                )
            maximum = max(self.probabilities.values())
            label_order = INTENTS if set(self.probabilities) == set(INTENTS) else TIERS
            if (
                set(self.probabilities) != set(label_order)
                or (_argmax_label(self.probabilities, label_order) != self.prediction)
                or not math.isclose(
                    float(self.confidence),
                    maximum,
                    rel_tol=1e-6,
                    abs_tol=1e-6,
                )
            ):
                raise ValueError("four_tier_mapping classifier audit output is inconsistent")
            if self.source == "classifier" and self.final != self.prediction:
                raise ValueError("four_tier_mapping classifier audit final value is inconsistent")
        if (
            self.source == "fallback"
            and self.run_status == "error"
            and any(
                value is not None
                for value in (self.prediction, self.probabilities, self.confidence)
            )
        ):
            raise ValueError("four_tier_mapping classifier error cannot carry trusted output")

    def trace(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "run_status": self.run_status,
            "prediction": self.prediction,
            "probabilities": (dict(self.probabilities) if self.probabilities is not None else None),
            "confidence": self.confidence,
            "final": self.final,
            "reason": self.reason,
            "version": self.version,
        }


@dataclass(frozen=True)
class RoutingRequest:
    """Truth-free, route-before data visible to the classifiers."""

    session_id: str
    request_id: str
    message: str
    input_message_id: str | None = None
    task_anchor: str | None = None
    user_history: tuple[str, ...] = ()
    previous_assistant_text: str | None = None
    previous_assistant_usage: Mapping[str, Any] | None = None
    previous_outcome: Literal["success", "failure", "clarification", "unknown"] = "unknown"
    route_history: tuple[Mapping[str, Any], ...] = ()
    context: Mapping[str, Any] | None = None
    tool_state: Mapping[str, Any] | None = None
    attachments: tuple[Mapping[str, Any], ...] = ()
    attachment_count: int = 0
    attachment_modalities: tuple[AttachmentModality, ...] | None = None
    control_event: str | None = None
    quality_failure_reason: QualityFailureReason | None = None
    quality_retry_budget_remaining: int = 0
    quality_retry_already_used: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str):
            raise ValueError("session_id must be a string")
        if not isinstance(self.request_id, str):
            raise ValueError("request_id must be a string")
        if not isinstance(self.message, str):
            raise ValueError("message must be a string")
        if self.input_message_id is not None and (
            not isinstance(self.input_message_id, str) or not self.input_message_id.strip()
        ):
            raise ValueError("input_message_id must be non-empty when provided")
        if self.task_anchor is not None and not isinstance(self.task_anchor, str):
            raise ValueError("task_anchor must be a string when provided")
        if not isinstance(self.user_history, tuple) or any(
            not isinstance(value, str) for value in self.user_history
        ):
            raise ValueError("user_history must be a tuple of strings")
        if self.previous_assistant_text is not None and not isinstance(
            self.previous_assistant_text, str
        ):
            raise ValueError("previous_assistant_text must be a string when provided")
        if self.previous_assistant_usage is not None and not isinstance(
            self.previous_assistant_usage, Mapping
        ):
            raise ValueError("previous_assistant_usage must be an object when provided")
        if self.previous_outcome not in {"success", "failure", "clarification", "unknown"}:
            raise ValueError("previous_outcome is invalid")
        if (
            self.quality_failure_reason is not None
            and self.quality_failure_reason not in QUALITY_FAILURE_REASONS
        ):
            raise ValueError("quality_failure_reason is invalid")
        if (
            isinstance(self.quality_retry_budget_remaining, bool)
            or not isinstance(self.quality_retry_budget_remaining, int)
            or self.quality_retry_budget_remaining < 0
        ):
            raise ValueError("quality_retry_budget_remaining must be a non-negative integer")
        if not isinstance(self.quality_retry_already_used, bool):
            raise ValueError("quality_retry_already_used must be a boolean")
        if (
            not isinstance(self.route_history, tuple)
            or len(self.route_history) > 5
            or any(not isinstance(value, Mapping) for value in self.route_history)
        ):
            raise ValueError("route_history must contain at most five objects")
        for name, value in (("context", self.context), ("tool_state", self.tool_state)):
            if value is not None and not isinstance(value, Mapping):
                raise ValueError(f"{name} must be an object when provided")
        if not isinstance(self.attachments, tuple) or any(
            not isinstance(value, Mapping) for value in self.attachments
        ):
            raise ValueError("attachments must be a tuple of objects")
        if self.control_event is not None and not isinstance(self.control_event, str):
            raise ValueError("control_event must be a string when provided")
        if (
            isinstance(self.attachment_count, bool)
            or not isinstance(self.attachment_count, int)
            or self.attachment_count < 0
        ):
            raise ValueError("attachment_count must be a non-negative integer")
        if self.attachment_modalities is not None:
            if not isinstance(self.attachment_modalities, tuple) or any(
                value not in ATTACHMENT_MODALITIES for value in self.attachment_modalities
            ):
                raise ValueError("attachment_modalities must be a normalized tuple")
            if len(self.attachment_modalities) != self.attachment_count:
                object.__setattr__(self, "attachment_modalities", None)
        if self.attachment_count == 0 and self.attachment_modalities is None:
            object.__setattr__(self, "attachment_modalities", ())


def normalize_attachment_modalities(
    attachments: Sequence[object],
) -> tuple[AttachmentModality, ...] | None:
    """Return a content-free, all-or-nothing attachment metadata bundle.

    A missing or malformed item makes the whole optional bundle unavailable;
    callers must then zero the modality block and set its missing mask.  An
    empty attachment list is a complete empty bundle, not missing metadata.
    """

    normalized: list[AttachmentModality] = []
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            return None
        raw_kind = next(
            (
                value.strip().casefold()
                for key in ("mime", "mime_type", "content_type", "type")
                if isinstance((value := attachment.get(key)), str) and value.strip()
            ),
            None,
        )
        if raw_kind is None:
            return None
        media_type = raw_kind.split(";", 1)[0].strip()
        if media_type.startswith("image/") or media_type == "image":
            modality: AttachmentModality = "image"
        elif media_type.startswith("audio/") or media_type == "audio":
            modality = "audio"
        elif media_type.startswith("video/") or media_type == "video":
            modality = "video"
        elif (
            media_type.startswith("text/")
            or media_type
            in {
                "application/json",
                "application/pdf",
                "application/rtf",
                "application/xml",
                "document",
            }
            or media_type.startswith("application/vnd.openxmlformats-officedocument")
            or media_type.startswith("application/vnd.oasis.opendocument")
        ):
            modality = "document"
        elif media_type in {
            "application/gzip",
            "application/x-7z-compressed",
            "application/x-rar-compressed",
            "application/x-tar",
            "application/zip",
            "archive",
        } or media_type.endswith("+zip"):
            modality = "archive"
        else:
            modality = "other"
        normalized.append(modality)
    return tuple(normalized)


@dataclass(frozen=True)
class FeatureInputAudit:
    """Content-safe audit summary of the classifier's effective text input.

    Registered classifiers audit the canonical ``RouterInput`` that their
    training runtime consumes.  Legacy mocks retain their task-scoped audit
    vocabulary so persisted mock-v2 traces remain readable. Raw transcript
    text stays in durable transcript storage in both cases.
    """

    input_contract: FeatureAuditInputContract
    classifier_snapshot_hash: str
    current_request_content_hash: str
    history_content_hashes: tuple[str, ...]
    history_aggregate_content_hash: str
    history_observed_count: int
    history_retained_count: int
    previous_assistant_content_hash: str | None
    input_message_id: str | None
    task_start_input_message_id: str | None
    attachment_count: int
    attachment_modalities: tuple[AttachmentModality, ...]
    missing_context: bool
    missing_usage: bool
    missing_execution: bool
    missing_attachment_metadata: bool
    truncated_current_request: bool
    truncated_history: bool
    truncated_history_window: bool
    truncated_history_segments: tuple[bool, ...]
    truncated_previous_assistant: bool

    def __post_init__(self) -> None:
        if self.input_contract not in {
            "legacy_mock_snapshot",
            "canonical_router_input",
        }:
            raise ValueError("four_tier_mapping feature audit input contract is invalid")
        for field_name, value in (
            ("classifier_snapshot_hash", self.classifier_snapshot_hash),
            ("current_request_content_hash", self.current_request_content_hash),
            ("history_aggregate_content_hash", self.history_aggregate_content_hash),
        ):
            if not _is_sha256(value):
                raise ValueError(f"four_tier_mapping feature audit has invalid {field_name}")
        if any(not _is_sha256(value) for value in self.history_content_hashes):
            raise ValueError("four_tier_mapping feature audit has invalid history content hash")
        if self.previous_assistant_content_hash is not None and not _is_sha256(
            self.previous_assistant_content_hash
        ):
            raise ValueError(
                "four_tier_mapping feature audit has invalid previous assistant content hash"
            )
        for count_field_name, count_value in (
            ("history_observed_count", self.history_observed_count),
            ("history_retained_count", self.history_retained_count),
            ("attachment_count", self.attachment_count),
        ):
            if isinstance(count_value, bool) or not isinstance(count_value, int) or count_value < 0:
                raise ValueError(f"four_tier_mapping feature audit has invalid {count_field_name}")
        if self.history_retained_count != len(self.history_content_hashes):
            raise ValueError(
                "four_tier_mapping feature audit retained history count is inconsistent"
            )
        if self.history_retained_count > self.history_observed_count:
            raise ValueError("four_tier_mapping feature audit history counts are inconsistent")
        if len(self.truncated_history_segments) != self.history_retained_count:
            raise ValueError(
                "four_tier_mapping feature audit history truncation mask is inconsistent"
            )
        if self.truncated_history != (
            self.truncated_history_window or any(self.truncated_history_segments)
        ):
            raise ValueError("four_tier_mapping feature audit history truncation is inconsistent")
        for bool_field_name, bool_value in (
            ("missing_context", self.missing_context),
            ("missing_usage", self.missing_usage),
            ("missing_execution", self.missing_execution),
            ("missing_attachment_metadata", self.missing_attachment_metadata),
            ("truncated_current_request", self.truncated_current_request),
            ("truncated_history", self.truncated_history),
            ("truncated_history_window", self.truncated_history_window),
            ("truncated_previous_assistant", self.truncated_previous_assistant),
        ):
            if not isinstance(bool_value, bool):
                raise ValueError(f"four_tier_mapping feature audit has invalid {bool_field_name}")
        if any(not isinstance(value, bool) for value in self.truncated_history_segments):
            raise ValueError(
                "four_tier_mapping feature audit has invalid history segment truncation"
            )
        if any(value not in ATTACHMENT_MODALITIES for value in self.attachment_modalities):
            raise ValueError("four_tier_mapping feature audit has invalid attachment modality")
        if self.missing_attachment_metadata:
            if self.attachment_modalities:
                raise ValueError("missing attachment metadata must zero its modality bundle")
        elif len(self.attachment_modalities) != self.attachment_count:
            raise ValueError("attachment metadata must cover every attachment")
        for ref_field_name, ref_value in (
            ("input_message_id", self.input_message_id),
            ("task_start_input_message_id", self.task_start_input_message_id),
        ):
            if ref_value is not None and not ref_value.strip():
                raise ValueError(f"four_tier_mapping feature audit has invalid {ref_field_name}")

    def trace(self) -> dict[str, Any]:
        if self.input_contract == "canonical_router_input":
            content_hashes = {
                "current_request": self.current_request_content_hash,
                "history_user_segments": list(self.history_content_hashes),
                "history_user_aggregate": self.history_aggregate_content_hash,
                "previous_answer": self.previous_assistant_content_hash,
            }
            previous_truncation_key = "previous_answer"
        else:
            content_hashes = {
                "current_request": self.current_request_content_hash,
                "task_user_history_segments": list(self.history_content_hashes),
                "task_user_history_aggregate": self.history_aggregate_content_hash,
                "previous_assistant": self.previous_assistant_content_hash,
            }
            previous_truncation_key = "previous_assistant"
        return {
            "classifier_snapshot_hash": self.classifier_snapshot_hash,
            "content_hashes": content_hashes,
            "history_observed_count": self.history_observed_count,
            "history_retained_count": self.history_retained_count,
            "attachment_count": self.attachment_count,
            "attachment_modalities": list(self.attachment_modalities),
            "transcript_refs": {
                "input_message_id": self.input_message_id,
                "task_start_input_message_id": self.task_start_input_message_id,
            },
            "missing": {
                "context": self.missing_context,
                "usage": self.missing_usage,
                "execution": self.missing_execution,
                "attachment_metadata": self.missing_attachment_metadata,
            },
            "truncated": {
                "current_request": self.truncated_current_request,
                "history": self.truncated_history,
                "history_window": self.truncated_history_window,
                "history_segments": list(self.truncated_history_segments),
                previous_truncation_key: self.truncated_previous_assistant,
            },
        }

    @classmethod
    def from_trace(cls, value: object) -> FeatureInputAudit:
        if not isinstance(value, Mapping):
            raise ValueError("four_tier_mapping decision is missing its feature input audit")
        if set(value) != {
            "classifier_snapshot_hash",
            "content_hashes",
            "history_observed_count",
            "history_retained_count",
            "attachment_count",
            "attachment_modalities",
            "transcript_refs",
            "missing",
            "truncated",
        }:
            raise ValueError("four_tier_mapping feature input audit shape is incompatible")
        content_hashes = value.get("content_hashes")
        transcript_refs = value.get("transcript_refs")
        missing = value.get("missing")
        truncated = value.get("truncated")
        if not all(
            isinstance(item, Mapping)
            for item in (content_hashes, transcript_refs, missing, truncated)
        ):
            raise ValueError("four_tier_mapping feature input audit is malformed")
        content_hashes = cast(Mapping[str, Any], content_hashes)
        transcript_refs = cast(Mapping[str, Any], transcript_refs)
        missing = cast(Mapping[str, Any], missing)
        truncated = cast(Mapping[str, Any], truncated)
        legacy_content_hash_fields = {
            "current_request",
            "task_user_history_segments",
            "task_user_history_aggregate",
            "previous_assistant",
        }
        canonical_content_hash_fields = {
            "current_request",
            "history_user_segments",
            "history_user_aggregate",
            "previous_answer",
        }
        content_hash_fields = set(content_hashes)
        if content_hash_fields == canonical_content_hash_fields:
            input_contract: FeatureAuditInputContract = "canonical_router_input"
            history_segments_key = "history_user_segments"
            history_aggregate_key = "history_user_aggregate"
            previous_answer_key = "previous_answer"
        elif content_hash_fields == legacy_content_hash_fields:
            input_contract = "legacy_mock_snapshot"
            history_segments_key = "task_user_history_segments"
            history_aggregate_key = "task_user_history_aggregate"
            previous_answer_key = "previous_assistant"
        else:
            raise ValueError("four_tier_mapping feature content hashes are incompatible")
        if set(transcript_refs) != {
            "input_message_id",
            "task_start_input_message_id",
        }:
            raise ValueError("four_tier_mapping feature transcript refs are incompatible")
        if set(missing) != {
            "context",
            "usage",
            "execution",
            "attachment_metadata",
        }:
            raise ValueError("four_tier_mapping feature missing mask is incompatible")
        expected_truncated_fields = {
            "current_request",
            "history",
            "history_window",
            "history_segments",
            previous_answer_key,
        }
        if set(truncated) != expected_truncated_fields:
            raise ValueError("four_tier_mapping feature truncation mask is incompatible")
        history_hashes = content_hashes.get(history_segments_key)
        history_segments = truncated.get("history_segments")
        attachment_modalities = value.get("attachment_modalities")
        if not isinstance(history_hashes, Sequence) or isinstance(history_hashes, (str, bytes)):
            raise ValueError("four_tier_mapping feature input history hashes are malformed")
        if not isinstance(history_segments, Sequence) or isinstance(history_segments, (str, bytes)):
            raise ValueError("four_tier_mapping feature input history mask is malformed")
        if not isinstance(attachment_modalities, Sequence) or isinstance(
            attachment_modalities, (str, bytes)
        ):
            raise ValueError("four_tier_mapping feature input attachment bundle is malformed")

        def required_bool(container: Mapping[str, Any], name: str) -> bool:
            raw = container.get(name)
            if not isinstance(raw, bool):
                raise ValueError(f"four_tier_mapping feature input has invalid {name}")
            return raw

        def optional_ref(name: str) -> str | None:
            raw = transcript_refs.get(name)
            if raw is None:
                return None
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"four_tier_mapping feature input has invalid {name}")
            return raw

        def required_count(name: str) -> int:
            raw = value.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ValueError(f"four_tier_mapping feature input has invalid {name}")
            return raw

        previous_hash = content_hashes.get(previous_answer_key)
        if previous_hash is not None and not isinstance(previous_hash, str):
            raise ValueError("four_tier_mapping feature input has invalid previous answer hash")

        def required_hash(container: Mapping[str, Any], name: str) -> str:
            raw = container.get(name)
            if not isinstance(raw, str):
                raise ValueError(f"four_tier_mapping feature input has invalid {name}")
            return raw

        if any(not isinstance(item, str) for item in history_hashes):
            raise ValueError("four_tier_mapping feature input history hashes are malformed")
        return cls(
            input_contract=input_contract,
            classifier_snapshot_hash=required_hash(value, "classifier_snapshot_hash"),
            current_request_content_hash=required_hash(content_hashes, "current_request"),
            history_content_hashes=tuple(history_hashes),
            history_aggregate_content_hash=required_hash(content_hashes, history_aggregate_key),
            history_observed_count=required_count("history_observed_count"),
            history_retained_count=required_count("history_retained_count"),
            previous_assistant_content_hash=previous_hash,
            input_message_id=optional_ref("input_message_id"),
            task_start_input_message_id=optional_ref("task_start_input_message_id"),
            attachment_count=required_count("attachment_count"),
            attachment_modalities=tuple(attachment_modalities),
            missing_context=required_bool(missing, "context"),
            missing_usage=required_bool(missing, "usage"),
            missing_execution=required_bool(missing, "execution"),
            missing_attachment_metadata=required_bool(missing, "attachment_metadata"),
            truncated_current_request=required_bool(truncated, "current_request"),
            truncated_history=required_bool(truncated, "history"),
            truncated_history_window=required_bool(truncated, "history_window"),
            truncated_history_segments=tuple(
                required_bool({"segment": item}, "segment") for item in history_segments
            ),
            truncated_previous_assistant=required_bool(truncated, previous_answer_key),
        )


@dataclass(frozen=True)
class FixedFourTierDecision:
    """One atomic routing decision and its task-context action."""

    route_id: str
    task_id: str
    request_id: str
    intent: ClassificationAudit
    tier: ClassificationAudit
    previous_tier: Tier | None
    final_tier: Tier
    switched: bool
    switch_reason: str
    context_action: ContextAction
    history_turns_to_keep: int
    task_turn_index: int
    input_snapshot_hash: str
    tier_snapshot_hash: str | None
    feature_input_audit: FeatureInputAudit
    decided_at_ms: int
    policy_hash: str
    classifier_backend: str
    classifier_identity: Mapping[str, Any] | None
    feature_schema_version: str
    feature_vector_dim: int | None
    feature_vector_status: str
    effective_mock_seed: int | None
    schema_version: str = SCHEMA_VERSION
    quality_escalation_reason: QualityFailureReason | None = None
    quality_escalation_used: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.intent, ClassificationAudit) or not isinstance(
            self.tier, ClassificationAudit
        ):
            raise ValueError("four_tier_mapping decision classifier audit is malformed")
        if not isinstance(self.feature_input_audit, FeatureInputAudit):
            raise ValueError("four_tier_mapping decision feature input audit is malformed")
        if self.schema_version not in READABLE_SCHEMA_VERSIONS:
            raise ValueError("four_tier_mapping decision schema_version is incompatible")
        if self.classifier_backend not in {"random_mock", "registered_model", "injected", "jev"}:
            raise ValueError("four_tier_mapping decision classifier backend is incompatible")
        if self.classifier_identity is not None:
            if not isinstance(self.classifier_identity, Mapping) or not self.classifier_identity:
                raise ValueError("four_tier_mapping decision classifier identity is invalid")
            try:
                json.dumps(self.classifier_identity, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "four_tier_mapping decision classifier identity is not JSON-safe"
                ) from exc
        if not isinstance(self.feature_schema_version, str) or not self.feature_schema_version:
            raise ValueError("four_tier_mapping decision feature schema is invalid")
        if self.feature_vector_dim is not None and (
            isinstance(self.feature_vector_dim, bool)
            or not isinstance(self.feature_vector_dim, int)
            or self.feature_vector_dim <= 0
        ):
            raise ValueError("four_tier_mapping decision feature dimension is invalid")
        if self.feature_vector_status not in {
            "mock_not_materialized",
            "materialized",
            "remote_evaluated",
        }:
            raise ValueError("four_tier_mapping decision feature status is invalid")
        if self.feature_vector_status == "remote_evaluated" and self.classifier_backend != "jev":
            raise ValueError("remote classifier features require the Jev backend")
        if self.feature_vector_status == "materialized" and self.classifier_identity is None:
            raise ValueError("materialized classifier features require a runtime identity")
        if self.classifier_backend == "registered_model":
            _validate_registered_classifier_contract(
                identity=self.classifier_identity,
                feature_schema_version=self.feature_schema_version,
                feature_vector_dim=self.feature_vector_dim,
                feature_vector_status=self.feature_vector_status,
            )
            if self.feature_input_audit.input_contract != "canonical_router_input":
                raise ValueError("registered model decision requires a canonical RouterInput audit")
        if self.classifier_backend == "jev":
            _validate_jev_classifier_contract(
                identity=self.classifier_identity,
                feature_schema_version=self.feature_schema_version,
                feature_vector_dim=self.feature_vector_dim,
                feature_vector_status=self.feature_vector_status,
            )
            if self.feature_input_audit.input_contract != "canonical_router_input":
                raise ValueError("Jev decision requires a canonical RouterInput audit")
        jev_argmax = (
            self.classifier_backend == "jev"
            and isinstance(self.classifier_identity, Mapping)
            and self.classifier_identity.get("tier_selection_mode") == "probability_argmax.v1"
        )
        if self.intent.probability_argmax or self.tier.probability_argmax != (
            jev_argmax and self.tier.run_status == "ran"
        ):
            raise ValueError("Jev argmax audit conflicts with classifier identity")
        if jev_argmax and self.tier.run_status == "ran":
            selected = (
                self.tier.source == "classifier"
                and self.tier.reason == "classifier_argmax_selected"
            )
            allowed_blocked_intents = (
                {"continue", "redo"} if self.schema_version == SCHEMA_VERSION else {"redo"}
            )
            downgrade_blocked = (
                self.intent.final in allowed_blocked_intents
                and self.tier.source == "fallback"
                and self.tier.reason == f"{self.intent.final}_downgrade_blocked"
                and self.previous_tier in TIERS
                and self.final_tier == self.previous_tier
                and self.tier.prediction in TIERS
                and TIERS.index(self.tier.prediction) < TIERS.index(self.previous_tier)
            )
            quality_upgrade = (
                self.schema_version == SCHEMA_VERSION
                and self.quality_escalation_used
                and self.tier.source == "fallback"
                and self.tier.reason == "quality_failure_upgrade"
            )
            if not (selected or downgrade_blocked or quality_upgrade):
                raise ValueError("Jev argmax audit cannot use confidence or validation fallback")
        for field_name, value in (
            ("route_id", self.route_id),
            ("task_id", self.task_id),
            ("request_id", self.request_id),
            ("switch_reason", self.switch_reason),
        ):
            if not value.strip():
                raise ValueError(f"four_tier_mapping decision has invalid {field_name}")
        if self.intent.final not in INTENTS:
            raise ValueError("four_tier_mapping decision has invalid final intent")
        if self.intent.source == "not_run":
            raise ValueError("four_tier_mapping decision has invalid intent audit source")
        if self.tier.final not in TIERS or self.final_tier not in TIERS:
            raise ValueError("four_tier_mapping decision has invalid final tier")
        if self.tier.final != self.final_tier:
            raise ValueError("four_tier_mapping decision tier audit is inconsistent")
        if self.previous_tier is not None and self.previous_tier not in TIERS:
            raise ValueError("four_tier_mapping decision has invalid previous tier")
        if self.context_action not in {"keep", "reset"}:
            raise ValueError("four_tier_mapping decision has invalid context action")
        if not isinstance(self.switched, bool):
            raise ValueError("four_tier_mapping decision has invalid switched flag")
        expected_switched = self.previous_tier is not None and self.previous_tier != self.final_tier
        if self.switched != expected_switched:
            raise ValueError("four_tier_mapping decision switched flag is inconsistent")
        for numeric_field_name, numeric_value in (
            ("history_turns_to_keep", self.history_turns_to_keep),
            ("task_turn_index", self.task_turn_index),
            ("decided_at_ms", self.decided_at_ms),
        ):
            if (
                isinstance(numeric_value, bool)
                or not isinstance(numeric_value, int)
                or numeric_value < 0
            ):
                raise ValueError(f"four_tier_mapping decision has invalid {numeric_field_name}")
        if not _is_sha256(self.input_snapshot_hash):
            raise ValueError("four_tier_mapping decision has invalid input snapshot hash")
        if self.tier_snapshot_hash is not None and not _is_sha256(self.tier_snapshot_hash):
            raise ValueError("four_tier_mapping decision has invalid tier snapshot hash")
        if not _is_sha256(self.policy_hash):
            raise ValueError("four_tier_mapping decision has invalid policy hash")
        if self.effective_mock_seed is not None and (
            isinstance(self.effective_mock_seed, bool)
            or not isinstance(self.effective_mock_seed, int)
            or not 0 <= self.effective_mock_seed <= (1 << 64) - 1
        ):
            raise ValueError("four_tier_mapping decision has invalid effective mock seed")
        if (
            self.classifier_backend in {"registered_model", "jev"}
            and self.effective_mock_seed is not None
        ):
            raise ValueError("model decision cannot carry a mock seed")

        if (
            self.quality_escalation_reason is not None
            and self.quality_escalation_reason not in QUALITY_FAILURE_REASONS
        ):
            raise ValueError("four_tier_mapping decision has invalid quality escalation reason")
        if not isinstance(self.quality_escalation_used, bool):
            raise ValueError("four_tier_mapping decision has invalid quality escalation flag")
        if self.schema_version != SCHEMA_VERSION:
            if self.quality_escalation_reason is not None or self.quality_escalation_used:
                raise ValueError("old four_tier_mapping decisions cannot carry quality escalation")
            for classifier_audit in (self.intent, self.tier):
                if classifier_audit.probabilities is not None and not (
                    classifier_audit is self.tier
                    and jev_argmax
                    and classifier_audit.probability_argmax
                ):
                    maximum = max(classifier_audit.probabilities.values())
                    if (
                        sum(value == maximum for value in classifier_audit.probabilities.values())
                        != 1
                    ):
                        raise ValueError("old four_tier_mapping decisions require a unique argmax")
        if self.quality_escalation_used and (
            self.quality_escalation_reason is None
            or self.previous_tier is None
            or self.intent.final == "new_task"
            or TIERS.index(self.final_tier) != TIERS.index(self.previous_tier) + 1
            or self.tier.source != "fallback"
            or self.tier.reason != "quality_failure_upgrade"
            or (
                self.tier.prediction is not None
                and TIERS.index(cast(Tier, self.tier.prediction)) >= TIERS.index(self.final_tier)
            )
        ):
            raise ValueError("four_tier_mapping quality escalation must upgrade exactly one tier")
        if self.schema_version == SCHEMA_VERSION:
            if self.tier.run_status not in {"ran", "error"}:
                raise ValueError("v4 tier classification must run on every user turn")
            if self.intent.source == "fallback" and self.intent.run_status == "ran":
                raise ValueError("v4 intent decisions cannot filter a valid argmax")
            if (
                self.intent.final == "new_task"
                and self.tier.source == "fallback"
                and self.tier.run_status == "ran"
            ):
                raise ValueError("v4 new-task decisions cannot filter a valid argmax")
            if self.tier.reason == "quality_failure_upgrade" and not self.quality_escalation_used:
                raise ValueError("quality upgrade requires its audited flag")
        intent = cast(Intent, self.intent.final)
        if intent == "new_task":
            if (
                self.context_action != "reset"
                or self.history_turns_to_keep != 0
                or self.task_turn_index != 0
                or self.tier_snapshot_hash is None
            ):
                raise ValueError("four_tier_mapping new-task decision is inconsistent")
            expected_switch_reason = (
                "new_task_initialized" if self.previous_tier is None else "new_task_reselected"
            )
        else:
            if (
                self.previous_tier is None
                or self.context_action != "keep"
                or self.history_turns_to_keep != self.task_turn_index
            ):
                raise ValueError("four_tier_mapping active-task decision is inconsistent")
            if intent == "continue" and self.schema_version != SCHEMA_VERSION:
                if (
                    self.final_tier != self.previous_tier
                    or self.tier_snapshot_hash is not None
                    or self.tier.source != "not_run"
                    or self.tier.run_status != "not_run"
                    or self.tier.reason != "continue_keeps_current_tier"
                ):
                    raise ValueError("four_tier_mapping continue decision is inconsistent")
                expected_switch_reason = "continue_hold"
            else:
                if self.tier.source in {"rule", "not_run"}:
                    raise ValueError("four_tier_mapping active-task tier audit is inconsistent")
                if TIERS.index(self.final_tier) < TIERS.index(self.previous_tier):
                    raise ValueError("four_tier_mapping active-task decision cannot downgrade")
                if self.tier_snapshot_hash is None:
                    raise ValueError("four_tier_mapping active-task decision lacks a tier snapshot")
                expected_switch_reason = (
                    f"{intent}_quality_upgrade"
                    if self.quality_escalation_used
                    else f"{intent}_upgrade"
                    if self.final_tier != self.previous_tier
                    else f"{intent}_hold"
                )
                if self.schema_version == SCHEMA_VERSION and not self.quality_escalation_used:
                    if self.tier.run_status == "error":
                        if self.final_tier != self.previous_tier:
                            raise ValueError(
                                "failed tier classification must hold the current tier"
                            )
                    elif self.tier.prediction is not None:
                        expected_tier = TIERS[
                            max(
                                TIERS.index(self.previous_tier),
                                TIERS.index(cast(Tier, self.tier.prediction)),
                            )
                        ]
                        if self.final_tier != expected_tier:
                            raise ValueError("active-task tier differs from argmax/hold policy")
                        if (
                            self.tier.source == "fallback"
                            and self.tier.reason != f"{intent}_downgrade_blocked"
                        ):
                            raise ValueError("active-task tier has invalid policy override")
        if intent == "new_task" and self.tier.source in {"rule", "not_run"}:
            raise ValueError("four_tier_mapping new-task tier audit is inconsistent")
        if self.switch_reason != expected_switch_reason:
            raise ValueError("four_tier_mapping decision switch reason is inconsistent")

    def trace(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        return {
            **(
                {
                    "quality_escalation_reason": self.quality_escalation_reason,
                    "quality_escalation_used": self.quality_escalation_used,
                }
                if self.schema_version == SCHEMA_VERSION
                else {}
            ),
            "mode": MODE,
            "schema_version": self.schema_version,
            "route_id": self.route_id,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "decided_at_ms": self.decided_at_ms,
            "feature_schema_version": self.feature_schema_version,
            "feature_vector_dim": self.feature_vector_dim,
            "feature_vector_status": self.feature_vector_status,
            "feature_input": self.feature_input_audit.trace(),
            "policy_hash": self.policy_hash,
            "classifier_backend": self.classifier_backend,
            "classifier_identity": (
                dict(self.classifier_identity) if self.classifier_identity is not None else None
            ),
            "effective_mock_seed": self.effective_mock_seed,
            "intent": self.intent.trace(),
            "tier": self.tier.trace(),
            "previous_tier": self.previous_tier,
            "final_tier": self.final_tier,
            "provider": provider,
            "model": model,
            "switched": self.switched,
            "switch_reason": self.switch_reason,
            "context_action": self.context_action,
            "history_turns_to_keep": self.history_turns_to_keep,
            "task_turn_index": self.task_turn_index,
            "input_snapshot_hash": self.input_snapshot_hash,
            "tier_snapshot_hash": self.tier_snapshot_hash,
        }

    @classmethod
    def from_trace(cls, value: Mapping[str, Any]) -> FixedFourTierDecision:
        """Rehydrate a staged decision for crash-safe request replay."""

        if value.get("mode") != MODE:
            raise ValueError("four_tier_mapping decision mode is incompatible")
        schema_version_value = value.get("schema_version")
        if schema_version_value not in READABLE_SCHEMA_VERSIONS:
            raise ValueError("four_tier_mapping decision schema_version is incompatible")
        quality_fields = {"quality_escalation_reason", "quality_escalation_used"}
        if schema_version_value == SCHEMA_VERSION:
            if not quality_fields.issubset(value):
                raise ValueError("v4 decision is missing quality escalation audit")
        elif quality_fields.intersection(value):
            raise ValueError("old decisions cannot carry quality escalation audit")
        feature_schema_value = value.get("feature_schema_version")
        feature_dim_value = value.get("feature_vector_dim")
        feature_status_value = value.get("feature_vector_status")
        if not isinstance(feature_schema_value, str) or not feature_schema_value:
            raise ValueError("four_tier_mapping decision feature schema is incompatible")
        if feature_dim_value is not None and (
            isinstance(feature_dim_value, bool)
            or not isinstance(feature_dim_value, int)
            or feature_dim_value <= 0
        ):
            raise ValueError("four_tier_mapping decision feature dimension is incompatible")
        if feature_status_value not in {
            "mock_not_materialized",
            "materialized",
            "remote_evaluated",
        }:
            raise ValueError("four_tier_mapping decision feature status is incompatible")
        if schema_version_value in LEGACY_SCHEMA_VERSIONS and (
            feature_schema_value != FEATURE_SCHEMA_VERSION
            or feature_dim_value != FEATURE_VECTOR_DIM
            or feature_status_value != FEATURE_VECTOR_STATUS
        ):
            raise ValueError("four_tier_mapping legacy decision feature contract is incompatible")
        classifier_backend_value = value.get("classifier_backend")
        classifier_identity_value = value.get("classifier_identity")
        if schema_version_value in LEGACY_SCHEMA_VERSIONS:
            classifier_backend_value = "random_mock"
            classifier_identity_value = None
        if classifier_backend_value not in {"random_mock", "registered_model", "injected", "jev"}:
            raise ValueError("four_tier_mapping decision classifier backend is incompatible")
        if classifier_identity_value is not None and (
            not isinstance(classifier_identity_value, Mapping) or not classifier_identity_value
        ):
            raise ValueError("four_tier_mapping decision classifier identity is incompatible")

        def audit(
            name: str,
            *,
            allowed_labels: Sequence[str],
        ) -> ClassificationAudit:
            raw = value.get(name)
            if not isinstance(raw, Mapping):
                raise ValueError(f"four_tier_mapping decision is missing {name} audit")
            if set(raw) != {
                "source",
                "run_status",
                "prediction",
                "probabilities",
                "confidence",
                "final",
                "reason",
                "version",
            }:
                raise ValueError(f"four_tier_mapping decision {name} audit shape is incompatible")
            source = raw.get("source")
            run_status = raw.get("run_status")
            if source not in {"rule", "classifier", "fallback", "not_run"}:
                raise ValueError(f"four_tier_mapping decision has invalid {name} source")
            if run_status not in {"ran", "not_run", "error"}:
                raise ValueError(f"four_tier_mapping decision has invalid {name} run status")
            prediction_value = raw.get("prediction")
            if prediction_value is not None and (
                not isinstance(prediction_value, str) or not prediction_value.strip()
            ):
                raise ValueError(f"four_tier_mapping decision has invalid {name} prediction")
            final_value = raw.get("final")
            if not isinstance(final_value, str) or final_value not in allowed_labels:
                raise ValueError(f"four_tier_mapping decision has invalid {name} final value")
            reason_value = raw.get("reason")
            if not isinstance(reason_value, str) or not reason_value.strip():
                raise ValueError(f"four_tier_mapping decision has invalid {name} reason")
            version_value = raw.get("version")
            if version_value is not None and (
                not isinstance(version_value, str) or not version_value.strip()
            ):
                raise ValueError(f"four_tier_mapping decision has invalid {name} version")
            probabilities_value = raw.get("probabilities")
            probabilities: dict[str, float] | None = None
            if probabilities_value is not None:
                if not isinstance(probabilities_value, Mapping):
                    raise ValueError(f"four_tier_mapping decision has invalid {name} probabilities")
                probabilities = {}
                for label, probability in probabilities_value.items():
                    if (
                        not isinstance(label, str)
                        or isinstance(probability, bool)
                        or not isinstance(probability, (int, float))
                    ):
                        raise ValueError(
                            f"four_tier_mapping decision has invalid {name} probabilities"
                        )
                    probabilities[label] = float(probability)
                if set(probabilities) != set(allowed_labels):
                    raise ValueError(
                        f"four_tier_mapping decision has invalid {name} probability labels"
                    )
            confidence_value = raw.get("confidence")
            if confidence_value is not None and (
                isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float))
            ):
                raise ValueError(f"four_tier_mapping decision has invalid {name} confidence")
            confidence = float(confidence_value) if confidence_value is not None else None
            result = ClassificationAudit(
                source=cast(ClassifierSource, source),
                run_status=cast(ClassifierRunStatus, run_status),
                prediction=prediction_value,
                probabilities=probabilities,
                confidence=confidence,
                final=final_value,
                reason=reason_value,
                version=version_value,
                probability_argmax=(
                    name == "tier"
                    and run_status == "ran"
                    and classifier_backend_value == "jev"
                    and isinstance(classifier_identity_value, Mapping)
                    and classifier_identity_value.get("tier_selection_mode")
                    == "probability_argmax.v1"
                ),
            )
            if result.source == "classifier":
                if result.prediction not in allowed_labels or result.final != result.prediction:
                    raise ValueError(
                        f"four_tier_mapping decision has inconsistent {name} classifier output"
                    )
                classifier_probabilities = cast(Mapping[str, float], result.probabilities)
                classifier_confidence = cast(float, result.confidence)
                maximum = max(classifier_probabilities.values())
                winner = _argmax_label(classifier_probabilities, allowed_labels)
                if winner != result.prediction or not math.isclose(
                    classifier_confidence,
                    maximum,
                    rel_tol=1e-6,
                    abs_tol=1e-6,
                ):
                    raise ValueError(
                        f"four_tier_mapping decision has inconsistent {name} probabilities"
                    )
            elif result.source == "fallback" and result.run_status == "ran":
                if result.prediction not in allowed_labels:
                    raise ValueError(
                        f"four_tier_mapping decision has inconsistent {name} fallback output"
                    )
                if result.probabilities is None or result.confidence is None:
                    raise ValueError(
                        f"four_tier_mapping decision has incomplete {name} fallback output"
                    )
            return result

        final_tier_value = value.get("final_tier")
        previous_tier_value = value.get("previous_tier")
        context_action_value = value.get("context_action")
        history_turns_value = value.get("history_turns_to_keep")
        task_turn_index_value = value.get("task_turn_index")
        decided_at_value = value.get("decided_at_ms")
        effective_seed_value = value.get("effective_mock_seed")
        if final_tier_value not in TIERS:
            raise ValueError("four_tier_mapping decision has invalid final tier")
        if previous_tier_value is not None and previous_tier_value not in TIERS:
            raise ValueError("four_tier_mapping decision has invalid previous tier")
        if context_action_value not in {"keep", "reset"}:
            raise ValueError("four_tier_mapping decision has invalid context action")
        for field_name, raw in (
            ("history_turns_to_keep", history_turns_value),
            ("task_turn_index", task_turn_index_value),
            ("decided_at_ms", decided_at_value),
        ):
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ValueError(f"four_tier_mapping decision has invalid {field_name}")
        if effective_seed_value is not None and (
            isinstance(effective_seed_value, bool)
            or not isinstance(effective_seed_value, int)
            or not 0 <= effective_seed_value <= (1 << 64) - 1
        ):
            raise ValueError("four_tier_mapping decision has invalid effective mock seed")
        switched_value = value.get("switched")
        if not isinstance(switched_value, bool):
            raise ValueError("four_tier_mapping decision has invalid switched flag")

        def required_string(name: str) -> str:
            raw = value.get(name)
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"four_tier_mapping decision has invalid {name}")
            return raw

        tier_snapshot_value = value.get("tier_snapshot_hash")
        if tier_snapshot_value is not None and not isinstance(tier_snapshot_value, str):
            raise ValueError("four_tier_mapping decision has invalid tier snapshot hash")
        return cls(
            route_id=required_string("route_id"),
            task_id=required_string("task_id"),
            request_id=required_string("request_id"),
            intent=audit("intent", allowed_labels=INTENTS),
            tier=audit("tier", allowed_labels=TIERS),
            previous_tier=cast(Tier | None, previous_tier_value),
            final_tier=cast(Tier, final_tier_value),
            switched=switched_value,
            switch_reason=required_string("switch_reason"),
            context_action=cast(ContextAction, context_action_value),
            history_turns_to_keep=cast(int, history_turns_value),
            task_turn_index=cast(int, task_turn_index_value),
            input_snapshot_hash=required_string("input_snapshot_hash"),
            tier_snapshot_hash=tier_snapshot_value,
            feature_input_audit=FeatureInputAudit.from_trace(value.get("feature_input")),
            decided_at_ms=cast(int, decided_at_value),
            policy_hash=required_string("policy_hash"),
            classifier_backend=cast(str, classifier_backend_value),
            classifier_identity=(
                dict(classifier_identity_value)
                if isinstance(classifier_identity_value, Mapping)
                else None
            ),
            feature_schema_version=feature_schema_value,
            feature_vector_dim=cast(int | None, feature_dim_value),
            feature_vector_status=cast(str, feature_status_value),
            effective_mock_seed=cast(int | None, effective_seed_value),
            schema_version=cast(str, schema_version_value),
            quality_escalation_reason=value.get("quality_escalation_reason"),
            quality_escalation_used=(
                cast(bool, value.get("quality_escalation_used"))
                if schema_version_value == SCHEMA_VERSION
                else False
            ),
        )


@dataclass(frozen=True)
class FixedFourTierTaskState:
    """Durable semantic task state supplied to the pure routing machine."""

    task_id: str
    tier: Tier
    turn_count: int
    version: int = 0
    task_start_input_message_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("four_tier_mapping task state requires task_id")
        if not isinstance(self.tier, str) or self.tier not in TIERS:
            raise ValueError("four_tier_mapping task state has an invalid tier")
        if (
            isinstance(self.turn_count, bool)
            or not isinstance(self.turn_count, int)
            or self.turn_count < 0
        ):
            raise ValueError("four_tier_mapping task state has an invalid turn_count")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("four_tier_mapping task state has an invalid version")
        if self.task_start_input_message_id is not None and (
            not isinstance(self.task_start_input_message_id, str)
            or not self.task_start_input_message_id.strip()
        ):
            raise ValueError(
                "four_tier_mapping task state has an invalid task_start_input_message_id"
            )

    def payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tier": self.tier,
            "turn_count": self.turn_count,
            "version": self.version,
            "task_start_input_message_id": self.task_start_input_message_id,
            "schema_version": SCHEMA_VERSION,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> FixedFourTierTaskState:
        expected_keys = {
            "task_id",
            "tier",
            "turn_count",
            "version",
            "task_start_input_message_id",
            "schema_version",
        }
        if set(payload) != expected_keys:
            raise ValueError("four_tier_mapping task state payload shape is incompatible")
        if payload.get("schema_version") not in READABLE_SCHEMA_VERSIONS:
            raise ValueError("four_tier_mapping task state schema_version is incompatible")
        task_id = payload.get("task_id")
        tier_value = payload.get("tier")
        turn_count = payload.get("turn_count")
        version = payload.get("version")
        task_start_input_message_id = payload.get("task_start_input_message_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("four_tier_mapping task state requires task_id")
        if not isinstance(tier_value, str) or tier_value not in TIERS:
            raise ValueError("four_tier_mapping task state has an invalid tier")
        if isinstance(turn_count, bool) or not isinstance(turn_count, int) or turn_count < 0:
            raise ValueError("four_tier_mapping task state has an invalid turn_count")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("four_tier_mapping task state has an invalid version")
        if task_start_input_message_id is not None and (
            not isinstance(task_start_input_message_id, str)
            or not task_start_input_message_id.strip()
        ):
            raise ValueError(
                "four_tier_mapping task state has an invalid task_start_input_message_id"
            )
        return cls(
            task_id=task_id,
            tier=cast(Tier, tier_value),
            turn_count=turn_count,
            version=version,
            task_start_input_message_id=(
                task_start_input_message_id if task_start_input_message_id is not None else None
            ),
        )


def _snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bounded_text(value: str | None) -> tuple[str | None, bool]:
    """Apply the legacy audit/mock contract's deterministic proxy bound.

    A registered model reads the unmodified nested ``router_input`` and lets
    its frozen tokenizer own truncation.  This bounded projection keeps legacy
    random-mock snapshots and their content-safe audit stable.
    """

    if value is None:
        return None, False
    text = str(value)
    if len(text) <= _MAX_SEGMENT_CHARS:
        return text, False
    head_chars = (_MAX_SEGMENT_CHARS * 3) // 4
    tail_chars = _MAX_SEGMENT_CHARS - head_chars
    return text[:head_chars] + text[-tail_chars:], True


def _bounded_task_history(
    history: Sequence[str],
) -> tuple[list[str], bool, list[bool]]:
    """Keep the four most recent route-before user messages."""

    normalized, history_truncated = _retained_task_history(history)
    bounded: list[str] = []
    segment_truncation: list[bool] = []
    for value in normalized:
        text, truncated = _bounded_text(value)
        bounded.append(text or "")
        segment_truncation.append(truncated)
    return bounded, history_truncated, segment_truncation


def _retained_task_history(history: Sequence[str]) -> tuple[list[str], bool]:
    normalized = [str(value) for value in history]
    history_truncated = len(normalized) > _HISTORY_USER_MESSAGES
    if history_truncated:
        normalized = normalized[-_HISTORY_USER_MESSAGES:]
    return normalized, history_truncated


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _previous_metadata_bundles(
    value: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Split the runtime's merged prior-turn metadata into complete bundles.

    Runtime deliberately attaches route execution metadata even when the
    assistant transcript row has no Provider ``turn_usage``. Presence of the
    merged mapping therefore cannot prove that usage exists. Under the v2
    feature contract, an incomplete optional bundle is represented as absent
    and its missing mask is set; partial fields must not become implicit zeroes.
    """

    if value is None:
        return None, None
    raw = dict(value)

    usage_complete = _PREVIOUS_USAGE_REQUIRED_KEYS.issubset(raw)
    if usage_complete:
        for key in _PREVIOUS_USAGE_REQUIRED_KEYS:
            metric = raw[key]
            if isinstance(metric, bool) or not isinstance(metric, int) or metric < 0:
                usage_complete = False
                break
    usage = (
        {key: item for key, item in raw.items() if key not in _PREVIOUS_EXECUTION_KEYS}
        if usage_complete
        else None
    )

    execution_complete = _PREVIOUS_EXECUTION_KEYS.issubset(raw)
    if execution_complete:
        route_id = raw["route_id"]
        execution_status = raw["execution_status"]
        error_code = raw["error_code"]
        response_id = raw["response_id"]
        attempt_ids = raw["attempt_ids"]
        retry_count = raw["retry_count"]
        execution_complete = (
            isinstance(route_id, str)
            and bool(route_id.strip())
            and isinstance(execution_status, str)
            and bool(execution_status.strip())
            and (error_code is None or isinstance(error_code, str))
            and (response_id is None or isinstance(response_id, str))
            and isinstance(attempt_ids, Sequence)
            and not isinstance(attempt_ids, (str, bytes))
            and all(isinstance(attempt_id, str) and attempt_id for attempt_id in attempt_ids)
            and not isinstance(retry_count, bool)
            and isinstance(retry_count, int)
            and retry_count >= 0
        )
    execution = {key: raw[key] for key in _PREVIOUS_EXECUTION_KEYS} if execution_complete else None
    return usage, execution


def _feature_input_audit(
    request: RoutingRequest,
    state: FixedFourTierTaskState | None,
    snapshot: Mapping[str, Any],
    *,
    classifier_backend: str,
) -> FeatureInputAudit:
    missing = snapshot.get("missing")
    truncated = snapshot.get("truncated")
    if not isinstance(missing, Mapping) or not isinstance(truncated, Mapping):
        raise FixedFourTierRoutingError(
            "four_tier_mapping feature masks are unavailable",
            reason="feature_masks_unavailable",
        )
    attachment_modalities_value = snapshot.get("attachment_modalities")
    if not isinstance(attachment_modalities_value, Sequence) or isinstance(
        attachment_modalities_value, (str, bytes)
    ):
        raise FixedFourTierRoutingError(
            "four_tier_mapping attachment metadata bundle is unavailable",
            reason="feature_masks_unavailable",
        )

    if classifier_backend in {"registered_model", "jev"}:
        router_input = snapshot.get("router_input")
        if not isinstance(router_input, Mapping):
            raise FixedFourTierRoutingError(
                "four_tier_mapping canonical RouterInput is unavailable",
                reason="canonical_router_input_unavailable",
            )
        current_request = router_input.get("current_request")
        history_user = router_input.get("history_user")
        previous_answer = router_input.get("previous_answer")
        if (
            not isinstance(current_request, str)
            or not isinstance(history_user, Sequence)
            or isinstance(history_user, (str, bytes))
            or any(not isinstance(value, str) for value in history_user)
            or not isinstance(previous_answer, str)
        ):
            raise FixedFourTierRoutingError(
                "four_tier_mapping canonical RouterInput text bundle is malformed",
                reason="canonical_router_input_unavailable",
            )
        retained_history = [str(value) for value in history_user]
        if tuple(retained_history) != request.user_history[-_HISTORY_USER_MESSAGES:]:
            raise FixedFourTierRoutingError(
                "four_tier_mapping canonical RouterInput history is inconsistent",
                reason="canonical_router_input_unavailable",
            )
        history_window_truncated = len(request.user_history) > len(retained_history)
        input_contract: FeatureAuditInputContract = "canonical_router_input"
        classifier_snapshot_hash = _snapshot_hash(router_input)
        history_aggregate_content_hash = _snapshot_hash({"history_user": retained_history})
        history_observed_count = len(request.user_history)
        history_segments = (False,) * len(retained_history)
        truncated_current_request = False
        truncated_history = history_window_truncated
        truncated_previous_assistant = False
        previous_assistant = previous_answer or None
    else:
        active_history = request.user_history if state is not None else ()
        retained_history, _ = _retained_task_history(active_history)
        raw_history_segments = truncated.get("history_segments")
        if not isinstance(raw_history_segments, Sequence) or isinstance(
            raw_history_segments, (str, bytes)
        ):
            raise FixedFourTierRoutingError(
                "four_tier_mapping history truncation mask is unavailable",
                reason="feature_masks_unavailable",
            )
        input_contract = "legacy_mock_snapshot"
        classifier_snapshot_hash = _snapshot_hash(snapshot)
        current_request = request.message
        history_aggregate_content_hash = _snapshot_hash({"task_user_history": list(active_history)})
        history_observed_count = len(active_history)
        history_segments = tuple(bool(value) for value in raw_history_segments)
        history_window_truncated = bool(truncated.get("history_window"))
        truncated_current_request = bool(truncated.get("current_request"))
        truncated_history = bool(truncated.get("history"))
        truncated_previous_assistant = bool(truncated.get("previous_assistant"))
        previous_assistant = request.previous_assistant_text if state is not None else None

    return FeatureInputAudit(
        input_contract=input_contract,
        classifier_snapshot_hash=classifier_snapshot_hash,
        current_request_content_hash=_content_hash(current_request),
        history_content_hashes=tuple(_content_hash(value) for value in retained_history),
        history_aggregate_content_hash=history_aggregate_content_hash,
        history_observed_count=history_observed_count,
        history_retained_count=len(retained_history),
        previous_assistant_content_hash=(
            _content_hash(previous_assistant) if previous_assistant is not None else None
        ),
        input_message_id=request.input_message_id,
        task_start_input_message_id=(
            state.task_start_input_message_id if state is not None else None
        ),
        attachment_count=int(snapshot.get("attachment_count") or 0),
        attachment_modalities=tuple(attachment_modalities_value),
        missing_context=bool(missing.get("context")),
        missing_usage=bool(missing.get("usage")),
        missing_execution=bool(missing.get("execution")),
        missing_attachment_metadata=bool(missing.get("attachment_metadata")),
        truncated_current_request=truncated_current_request,
        truncated_history=truncated_history,
        truncated_history_window=history_window_truncated,
        truncated_history_segments=tuple(history_segments),
        truncated_previous_assistant=truncated_previous_assistant,
    )


def _classifier_audit(
    *,
    classifier: IntentClassifier | TierClassifier,
    predict: Callable[[], ClassifierPrediction],
    allowed: Sequence[str],
    probability_labels: Sequence[str],
    fallback: str,
) -> ClassificationAudit:
    version = str(getattr(classifier, "version", "") or "") or None
    try:
        result = predict()
    except Exception as exc:  # noqa: BLE001 - local model failures have a defined fallback
        if getattr(exc, "fail_closed", False) is True:
            raise
        return ClassificationAudit(
            source="fallback",
            run_status="error",
            prediction=None,
            probabilities=None,
            confidence=None,
            final=fallback,
            reason=f"classifier_error:{type(exc).__name__}",
            version=version,
        )

    if (
        tuple(probability_labels) == TIERS
        and getattr(classifier, "backend", None) == "jev"
        and getattr(classifier, "tier_selection_mode", None) == "probability_argmax.v1"
    ):
        # The Jev adapter has already validated the four finite probabilities.
        # Do not apply local-model confidence, margin, sum or tie fallbacks.
        jev_probabilities = cast(Mapping[str, float], result.probabilities)
        jev_prediction = max(TIERS, key=lambda label: jev_probabilities[label])
        return ClassificationAudit(
            source="classifier",
            run_status="ran",
            prediction=jev_prediction,
            probabilities=dict(jev_probabilities),
            confidence=jev_probabilities[jev_prediction],
            final=jev_prediction,
            reason="classifier_argmax_selected",
            version=result.version or version,
            probability_argmax=True,
        )

    try:
        prediction = str(result.label or "").strip().lower()
        result_version = str(result.version or "").strip() or version
        raw_probabilities = result.probabilities
        confidence = result.confidence
    except Exception:  # noqa: BLE001 - malformed local output is fail-safe input
        return ClassificationAudit(
            source="fallback",
            run_status="error",
            prediction=None,
            probabilities=None,
            confidence=None,
            final=fallback,
            reason="invalid_classifier_result",
            version=version,
        )

    probabilities: dict[str, float] | None = None
    probabilities_valid = isinstance(raw_probabilities, Mapping)
    if isinstance(raw_probabilities, Mapping):
        probabilities = {}
        for label, raw_value in raw_probabilities.items():
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                probabilities_valid = False
                break
            value = float(raw_value)
            if not math.isfinite(value) or value < 0.0 or value > 1.0:
                probabilities_valid = False
                break
            probabilities[str(label)] = value
        expected_labels = set(probability_labels)
        probabilities_valid = probabilities_valid and set(probabilities) == expected_labels
        probability_sum = sum(probabilities.values())
        probabilities_valid = probabilities_valid and math.isclose(
            probability_sum,
            1.0,
            rel_tol=1e-6,
            abs_tol=1e-6,
        )
    if confidence is None and probabilities is not None:
        confidence = probabilities.get(prediction)
    max_probability = max(probabilities.values()) if probabilities else None
    winner = (
        _argmax_label(probabilities, probability_labels)
        if probabilities_valid and probabilities is not None
        else None
    )
    if (
        not probabilities_valid
        or result_version is None
        or prediction not in allowed
        or probabilities is None
        or probabilities.get(prediction) != max_probability
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or float(confidence) < 0.0
        or float(confidence) > 1.0
        or max_probability is None
        or not math.isclose(float(confidence), max_probability, rel_tol=1e-6, abs_tol=1e-6)
    ):
        return ClassificationAudit(
            source="fallback",
            run_status="error",
            # Malformed output is diagnostic-only and must never be
            # rehydrated as trusted classifier evidence.
            prediction=None,
            probabilities=None,
            confidence=None,
            final=fallback,
            reason="invalid_classifier_result",
            version=result_version,
        )

    prediction = cast(str, winner)
    normalized_confidence = float(confidence)
    return ClassificationAudit(
        source="classifier",
        run_status="ran",
        prediction=prediction,
        probabilities=probabilities,
        confidence=normalized_confidence,
        final=prediction,
        reason="classifier_selected",
        version=result_version,
    )


def _explicit_intent(request: RoutingRequest) -> tuple[Intent, str] | None:
    event = str(request.control_event or "").strip().casefold()
    if event in _NEW_TASK_CONTROL_EVENTS:
        return "new_task", "explicit_new_task_control"
    if event in _REDO_CONTROL_EVENTS:
        return "redo", "explicit_redo_control"
    if _NEW_TASK_TEXT.search(request.message):
        return "new_task", "explicit_new_task_text"
    if _REDO_TEXT.search(request.message):
        return "redo", "explicit_redo_text"
    return None


class FixedFourTierV2Router:
    """Pure four_tier_mapping policy over caller-supplied durable task state.

    The router deliberately owns no session state.  Gateway persistence is the
    authority, so worker restarts, multi-worker execution and Web prefix forks
    cannot silently turn an active task into a new task.
    """

    def __init__(
        self,
        *,
        intent_classifier: IntentClassifier | None = None,
        tier_classifier: TierClassifier | None = None,
        mock_seed: int | None = None,
        default_new_task_tier: Tier = "c1",
        intent_min_confidence: float = 0.5,
        tier_min_confidence: float = 0.5,
        min_margin: float = 0.05,
        route_id_factory: Callable[[], str] | None = None,
        task_id_factory: Callable[[], str] | None = None,
        clock_ms: Callable[[], int] | None = None,
        policy_config: Mapping[str, Any] | None = None,
    ) -> None:
        if default_new_task_tier not in TIERS:
            raise ValueError("default_new_task_tier must be one of c0, c1, c2, c3")
        for label, value in (
            ("intent_min_confidence", intent_min_confidence),
            ("tier_min_confidence", tier_min_confidence),
            ("min_margin", min_margin),
        ):
            if not math.isfinite(value) or value < 0.0 or value > 1.0:
                raise ValueError(f"{label} must be between 0 and 1")

        effective_mock_seed = (
            mock_seed if mock_seed is not None else random.SystemRandom().getrandbits(64)
        )
        tier_seed = effective_mock_seed ^ 0x9E3779B97F4A7C15
        self._intent_classifier = intent_classifier or RandomMockIntentClassifier(
            effective_mock_seed
        )
        self._tier_classifier = tier_classifier or RandomMockTierClassifier(tier_seed)
        self._default_new_task_tier = default_new_task_tier
        self._intent_min_confidence = intent_min_confidence
        self._tier_min_confidence = tier_min_confidence
        self._min_margin = min_margin
        self._route_id_factory = route_id_factory or (lambda: uuid.uuid4().hex)
        self._task_id_factory = task_id_factory or (lambda: uuid.uuid4().hex)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._effective_mock_seed = (
            effective_mock_seed if intent_classifier is None or tier_classifier is None else None
        )
        classifier_backends = {
            str(getattr(classifier, "backend", "") or "injected")
            for classifier in (self._intent_classifier, self._tier_classifier)
        }
        self._classifier_backend = (
            next(iter(classifier_backends)) if len(classifier_backends) == 1 else "injected"
        )
        classifier_identities = [
            getattr(classifier, "identity", None)
            for classifier in (self._intent_classifier, self._tier_classifier)
        ]
        self._classifier_identity = (
            dict(classifier_identities[0])
            if isinstance(classifier_identities[0], Mapping)
            and classifier_identities[0] == classifier_identities[1]
            else None
        )
        feature_schemas = {
            str(getattr(classifier, "feature_schema_version", FEATURE_SCHEMA_VERSION))
            for classifier in (self._intent_classifier, self._tier_classifier)
        }
        feature_statuses = {
            str(getattr(classifier, "feature_vector_status", FEATURE_VECTOR_STATUS))
            for classifier in (self._intent_classifier, self._tier_classifier)
        }
        feature_dimensions = {
            getattr(classifier, "feature_vector_dim", FEATURE_VECTOR_DIM)
            for classifier in (self._intent_classifier, self._tier_classifier)
        }
        self._feature_schema_version = (
            next(iter(feature_schemas)) if len(feature_schemas) == 1 else FEATURE_SCHEMA_VERSION
        )
        self._feature_vector_status = (
            next(iter(feature_statuses)) if len(feature_statuses) == 1 else FEATURE_VECTOR_STATUS
        )
        self._feature_vector_dim = (
            next(iter(feature_dimensions)) if len(feature_dimensions) == 1 else FEATURE_VECTOR_DIM
        )
        # Registry status is live authorization evidence and can legitimately
        # move from CANDIDATE to VALIDATED while this exact hash-pinned model is
        # resident.  The opt-in policy is already frozen in ``policy_config``;
        # exclude the mutable observed status so the policy identity remains
        # stable across that administrative transition.
        policy_classifier_identity = (
            dict(self._classifier_identity) if self._classifier_identity is not None else None
        )
        if policy_classifier_identity is not None:
            policy_classifier_identity.pop("registry_status", None)
        policy_payload = {
            "schema_version": SCHEMA_VERSION,
            "rule_version": RULE_VERSION,
            "feature_schema_version": self._feature_schema_version,
            "feature_vector_dim": self._feature_vector_dim,
            "feature_vector_status": self._feature_vector_status,
            "classifier_backend": self._classifier_backend,
            "classifier_identity": policy_classifier_identity,
            "intent_classifier_version": str(getattr(self._intent_classifier, "version", "") or ""),
            "tier_classifier_version": str(getattr(self._tier_classifier, "version", "") or ""),
            "effective_mock_seed": self._effective_mock_seed,
            "default_new_task_tier": default_new_task_tier,
            "config": fixed_four_tier_semantic_policy_config(policy_config or {}),
        }
        self._policy_hash = _snapshot_hash(policy_payload)

    @staticmethod
    def _snapshot(
        request: RoutingRequest,
        state: FixedFourTierTaskState | None,
        *,
        include_control_event: bool,
    ) -> dict[str, Any]:
        # ``user_history`` is already task-scoped by the durable transcript
        # boundary in the runtime.  Do not slice by ``turn_count``: historical
        # or mixed-version transcripts can contain multiple user rows per turn.
        active_history_raw = request.user_history if state is not None else ()
        active_history, history_window_truncated, history_segment_truncated = _bounded_task_history(
            active_history_raw
        )
        current_request, current_request_truncated = _bounded_text(request.message)
        previous_assistant_text, previous_assistant_truncated = _bounded_text(
            request.previous_assistant_text if state is not None else None
        )
        previous_usage, previous_execution = _previous_metadata_bundles(
            request.previous_assistant_usage if state is not None else None
        )
        # This nested object is the canonical training/serving contract.  It
        # intentionally uses the raw route-before text and the latest four
        # cross-task user turns; the frozen training feature runtime owns all
        # tokenization and truncation.  The surrounding legacy fields remain
        # bounded for mock compatibility and content-safe auditing.
        model_previous_usage, _ = _previous_metadata_bundles(request.previous_assistant_usage)
        router_input = {
            "current_request": request.message,
            "task_anchor": request.task_anchor or "",
            "history_user": list(request.user_history[-4:]),
            "previous_answer": request.previous_assistant_text or "",
            "previous_usage": model_previous_usage or {},
            "previous_outcome": request.previous_outcome,
            "active_route_tier": state.tier.upper() if state is not None else None,
            "route_history": [dict(value) for value in request.route_history[-5:]],
            "context": dict(request.context or {}),
            "tool_state": dict(request.tool_state or {}),
            "attachments": [dict(value) for value in request.attachments],
        }
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            # The 413-vector is intentionally not materialized while both
            # classifiers are random mocks.  Recording this explicitly avoids
            # presenting a scaffold/zero vector as real training evidence.
            "feature_vector_dim": FEATURE_VECTOR_DIM,
            "feature_vector_status": FEATURE_VECTOR_STATUS,
            "current_request": current_request or "",
            "active_task": state is not None,
            "current_tier": state.tier if state is not None else None,
            "task_turn_count": state.turn_count if state is not None else 0,
            "task_user_history": active_history,
            "previous_assistant_text": previous_assistant_text,
            "previous_assistant_usage": previous_usage,
            "previous_execution": previous_execution,
            "router_input": router_input,
            # Count and modalities form one optional schema bundle. If any
            # item's metadata is missing, zero all base values and use the
            # missing mask instead of exposing a partially populated group.
            "attachment_count": (
                request.attachment_count if request.attachment_modalities is not None else 0
            ),
            "attachment_modalities": list(request.attachment_modalities or ()),
            "missing": {
                # The durable transcript loader fails closed before reaching
                # this pure router.  No active task means "no prior", not a
                # missing optional context bundle.
                "context": False,
                "usage": state is not None and previous_usage is None,
                "execution": state is not None and previous_execution is None,
                "attachment_metadata": request.attachment_modalities is None,
            },
            "truncated": {
                "current_request": current_request_truncated,
                "history": history_window_truncated or any(history_segment_truncated),
                "history_window": history_window_truncated,
                "history_segments": history_segment_truncated,
                "previous_assistant": previous_assistant_truncated,
            },
        }
        if include_control_event:
            snapshot["session_id"] = request.session_id
            snapshot["request_id"] = request.request_id
            snapshot["input_message_id"] = request.input_message_id
            snapshot["control_event"] = (
                str(request.control_event).strip().casefold()
                if request.control_event is not None
                else None
            )
        return snapshot

    @staticmethod
    def _new_task_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        masked = dict(snapshot)
        missing = snapshot.get("missing")
        truncated = snapshot.get("truncated")
        if not isinstance(missing, Mapping) or not isinstance(truncated, Mapping):
            raise FixedFourTierRoutingError(
                "four_tier_mapping feature masks are unavailable",
                reason="feature_masks_unavailable",
            )
        masked.update(
            {
                "active_task": False,
                "current_tier": None,
                "task_turn_count": 0,
                "task_user_history": [],
                "previous_assistant_text": None,
                "previous_assistant_usage": None,
                "previous_execution": None,
                # The design explicitly retains the input-quality missing
                # mask while zeroing prior-task feature blocks.
                "missing": dict(missing),
                "truncated": {
                    "current_request": bool(truncated.get("current_request")),
                    "history": False,
                    "history_window": False,
                    "history_segments": [],
                    "previous_assistant": False,
                },
                "task_reset_mask": True,
            }
        )
        # Never mask ``router_input``.  The trained contract defines new_task
        # as the current intent label, not as absence of route-before history.
        return masked

    def _intent_decision(
        self,
        request: RoutingRequest,
        state: FixedFourTierTaskState | None,
        snapshot: Mapping[str, Any],
    ) -> ClassificationAudit:
        explicit = _explicit_intent(request)
        if explicit is not None:
            intent, reason = explicit
            if state is None and intent == "redo":
                return ClassificationAudit(
                    source="fallback",
                    run_status="not_run",
                    prediction=None,
                    probabilities=None,
                    confidence=None,
                    final="new_task",
                    reason="redo_without_active_task",
                    version=RULE_VERSION,
                )
            return ClassificationAudit(
                source="rule",
                run_status="not_run",
                prediction=None,
                probabilities=None,
                confidence=None,
                final=intent,
                reason=reason,
                version=RULE_VERSION,
            )
        if state is None:
            return ClassificationAudit(
                source="fallback",
                run_status="not_run",
                prediction=None,
                probabilities=None,
                confidence=None,
                final="new_task",
                reason="no_active_task",
                version=None,
            )
        return _classifier_audit(
            classifier=self._intent_classifier,
            predict=lambda: self._intent_classifier.predict(snapshot),
            allowed=INTENTS,
            probability_labels=INTENTS,
            fallback="continue",
        )

    def _tier_decision(
        self,
        *,
        intent: Intent,
        state: FixedFourTierTaskState | None,
        snapshot: Mapping[str, Any],
    ) -> tuple[ClassificationAudit, dict[str, Any] | None]:
        if intent in {"continue", "redo"}:
            if state is None:
                raise FixedFourTierRoutingError(
                    f"{intent} requires an active task",
                    reason=f"{intent}_without_active_task",
                )
            policy_allowed = TIERS[TIERS.index(state.tier) :]
            fallback: Tier = state.tier
            tier_snapshot = dict(snapshot)
            tier_snapshot["classifier_label_space"] = list(TIERS)
            tier_snapshot["policy_allowed_tiers"] = list(policy_allowed)
            audit = _classifier_audit(
                classifier=self._tier_classifier,
                predict=lambda: self._tier_classifier.predict(tier_snapshot, TIERS),
                allowed=TIERS,
                probability_labels=TIERS,
                fallback=fallback,
            )
            if audit.source == "classifier" and cast(Tier, audit.final) not in policy_allowed:
                audit = replace(
                    audit,
                    source="fallback",
                    final=state.tier,
                    reason=f"{intent}_downgrade_blocked",
                )
            return audit, tier_snapshot

        tier_snapshot = self._new_task_snapshot(snapshot)
        tier_snapshot["classifier_label_space"] = list(TIERS)
        tier_snapshot["policy_allowed_tiers"] = list(TIERS)
        audit = _classifier_audit(
            classifier=self._tier_classifier,
            predict=lambda: self._tier_classifier.predict(tier_snapshot, TIERS),
            allowed=TIERS,
            probability_labels=TIERS,
            fallback=self._default_new_task_tier,
        )
        return audit, tier_snapshot

    def decide(
        self,
        request: RoutingRequest,
        state: FixedFourTierTaskState | None = None,
    ) -> tuple[FixedFourTierDecision, FixedFourTierTaskState]:
        """Classify one request without mutating or persisting session state."""

        session_id = request.session_id.strip()
        if not session_id:
            raise ValueError("session_id must be non-empty")
        request_id = request.request_id.strip()
        if not request_id:
            raise ValueError("request_id must be non-empty")
        request = replace(
            request,
            session_id=session_id,
            request_id=request_id,
        )

        routing_snapshot = self._snapshot(request, state, include_control_event=True)
        routing_snapshot["quality_escalation_control"] = {
            "reason": request.quality_failure_reason,
            "budget_remaining": request.quality_retry_budget_remaining,
            "already_used": request.quality_retry_already_used,
        }
        classifier_snapshot = self._snapshot(request, state, include_control_event=False)
        feature_input_audit = _feature_input_audit(
            request,
            state,
            classifier_snapshot,
            classifier_backend=self._classifier_backend,
        )
        intent_audit = self._intent_decision(request, state, classifier_snapshot)
        intent_value = intent_audit.final
        if intent_value not in INTENTS:
            raise FixedFourTierRoutingError(
                "intent normalization produced an invalid value",
                reason="invalid_final_intent",
            )
        intent = cast(Intent, intent_value)
        tier_audit, tier_snapshot = self._tier_decision(
            intent=intent,
            state=state,
            snapshot=classifier_snapshot,
        )
        quality_escalation_used = False
        if (
            state is not None
            and intent != "new_task"
            and request.quality_failure_reason is not None
            and request.quality_retry_budget_remaining > 0
            and not request.quality_retry_already_used
            and state.tier != "c3"
        ):
            remedial_tier = TIERS[TIERS.index(state.tier) + 1]
            if TIERS.index(cast(Tier, tier_audit.final)) < TIERS.index(remedial_tier):
                tier_audit = replace(
                    tier_audit,
                    source="fallback",
                    final=remedial_tier,
                    reason="quality_failure_upgrade",
                )
                quality_escalation_used = True
        final_tier_value = tier_audit.final
        if final_tier_value not in TIERS:
            raise FixedFourTierRoutingError(
                "tier normalization produced an invalid value",
                reason="invalid_final_tier",
            )
        final_tier = cast(Tier, final_tier_value)

        previous_tier = state.tier if state is not None else None
        next_version = (state.version + 1) if state is not None else 1
        if intent == "new_task":
            task_id = self._task_id_factory()
            context_action: ContextAction = "reset"
            history_turns_to_keep = 0
            task_turn_index = 0
            switch_reason = (
                "new_task_initialized" if previous_tier is None else "new_task_reselected"
            )
            next_state = FixedFourTierTaskState(
                task_id=task_id,
                tier=final_tier,
                turn_count=1,
                version=next_version,
                task_start_input_message_id=request.input_message_id,
            )
        else:
            if state is None:
                raise FixedFourTierRoutingError(
                    "active task disappeared during routing",
                    reason="active_task_state_missing",
                )
            task_id = state.task_id
            context_action = "keep"
            history_turns_to_keep = state.turn_count
            task_turn_index = state.turn_count
            switch_reason = (
                f"{intent}_quality_upgrade"
                if quality_escalation_used
                else f"{intent}_upgrade"
                if final_tier != state.tier
                else f"{intent}_hold"
            )
            next_state = FixedFourTierTaskState(
                task_id=state.task_id,
                tier=final_tier,
                turn_count=state.turn_count + 1,
                version=next_version,
                task_start_input_message_id=state.task_start_input_message_id,
            )

        decision_classifier_identity = self._classifier_identity
        if self._classifier_backend == "registered_model":
            current_intent_identity = getattr(self._intent_classifier, "identity", None)
            current_tier_identity = getattr(self._tier_classifier, "identity", None)
            if (
                not isinstance(current_intent_identity, Mapping)
                or current_intent_identity != current_tier_identity
            ):
                raise FixedFourTierRoutingError(
                    "registered classifier runtime identity changed incompatibly",
                    reason="classifier_identity_unavailable",
                )
            decision_classifier_identity = dict(current_intent_identity)

        decision = FixedFourTierDecision(
            route_id=self._route_id_factory(),
            task_id=task_id,
            request_id=request_id,
            intent=intent_audit,
            tier=tier_audit,
            previous_tier=previous_tier,
            final_tier=final_tier,
            switched=previous_tier is not None and previous_tier != final_tier,
            switch_reason=switch_reason,
            context_action=context_action,
            history_turns_to_keep=history_turns_to_keep,
            task_turn_index=task_turn_index,
            input_snapshot_hash=_snapshot_hash(routing_snapshot),
            tier_snapshot_hash=(
                _snapshot_hash(tier_snapshot) if tier_snapshot is not None else None
            ),
            feature_input_audit=feature_input_audit,
            decided_at_ms=self._clock_ms(),
            policy_hash=self._policy_hash,
            classifier_backend=self._classifier_backend,
            classifier_identity=decision_classifier_identity,
            feature_schema_version=self._feature_schema_version,
            feature_vector_dim=self._feature_vector_dim,
            feature_vector_status=self._feature_vector_status,
            effective_mock_seed=self._effective_mock_seed,
            quality_escalation_reason=request.quality_failure_reason,
            quality_escalation_used=quality_escalation_used,
        )
        return decision, next_state

    def close(self) -> None:
        """Release an injected model runtime once, if it owns resources."""

        closed: set[int] = set()
        for classifier in (self._intent_classifier, self._tier_classifier):
            identity = id(classifier)
            if identity in closed:
                continue
            closed.add(identity)
            close = getattr(classifier, "close", None)
            if callable(close):
                close()

    def route(
        self,
        request: RoutingRequest,
        state: FixedFourTierTaskState | None = None,
    ) -> FixedFourTierDecision:
        """Compatibility wrapper returning only the pure decision."""

        return self.decide(request, state)[0]
