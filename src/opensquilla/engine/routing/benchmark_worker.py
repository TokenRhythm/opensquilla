"""Headless, result-blind ``four_tier_mapping`` Benchmark worker.

The worker is a deliberately narrow process boundary for
``routing-training-platform``.  It loads one hash-pinned registered Router,
executes the same :class:`FixedFourTierV2Router` policy used by production,
and stops after selecting the frozen downstream deployment.  It never builds
an Agent, TurnRunner, Provider, or downstream LLM request.

The primary entry point intentionally bypasses ``opensquilla.cli.main`` so a
Benchmark subprocess does not load the interactive CLI's profile or ``.env``::

    python -m opensquilla.engine.routing.benchmark_worker \
        --request /absolute/request.json \
        --output-dir /absolute/output-directory

All request artifacts use canonical JSON/JSONL.  A batch is committed only when
both ``decisions.jsonl`` and ``attestation.json`` exist and the latter's
``output_hash`` verifies the former.  ``attestation.json`` is the commit marker;
a process killed between renames may leave an uncommitted decisions-only file,
which consumers must reject.

``independent`` is the formal v1 replay scope.  ``episode`` is diagnostic: each
episode must start at turn zero with empty Router task state, then the worker
applies the production transitions to the supplied fixed per-turn context.  It
does not attest a captured mid-session state or a counterfactual live session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from opensquilla import __version__ as opensquilla_version
from opensquilla.engine.routing.fixed_four_tier_v2 import (
    FIXED_FOUR_TIER_DEPLOYMENT_SPECS,
    FixedFourTierTaskState,
    FixedFourTierV2Router,
    RoutingRequest,
    fixed_four_tier_semantic_policy_config,
    normalize_attachment_modalities,
)
from opensquilla.engine.routing.registered_model import RegisteredModelClassifier

REQUEST_SCHEMA_VERSION = "opensquilla.route_only_request.v1"
DECISION_SCHEMA_VERSION = "opensquilla.route_only_decision.v1"
ATTESTATION_SCHEMA_VERSION = "opensquilla.route_only_attestation.v1"
ERROR_SCHEMA_VERSION = "opensquilla.route_only_error.v1"
EXECUTION_MODE = "system_replay"
SEMANTIC_EXECUTION_SCHEMA_VERSION = "opensquilla.route_only_semantic_execution.v1"
DECISIONS_FILENAME = "decisions.jsonl"
ATTESTATION_FILENAME = "attestation.json"

_TIERS = ("C0", "C1", "C2", "C3")
_FEATURE_ROUTE_TIERS = frozenset((*_TIERS, "R0", "R1", "R2", "R3"))
_MAX_JSON_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_JSONL_BYTES = 512 * 1024 * 1024
_MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
_MAX_DECISIONS_JSONL_BYTES = 512 * 1024 * 1024
_MAX_DECISION_LINE_BYTES = 16 * 1024 * 1024
_MAX_JSON_NODES = 1_000_000
_MAX_JSON_DEPTH = 64
_MAX_FEATURE_INTEGER = (1 << 63) - 1
_IDENTIFIER = re.compile(r"^[^\x00\r\n]{1,256}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

_DIRECT_RESULT_KEYS = frozenset(
    {
        ("oracle",),
        ("result",),
        ("results",),
        ("quality",),
        ("score",),
        ("scores",),
        ("official", "cost"),
        ("actual", "cost"),
        ("observed", "cost"),
        ("benchmark", "cost"),
        ("input", "tokens"),
        ("input", "token", "count"),
        ("output", "tokens"),
        ("output", "token", "count"),
        ("total", "tokens"),
        ("total", "token", "count"),
        ("cache", "read", "tokens"),
        ("cache", "read", "token", "count"),
        ("cache", "write", "tokens"),
        ("cache", "write", "token", "count"),
        ("ground", "truth"),
        ("expected", "model"),
        ("expected", "tier"),
        ("selected", "model"),
        ("selected", "tier"),
        ("best", "model"),
        ("best", "tier"),
    }
)
_ASSIGNMENT_OPERATOR = re.compile(r"->|=|:")
_MAX_ASSIGNMENT_LHS_CHARS = 4096

_PREVIOUS_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    }
)
_PREVIOUS_USAGE_OPTIONAL_KEYS = frozenset({"duration_ms"})
_PREVIOUS_EXECUTION_KEYS = frozenset(
    {
        "route_id",
        "execution_status",
        "error_code",
        "response_id",
        "attempt_ids",
        "retry_count",
    }
)


class BenchmarkRouteOnlyError(ValueError):
    """The route-only request is unsafe, malformed, or internally inconsistent."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _RouteBeforeInput(_FrozenModel):
    """Local mirror of the canonical ``router_training.contracts.RouterInput``.

    ``RegisteredModelClassifier`` validates the effective value against the
    training package again immediately before inference.  Keeping this small
    mirror here lets the OpenSquilla boundary reject extra/oracle fields before
    loading a native model runtime.
    """

    current_request: str
    task_anchor: str = ""
    history_user: tuple[str, ...] = Field(default=(), max_length=4)
    previous_answer: str = ""
    previous_usage: dict[str, JsonValue] = Field(default_factory=dict)
    previous_outcome: Literal["success", "failure", "clarification", "unknown"] = "unknown"
    active_route_tier: Literal["C0", "C1", "C2", "C3"] | None = None
    route_history: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=5)
    context: dict[str, JsonValue] = Field(default_factory=dict)
    tool_state: dict[str, JsonValue] = Field(default_factory=dict)
    attachments: tuple[dict[str, JsonValue], ...] = ()


class _ModelIdentity(_FrozenModel):
    model_id: str
    revision: str
    definition_hash: str


class _TierDeployment(_FrozenModel):
    provider: str
    model: str
    reasoning: Literal["thinking", "max"]
    deployment_version: str


class _RegisteredClassifierConfig(_FrozenModel):
    backend: Literal["registered_model"]
    artifact_root: str
    metadata_db: str
    model_set_id: str
    expected_manifest_hash: str
    allow_candidate: bool = False

    @model_validator(mode="after")
    def _validate_identity(self) -> _RegisteredClassifierConfig:
        if not Path(self.artifact_root).is_absolute() or not Path(self.metadata_db).is_absolute():
            raise ValueError("registered model paths must be absolute")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", self.model_set_id) is None:
            raise ValueError("registered model_set_id is invalid")
        if _SHA256.fullmatch(self.expected_manifest_hash) is None:
            raise ValueError("registered expected_manifest_hash is invalid")
        return self


class _FourTierConfig(_FrozenModel):
    schema_version: Literal["fixed-four-tier-v2-v3"]
    classifier: _RegisteredClassifierConfig
    default_new_task_tier: Literal["c0", "c1", "c2", "c3"]
    intent_min_confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    tier_min_confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    min_margin: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    tiers: dict[Literal["c0", "c1", "c2", "c3"], _TierDeployment]

    @model_validator(mode="after")
    def _validate_frozen_ladder(self) -> _FourTierConfig:
        expected = {
            tier: {
                "provider": provider,
                "model": model,
                "reasoning": reasoning,
                "deployment_version": deployment_version,
            }
            for tier, provider, model, reasoning, deployment_version in (
                FIXED_FOUR_TIER_DEPLOYMENT_SPECS
            )
        }
        actual = {
            tier: deployment.model_dump(mode="json") for tier, deployment in self.tiers.items()
        }
        if actual != expected:
            raise ValueError("four-tier deployment ladder does not match production")
        return self


class _RequestDocument(_FrozenModel):
    schema_version: Literal["opensquilla.route_only_request.v1"]
    inference_run_id: str
    input_jsonl: str
    router_config: str
    model_pool: str
    routing_session_mode: Literal["independent", "episode"]


class _InputRow:
    """Validated input plus explicit stateful replay metadata."""

    __slots__ = (
        "control_event",
        "episode_id",
        "input",
        "input_message_id",
        "item_id",
        "request_id",
        "row_sha256",
        "turn_index",
    )

    def __init__(
        self,
        *,
        item_id: str,
        input: _RouteBeforeInput,
        row_sha256: str,
        episode_id: str | None,
        turn_index: int | None,
        request_id: str | None,
        input_message_id: str | None,
        control_event: str | None,
    ) -> None:
        self.item_id = item_id
        self.input = input
        self.row_sha256 = row_sha256
        self.episode_id = episode_id
        self.turn_index = turn_index
        self.request_id = request_id
        self.input_message_id = input_message_id
        self.control_event = control_event


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BenchmarkRouteOnlyError("non-finite JSON numbers are forbidden")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise BenchmarkRouteOnlyError("canonical JSON object keys must be strings")
            key = _normalize_json(raw_key)
            if key in normalized:
                raise BenchmarkRouteOnlyError(
                    f"canonical JSON keys collide after normalization: {key!r}"
                )
            normalized[key] = _normalize_json(raw_value)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _normalize_json(model_dump(mode="json"))
    raise BenchmarkRouteOnlyError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the cross-project canonical JSON contract."""

    return json.dumps(
        _normalize_json(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _strict_json_loads(payload: bytes, *, label: str) -> object:
    def reject_constant(value: str) -> object:
        raise BenchmarkRouteOnlyError(f"{label} contains non-finite constant {value!r}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise BenchmarkRouteOnlyError(f"{label} contains a non-finite number")
        return parsed

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise BenchmarkRouteOnlyError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkRouteOnlyError(f"{label} is not valid UTF-8 JSON") from exc

    stack: list[tuple[object, int]] = [(value, 1)]
    visited = 0
    while stack:
        current, depth = stack.pop()
        visited += 1
        if visited > _MAX_JSON_NODES:
            raise BenchmarkRouteOnlyError(f"{label} contains too many JSON nodes")
        if depth > _MAX_JSON_DEPTH:
            raise BenchmarkRouteOnlyError(f"{label} exceeds the JSON nesting limit")
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return value


def _load_canonical_document(path: Path, *, label: str) -> tuple[object, bytes]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BenchmarkRouteOnlyError(f"{label} cannot be inspected") from exc
    if size <= 0 or size > _MAX_JSON_DOCUMENT_BYTES:
        raise BenchmarkRouteOnlyError(f"{label} must be non-empty and below the size limit")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise BenchmarkRouteOnlyError(f"{label} cannot be read") from exc
    value = _strict_json_loads(payload, label=label)
    if canonical_json_bytes(value) != payload:
        raise BenchmarkRouteOnlyError(f"{label} is not canonical JSON")
    return value, payload


def _absolute_existing_file(value: str | Path, *, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise BenchmarkRouteOnlyError(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BenchmarkRouteOnlyError(f"{label} does not exist") from exc
    if not resolved.is_file():
        raise BenchmarkRouteOnlyError(f"{label} must be a regular file")
    return resolved


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise BenchmarkRouteOnlyError(f"{label} must be a non-empty identifier up to 256 chars")
    return value


def _optional_identifier(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, label=label)


def _key_tokens(key: str) -> tuple[str, ...]:
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return tuple(token for token in re.split(r"[^A-Za-z0-9]+", camel_split.lower()) if token)


def _contains_token_pattern(
    tokens: tuple[str, ...],
    pattern: tuple[str, ...],
) -> bool:
    width = len(pattern)
    return any(tokens[index : index + width] == pattern for index in range(len(tokens) - width + 1))


def _is_result_key(tokens: tuple[str, ...]) -> bool:
    patterns = _DIRECT_RESULT_KEYS
    return any(_contains_token_pattern(tokens, pattern) for pattern in patterns)


def _contains_result_assignment(value: str) -> bool:
    """Detect result-shaped assignments in an opaque string in linear time.

    Assignment operators divide the string into disjoint left-hand-side
    segments.  Tokenizing those segments with the same function used for JSON
    object keys avoids a second, subtly different alias grammar (and catches
    arbitrary separators such as ``quality/value=...``).  An implausibly long
    assignment key is rejected without tokenizing it, which keeps adversarial
    input bounded while failing closed.
    """

    # Colons can be either an assignment operator (JSON) or a separator inside
    # a flattened key (``official:cost=...``).  Carry only enough trailing
    # tokens to recognize a result-key pattern split across adjacent operators;
    # segments themselves remain disjoint, so total work stays linear.
    maximum_pattern_width = max(len(pattern) for pattern in _DIRECT_RESULT_KEYS)
    carry: tuple[str, ...] = ()
    segment_start = 0
    for operator in _ASSIGNMENT_OPERATOR.finditer(value):
        segment = value[segment_start : operator.start()]
        if len(segment) > _MAX_ASSIGNMENT_LHS_CHARS:
            return True
        combined = carry + _key_tokens(segment)
        if _is_result_key(combined):
            return True
        carry = combined[-(maximum_pattern_width - 1) :]
        if operator.group() in {"=", "->"}:
            carry = ()
        segment_start = operator.end()
    return False


def _find_result_data(value: object, path: str) -> str | None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            tokens = _key_tokens(key)
            if _is_result_key(tokens):
                return f"{path}.{key}"
            found = _find_result_data(child, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found = _find_result_data(child, f"{path}[{index}]")
            if found is not None:
                return found
    elif isinstance(value, str) and _contains_result_assignment(value):
        return path
    return None


def _assert_safe_previous_usage(value: Mapping[str, JsonValue], *, item_id: str) -> None:
    """Accept only the flat prior-turn fields defined by the Router contract.

    The production runtime's merged usage object contains many diagnostics and
    the state machine historically forwarded every unknown non-execution key
    into the registered model input.  A result-blind Benchmark boundary cannot
    safely accept that open-ended shape: an oracle label or current-item score
    could otherwise be hidden under an arbitrary key.  Empty usage is valid;
    any present usage or execution bundle must be complete and strictly typed.
    """

    keys = set(value)
    allowed = _PREVIOUS_USAGE_KEYS | _PREVIOUS_USAGE_OPTIONAL_KEYS | _PREVIOUS_EXECUTION_KEYS
    unknown = keys - allowed
    if unknown:
        raise BenchmarkRouteOnlyError(
            f"input row {item_id!r} previous_usage contains unsupported fields"
        )

    present_usage = keys & (_PREVIOUS_USAGE_KEYS | _PREVIOUS_USAGE_OPTIONAL_KEYS)
    if present_usage and not _PREVIOUS_USAGE_KEYS.issubset(keys):
        raise BenchmarkRouteOnlyError(
            f"input row {item_id!r} previous_usage token bundle is incomplete"
        )
    for key in present_usage:
        metric = value[key]
        if (
            isinstance(metric, bool)
            or not isinstance(metric, int)
            or not 0 <= metric <= _MAX_FEATURE_INTEGER
        ):
            raise BenchmarkRouteOnlyError(
                f"input row {item_id!r} previous_usage.{key} must be a bounded non-negative integer"
            )

    present_execution = keys & _PREVIOUS_EXECUTION_KEYS
    if present_execution and present_execution != _PREVIOUS_EXECUTION_KEYS:
        raise BenchmarkRouteOnlyError(
            f"input row {item_id!r} previous_usage execution bundle is incomplete"
        )
    if present_execution:
        route_id = value["route_id"]
        execution_status = value["execution_status"]
        error_code = value["error_code"]
        response_id = value["response_id"]
        attempt_ids = value["attempt_ids"]
        retry_count = value["retry_count"]
        valid = (
            isinstance(route_id, str)
            and bool(route_id.strip())
            and isinstance(execution_status, str)
            and bool(execution_status.strip())
            and (error_code is None or isinstance(error_code, str))
            and (response_id is None or isinstance(response_id, str))
            and isinstance(attempt_ids, list)
            and all(
                isinstance(attempt_id, str) and bool(attempt_id.strip())
                for attempt_id in attempt_ids
            )
            and not isinstance(retry_count, bool)
            and isinstance(retry_count, int)
            and 0 <= retry_count <= _MAX_FEATURE_INTEGER
        )
        if not valid:
            raise BenchmarkRouteOnlyError(
                f"input row {item_id!r} previous_usage execution bundle is invalid"
            )


def _assert_feature_number(
    value: JsonValue,
    *,
    item_id: str,
    path: str,
) -> None:
    """Reject numeric values that cannot enter the frozen float feature path."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    if isinstance(value, int):
        safe = -_MAX_FEATURE_INTEGER <= value <= _MAX_FEATURE_INTEGER
    else:
        safe = math.isfinite(value) and -_MAX_FEATURE_INTEGER <= value <= _MAX_FEATURE_INTEGER
    if not safe:
        raise BenchmarkRouteOnlyError(
            f"input row {item_id!r} {path} exceeds the feature numeric range"
        )


def _assert_feature_ready(value: _RouteBeforeInput, *, item_id: str) -> None:
    """Validate the subset that the frozen numeric feature runtime assumes."""

    if not value.current_request.strip():
        raise BenchmarkRouteOnlyError(
            f"input row {item_id!r} current_request must contain non-whitespace text"
        )

    for index, entry in enumerate(value.route_history):
        raw_tier = entry.get("tier_id") or entry.get("tier") or entry.get("route_class")
        if not isinstance(raw_tier, str) or raw_tier.upper() not in _FEATURE_ROUTE_TIERS:
            raise BenchmarkRouteOnlyError(
                f"input row {item_id!r} route_history[{index}] has an invalid tier"
            )
        for key in ("difficulty", "difficulty_score", "margin"):
            if key in entry:
                _assert_feature_number(
                    entry[key],
                    item_id=item_id,
                    path=f"route_history[{index}].{key}",
                )

    if "context_tokens_est" in value.context:
        _assert_feature_number(
            value.context["context_tokens_est"],
            item_id=item_id,
            path="context.context_tokens_est",
        )


def _assert_result_blind(value: _RouteBeforeInput, *, item_id: str) -> None:
    _assert_safe_previous_usage(value.previous_usage, item_id=item_id)
    opaque = {
        "context": value.context,
        "tool_state": value.tool_state,
        "route_history": value.route_history,
        "attachments": value.attachments,
    }
    leaked_path = _find_result_data(opaque, "input")
    if leaked_path is not None:
        raise BenchmarkRouteOnlyError(
            f"input row {item_id!r} contains result-derived data at {leaked_path}"
        )


def _parse_input_row(
    value: object,
    *,
    line_number: int,
    routing_session_mode: Literal["independent", "episode"],
    raw_line: bytes,
) -> _InputRow:
    if not isinstance(value, Mapping):
        raise BenchmarkRouteOnlyError(f"input line {line_number} must be an object")
    base_keys = {"item_id", "input"}
    episode_required = {"episode_id", "turn_index"}
    episode_optional = {"request_id", "input_message_id", "control_event"}
    keys = set(value)
    if routing_session_mode == "independent":
        if keys != base_keys:
            raise BenchmarkRouteOnlyError(
                f"independent input line {line_number} must contain exactly item_id and input"
            )
    elif not base_keys.union(episode_required).issubset(keys) or not keys.issubset(
        base_keys.union(episode_required, episode_optional)
    ):
        raise BenchmarkRouteOnlyError(
            f"episode input line {line_number} has an incompatible field set"
        )

    item_id = _identifier(value.get("item_id"), label=f"input line {line_number} item_id")
    try:
        # Validate with Pydantic's JSON-aware strict mode.  JSON arrays are the
        # canonical representation of tuple fields; validating the already
        # parsed Python list in strict mode would reject valid RouterInput.
        route_input = _RouteBeforeInput.model_validate_json(
            canonical_json_bytes(value.get("input")), strict=True
        )
    except ValidationError as exc:
        raise BenchmarkRouteOnlyError(
            f"input line {line_number} violates the canonical RouterInput contract"
        ) from exc
    _assert_feature_ready(route_input, item_id=item_id)
    _assert_result_blind(route_input, item_id=item_id)

    episode_id: str | None = None
    turn_index: int | None = None
    request_id: str | None = None
    input_message_id: str | None = None
    control_event: str | None = None
    if routing_session_mode == "episode":
        episode_id = _identifier(
            value.get("episode_id"), label=f"input line {line_number} episode_id"
        )
        raw_turn_index = value.get("turn_index")
        if (
            isinstance(raw_turn_index, bool)
            or not isinstance(raw_turn_index, int)
            or raw_turn_index < 0
        ):
            raise BenchmarkRouteOnlyError(
                f"input line {line_number} turn_index must be a non-negative integer"
            )
        turn_index = raw_turn_index
        request_id = _optional_identifier(
            value.get("request_id"), label=f"input line {line_number} request_id"
        )
        input_message_id = _optional_identifier(
            value.get("input_message_id"),
            label=f"input line {line_number} input_message_id",
        )
        raw_control_event = value.get("control_event")
        if raw_control_event is not None:
            if not isinstance(raw_control_event, str) or not raw_control_event.strip():
                raise BenchmarkRouteOnlyError(
                    f"input line {line_number} control_event must be non-empty when provided"
                )
            control_event = raw_control_event

    return _InputRow(
        item_id=item_id,
        input=route_input,
        row_sha256=_sha256_bytes(raw_line),
        episode_id=episode_id,
        turn_index=turn_index,
        request_id=request_id,
        input_message_id=input_message_id,
        control_event=control_event,
    )


def _iter_input_rows(
    stream: BinaryIO,
    *,
    routing_session_mode: Literal["independent", "episode"],
) -> Iterator[_InputRow]:
    stream.seek(0)
    line_number = 0
    while line := stream.readline(_MAX_JSONL_LINE_BYTES + 2):
        line_number += 1
        if len(line) > _MAX_JSONL_LINE_BYTES + 1:
            raise BenchmarkRouteOnlyError(f"input line {line_number} exceeds the line size limit")
        if not line.endswith(b"\n"):
            if len(line) > _MAX_JSONL_LINE_BYTES:
                raise BenchmarkRouteOnlyError(
                    f"input line {line_number} exceeds the line size limit"
                )
            raise BenchmarkRouteOnlyError("input JSONL must end every row with LF")
        raw_line = line[:-1]
        if not raw_line or len(raw_line) > _MAX_JSONL_LINE_BYTES:
            raise BenchmarkRouteOnlyError(
                f"input line {line_number} is blank or exceeds the line size limit"
            )
        value = _strict_json_loads(raw_line, label=f"input line {line_number}")
        if canonical_json_bytes(value) != raw_line:
            raise BenchmarkRouteOnlyError(f"input line {line_number} is not canonical JSON")
        yield _parse_input_row(
            value,
            line_number=line_number,
            routing_session_mode=routing_session_mode,
            raw_line=raw_line,
        )


def _validate_input_bundle(
    stream: BinaryIO,
    *,
    routing_session_mode: Literal["independent", "episode"],
) -> int:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if size <= 0 or size > _MAX_JSONL_BYTES:
        raise BenchmarkRouteOnlyError("input JSONL must be non-empty and below the size limit")

    item_ids: set[str] = set()
    previous_sort_key: str | tuple[str, int] | None = None
    expected_episode_turn: dict[str, int] = {}
    count = 0
    for row in _iter_input_rows(stream, routing_session_mode=routing_session_mode):
        if row.item_id in item_ids:
            raise BenchmarkRouteOnlyError(f"duplicate input item_id: {row.item_id!r}")
        item_ids.add(row.item_id)
        if routing_session_mode == "independent":
            sort_key: str | tuple[str, int] = row.item_id
            if row.input.active_route_tier is not None:
                raise BenchmarkRouteOnlyError(
                    f"independent input {row.item_id!r} must have active_route_tier=null"
                )
        else:
            assert row.episode_id is not None and row.turn_index is not None
            sort_key = (row.episode_id, row.turn_index)
            expected = expected_episode_turn.get(row.episode_id, 0)
            if row.turn_index != expected:
                raise BenchmarkRouteOnlyError(
                    f"episode {row.episode_id!r} turn_index must be contiguous from zero"
                )
            expected_episode_turn[row.episode_id] = expected + 1
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            ordering = (
                "item_id" if routing_session_mode == "independent" else "(episode_id, turn_index)"
            )
            raise BenchmarkRouteOnlyError(f"input rows must be strictly sorted by {ordering}")
        previous_sort_key = sort_key
        count += 1
    if count == 0:
        raise BenchmarkRouteOnlyError("input JSONL must contain at least one row")
    return count


def _snapshot_input(path: Path, destination: BinaryIO) -> str:
    """Copy one path-opened input into the exact immutable stream we consume."""

    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                if size > _MAX_JSONL_BYTES:
                    raise BenchmarkRouteOnlyError("input JSONL exceeds the size limit")
                digest.update(chunk)
                destination.write(chunk)
    except BenchmarkRouteOnlyError:
        raise
    except OSError as exc:
        raise BenchmarkRouteOnlyError("input JSONL cannot be snapshotted") from exc
    if size == 0:
        raise BenchmarkRouteOnlyError("input JSONL must be non-empty")
    destination.flush()
    destination.seek(0)
    return f"sha256:{digest.hexdigest()}"


def _load_request(path: Path) -> tuple[_RequestDocument, bytes]:
    value, payload = _load_canonical_document(path, label="route-only request")
    try:
        request = _RequestDocument.model_validate(value, strict=True)
    except ValidationError as exc:
        raise BenchmarkRouteOnlyError("route-only request violates its strict contract") from exc
    _identifier(request.inference_run_id, label="inference_run_id")
    return request, payload


def _load_router_config(path: Path) -> tuple[_FourTierConfig, bytes]:
    value, payload = _load_canonical_document(path, label="router config")
    try:
        config = _FourTierConfig.model_validate_json(payload, strict=True)
    except ValidationError as exc:
        raise BenchmarkRouteOnlyError(
            "router config is not a valid fixed-four-tier policy"
        ) from exc
    if canonical_json_bytes(config.model_dump(mode="json")) != payload:
        raise BenchmarkRouteOnlyError(
            "router config must be the complete canonical current-version policy"
        )
    return config, payload


def _load_model_pool(
    path: Path,
    *,
    config: _FourTierConfig,
) -> tuple[dict[str, _ModelIdentity], bytes]:
    value, payload = _load_canonical_document(path, label="model pool")
    if not isinstance(value, Mapping) or set(value) != set(_TIERS):
        raise BenchmarkRouteOnlyError("model pool must define exactly C0-C3")
    identities: dict[str, _ModelIdentity] = {}
    for tier in _TIERS:
        try:
            identity = _ModelIdentity.model_validate(value[tier], strict=True)
        except ValidationError as exc:
            raise BenchmarkRouteOnlyError(f"model pool {tier} identity is invalid") from exc
        _identifier(identity.model_id, label=f"model pool {tier} model_id")
        _identifier(identity.revision, label=f"model pool {tier} revision")
        if _SHA256.fullmatch(identity.definition_hash) is None:
            raise BenchmarkRouteOnlyError(
                f"model pool {tier} definition_hash must be tagged SHA-256"
            )
        tier_config = config.tiers[tier.lower()]
        if identity.model_id != tier_config.model:
            raise BenchmarkRouteOnlyError(
                f"model pool {tier} model_id does not match the production ladder"
            )
        if identity.revision != tier_config.deployment_version:
            raise BenchmarkRouteOnlyError(
                f"model pool {tier} revision does not match deployment_version"
            )
        identities[tier] = identity
    if len({identity.model_id for identity in identities.values()}) != 4:
        raise BenchmarkRouteOnlyError("model pool must contain four distinct model ids")
    normalized = {tier: identities[tier].model_dump(mode="json") for tier in _TIERS}
    if canonical_json_bytes(normalized) != payload:
        raise BenchmarkRouteOnlyError("model pool is not canonical CandidateModelIdentity JSON")
    return identities, payload


class _DeterministicEvidence:
    """Deterministic non-semantic IDs and logical clock for replay evidence."""

    def __init__(self, semantic_execution_hash: str) -> None:
        self._semantic_execution_hash = semantic_execution_hash
        self._row_key = "uninitialized"
        self._logical_ms = -1
        self._route_counter = 0
        self._task_counter = 0

    def begin_row(self, row: _InputRow, *, logical_ms: int) -> None:
        episode = row.episode_id or "independent"
        turn = str(row.turn_index) if row.turn_index is not None else "0"
        self._row_key = f"{episode}\0{turn}\0{row.item_id}"
        self._logical_ms = logical_ms
        self._route_counter = 0
        self._task_counter = 0

    def _identifier(self, namespace: str, counter: int) -> str:
        raw = (
            f"{REQUEST_SCHEMA_VERSION}\0{self._semantic_execution_hash}\0{namespace}\0"
            f"{self._row_key}\0{counter}"
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    def route_id(self) -> str:
        value = self._identifier("route", self._route_counter)
        self._route_counter += 1
        return value

    def task_id(self) -> str:
        value = self._identifier("task", self._task_counter)
        self._task_counter += 1
        return value

    def clock_ms(self) -> int:
        return self._logical_ms


def _build_router(
    config: _FourTierConfig,
    *,
    evidence: _DeterministicEvidence,
) -> FixedFourTierV2Router:
    classifier_config = config.classifier
    classifier = RegisteredModelClassifier(
        artifact_root=str(classifier_config.artifact_root),
        metadata_db=str(classifier_config.metadata_db),
        model_set_id=classifier_config.model_set_id,
        expected_manifest_hash=classifier_config.expected_manifest_hash,
        allow_candidate=classifier_config.allow_candidate,
    )
    policy_payload = fixed_four_tier_semantic_policy_config(config.model_dump(mode="json"))
    try:
        return FixedFourTierV2Router(
            intent_classifier=classifier,
            tier_classifier=classifier,
            default_new_task_tier=cast(Any, config.default_new_task_tier),
            intent_min_confidence=config.intent_min_confidence,
            tier_min_confidence=config.tier_min_confidence,
            min_margin=config.min_margin,
            route_id_factory=evidence.route_id,
            task_id_factory=evidence.task_id,
            clock_ms=evidence.clock_ms,
            policy_config=policy_payload,
        )
    except BaseException:
        classifier.close()
        raise


def _stable_ref(namespace: str, inference_run_id: str, row: _InputRow) -> str:
    value = (
        f"{REQUEST_SCHEMA_VERSION}\0{namespace}\0{inference_run_id}\0"
        f"{row.episode_id or 'independent'}\0{row.turn_index or 0}\0{row.item_id}"
    ).encode()
    return f"benchmark-{namespace}-{hashlib.sha256(value).hexdigest()}"


def _routing_request(
    row: _InputRow,
    *,
    inference_run_id: str,
) -> RoutingRequest:
    route_input = row.input
    attachments = tuple(dict(value) for value in route_input.attachments)
    modalities = normalize_attachment_modalities(attachments)
    return RoutingRequest(
        session_id=(
            row.episode_id
            if row.episode_id is not None
            else _stable_ref("session", inference_run_id, row)
        ),
        request_id=row.request_id or _stable_ref("request", inference_run_id, row),
        message=route_input.current_request,
        input_message_id=(
            row.input_message_id or _stable_ref("input-message", inference_run_id, row)
        ),
        task_anchor=route_input.task_anchor,
        user_history=tuple(route_input.history_user),
        previous_assistant_text=route_input.previous_answer,
        previous_assistant_usage=dict(route_input.previous_usage),
        previous_outcome=route_input.previous_outcome,
        route_history=tuple(dict(value) for value in route_input.route_history),
        context=dict(route_input.context),
        tool_state=dict(route_input.tool_state),
        attachments=attachments,
        attachment_count=len(attachments),
        attachment_modalities=modalities,
        control_event=row.control_event,
    )


def _tagged(value: str, *, label: str) -> str:
    if _SHA256.fullmatch(value):
        return value
    if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
        return f"sha256:{value}"
    raise BenchmarkRouteOnlyError(f"{label} is not a SHA-256 identity")


def _decision_row(
    *,
    inference_run_id: str,
    input_row: _InputRow,
    decision: Any,
    next_state: FixedFourTierTaskState,
    model_pool: Mapping[str, _ModelIdentity],
    config: _FourTierConfig,
) -> dict[str, Any]:
    final_tier = str(decision.final_tier).upper()
    identity = model_pool[final_tier]
    deployment = config.tiers[final_tier.lower()]
    deployment_identity = {
        "provider": deployment.provider,
        "model": deployment.model,
        "reasoning": deployment.reasoning,
        "deployment_version": deployment.deployment_version,
    }
    trace = decision.trace(provider=deployment.provider, model=deployment.model)
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "inference_run_id": inference_run_id,
        "item_id": input_row.item_id,
        "episode_id": input_row.episode_id,
        "turn_index": input_row.turn_index,
        "request_id": decision.request_id,
        "input_row_sha256": input_row.row_sha256,
        "final_intent": decision.intent.final,
        "final_tier": final_tier,
        "provider": deployment.provider,
        "model_id": identity.model_id,
        "model_revision": identity.revision,
        "model_definition_hash": identity.definition_hash,
        "deployment_definition_hash": _sha256_bytes(canonical_json_bytes(deployment_identity)),
        "reasoning": deployment.reasoning,
        "route_id": decision.route_id,
        "task_id": decision.task_id,
        "previous_tier": (
            decision.previous_tier.upper() if decision.previous_tier is not None else None
        ),
        "switched": decision.switched,
        "switch_reason": decision.switch_reason,
        "context_action": decision.context_action,
        "state_version_after": next_state.version,
        "policy_hash": _tagged(decision.policy_hash, label="decision policy_hash"),
        "input_snapshot_hash": _tagged(
            decision.input_snapshot_hash, label="decision input_snapshot_hash"
        ),
        "classifier_identity": dict(decision.classifier_identity or {}),
        "route_trace": trace,
    }


def _route_rows(
    *,
    input_stream: BinaryIO,
    request: _RequestDocument,
    evidence: _DeterministicEvidence,
    router: FixedFourTierV2Router,
    config: _FourTierConfig,
    model_pool: Mapping[str, _ModelIdentity],
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    states: dict[str, FixedFourTierTaskState] = {}
    decisions: list[dict[str, Any]] = []
    policy_hash: str | None = None
    runtime_identity: dict[str, Any] | None = None

    for logical_ms, row in enumerate(
        _iter_input_rows(input_stream, routing_session_mode=request.routing_session_mode)
    ):
        evidence.begin_row(row, logical_ms=logical_ms)
        episode_key = row.episode_id
        state = states.get(episode_key) if episode_key is not None else None
        expected_active_tier = state.tier.upper() if state is not None else None
        if row.input.active_route_tier != expected_active_tier:
            raise BenchmarkRouteOnlyError(
                f"input {row.item_id!r} active_route_tier does not match replay state"
            )
        decision, next_state = router.decide(
            _routing_request(row, inference_run_id=request.inference_run_id),
            state,
        )
        if request.routing_session_mode == "independent" and decision.intent.final != "new_task":
            raise BenchmarkRouteOnlyError(
                "independent route-only replay must produce new_task intent"
            )
        if decision.classifier_backend != "registered_model":
            raise BenchmarkRouteOnlyError("route-only worker did not execute registered_model")
        current_policy_hash = _tagged(decision.policy_hash, label="policy_hash")
        current_identity = dict(decision.classifier_identity or {})
        if not current_identity:
            raise BenchmarkRouteOnlyError("registered model runtime identity is unavailable")
        if policy_hash is None:
            policy_hash = current_policy_hash
            runtime_identity = current_identity
        elif policy_hash != current_policy_hash or runtime_identity != current_identity:
            raise BenchmarkRouteOnlyError("Router policy or runtime identity changed during replay")
        if episode_key is not None:
            states[episode_key] = next_state
        decisions.append(
            _decision_row(
                inference_run_id=request.inference_run_id,
                input_row=row,
                decision=decision,
                next_state=next_state,
                model_pool=model_pool,
                config=config,
            )
        )

    if policy_hash is None or runtime_identity is None:
        raise BenchmarkRouteOnlyError("route-only replay produced no decisions")
    decisions.sort(key=lambda value: str(value["item_id"]))
    return decisions, policy_hash, runtime_identity


def _source_identity() -> tuple[dict[str, dict[str, Any]], str]:
    package_root = Path(__file__).resolve().parents[2]
    relative_files = (
        "__init__.py",
        "engine/__init__.py",
        "engine/routing/__init__.py",
        "engine/routing/benchmark_worker.py",
        "engine/routing/fixed_four_tier_v2.py",
        "engine/routing/registered_model.py",
        "gateway/config.py",
    )
    files: dict[str, dict[str, Any]] = {}
    for relative in relative_files:
        path = package_root / relative
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("not a regular source file")
            size = metadata.st_size
        except OSError as exc:
            raise BenchmarkRouteOnlyError(
                f"OpenSquilla source identity is unavailable for {relative}"
            ) from exc
        logical_name = f"opensquilla/{relative}"
        files[logical_name] = {"size": size, "sha256": _sha256_file(path)}
    return files, _sha256_bytes(canonical_json_bytes(files))


def _replay_scope(routing_session_mode: str) -> str:
    return (
        "independent_items"
        if routing_session_mode == "independent"
        else "synthetic_ordered_fixed_context_episodes"
    )


def _semantic_execution_hash(
    *,
    request: _RequestDocument,
    input_hash: str,
    semantic_router_config_hash: str,
    model_pool_hash: str,
) -> str:
    return _sha256_bytes(
        canonical_json_bytes(
            {
                "schema_version": SEMANTIC_EXECUTION_SCHEMA_VERSION,
                "inference_run_id": request.inference_run_id,
                "input_hash": input_hash,
                "semantic_router_config_hash": semantic_router_config_hash,
                "model_pool_hash": model_pool_hash,
                "routing_session_mode": request.routing_session_mode,
                "replay_scope": _replay_scope(request.routing_session_mode),
            }
        )
    )


def _attestation(
    *,
    request: _RequestDocument,
    input_payload_sha256: str,
    model_pool_payload: bytes,
    output_hash: str,
    item_count: int,
    semantic_execution_hash: str,
    semantic_router_config_hash: str,
    policy_hash: str,
    runtime_identity: Mapping[str, Any],
    config: _FourTierConfig,
    source_files: dict[str, dict[str, Any]],
    opensquilla_code_digest: str,
) -> dict[str, Any]:
    required_runtime_hashes = (
        "model_manifest_hash",
        "artifact_closure_hash",
        "runner_digest",
        "environment_digest",
    )
    for name in required_runtime_hashes:
        _tagged(str(runtime_identity.get(name) or ""), label=f"runtime identity {name}")
    deployment_mapping = {
        tier.upper(): {
            "provider": deployment.provider,
            "model": deployment.model,
            "reasoning": deployment.reasoning,
            "deployment_version": deployment.deployment_version,
        }
        for tier, deployment in config.tiers.items()
    }
    return {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "execution_mode": EXECUTION_MODE,
        "routing_session_mode": request.routing_session_mode,
        "replay_scope": _replay_scope(request.routing_session_mode),
        "inference_run_id": request.inference_run_id,
        "semantic_execution_hash": semantic_execution_hash,
        "input_hash": input_payload_sha256,
        "semantic_router_config_hash": semantic_router_config_hash,
        "model_pool_hash": _sha256_bytes(model_pool_payload),
        "deployment_mapping_hash": _sha256_bytes(canonical_json_bytes(deployment_mapping)),
        "output_hash": _tagged(output_hash, label="output_hash"),
        "item_count": item_count,
        "decision_count": item_count,
        "policy_hash": policy_hash,
        "model_manifest_hash": runtime_identity["model_manifest_hash"],
        "artifact_closure_hash": runtime_identity["artifact_closure_hash"],
        "runner_digest": runtime_identity["runner_digest"],
        "environment_digest": runtime_identity["environment_digest"],
        "classifier_identity": dict(runtime_identity),
        "opensquilla_version": opensquilla_version,
        "opensquilla_code_digest": opensquilla_code_digest,
        "opensquilla_code_files": source_files,
        "clock_mode": "deterministic_logical_ms",
        "no_dispatch": True,
        "result_blind": True,
    }


def _write_canonical_jsonl(
    rows: list[dict[str, Any]],
    destination: BinaryIO,
) -> str:
    """Incrementally encode bounded decision JSONL and return its tagged hash."""

    digest = hashlib.sha256()
    total_size = 0
    destination.seek(0)
    destination.truncate(0)
    for row in rows:
        payload = canonical_json_bytes(row)
        if len(payload) > _MAX_DECISION_LINE_BYTES:
            raise BenchmarkRouteOnlyError("route-only decision exceeds the line size limit")
        next_size = total_size + len(payload) + 1
        if next_size > _MAX_DECISIONS_JSONL_BYTES:
            raise BenchmarkRouteOnlyError("route-only decisions exceed the output size limit")
        destination.write(payload)
        destination.write(b"\n")
        digest.update(payload)
        digest.update(b"\n")
        total_size = next_size
    destination.flush()
    destination.seek(0)
    return f"sha256:{digest.hexdigest()}"


def _prepare_output_dir(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise BenchmarkRouteOnlyError("output directory must be an absolute path")
    resolved = path.resolve(strict=False)
    try:
        if resolved.exists():
            if not resolved.is_dir():
                raise BenchmarkRouteOnlyError("output directory path is not a directory")
            if any(resolved.iterdir()):
                raise BenchmarkRouteOnlyError("output directory must be empty")
        else:
            resolved.mkdir(parents=True, exist_ok=False)
    except BenchmarkRouteOnlyError:
        raise
    except OSError as exc:
        raise BenchmarkRouteOnlyError("output directory cannot be prepared") from exc
    return resolved


def _write_bundle(
    output_dir: Path,
    *,
    decisions_payload: bytes | BinaryIO,
    attestation_payload: bytes,
) -> None:
    decisions_path = output_dir / DECISIONS_FILENAME
    attestation_path = output_dir / ATTESTATION_FILENAME
    temporary_paths: list[Path] = []
    try:
        for payload, target in (
            (decisions_payload, decisions_path),
            (attestation_payload, attestation_path),
        ):
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=output_dir
            )
            temporary = Path(temporary_name)
            temporary_paths.append(temporary)
            with os.fdopen(descriptor, "wb") as stream:
                if isinstance(payload, bytes):
                    stream.write(payload)
                else:
                    payload.seek(0)
                    for chunk in iter(lambda: payload.read(1024 * 1024), b""):
                        stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary_paths[0], decisions_path)
        os.replace(temporary_paths[1], attestation_path)
        descriptor = os.open(output_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        # This directory was required to be empty.  Remove either/both renamed
        # targets so an fsync failure cannot leave a misleading commit marker.
        for target in (attestation_path, decisions_path):
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise BenchmarkRouteOnlyError("route-only output bundle could not be committed") from exc
    finally:
        for temporary in temporary_paths:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def run_route_only(
    request_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Execute one complete headless route-only batch and return its attestation."""

    request_file = _absolute_existing_file(request_path, label="request file")
    request, _request_payload = _load_request(request_file)
    input_path = _absolute_existing_file(request.input_jsonl, label="input_jsonl")
    router_config_path = _absolute_existing_file(request.router_config, label="router_config")
    model_pool_path = _absolute_existing_file(request.model_pool, label="model_pool")
    destination = _prepare_output_dir(output_dir)

    config, _router_config_payload = _load_router_config(router_config_path)
    model_pool, model_pool_payload = _load_model_pool(model_pool_path, config=config)
    semantic_router_config_hash = _sha256_bytes(
        canonical_json_bytes(fixed_four_tier_semantic_policy_config(config.model_dump(mode="json")))
    )
    model_pool_hash = _sha256_bytes(model_pool_payload)
    source_files, opensquilla_code_digest = _source_identity()
    with tempfile.TemporaryFile(mode="w+b") as input_snapshot:
        input_hash = _snapshot_input(input_path, input_snapshot)
        item_count = _validate_input_bundle(
            input_snapshot, routing_session_mode=request.routing_session_mode
        )
        semantic_execution_hash = _semantic_execution_hash(
            request=request,
            input_hash=input_hash,
            semantic_router_config_hash=semantic_router_config_hash,
            model_pool_hash=model_pool_hash,
        )
        evidence = _DeterministicEvidence(semantic_execution_hash)
        router: FixedFourTierV2Router | None = None
        try:
            router = _build_router(config, evidence=evidence)
            decisions, policy_hash, runtime_identity = _route_rows(
                input_stream=input_snapshot,
                request=request,
                evidence=evidence,
                router=router,
                config=config,
                model_pool=model_pool,
            )
        except BenchmarkRouteOnlyError:
            raise
        except Exception as exc:
            raise BenchmarkRouteOnlyError("route-only Router execution failed") from exc
        finally:
            if router is not None:
                try:
                    router.close()
                except Exception as exc:
                    raise BenchmarkRouteOnlyError("route-only Router cleanup failed") from exc

    final_source_files, final_code_digest = _source_identity()
    if final_source_files != source_files or final_code_digest != opensquilla_code_digest:
        raise BenchmarkRouteOnlyError("OpenSquilla routing source changed during replay")

    with tempfile.TemporaryFile(mode="w+b") as decisions_snapshot:
        output_hash = _write_canonical_jsonl(decisions, decisions_snapshot)
        attestation = _attestation(
            request=request,
            input_payload_sha256=input_hash,
            model_pool_payload=model_pool_payload,
            output_hash=output_hash,
            item_count=item_count,
            semantic_execution_hash=semantic_execution_hash,
            semantic_router_config_hash=semantic_router_config_hash,
            policy_hash=policy_hash,
            runtime_identity=runtime_identity,
            config=config,
            source_files=source_files,
            opensquilla_code_digest=opensquilla_code_digest,
        )
        attestation_payload = canonical_json_bytes(attestation)
        _write_bundle(
            destination,
            decisions_payload=decisions_snapshot,
            attestation_payload=attestation_payload,
        )
    return attestation


def _error_payload(exc: BaseException) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": ERROR_SCHEMA_VERSION,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run result-blind OpenSquilla four-tier routing without dispatching an LLM."
    )
    parser.add_argument("--request", required=True, help="Absolute canonical request JSON path.")
    parser.add_argument("--output-dir", required=True, help="Absolute empty output directory.")
    arguments = parser.parse_args(argv)
    try:
        attestation = run_route_only(arguments.request, arguments.output_dir)
    except Exception as exc:  # noqa: BLE001 - process boundary returns structured failure.
        sys.stderr.buffer.write(_error_payload(exc) + b"\n")
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(attestation) + b"\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - covered through ``main``.
    raise SystemExit(main())
