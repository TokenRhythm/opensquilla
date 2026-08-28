"""Pre-router attachment materialization and post-router prompt rebind.

The harness invokes ``AttachmentStage.run`` once after provider/tool setup and
before prompt routing. Extracted text and typed media are then reused by
compaction and provider delivery. A pure post-router helper replaces only the
prompt block, without reading or parsing an attachment again.
The bounded worker may materialize already-ingested uploads into the configured
workspace, but the stage never discovers or reads arbitrary workspace paths.
Validation failures
(count cap, disallowed
media type, ref-without-media-root, invalid base64, oversize) raise
``ValueError`` from the port and propagate as-is to the outer terminal
handler in ``_run_turn``. Per-attachment soft failures (missing ref
bytes, PDF parse failure, text-family decode failure) are absorbed
inside the build call into ``[attachment unavailable: …]`` placeholder
text blocks; the stage records only their count.

``AttachmentStage`` does NOT call any ``TurnHook`` or
``CompactionHook``. The slice has no observability emission today; the
stage preserves that.

Successful preparation returns ``StageOutcome.success(...)``. Cancellation
propagates immediately; deadline and validation failures propagate to the
outer turn error boundary.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opensquilla.provider.request_proof import estimate_provider_media_tokens
from opensquilla.token_estimation import estimate_attachment_text_tokens

if TYPE_CHECKING:
    from opensquilla.engine.turn_runner.outcome import StageOutcome


_UNAVAILABLE_MARKER = "[attachment unavailable:"
_GENERATED_TEXT_ATTACHMENT_SOURCE = "input_normalization"
_ATTACHMENT_PREPARATION_TIMEOUT_SECONDS = 30.0
_ATTACHMENT_PREPARATION_WORKERS = 2
_ATTACHMENT_PREPARATION_ADMISSION_CAPACITY = 4


class _AttachmentPreparationCancelledError(Exception):
    """Cooperative worker stop requested after the awaiting turn was cancelled."""


class _AttachmentPreparationControl:
    def __init__(self, *, deadline_at_monotonic: float) -> None:
        self._cancelled = threading.Event()
        self._deadline_at_monotonic = deadline_at_monotonic

    def cancel(self) -> None:
        self._cancelled.set()

    def check(self) -> None:
        if self._cancelled.is_set():
            raise _AttachmentPreparationCancelledError
        if time.monotonic() >= self._deadline_at_monotonic:
            raise TimeoutError("attachment preparation deadline exceeded")


def _base64_decoded_size(value: str) -> int:
    encoded = value.strip()
    if not encoded:
        return 0
    padding = len(encoded) - len(encoded.rstrip("="))
    return max(0, (len(encoded) * 3) // 4 - padding)


@dataclass(frozen=True)
class AttachmentMaterializationStats:
    """Sanitized capacity and failure facts for one attachment batch."""

    attachment_count: int = 0
    estimated_tokens: int = 0
    generated_normalization_estimated_tokens: int = 0
    parse_failure_count: int = 0
    provider_visible_text_chars: int = 0
    image_count: int = 0


def _materialization_stats(
    extra_messages: list[Any] | None,
    *,
    attachments: list[dict[str, Any]],
    generated_normalization_attachment_count: int,
) -> AttachmentMaterializationStats:
    if not extra_messages:
        return AttachmentMaterializationStats()

    from opensquilla.provider.types import ContentBlockImage, ContentBlockText

    estimated_tokens = 0
    generated_normalization_estimated_tokens = 0
    parse_failure_count = 0
    provider_visible_text_chars = 0
    image_count = 0
    attachment_blocks: list[Any] = []
    for message in extra_messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        # The first block is the ordinary prompt. Routing already counts it.
        attachment_blocks.extend(content[1:])

    block_tokens_by_index: list[int] = []
    for block in attachment_blocks:
        block_tokens = 0
        if isinstance(block, ContentBlockText):
            provider_visible_text_chars += len(block.text)
            block_tokens = estimate_attachment_text_tokens(block.text)
            parse_failure_count += block.text.count(_UNAVAILABLE_MARKER)
        elif isinstance(block, ContentBlockImage):
            image_count += 1
            block_tokens = estimate_provider_media_tokens(
                "image",
                _base64_decoded_size(block.data),
            )
        estimated_tokens += block_tokens
        block_tokens_by_index.append(block_tokens)

    # Most attachments render one block. A materialized image renders its
    # image block plus a standalone workspace marker; advance across both so a
    # following generated text attachment is attributed to its own block.
    block_index = 0
    for attachment in attachments:
        if block_index >= len(attachment_blocks):
            break
        block = attachment_blocks[block_index]
        block_tokens = block_tokens_by_index[block_index]
        if (
            generated_normalization_attachment_count > 0
            and attachment.get("_generated_by")
            == _GENERATED_TEXT_ATTACHMENT_SOURCE
            and attachment.get("source") == _GENERATED_TEXT_ATTACHMENT_SOURCE
            and attachment.get("_provider_inline_policy") == "preview_only"
        ):
            generated_normalization_estimated_tokens += block_tokens
            generated_normalization_attachment_count -= 1
        block_index += 1
        if (
            isinstance(block, ContentBlockImage)
            and block_index < len(attachment_blocks)
            and isinstance(attachment_blocks[block_index], ContentBlockText)
            and attachment_blocks[block_index].text.startswith(
                ("[attachment available:", "[attachment unavailable:")
            )
        ):
            block_index += 1
    return AttachmentMaterializationStats(
        attachment_count=len(attachments),
        estimated_tokens=estimated_tokens,
        generated_normalization_estimated_tokens=(
            generated_normalization_estimated_tokens
        ),
        parse_failure_count=min(parse_failure_count, len(attachments)),
        provider_visible_text_chars=provider_visible_text_chars,
        image_count=image_count,
    )


def rebind_attachment_prompt(
    extra_messages: list[Any] | None,
    runtime_message: str,
) -> list[Any] | None:
    """Replace the cached envelope's prompt block without reparsing files."""

    if not extra_messages:
        return extra_messages
    from opensquilla.provider.types import ContentBlockText, Message

    first = extra_messages[0]
    content = getattr(first, "content", None)
    if not isinstance(first, Message) or not isinstance(content, list) or not content:
        return extra_messages
    if not isinstance(content[0], ContentBlockText):
        return extra_messages
    rebound_content = [ContentBlockText(text=runtime_message), *content[1:]]
    return [first.model_copy(update={"content": rebound_content}), *extra_messages[1:]]


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

    - ``effective_runtime_message`` is the normalized pre-pipeline message.
      It is passed as the first positional argument to the builder; a later
      pure rebind installs any post-pipeline prompt transformation.
    - ``attachments`` is the caller-provided attachment list (may be
      empty or ``None``). The stage normalizes ``None`` to ``[]`` to
      preserve the ``if not attachments: return None`` early exit.
    """

    effective_runtime_message: str
    attachments: list[dict] | None
    workspace_dir: str | Path | None = None
    session_id: str | None = None
    generated_normalization_attachment_count: int = 0
    timeout_seconds: float | None = None


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
    stats: AttachmentMaterializationStats


class AttachmentStage:
    """Build and measure the multimodal attachment envelope exactly once.

    Stable boundary: runs once per turn before the routing pipeline. The
    synchronous builder and material token scan run in a small bounded worker
    pool; downstream code only rebinds the already-built prompt envelope.

    Exception model: ``ValueError`` from the builder propagates unchanged.
    Cancellation signals the cooperative worker and re-raises; a deadline is
    normalized to one stable ``TimeoutError`` message.
    """

    name = "attachment_stage"

    def __init__(self, *, builder: AttachmentMessageBuilderPort) -> None:
        self._builder = builder
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_ATTACHMENT_PREPARATION_WORKERS,
            thread_name_prefix="opensquilla-attachment-preparation",
        )
        # ThreadPoolExecutor's internal queue is unbounded. Keep at most one
        # queued job per worker in addition to the active workers; later turns
        # wait here without submitting a closure to the executor.
        self._admission = asyncio.BoundedSemaphore(
            _ATTACHMENT_PREPARATION_ADMISSION_CAPACITY
        )

    async def run(
        self,
        inp: AttachmentStageInput,
    ) -> StageOutcome[AttachmentStageOutput]:
        from opensquilla.engine.turn_runner.outcome import StageOutcome

        attachments = inp.attachments or []
        if not attachments:
            extra_messages = self._builder.build(
                inp.effective_runtime_message,
                [],
                workspace_dir=inp.workspace_dir,
                session_id=inp.session_id,
            )
            return StageOutcome.success(
                AttachmentStageOutput(
                    extra_messages=extra_messages,
                    turn_input=(
                        inp.effective_runtime_message
                        if extra_messages is None
                        else ""
                    ),
                    stats=AttachmentMaterializationStats(),
                )
            )
        configured_timeout = inp.timeout_seconds
        timeout_seconds = _ATTACHMENT_PREPARATION_TIMEOUT_SECONDS
        if (
            isinstance(configured_timeout, int | float)
            and not isinstance(configured_timeout, bool)
            and configured_timeout > 0
        ):
            timeout_seconds = min(timeout_seconds, float(configured_timeout))
        deadline_at_monotonic = time.monotonic() + timeout_seconds
        control = _AttachmentPreparationControl(
            deadline_at_monotonic=deadline_at_monotonic
        )

        def _prepare() -> tuple[list[Any] | None, AttachmentMaterializationStats]:
            build_cancellable = getattr(self._builder, "build_cancellable", None)
            if callable(build_cancellable):
                extra = build_cancellable(
                    inp.effective_runtime_message,
                    attachments,
                    workspace_dir=inp.workspace_dir,
                    session_id=inp.session_id,
                    cancel_check=control.check,
                )
            else:
                extra = self._builder.build(
                    inp.effective_runtime_message,
                    attachments,
                    workspace_dir=inp.workspace_dir,
                    session_id=inp.session_id,
                )
            control.check()
            stats = _materialization_stats(
                extra,
                attachments=attachments,
                generated_normalization_attachment_count=max(
                    0, inp.generated_normalization_attachment_count
                ),
            )
            return extra, stats

        try:
            await asyncio.wait_for(
                self._admission.acquire(),
                timeout=max(0.0, deadline_at_monotonic - time.monotonic()),
            )
        except TimeoutError as exc:
            control.cancel()
            raise TimeoutError(
                f"attachment preparation timed out after {timeout_seconds:g}s"
            ) from exc

        remaining_seconds = deadline_at_monotonic - time.monotonic()
        if remaining_seconds <= 0:
            self._admission.release()
            control.cancel()
            raise TimeoutError(
                f"attachment preparation timed out after {timeout_seconds:g}s"
            )

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._executor, _prepare)
        except BaseException:
            self._admission.release()
            raise
        def _release_admission(done: asyncio.Future[Any]) -> None:
            self._admission.release()
            if not done.cancelled():
                done.exception()

        future.add_done_callback(_release_admission)
        try:
            extra_messages, stats = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=remaining_seconds,
            )
        except asyncio.CancelledError:
            control.cancel()
            raise
        except TimeoutError as exc:
            control.cancel()
            raise TimeoutError(
                f"attachment preparation timed out after {timeout_seconds:g}s"
            ) from exc
        turn_input = (
            inp.effective_runtime_message if extra_messages is None else ""
        )
        return StageOutcome.success(
            AttachmentStageOutput(
                extra_messages=extra_messages,
                turn_input=turn_input,
                stats=stats,
            )
        )
