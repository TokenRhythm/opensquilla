"""Server-constructed authority for one accepted PromptAnnotation turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PROMPT_ANNOTATION_TOOL_NAMES = frozenset(
    {
        "document_browser_act",
        "document_browser_inspect",
        "document_browser_reload",
        "document_browser_screenshot",
        "document_apply",
        "document_finish",
        "document_inspect",
        "document_locate",
        "document_patch",
        "document_read",
    }
)
# Rolling-compatibility surface for protocol-v3 Electron shells.  It retains
# the original source-only writer path; candidate preview/browser lifecycle
# tools are deliberately withheld because v3 cannot bind or verify a draft.
PROMPT_ANNOTATION_SOURCE_TOOL_NAMES = frozenset(
    {
        "document_apply",
        "document_inspect",
        "document_locate",
        "document_patch",
        "document_read",
    }
)
DOCUMENT_CONTEXT_TOOL_NAMES = frozenset({"document_patch", "document_read"})
DOCUMENT_CONTEXT_WORKSPACE_MUTATOR_DENY = frozenset(
    {"apply_patch", "edit_file", "write_file"}
)


@dataclass(frozen=True, slots=True)
class BoundPromptAnnotationTarget:
    """One ordered, server-normalized annotation authority."""

    annotation_id: str
    anchor_id: str
    status: Literal["ready", "contextual"]
    reason: Literal["no_match", "ambiguous"] | None
    tag_name: str
    target_kind: str
    target_text: str | None


@dataclass(frozen=True, slots=True, init=False)
class BoundPromptAnnotationContext:
    """Durable multi-anchor authority for one accepted annotated turn.

    The context is reconstructed from sent database rows and the immutable
    transcript snapshot. Artifact tools revalidate the document head and every
    anchor before each read or write.
    """

    session_key: str
    session_id: str
    document_id: str
    revision_id: str
    targets: tuple[BoundPromptAnnotationTarget, ...]
    snapshots: tuple[dict[str, object], ...]
    artifact_format: str
    tool_names: frozenset[str]
    operation_class: str
    request_context_prompt: str

    def __init__(
        self,
        *,
        session_key: str,
        session_id: str,
        document_id: str,
        revision_id: str,
        snapshots: tuple[dict[str, object], ...],
        artifact_format: str,
        tool_names: frozenset[str],
        operation_class: str,
        request_context_prompt: str,
        targets: tuple[BoundPromptAnnotationTarget, ...] = (),
        annotation_ids: tuple[str, ...] = (),
        anchor_ids: tuple[str, ...] = (),
    ) -> None:
        """Accept legacy id tuples while making ordered targets authoritative."""

        if targets and (annotation_ids or anchor_ids):
            target_annotation_ids = tuple(item.annotation_id for item in targets)
            target_anchor_ids = tuple(item.anchor_id for item in targets)
            if annotation_ids != target_annotation_ids or anchor_ids != target_anchor_ids:
                raise ValueError("prompt annotation target compatibility ids do not match")
        elif not targets:
            if len(annotation_ids) != len(anchor_ids):
                raise ValueError("prompt annotation id and anchor counts must match")
            targets = tuple(
                BoundPromptAnnotationTarget(
                    annotation_id=annotation_id,
                    anchor_id=anchor_id,
                    status="ready",
                    reason=None,
                    tag_name="region",
                    target_kind="region",
                    target_text=None,
                )
                for annotation_id, anchor_id in zip(annotation_ids, anchor_ids, strict=True)
            )
        object.__setattr__(self, "session_key", session_key)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "snapshots", snapshots)
        object.__setattr__(self, "artifact_format", artifact_format)
        object.__setattr__(self, "tool_names", tool_names)
        object.__setattr__(self, "operation_class", operation_class)
        object.__setattr__(self, "request_context_prompt", request_context_prompt)

    @property
    def annotation_ids(self) -> tuple[str, ...]:
        """Compatibility projection for one release cycle."""

        return tuple(item.annotation_id for item in self.targets)

    @property
    def anchor_ids(self) -> tuple[str, ...]:
        """Compatibility projection for one release cycle."""

        return tuple(item.anchor_id for item in self.targets)


@dataclass(frozen=True, slots=True)
class BoundDocumentContext:
    """Server-validated authority for the current mutable document head.

    Unlike ``BoundPromptAnnotationContext``, this context carries no selected
    anchors and does not replace the ordinary agent tool set.  It only makes
    the current document readable and patchable for one accepted turn.
    """

    session_key: str
    session_id: str
    document_id: str
    revision_id: str
    artifact_format: str
    tool_names: frozenset[str]
    operation_class: str
    request_context_prompt: str

__all__ = [
    "BoundDocumentContext",
    "BoundPromptAnnotationContext",
    "BoundPromptAnnotationTarget",
    "DOCUMENT_CONTEXT_TOOL_NAMES",
    "DOCUMENT_CONTEXT_WORKSPACE_MUTATOR_DENY",
    "PROMPT_ANNOTATION_TOOL_NAMES",
    "PROMPT_ANNOTATION_SOURCE_TOOL_NAMES",
]
