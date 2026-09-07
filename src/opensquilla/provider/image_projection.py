"""Pure, request-local projection of image-bearing provider messages.

The transcript is the source of truth for user input and attachments.  This
module deliberately operates on a deep copy of that transcript and produces
the view for one *physical* provider call.  In particular, replacing an image
with a text marker here never changes the persisted message, which lets a
later turn (or a different configured model) recover the original image.

Only the projection boundary belongs here.  Session storage, model selection,
and retry orchestration can consume the value objects below without making the
projection module depend on the runtime.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from .types import (
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    Message,
    VisionSupport,
)

# ---------------------------------------------------------------------------
# Capability and projection value objects
# ---------------------------------------------------------------------------


class VisionSupportSource(StrEnum):
    """Where a model's tri-state vision fact came from.

    The source is diagnostic only.  It intentionally does not imply that a
    model may be selected; authorization remains owned by the caller.
    """

    ENSEMBLE_CONTRACT = "ensemble_contract"
    USER_CONFIG = "user_config"
    RUNTIME_OBSERVATION = "runtime_observation"
    CATALOG = "catalog"
    NONE = "none"
    UNKNOWN = "unknown"


class VisionSupportStatus(StrEnum):
    """Enum spelling of :data:`VisionSupport` for runtime consumers."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


type VisionSupportValue = VisionSupport | bool | None


@dataclass(frozen=True, slots=True)
class VisionSupportEvidence:
    """Tri-state capability evidence for one exact model deployment.

    ``unsupported`` is authoritative only when ``source`` carries an
    explicit user/configuration fact or a precise runtime observation.  A
    missing catalog row should be represented as ``unknown`` rather than as a
    false value.
    """

    status: VisionSupport = "unknown"
    source: VisionSupportSource | str = VisionSupportSource.UNKNOWN
    deployment: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        status = normalize_vision_support(self.status)
        source = str(self.source or VisionSupportSource.UNKNOWN).strip().lower()
        if not source:
            source = VisionSupportSource.UNKNOWN
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "deployment", str(self.deployment or "").strip())
        object.__setattr__(self, "reason", str(self.reason or "").strip())

    @property
    def supports_images(self) -> bool:
        """Whether this evidence authorizes sending an image natively."""

        return self.status == "supported"

    @property
    def rejects_images(self) -> bool:
        return self.status == "unsupported"

    @property
    def is_unknown(self) -> bool:
        return self.status == "unknown"

    @classmethod
    def from_value(
        cls,
        value: VisionSupportValue | VisionSupportEvidence,
        *,
        source: VisionSupportSource | str = VisionSupportSource.UNKNOWN,
        deployment: str = "",
        reason: str = "",
    ) -> VisionSupportEvidence:
        if isinstance(value, cls):
            return value
        return cls(
            status=normalize_vision_support(value),
            source=source,
            deployment=deployment,
            reason=reason,
        )


def normalize_vision_support(value: object, *, field_present: bool | None = None) -> VisionSupport:
    """Normalize legacy booleans and absent fields without collapsing unknown.

    ``field_present=False`` is useful when reading old Router/tier mappings:
    an omitted ``supports_image`` key means *unknown*, while an explicit
    ``False`` means *unsupported*.
    """

    if field_present is False:
        return "unknown"
    if isinstance(value, VisionSupportEvidence):
        return value.status
    if value is True:
        return "supported"
    if value is False:
        return "unsupported"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"supported", "support", "true", "yes", "vision", "native"}:
            return "supported"
        if normalized in {
            "unsupported",
            "unsupported_feature",
            "false",
            "no",
            "text_only",
            "text-only",
        }:
            return "unsupported"
    return "unknown"


def resolve_vision_support(
    value: VisionSupportValue | VisionSupportEvidence,
    *,
    source: VisionSupportSource | str = VisionSupportSource.UNKNOWN,
    deployment: str = "",
    reason: str = "",
    field_present: bool | None = None,
) -> VisionSupportEvidence:
    """Return normalized evidence while retaining source and deployment facts."""

    if isinstance(value, VisionSupportEvidence):
        return value
    return VisionSupportEvidence(
        status=normalize_vision_support(value, field_present=field_present),
        source=source,
        deployment=deployment,
        reason=reason,
    )


class ImageProjectionMode(StrEnum):
    """How an image is represented in one outbound request."""

    NATIVE = "native"
    MARKER = "marker"
    TEXT_ONLY = "marker"
    SURROGATE = "surrogate"


class ImageIntentKind(StrEnum):
    """The caller's reason for including historical/current image context."""

    NONE = "none"
    CURRENT_UPLOAD = "current_upload"
    EXPLICIT_HISTORY = "explicit_history"
    IMPLICIT_RECENT_FOLLOWUP = "implicit_recent_followup"
    EXPLICITLY_IGNORED = "explicitly_ignored"


@dataclass(frozen=True, slots=True)
class ImageIntent:
    """Request-local image intent, independent of model capability."""

    kind: ImageIntentKind | str = ImageIntentKind.NONE
    attachment_ids: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        raw_kind = str(self.kind or ImageIntentKind.NONE).strip().lower()
        aliases = {
            "current": ImageIntentKind.CURRENT_UPLOAD,
            "upload": ImageIntentKind.CURRENT_UPLOAD,
            "history": ImageIntentKind.EXPLICIT_HISTORY,
            "explicit": ImageIntentKind.EXPLICIT_HISTORY,
            "recent": ImageIntentKind.IMPLICIT_RECENT_FOLLOWUP,
            "implicit": ImageIntentKind.IMPLICIT_RECENT_FOLLOWUP,
            "ignored": ImageIntentKind.EXPLICITLY_IGNORED,
        }
        normalized = aliases.get(raw_kind, raw_kind)
        try:
            normalized_kind: ImageIntentKind | str = ImageIntentKind(normalized)
        except ValueError:
            # Preserve forward-compatible values for callers that add a new
            # intent kind before this package is upgraded.
            normalized_kind = raw_kind or ImageIntentKind.NONE
        raw_ids = self.attachment_ids or ()
        ids = tuple(
            item.strip()
            for item in raw_ids
            if isinstance(item, str) and item.strip()
        )
        object.__setattr__(self, "kind", normalized_kind)
        object.__setattr__(self, "attachment_ids", ids)
        object.__setattr__(self, "reason", str(self.reason or "").strip())


class ImageMarkerState(StrEnum):
    """Truthful explanation used when an image is absent from a text request."""

    NOT_ANALYZED = "not_analyzed"
    ANALYSIS_FAILED = "analysis_failed"
    NOT_REREAD = "not_reread"
    UNAVAILABLE = "unavailable"
    NOT_SENT = "not_sent"


_MARKER_STATE_ALIASES: dict[str, ImageMarkerState] = {
    "failed": ImageMarkerState.ANALYSIS_FAILED,
    "analysis_error": ImageMarkerState.ANALYSIS_FAILED,
    "historical_not_read": ImageMarkerState.NOT_REREAD,
    "not_read": ImageMarkerState.NOT_REREAD,
    "unread": ImageMarkerState.NOT_REREAD,
    "missing": ImageMarkerState.UNAVAILABLE,
    "invalid": ImageMarkerState.UNAVAILABLE,
    "omitted": ImageMarkerState.NOT_SENT,
}


def normalize_marker_state(value: ImageMarkerState | str | None) -> ImageMarkerState:
    if isinstance(value, ImageMarkerState):
        return value
    raw = str(value or ImageMarkerState.NOT_ANALYZED).strip().lower()
    try:
        return ImageMarkerState(raw)
    except ValueError:
        return _MARKER_STATE_ALIASES.get(raw, ImageMarkerState.NOT_ANALYZED)


def _safe_attachment_id(value: object) -> str:
    """Keep IDs in a marker bounded and free of control/injection characters."""

    text = str(value or "").strip()
    if not text:
        return ""
    sanitized = re.sub(r"[^A-Za-z0-9_.:-]", "_", text)
    # Manifest IDs allow the ``att_`` prefix plus up to 160 payload
    # characters. Preserve a valid maximum-length ID exactly so a marker can
    # be used for a later explicit archive lookup.
    return sanitized[:164]


def image_marker(
    state: ImageMarkerState | str = ImageMarkerState.NOT_ANALYZED,
    *,
    attachment_id: str | None = None,
) -> str:
    """Build the stable, model-visible marker for one omitted image."""

    normalized = normalize_marker_state(state)
    safe_id = _safe_attachment_id(attachment_id)
    suffix = f"原图已保留：{safe_id}" if safe_id else "原图已保留"
    if normalized is ImageMarkerState.ANALYSIS_FAILED:
        return f"[图片分析失败：本回合无法读取原图；{suffix}]"
    if normalized is ImageMarkerState.NOT_REREAD:
        return f"[历史图片本回合未重新读取；可参考先前回合文字分析；{suffix}]"
    if normalized is ImageMarkerState.UNAVAILABLE:
        if safe_id:
            return f"[历史图片不可用：{safe_id}；如需重新分析请重新上传]"
        return "[历史图片不可用；如需重新分析请重新上传]"
    if normalized is ImageMarkerState.NOT_SENT:
        return f"[图片本回合未发送；{suffix}]"
    return f"[图片未分析：当前模型不支持图片输入；{suffix}]"


# Explicit aliases make call sites read naturally and keep the helper easy to
# discover without committing callers to one spelling.
build_image_marker = image_marker
marker_for_image = image_marker


@dataclass(frozen=True, slots=True)
class ImageProjectionPolicy:
    """Immutable policy used to project one exact physical request."""

    mode: ImageProjectionMode | str = ImageProjectionMode.NATIVE
    marker_state: ImageMarkerState | str = ImageMarkerState.NOT_ANALYZED
    attachment_ids: tuple[str, ...] = ()
    marker_states: Mapping[str, ImageMarkerState | str] = field(default_factory=dict)
    surrogate_by_attachment_id: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            mode = ImageProjectionMode(str(self.mode).strip().lower())
        except ValueError:
            mode = ImageProjectionMode.NATIVE
        raw_ids = self.attachment_ids or ()
        ids = tuple(
            item.strip()
            for item in raw_ids
            if isinstance(item, str) and item.strip()
        )
        states = {
            str(key): normalize_marker_state(value)
            for key, value in dict(self.marker_states or {}).items()
        }
        surrogates = {
            str(key): str(value)
            for key, value in dict(self.surrogate_by_attachment_id or {}).items()
            if str(value)
        }
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "marker_state", normalize_marker_state(self.marker_state))
        object.__setattr__(self, "attachment_ids", ids)
        object.__setattr__(self, "marker_states", states)
        object.__setattr__(self, "surrogate_by_attachment_id", surrogates)

    @classmethod
    def for_support(
        cls,
        support: VisionSupportValue | VisionSupportEvidence,
        *,
        marker_state: ImageMarkerState | str = ImageMarkerState.NOT_ANALYZED,
        **kwargs: Any,
    ) -> ImageProjectionPolicy:
        evidence = resolve_vision_support(support)
        mode = (
            ImageProjectionMode.MARKER
            if evidence.status == "unsupported"
            else ImageProjectionMode.NATIVE
        )
        return cls(mode=mode, marker_state=marker_state, **kwargs)


def projection_mode_for_support(
    support: VisionSupportValue | VisionSupportEvidence,
    *,
    force_text_only: bool = False,
) -> ImageProjectionMode:
    """Resolve the request projection without selecting another model.

    Unknown capability deliberately stays native: the provider gets one
    chance to prove whether this exact deployment accepts the image.  The
    caller may pass ``force_text_only=True`` for Ensemble's outer contract.
    """

    if force_text_only:
        return ImageProjectionMode.MARKER
    evidence = resolve_vision_support(support)
    return (
        ImageProjectionMode.MARKER
        if evidence.status == "unsupported"
        else ImageProjectionMode.NATIVE
    )


@dataclass(frozen=True, slots=True)
class ImageProjectionDecision:
    """Per-image accounting emitted by :func:`project_messages`."""

    ordinal: int
    attachment_id: str | None
    mode: ImageProjectionMode
    marker_state: ImageMarkerState | None = None
    marker: str | None = None


@dataclass(frozen=True, slots=True)
class MediaProjectionResult:
    """Projected messages and sanitized image accounting."""

    messages: list[Message]
    mode: ImageProjectionMode
    input_image_count: int
    output_image_count: int
    marker_count: int
    decisions: tuple[ImageProjectionDecision, ...] = ()

    @property
    def projected_messages(self) -> list[Message]:
        """Compatibility/readability alias for callers using that spelling."""

        return self.messages

    @property
    def changed(self) -> bool:
        return self.mode is not ImageProjectionMode.NATIVE and self.marker_count > 0

    @property
    def text_only(self) -> bool:
        return self.output_image_count == 0


ImageProjectionResult = MediaProjectionResult


# ---------------------------------------------------------------------------
# Recursive image accounting and transformation
# ---------------------------------------------------------------------------


def _is_image_mapping(value: Mapping[str, Any]) -> bool:
    raw_type = value.get("type")
    return isinstance(raw_type, str) and raw_type.strip().lower() == "image"


def _count_nested_images(value: object, *, include_mapping_blocks: bool = True) -> int:
    if isinstance(value, ContentBlockImage):
        return 1
    if isinstance(value, ContentBlockToolResult):
        return _count_nested_images(value.content, include_mapping_blocks=True)
    if isinstance(value, Message):
        return _count_nested_images(value.content, include_mapping_blocks=True)
    if include_mapping_blocks and isinstance(value, Mapping):
        if _is_image_mapping(value):
            return 1
        if str(value.get("type", "")).strip().lower() == "tool_result":
            return _count_nested_images(value.get("content"), include_mapping_blocks=True)
        return 0
    if isinstance(value, (list, tuple)):
        return sum(_count_nested_images(item, include_mapping_blocks=True) for item in value)
    return 0


def count_image_blocks(messages: Sequence[object]) -> int:
    """Count image blocks at any depth in message/tool-result content.

    Tool-use arguments are intentionally not traversed.  An application JSON
    object with ``{"type": "image"}`` is not a provider content block unless
    it is inside a message or tool-result content list.
    """

    return sum(_count_nested_images(message) for message in messages)


def has_image_blocks(messages: Sequence[object]) -> bool:
    return count_image_blocks(messages) > 0


def _bound_image_attachment_ids(value: object) -> set[str]:
    if isinstance(value, ContentBlockImage):
        return {value.attachment_id} if value.attachment_id else set()
    if isinstance(value, ContentBlockToolResult):
        return _bound_image_attachment_ids(value.content)
    if isinstance(value, Message):
        return _bound_image_attachment_ids(value.content)
    if isinstance(value, (list, tuple)):
        result: set[str] = set()
        for item in value:
            result.update(_bound_image_attachment_ids(item))
        return result
    if isinstance(value, Mapping):
        if _is_image_mapping(value):
            attachment_id = value.get("attachment_id")
            return (
                {attachment_id.strip()[:164]}
                if isinstance(attachment_id, str) and attachment_id.strip()
                else set()
            )
        if str(value.get("type", "")).strip().lower() == "tool_result":
            return _bound_image_attachment_ids(value.get("content"))
    return set()


def bind_image_attachment_ids(
    messages: Sequence[Message],
    attachment_ids: Sequence[str],
) -> list[Message]:
    """Return a deep copy with IDs bound to otherwise-unbound typed images.

    This is used for the current upload envelope after persistence assigned its
    canonical occurrence IDs.  The field is internal/excluded from provider
    serialization; it exists only to keep marker decisions correct when a
    request also contains historical or nested tool-result images.
    """

    ids = [value.strip()[:164] for value in attachment_ids if value.strip()]
    next_id = 0

    def visit(value: object) -> object:
        nonlocal next_id
        if isinstance(value, ContentBlockImage):
            attachment_id = value.attachment_id
            if not attachment_id and next_id < len(ids):
                attachment_id = ids[next_id]
                next_id += 1
            return value.model_copy(
                deep=True,
                update={"attachment_id": attachment_id},
            )
        if isinstance(value, ContentBlockToolResult):
            return value.model_copy(deep=True, update={"content": visit(value.content)})
        if isinstance(value, Message):
            return value.model_copy(deep=True, update={"content": visit(value.content)})
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, tuple):
            return tuple(visit(item) for item in value)
        return copy.deepcopy(value)

    return [cast(Message, visit(message)) for message in messages]


@dataclass
class _ProjectionContext:
    policy: ImageProjectionPolicy
    next_ordinal: int = 0
    next_fallback_id: int = 0
    reserved_attachment_ids: set[str] = field(default_factory=set)
    decisions: list[ImageProjectionDecision] = field(default_factory=list)

    def occurrence(
        self,
        explicit_attachment_id: object = None,
    ) -> tuple[int, str | None, ImageMarkerState]:
        ordinal = self.next_ordinal
        self.next_ordinal += 1
        attachment_id = (
            str(explicit_attachment_id).strip()[:164]
            if isinstance(explicit_attachment_id, str)
            and explicit_attachment_id.strip()
            else None
        )
        if attachment_id is None:
            while self.next_fallback_id < len(self.policy.attachment_ids):
                candidate = self.policy.attachment_ids[self.next_fallback_id]
                self.next_fallback_id += 1
                if candidate not in self.reserved_attachment_ids:
                    attachment_id = candidate
                    break
        state = self.policy.marker_state
        if attachment_id is not None:
            state = self.policy.marker_states.get(attachment_id, state)
        return ordinal, attachment_id, normalize_marker_state(state)


def _project_content_value(value: object, context: _ProjectionContext) -> tuple[object, bool]:
    """Project a content value while preserving non-content tool arguments."""

    if isinstance(value, ContentBlockImage):
        ordinal, attachment_id, state = context.occurrence(value.attachment_id)
        mode = cast(ImageProjectionMode, context.policy.mode)
        if mode is ImageProjectionMode.NATIVE:
            cloned = value.model_copy(deep=True)
            context.decisions.append(ImageProjectionDecision(ordinal, attachment_id, mode))
            return cloned, False

        if mode is ImageProjectionMode.SURROGATE and attachment_id:
            surrogate = context.policy.surrogate_by_attachment_id.get(attachment_id)
            if surrogate:
                marker_text = (
                    f"[图片派生描述（{_safe_attachment_id(attachment_id)}）：{surrogate}]"
                )
            else:
                marker_text = image_marker(state, attachment_id=attachment_id)
        else:
            marker_text = image_marker(state, attachment_id=attachment_id)
        context.decisions.append(
            ImageProjectionDecision(
                ordinal,
                attachment_id,
                mode,
                marker_state=state,
                marker=marker_text,
            )
        )
        return ContentBlockText(text=marker_text), True

    if isinstance(value, ContentBlockToolResult):
        projected_content, changed = _project_content_value(value.content, context)
        # Always deep-copy a tool result, even when it contains no image, so
        # callers can safely mutate the returned request without touching the
        # canonical object graph.
        if changed:
            projected = value.model_copy(deep=True, update={"content": projected_content})
        else:
            projected = value.model_copy(deep=True)
        return projected, changed

    if isinstance(value, Message):
        if isinstance(value.content, (list, tuple)):
            projected_content, changed = _project_content_value(value.content, context)
            if changed:
                return value.model_copy(deep=True, update={"content": projected_content}), True
        return value.model_copy(deep=True), False

    if isinstance(value, list):
        projected_items: list[object] = []
        changed = False
        for item in value:
            projected_item, item_changed = _project_content_value(item, context)
            projected_items.append(projected_item)
            changed = changed or item_changed
        return projected_items, changed

    if isinstance(value, tuple):
        projected_tuple_items: list[object] = []
        changed = False
        for item in value:
            projected_item, item_changed = _project_content_value(item, context)
            projected_tuple_items.append(projected_item)
            changed = changed or item_changed
        return tuple(projected_tuple_items), changed

    if isinstance(value, Mapping):
        if _is_image_mapping(value):
            ordinal, attachment_id, state = context.occurrence(
                value.get("attachment_id")
            )
            mode = cast(ImageProjectionMode, context.policy.mode)
            surrogate = (
                context.policy.surrogate_by_attachment_id.get(attachment_id or "")
                if mode is ImageProjectionMode.SURROGATE
                else None
            )
            marker_text = (
                f"[图片派生描述（{_safe_attachment_id(attachment_id)}）：{surrogate}]"
                if surrogate and attachment_id
                else image_marker(state, attachment_id=attachment_id)
            )
            context.decisions.append(
                ImageProjectionDecision(
                    ordinal,
                    attachment_id,
                    mode,
                    marker_state=state,
                    marker=marker_text,
                )
            )
            if mode is ImageProjectionMode.NATIVE:
                cloned_mapping = copy.deepcopy(dict(value))
                # Mapping-shaped compatibility blocks cannot express a
                # Pydantic excluded field, so remove request-local provenance
                # explicitly before the native provider boundary.
                cloned_mapping.pop("attachment_id", None)
                return cloned_mapping, False
            # Keep dictionary-shaped content dictionary-shaped.  Adapters that
            # accept untyped tool-result blocks can serialize this naturally.
            return {"type": "text", "text": marker_text}, True
        if str(value.get("type", "")).strip().lower() == "tool_result":
            projected_content, changed = _project_content_value(value.get("content"), context)
            projected_mapping = copy.deepcopy(dict(value))
            if changed:
                projected_mapping["content"] = projected_content
            return projected_mapping, changed
        # Do not inspect arbitrary dictionaries (especially tool-use input).
        return copy.deepcopy(value), False

    # A content list can contain provider-compatible custom block objects.  A
    # deep copy keeps their identity/value intact without making assumptions.
    return copy.deepcopy(value), False


def project_messages(
    messages: Sequence[Message],
    mode: ImageProjectionMode | str | None = None,
    *,
    policy: ImageProjectionPolicy | None = None,
    vision_support: VisionSupportValue | VisionSupportEvidence | None = None,
    marker_state: ImageMarkerState | str = ImageMarkerState.NOT_ANALYZED,
    attachment_ids: Sequence[str] = (),
    marker_states: Mapping[str, ImageMarkerState | str] | None = None,
    surrogate_by_attachment_id: Mapping[str, str] | None = None,
    force_text_only: bool = False,
) -> MediaProjectionResult:
    """Deep-copy ``messages`` and project image blocks for one provider call.

    ``mode`` takes precedence over ``vision_support``.  When neither is
    supplied, native projection is used.  ``unknown`` capability also uses a
    native request so the provider can be probed once; callers can retry with
    ``mode="marker"`` after a precise unsupported-image error.
    """

    if policy is None:
        if mode is None:
            selected_mode = projection_mode_for_support(
                "unknown" if vision_support is None else vision_support,
                force_text_only=force_text_only,
            )
        else:
            try:
                selected_mode = ImageProjectionMode(str(mode).strip().lower())
            except ValueError:
                selected_mode = ImageProjectionMode.NATIVE
        policy = ImageProjectionPolicy(
            mode=selected_mode,
            marker_state=marker_state,
            attachment_ids=tuple(attachment_ids),
            marker_states=marker_states or {},
            surrogate_by_attachment_id=surrogate_by_attachment_id or {},
        )
    elif force_text_only and policy.mode is not ImageProjectionMode.MARKER:
        policy = ImageProjectionPolicy(
            mode=ImageProjectionMode.MARKER,
            marker_state=policy.marker_state,
            attachment_ids=policy.attachment_ids,
            marker_states=policy.marker_states,
            surrogate_by_attachment_id=policy.surrogate_by_attachment_id,
        )

    input_count = count_image_blocks(messages)
    reserved_attachment_ids: set[str] = set()
    for message in messages:
        reserved_attachment_ids.update(_bound_image_attachment_ids(message))
    context = _ProjectionContext(
        policy=policy,
        reserved_attachment_ids=reserved_attachment_ids,
    )
    projected_messages: list[Message] = []
    for message in messages:
        projected, _ = _project_content_value(message, context)
        # The public type is Message, but preserving a malformed custom value
        # is safer than silently dropping it.  Normal callers always pass
        # Message instances.
        projected_messages.append(cast(Message, projected))

    marker_count = sum(
        1
        for decision in context.decisions
        if decision.mode is not ImageProjectionMode.NATIVE
        and decision.marker is not None
    )
    output_count = count_image_blocks(projected_messages)
    return MediaProjectionResult(
        messages=projected_messages,
        mode=cast(ImageProjectionMode, policy.mode),
        input_image_count=input_count,
        output_image_count=output_count,
        marker_count=marker_count,
        decisions=tuple(context.decisions),
    )


def project_messages_for_model(
    messages: Sequence[Message],
    *,
    vision_support: VisionSupportValue | VisionSupportEvidence = "unknown",
    **kwargs: Any,
) -> MediaProjectionResult:
    """Named façade for the common exact-deployment projection call."""

    return project_messages(messages, vision_support=vision_support, **kwargs)


def project_image_messages(
    messages: Sequence[Message],
    mode: ImageProjectionMode | str | None = None,
    **kwargs: Any,
) -> list[Message]:
    """Compatibility façade returning only the projected message list."""

    return project_messages(messages, mode, **kwargs).messages


def assert_text_only_messages(messages: Sequence[object]) -> None:
    """Raise if a supposedly text-only physical request still has an image."""

    count = count_image_blocks(messages)
    if count:
        raise ValueError(f"text-only provider request contains {count} image block(s)")


# ---------------------------------------------------------------------------
# Image-specific provider failure classification
# ---------------------------------------------------------------------------


class ImageFailureKind(StrEnum):
    """Failure classes relevant to image projection/retry decisions."""

    UNSUPPORTED_INPUT = "unsupported_input"
    # Readable aliases for callers that use provider terminology.
    IMAGE_UNSUPPORTED = "unsupported_input"
    INVALID_MEDIA = "invalid_media"
    CONTEXT_OVERFLOW = "context_overflow"
    AUTHENTICATION = "authentication"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    MODEL_NOT_FOUND = "model_not_found"
    POLICY_REFUSAL = "policy_refusal"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ImageFailureClassification:
    """Image error class plus the safe retry/cache decision."""

    kind: ImageFailureKind
    caches_unsupported: bool = False
    retry_without_image: bool = False
    reason: str = ""

    @property
    def is_unsupported(self) -> bool:
        return self.kind is ImageFailureKind.UNSUPPORTED_INPUT


def _error_fields(error: object) -> tuple[int | None, str, str]:
    if isinstance(error, Mapping):
        status = error.get("status_code", error.get("status"))
        code = error.get("code", error.get("error_code", ""))
        message = error.get("message", error.get("error", ""))
    else:
        status = getattr(error, "status_code", None)
        code = getattr(error, "code", getattr(error, "error_code", ""))
        message = getattr(error, "message", "")
        if not message:
            message = str(error or "")
    raw_code = str(code or "").strip().lower()
    try:
        status_code = int(status) if status is not None and str(status).strip() else None
    except (TypeError, ValueError):
        status_code = None
    # Provider adapters commonly normalize HTTP failures into ``ErrorEvent``
    # and keep the status only in its string ``code`` field.  Preserve that
    # stronger signal so an incidental "image unsupported" phrase in a
    # 401/429/5xx body cannot poison the exact deployment's vision cache.
    if status_code is None and raw_code.isascii() and raw_code.isdigit():
        candidate = int(raw_code)
        if 100 <= candidate <= 599:
            status_code = candidate
    return status_code, raw_code, str(message or "").strip().lower()


_IMAGE_UNSUPPORTED_CODES = frozenset(
    {
        "image_input_unsupported",
        "image_not_supported",
        "images_not_supported",
        "vision_not_supported",
        "multimodal_not_supported",
        "unsupported_image",
        "unsupported_images",
        "ensemble_multimodal_unsupported",
    }
)
_IMAGE_UNSUPPORTED_RE = re.compile(
    r"(?:image|images|vision|multimodal|picture|图片|图像|视觉).{0,80}"
    r"(?:not supported|unsupported|does not support|cannot process|"
    r"unable to (?:process|handle|accept|analy[sz]e|read)|不支持|无法处理|不能处理)"
    r"|(?:does not support|unsupported|not supported|不支持|无法处理).{0,80}"
    r"(?:image|images|vision|multimodal|picture|图片|图像|视觉)",
)
_IMAGE_ENDPOINT_UNAVAILABLE_RE = re.compile(
    r"\bno endpoints found that support image inputs?\b",
)
_INVALID_MEDIA_RE = re.compile(
    r"(?:image|images|picture|图片|图像).{0,80}"
    r"(?:invalid|corrupt|malformed|decode|format|mime|media type|too large|"
    r"download|fetch|load|inaccessible|尺寸|大小|损坏|格式)",
)

_INVALID_MEDIA_CODES = frozenset(
    {
        "invalid_image",
        "invalid_media",
        "image_too_large",
        "unsupported_media_type",
    }
)

_TRANSIENT_STATUS_CODES = frozenset(
    {408, 409, 425, 499, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
)


def classify_image_input_error(
    error: object,
    *,
    provider_name: str = "",
) -> ImageFailureKind:
    """Classify only precise image-related evidence.

    Generic ``unsupported`` text is not enough: the message must identify an
    image/vision input.  This prevents authentication, rate-limit, transport,
    and ordinary bad-request failures from poisoning a deployment's capability
    cache.
    """

    status_code, raw_code, message = _error_fields(error)
    joined = f"{raw_code} {message}".strip()

    # HTTP admission/transport status is stronger evidence than incidental
    # image wording in a gateway message.  In particular, a 401/429/503 must
    # never poison the exact deployment's vision-capability cache, even if the
    # body repeats an upstream "image unsupported" sentence.
    if status_code in {401, 403}:
        return ImageFailureKind.AUTHENTICATION
    if status_code == 402:
        return ImageFailureKind.INSUFFICIENT_CREDITS
    if status_code == 429:
        return ImageFailureKind.RATE_LIMITED
    if status_code in _TRANSIENT_STATUS_CODES:
        return ImageFailureKind.TRANSIENT

    # Keep this import lazy: failures.py imports the provider registry, while
    # this low-level module is also imported by provider package initialisation.
    try:
        from .failures import ProviderFailureKind, classify_provider_error

        provider_kind = classify_provider_error(provider_name, status_code, raw_code, message)
    except Exception:  # noqa: BLE001 - classification must never break recovery
        provider_kind = None

    if provider_kind is not None:
        # These provider-wide failures outrank all message-level image text.
        # BAD_REQUEST is intentionally handled later: real vision capability
        # rejections are commonly delivered as HTTP 400.
        if provider_kind is ProviderFailureKind.CONTEXT_OVERFLOW:
            return ImageFailureKind.CONTEXT_OVERFLOW
        if provider_kind is ProviderFailureKind.AUTH_INVALID:
            return ImageFailureKind.AUTHENTICATION
        if provider_kind is ProviderFailureKind.INSUFFICIENT_CREDITS:
            return ImageFailureKind.INSUFFICIENT_CREDITS
        if provider_kind is ProviderFailureKind.RATE_LIMITED:
            return ImageFailureKind.RATE_LIMITED
        if provider_kind is ProviderFailureKind.MODEL_NOT_FOUND:
            # An aggregator can use 404 for a valid model whose endpoints
            # cannot accept the requested modality. Only this precise image
            # admission response overrides the ordinary missing-model path.
            if status_code == 404 and _IMAGE_ENDPOINT_UNAVAILABLE_RE.search(message):
                return ImageFailureKind.UNSUPPORTED_INPUT
            return ImageFailureKind.MODEL_NOT_FOUND
        if provider_kind is ProviderFailureKind.POLICY_REFUSAL:
            return ImageFailureKind.POLICY_REFUSAL
        if provider_kind in {
            ProviderFailureKind.PROVIDER_OVERLOADED,
            ProviderFailureKind.TRANSPORT_TRANSIENT,
        }:
            return ImageFailureKind.TRANSIENT

    # Invalid/corrupt input is not evidence that the configured model lacks
    # multimodal capability.  Exact media codes therefore precede the
    # unsupported-input matcher.
    if raw_code in _INVALID_MEDIA_CODES:
        return ImageFailureKind.INVALID_MEDIA
    if raw_code in _IMAGE_UNSUPPORTED_CODES:
        return ImageFailureKind.UNSUPPORTED_INPUT
    if _INVALID_MEDIA_RE.search(joined):
        return ImageFailureKind.INVALID_MEDIA
    if _IMAGE_UNSUPPORTED_RE.search(joined):
        return ImageFailureKind.UNSUPPORTED_INPUT

    if provider_kind is not None:
        if provider_kind is ProviderFailureKind.BAD_REQUEST:
            return ImageFailureKind.BAD_REQUEST
    return ImageFailureKind.UNKNOWN


def classify_image_failure(
    error: object,
    *,
    provider_name: str = "",
) -> ImageFailureClassification:
    """Return image classification and the corresponding safe action."""

    kind = classify_image_input_error(error, provider_name=provider_name)
    if kind is ImageFailureKind.UNSUPPORTED_INPUT:
        return ImageFailureClassification(
            kind=kind,
            caches_unsupported=True,
            retry_without_image=True,
            reason="precise image-input capability rejection",
        )
    if kind is ImageFailureKind.INVALID_MEDIA:
        return ImageFailureClassification(
            kind=kind,
            reason="the image material is invalid or exceeds media limits",
        )
    if kind is ImageFailureKind.CONTEXT_OVERFLOW:
        return ImageFailureClassification(kind=kind, reason="rebuild after context compaction")
    return ImageFailureClassification(kind=kind)


# Alternate names used by orchestration code and tests.
classify_provider_image_error = classify_image_input_error
ImageInputFailureKind = ImageFailureKind


__all__ = [
    "VisionSupport",
    "VisionSupportValue",
    "VisionSupportSource",
    "VisionSupportStatus",
    "VisionSupportEvidence",
    "normalize_vision_support",
    "resolve_vision_support",
    "ImageProjectionMode",
    "ImageIntentKind",
    "ImageIntent",
    "ImageMarkerState",
    "normalize_marker_state",
    "image_marker",
    "build_image_marker",
    "marker_for_image",
    "ImageProjectionPolicy",
    "projection_mode_for_support",
    "ImageProjectionDecision",
    "MediaProjectionResult",
    "ImageProjectionResult",
    "count_image_blocks",
    "has_image_blocks",
    "bind_image_attachment_ids",
    "project_messages",
    "project_messages_for_model",
    "project_image_messages",
    "assert_text_only_messages",
    "ImageFailureKind",
    "ImageInputFailureKind",
    "ImageFailureClassification",
    "classify_image_input_error",
    "classify_provider_image_error",
    "classify_image_failure",
]
