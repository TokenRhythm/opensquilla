"""Shared consumer budget for automatic and explicit context compaction.

Callers assemble their authoritative request envelope; this module owns the
capacity arithmetic, policy and admission identity. Provider proofs already
reserve generation/headroom, so neither caller deducts them a second time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from opensquilla.provider.request_proof import projected_generation_budget
from opensquilla.session.compaction_deployment import DEFAULT_COMPACTION_OUTPUT_TOKENS

CompactionProjector = Callable[[str, list[dict[str, Any]]], Any]


def named_auth_profile_fingerprint(profile_id: str | None) -> str:
    """Identify an exact named-profile key without accessing its credentials.

    Named-profile resolution matches trimmed keys case-insensitively. Bare
    keys stay bare; their bound provider is carried separately in the consumer
    identity, and qualified keys retain their provider prefix.
    """
    normalized = str(profile_id or "").strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16] if normalized else ""


def history_capacity_from_proof(proof: Mapping[str, Any]) -> tuple[int, int]:
    """Subtract the fixed request once, preserving genuinely exhausted capacity."""
    return (
        max(0, int(proof.get("effective_proof_token_budget", 0) or 0)
            - int(proof.get("estimated_tokens", 0) or 0)),
        max(0, int(proof.get("effective_proof_budget", 0) or 0)
            - int(proof.get("estimated_chars", 0) or 0)),
    )


@dataclass(frozen=True, slots=True)
class CompactionBudget:
    physical_context_window_tokens: int
    generation_reserve_tokens: int
    history_capacity_tokens: int
    history_capacity_chars: int
    auto_trigger_tokens: int
    auto_trigger_chars: int
    retained_tail_tokens: int
    retained_tail_messages: int
    summary_output_tokens: int
    provider_request_max_chars: int
    consumer_admission_fingerprint: str
    consumer_admission: Callable[[str, list[dict[str, Any]]], bool] = field(
        repr=False, compare=False,
    )


def resolve_compaction_budget(
    *,
    project: CompactionProjector,
    capacity_project: CompactionProjector | None = None,
    physical_context_window_tokens: int,
    generation_reserve_tokens: int,
    provider_identity: str = "",
    history_limit_tokens: int | None = None,
    envelope_reserve_tokens: int = 0,
    envelope_reserve_chars: int = 0,
    trigger_ratio: float = 0.85,
    retained_tail_messages: int = 0,
    summary_output_tokens: int = DEFAULT_COMPACTION_OUTPUT_TOKENS,
) -> CompactionBudget:
    """Freeze one final-request proof; trigger intent never changes this budget.

    Idle maintenance can reserve space for the unknown next request. A live turn
    instead supplies its actual prompt/media in ``project`` and needs no estimate.
    The same envelope and limits yield the same fingerprint in either entrypoint.
    """
    template = "[candidate checkpoint]"
    projection = project(template, [])
    proof = projection.proof if projection is not None else {}
    # Persisted current input is already part of the compactor's source/tail.
    # Its complete request still participates in identity and admission, but
    # capacity must reserve only the envelope absent from those source entries.
    capacity_projection = capacity_project(template, []) if capacity_project else projection
    capacity_proof = capacity_projection.proof if capacity_projection is not None else {}
    tokens, chars = history_capacity_from_proof(capacity_proof)
    tokens = max(0, tokens - max(0, envelope_reserve_tokens))
    chars = max(0, chars - max(0, envelope_reserve_chars))
    if history_limit_tokens is not None:
        tokens = min(tokens, max(0, history_limit_tokens))
        chars = min(chars, max(0, history_limit_tokens) * 4)
    payload = projection.payload if projection is not None else None

    def payload_hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    template_hash = payload_hash(payload)
    capacity_template_hash = payload_hash(
        capacity_projection.payload if capacity_projection is not None else None,
    )
    proof_limits = (
        proof.get("effective_proof_token_budget"), proof.get("effective_proof_budget"),
    )
    capacity_proof_limits = (
        capacity_proof.get("effective_proof_token_budget"),
        capacity_proof.get("effective_proof_budget"),
    )
    generation = (
        projected_generation_budget(payload, generation_reserve_tokens)
        if payload is not None else max(0, generation_reserve_tokens)
    )
    fingerprint = payload_hash({
        "schema": "compaction_consumer_v1", "provider": provider_identity,
        "physical_window": physical_context_window_tokens,
        "generation": generation, "template": template_hash,
        "capacity_template": capacity_template_hash,
        "history_tokens": tokens, "history_chars": chars,
        "reserve_tokens": envelope_reserve_tokens, "reserve_chars": envelope_reserve_chars,
        "proof_tokens": proof.get("effective_proof_token_budget"),
        "proof_chars": proof.get("effective_proof_budget"),
    })

    def admit(summary: str, kept: list[dict[str, Any]]) -> bool:
        from opensquilla.session.compaction import ConsumerAdmissionStaleError

        if projection is None or tokens <= 0 or chars <= 0:
            return False
        current = project(template, [])
        current_capacity = capacity_project(template, []) if capacity_project else current
        if (
            current is None or payload_hash(current.payload) != template_hash
            or (current.proof.get("effective_proof_token_budget"),
                current.proof.get("effective_proof_budget")) != proof_limits
            or current_capacity is None
            or payload_hash(current_capacity.payload) != capacity_template_hash
            or (current_capacity.proof.get("effective_proof_token_budget"),
                current_capacity.proof.get("effective_proof_budget")) != capacity_proof_limits
        ):
            raise ConsumerAdmissionStaleError("compaction consumer request changed")
        candidate = project(summary, kept)
        if candidate is None or not candidate.fits:
            return False
        candidate_proof = candidate.proof
        used_tokens = int(candidate_proof.get("estimated_tokens", 0) or 0)
        used_chars = int(candidate_proof.get("estimated_chars", 0) or 0)
        return bool(
            used_tokens + envelope_reserve_tokens
            <= int(candidate_proof.get("effective_proof_token_budget", 0) or 0)
            and used_chars + envelope_reserve_chars
            <= int(candidate_proof.get("effective_proof_budget", 0) or 0)
            and max(0, used_tokens - int(capacity_proof.get("estimated_tokens", 0) or 0)) <= tokens
            and max(0, used_chars - int(capacity_proof.get("estimated_chars", 0) or 0)) <= chars
        )

    ratio = min(1.0, max(0.0, trigger_ratio))
    return CompactionBudget(
        physical_context_window_tokens=physical_context_window_tokens,
        generation_reserve_tokens=generation,
        history_capacity_tokens=tokens, history_capacity_chars=chars,
        auto_trigger_tokens=int(tokens * ratio), auto_trigger_chars=int(chars * ratio),
        retained_tail_tokens=tokens // 5,
        retained_tail_messages=max(0, retained_tail_messages),
        summary_output_tokens=max(1, summary_output_tokens),
        provider_request_max_chars=int(proof.get("effective_proof_budget", 0) or 0),
        consumer_admission_fingerprint=fingerprint, consumer_admission=admit,
    )
