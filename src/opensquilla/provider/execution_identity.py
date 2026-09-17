"""Pure projection of runtime-owned identity spans into physical request views.

Only explicitly marked spans can be rewritten. User text and canonical history
are never scanned for markers. Spans travel with their text blocks through
request compaction and image projection, without entering serialized history.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Literal

from opensquilla.provider.types import ChatConfig, ContentBlockText, ExecutionIdentity, Message


def with_execution_span[T: (Message, ContentBlockText)](
    value: T, span: tuple[int, int] | None, **updates: object,
) -> T:
    """Grant process-local provenance to a copied, runtime-owned text span.

    The private attribute cannot be populated by a provider/client JSON payload.
    """
    cloned = value.model_copy(update=updates)
    cloned._execution_identity_span = span
    return cloned


def render_execution_identity(identity: ExecutionIdentity) -> str:
    facts: dict[str, str] = {"kind": identity.kind}
    if identity.kind == "single_model":
        facts["provider"] = identity.provider or "unknown"
        facts["model"] = identity.model or "unknown"
    return "Current response execution: " + json.dumps(
        facts, ensure_ascii=False, separators=(",", ":")
    )


def execution_from_evidence(
    evidence: Mapping[str, object], *, request_identity: ExecutionIdentity | None = None,
) -> dict[str, str]:
    """Project deployment identifiers only; never expose trace text or credentials.

    A response model is reported evidence, not necessarily the requested model
    (proxies may restamp aliases). Fusion requires a started final execution,
    not merely an ensemble setting in the logical route plan.
    """
    result: dict[str, str] = {}
    for source, target in (("model", "reported_model"), ("provider", "provider")):
        value = evidence.get(source)
        if isinstance(value, str) and value:
            result[target] = value
    if request_identity is not None and request_identity.kind == "single_model":
        result["kind"] = request_identity.kind
        for name in ("provider", "model"):
            value = getattr(request_identity, name)
            if value:
                result[name] = value
    trace = evidence.get("ensemble_trace")
    final = trace.get("final_request") if isinstance(trace, Mapping) else None
    if isinstance(final, Mapping) and final.get("request_started") is True:
        role = final.get("role")
        if role in {"aggregator", "fixed_aggregator", "fixed_direct"}:
            result["kind"] = "single_model" if role == "fixed_direct" else "multi_model_fusion"
            execution = final.get("execution")
            if isinstance(execution, Mapping):
                for name in ("provider", "model"):
                    value = execution.get(name)
                    if isinstance(value, str) and value:
                        result[name] = value
    legs = evidence.get("execution_legs")
    if isinstance(legs, list) and legs and isinstance(legs[-1], Mapping):
        for name in ("provider", "model"):
            value = legs[-1].get(name)
            if isinstance(value, str) and value:
                result[name] = value
    return result


def with_execution_identity(message: Message, identity: ExecutionIdentity | None) -> Message:
    """Mark a new identity suffix on an agent-owned runtime context message."""
    if identity is None:
        return message
    if not isinstance(message.content, str):
        raise TypeError("runtime execution context must be text")
    prefix = message.content + "\n"
    text = prefix + render_execution_identity(identity)
    return with_execution_span(message, (len(prefix), len(text)), content=text)


def rebind_execution_identity(
    config: ChatConfig | None,
    *,
    provider: str | None = None,
    model: str | None = None,
    kind: Literal["single_model", "multi_model_fusion"] | None = None,
) -> ChatConfig | None:
    """Bind facts without changing a sibling request or opting legacy calls in."""
    if not isinstance(config, ChatConfig) or config.execution_identity is None:
        return config
    identity = config.execution_identity
    rebound = replace(
        identity,
        kind=kind if kind is not None else identity.kind,
        provider=provider if provider is not None else identity.provider,
        model=model if model is not None else identity.model,
    )
    return config.model_copy(update={"execution_identity": rebound})


def _replace_span(
    text: str, span: tuple[int, int], rendered: str,
) -> tuple[str, tuple[int, int]]:
    start, end = span
    if not 0 <= start <= end <= len(text):
        raise ValueError("invalid runtime execution identity span")
    return text[:start] + rendered + text[end:], (start, start + len(rendered))


def project_execution_identity(
    messages: list[Message], config: ChatConfig | None,
) -> list[Message]:
    """Idempotently render the exact bound facts before admission and dispatch."""
    identity = getattr(config, "execution_identity", None)
    if not isinstance(identity, ExecutionIdentity):
        return messages
    rendered = render_execution_identity(identity)
    projected: list[Message] = []
    changed = False
    for message in messages:
        if isinstance(message.content, str) and message.execution_identity_span is not None:
            content, span = _replace_span(
                message.content, message.execution_identity_span, rendered,
            )
            if content != message.content:
                changed = True
                message = with_execution_span(message, span, content=content)
            projected.append(message)
        elif isinstance(message.content, list):
            blocks = []
            block_changed = False
            for block in message.content:
                if (
                    isinstance(block, ContentBlockText)
                    and block.execution_identity_span is not None
                ):
                    text, span = _replace_span(block.text, block.execution_identity_span, rendered)
                    if text != block.text:
                        block_changed = True
                        block = with_execution_span(block, span, text=text)
                blocks.append(block)
            if block_changed:
                changed = True
                message = message.model_copy(update={"content": blocks})
            projected.append(message)
        else:
            projected.append(message)
    return projected if changed else messages
