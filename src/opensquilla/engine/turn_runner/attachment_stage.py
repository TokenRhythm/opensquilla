"""Stage object for the attachment-message build + ``turn_input`` rebind.

Owns the per-turn slice between the compaction/history stage boundary
and the stream-consumer stage boundary: the
``_build_attachment_messages`` call and the immediately-following
``turn_input`` rebind. The harness invokes ``AttachmentStage.run`` once
per turn, AFTER ``CompactionAndHistoryStage`` and BEFORE the
``async for event in agent.run_turn(...)`` loop.
Side-effect contract: the stage is a pure transformation. The single
port executes synchronously and has no observable side effects on the
agent / runner / session. Validation failures (count cap, disallowed
media type, ref-without-media-root, invalid base64, oversize) raise
``ValueError`` from the port and propagate as-is to the outer terminal
handler in ``_run_turn``. Per-attachment soft failures (missing ref
bytes, PDF parse failure, text-family decode failure) are absorbed
inside the build call into ``[attachment unavailable: …]`` placeholder
text blocks; the stage never sees them.

``AttachmentStage`` does NOT call any ``TurnHook`` or
``CompactionHook``. The slice has no observability emission today; the
stage preserves that.

NEVER terminates. Always returns ``StageOutcome.success(...)``. The
``StageOutcome`` shape is preserved for forward-compatibility with a
future ``ErrorEvent`` early-yield branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from opensquilla.engine.turn_runner.outcome import StageOutcome

@runtime_checkable
class AttachmentMessageBuilderPort(Protocol):
    """Wraps ``TurnRunner._build_attachment_messages`` + media-root resolution.

    The adapter forwards verbatim and supplies the
    ``media_root`` argument from the runner. Returns
    ``list[Message] | None`` the historical return: ``None`` when
    ``attachments`` is empty/``None``, else a single-element
    ``list[Message]`` (one multimodal user message carrying every
    attachment block).

    Validation failures (count cap, disallowed media type, ref without
    media_root, invalid base64, oversize) raise ``ValueError`` — the
    stage does NOT catch. Per-attachment soft failures are absorbed
    inside the build function into placeholder text blocks.
    """

    def build(
        self,
        message: str,
        attachments: list[dict],
        *,
        workspace_dir: str | Path | None = None,
        session_id: str | None = None,
    ) -> list[Any] | None: ...

@dataclass(frozen=True)
class AttachmentStageInput:
    """Inputs the ``AttachmentStage`` needs at its boundary.

    - ``effective_runtime_message`` is the post-pipeline message string
      from ``PromptAssemblerStage``. It is passed as the
      first positional argument to ``_build_attachment_messages``.
    - ``attachments`` is the caller-provided attachment list (may be
      empty or ``None``). The stage normalizes ``None`` to ``[]`` to
      preserve the ``if not attachments: return None`` early exit.
    """

    effective_runtime_message: str
    attachments: list[dict] | None
    workspace_dir: str | Path | None = None
    session_id: str | None = None

@dataclass(frozen=True)
class AttachmentStageOutput:
    """The two pieces of state subsequent stages and the harness consume.

    - ``extra_messages``: the ``list[Message] | None`` envelope passed
      to ``agent.run_turn(..., extra_messages=extra_messages)``. ``None``
      when no attachments were supplied.
    - ``turn_input``: the post-rebind turn-input string. Equal to
      ``effective_runtime_message`` when ``extra_messages is None``,
      else ``""`` (the attachment envelope carries the prompt block
      instead).
    """

    extra_messages: list[Any] | None
    turn_input: str

class AttachmentStage:
    """Build the multimodal attachment envelope and rebind ``turn_input``.

    Stable boundary: runs ONCE per turn, after
    ``CompactionAndHistoryStage`` and before the
    ``async for event in agent.run_turn(...)`` loop. The single port
    executes synchronously; the stage body is two statements (build +
    rebind).

    Async ``run`` matches every prior stage so the harness call site
    ``await stage.run(inp)`` is uniform. The port itself is synchronous —
    the stage simply does not ``await`` it.

    Exception model: ``ValueError`` from
    ``AttachmentMessageBuilderPort.build`` propagates unchanged (the
    no surrounding try/except is needed). No try/except is
    introduced.
    """

    name = "attachment_stage"

    def __init__(self, *, builder: AttachmentMessageBuilderPort) -> None:
        self._builder = builder

    async def run(
        self,
        inp: AttachmentStageInput,
    ) -> StageOutcome[AttachmentStageOutput]:
        from opensquilla.engine.turn_runner.outcome import StageOutcome

        extra_messages = self._builder.build(
            inp.effective_runtime_message,
            inp.attachments or [],
            workspace_dir=inp.workspace_dir,
            session_id=inp.session_id,
        )
        turn_input = (
            inp.effective_runtime_message if extra_messages is None else ""
        )
        return StageOutcome.success(
            AttachmentStageOutput(
                extra_messages=extra_messages,
                turn_input=turn_input,
            )
        )
