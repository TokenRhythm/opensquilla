"""Server-constructed authority for one accepted PromptAnnotation turn."""

from __future__ import annotations

from dataclasses import dataclass

PROMPT_ANNOTATION_TOOL_NAMES = frozenset(
    {"document_apply", "document_inspect", "document_locate", "document_read"}
)
DOCUMENT_CONTEXT_TOOL_NAMES = frozenset({"document_patch", "document_read"})


@dataclass(frozen=True, slots=True)
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
    annotation_ids: tuple[str, ...]
    anchor_ids: tuple[str, ...]
    snapshots: tuple[dict[str, object], ...]
    artifact_format: str
    tool_names: frozenset[str]
    operation_class: str
    request_context_prompt: str


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
    "DOCUMENT_CONTEXT_TOOL_NAMES",
    "PROMPT_ANNOTATION_TOOL_NAMES",
]
