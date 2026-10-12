"""Fail-closed replay policy for non-idempotent paid meta steps.

Paid generation entrypoints communicate only one positive fact to the parent:
the process failed before any provider submission was possible.  Every other
failure of a step declared as ``external_paid_submit`` is treated as possibly
accepted/billed.  The durable marker is intentionally stored with the step
error so reconnect recovery and a later gateway process enforce the same
decision as the live scheduler.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from opensquilla.skills.meta.templating import evaluate_when

EXTERNAL_PAID_SUBMIT = "external_paid_submit"
# Reserved process exit used only by audited bundled paid entrypoints after a
# failure that occurs before constructing/sending the provider request.
SAFE_NO_SUBMIT_EXIT_CODE = 78
_DURABLE_SAFE_MARKER = "[opensquilla-replay:safe-no-paid-submit]"
_DURABLE_UNSAFE_MARKER = "[opensquilla-replay:paid-submit-may-have-been-accepted]"
_LEGACY_PAID_SKILL_NAMES = frozenset({"nano-banana-pro", "seedance-2-prompt"})

# Parent-owned, in-memory scheduler output consumed only by trusted templates.
# A plan step may not claim this id. The serialized value contains no provider
# text: only bounded static step ids and one of the fixed dispositions below.
PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY = (
    "__opensquilla_paid_submission_dispositions_v1__"
)
# Separate parent-owned channel containing proof that a receipt was emitted by
# the exact bundled paid subprocess *during this scheduler run*.  The value is
# never seeded from persisted/workspace output and is stripped before public
# step output or persistence.  A receipt sidecar is therefore evidence only
# when its canonical digest matches the proof for its paid step.
PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY = (
    "__opensquilla_paid_submission_receipt_proofs_v1__"
)
PAID_SUBMISSION_SAFE_NO_SUBMIT = "safe_no_submit"
PAID_SUBMISSION_MAYBE_ACCEPTED = "maybe_accepted"
PAID_SUBMISSION_RECEIPT = "receipt"
_PAID_SUBMISSION_DISPOSITIONS = frozenset(
    {
        PAID_SUBMISSION_SAFE_NO_SUBMIT,
        PAID_SUBMISSION_MAYBE_ACCEPTED,
        PAID_SUBMISSION_RECEIPT,
    }
)
_MACHINE_STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_MACHINE_RECEIPT_PROOF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_MACHINE_DISPOSITION_STEPS = 64


class PaidReceiptProofText(str):
    """A normal step-output string carrying parent-private receipt proof.

    ``str`` compatibility keeps the executor/orchestrator protocol stable.
    The scheduler immediately extracts the proof and stores only a plain
    string in step outputs, so the attribute never reaches templates,
    persistence, logs, or public events.
    """

    _opensquilla_paid_receipt_proof: str

    def __new__(cls, value: str, receipt_proof: str) -> PaidReceiptProofText:
        instance = super().__new__(cls, value)
        instance._opensquilla_paid_receipt_proof = receipt_proof
        return instance


class PaidReceiptProofError(RuntimeError):
    """Executor failure carrying a current-run sanitized receipt digest."""

    _opensquilla_paid_receipt_proof: str

    def __init__(self, message: str, *, receipt_proof: str) -> None:
        super().__init__(message)
        self._opensquilla_paid_receipt_proof = receipt_proof


def encode_paid_replay_safety(error: str, *, safe_no_submit: bool) -> str:
    """Prefix a bounded executor error with its durable replay decision."""

    marker = _DURABLE_SAFE_MARKER if safe_no_submit else _DURABLE_UNSAFE_MARKER
    detail = str(error or "").strip()
    return f"{marker} {detail}" if detail else marker


def paid_replay_is_safe(error: object) -> bool:
    """Return true only for a persisted, explicit pre-submit proof."""

    text = str(error or "")
    # Unsafe wins if untrusted stderr happened to contain the safe marker.
    return _DURABLE_SAFE_MARKER in text and _DURABLE_UNSAFE_MARKER not in text


def paid_replay_may_duplicate(error: object) -> bool:
    """Return true for an explicit possibly-accepted paid submission."""

    return _DURABLE_UNSAFE_MARKER in str(error or "")


def encode_paid_submission_dispositions(
    dispositions: Mapping[str, object],
) -> str:
    """Serialize the bounded parent-owned paid-submission state.

    Values are selected by the scheduler from trusted executor outcomes. Raw
    child stderr/stdout, provider messages, request ids, and credentials are
    deliberately not accepted by this encoder.
    """

    sanitized: dict[str, str] = {}
    for step_id in sorted(dispositions):
        if len(sanitized) >= _MAX_MACHINE_DISPOSITION_STEPS:
            break
        disposition = dispositions[step_id]
        if (
            isinstance(step_id, str)
            and _MACHINE_STEP_ID_RE.fullmatch(step_id)
            and isinstance(disposition, str)
            and disposition in _PAID_SUBMISSION_DISPOSITIONS
        ):
            sanitized[step_id] = disposition
    return json.dumps(sanitized, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def encode_paid_submission_receipt_proofs(
    proofs: Mapping[str, object],
) -> str:
    """Serialize bounded, secret-free current-run receipt digests."""

    sanitized: dict[str, str] = {}
    for step_id in sorted(proofs):
        if len(sanitized) >= _MAX_MACHINE_DISPOSITION_STEPS:
            break
        proof = proofs[step_id]
        if (
            isinstance(step_id, str)
            and _MACHINE_STEP_ID_RE.fullmatch(step_id)
            and isinstance(proof, str)
            and _MACHINE_RECEIPT_PROOF_RE.fullmatch(proof)
        ):
            sanitized[step_id] = proof
    return json.dumps(sanitized, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def paid_receipt_proof(value: object) -> str | None:
    """Return a valid machine-attached proof without trusting string text."""

    proof = getattr(value, "_opensquilla_paid_receipt_proof", None)
    if isinstance(proof, str) and _MACHINE_RECEIPT_PROOF_RE.fullmatch(proof):
        return proof
    return None


def public_step_error(error: object) -> str:
    """Remove internal replay markers from user-visible failure text."""

    text = str(error or "")
    for marker in (_DURABLE_SAFE_MARKER, _DURABLE_UNSAFE_MARKER):
        text = text.replace(marker, "")
    return text.strip()


def is_external_paid_step(step: Any) -> bool:
    if getattr(step, "side_effect", "") == EXTERNAL_PAID_SUBMIT:
        return True
    # Version-1 saved plans predate ``side_effect``. Preserve safe upgrade
    # behavior for the two bundled non-idempotent media entrypoints instead of
    # silently allowing old failed runs to resubmit them.
    return (
        getattr(step, "kind", "") == "skill_exec"
        and getattr(step, "skill", "") in _LEGACY_PAID_SKILL_NAMES
    )


def paid_live_replay_block_reason(
    *,
    plan: Any,
    persisted_steps: Any,
    failed_step_id: str,
) -> str | None:
    """Reject a direct paid-step replay unless no submit is durably proven.

    A failed local fallback is not rejected here: replay reconstructs that
    failover edge and reruns the fallback while keeping its paid primary
    skipped. Missing/duplicate step records fail closed for paid steps.
    """

    plan_steps = tuple(getattr(plan, "steps", ()) or ())
    failed_step = next(
        (
            step
            for step in plan_steps
            if getattr(step, "id", None) == failed_step_id
        ),
        None,
    )
    if failed_step is None:
        if any(is_external_paid_step(step) for step in plan_steps):
            return "The failed step is not present in the saved paid-workflow plan."
        return None

    try:
        records = tuple(persisted_steps or ())
    except TypeError:
        records = ()
    records_by_id: dict[str, list[Any]] = {}
    for record in records:
        step_id = getattr(record, "step_id", None)
        if isinstance(step_id, str):
            records_by_id.setdefault(step_id, []).append(record)

    def complete_output(record: Any) -> str | None:
        truncated = getattr(record, "truncated_fields", ())
        if not isinstance(truncated, (tuple, list, set, frozenset)):
            return None
        if "output_text" in truncated:
            return None
        output = getattr(record, "output_text", None)
        return output if isinstance(output, str) else None

    trusted_outputs: dict[str, str] = {}
    for step in plan_steps:
        step_records = records_by_id.get(str(getattr(step, "id", "")), [])
        if len(step_records) != 1:
            continue
        record = step_records[0]
        if getattr(record, "status", None) == "ok":
            output = complete_output(record)
            if output is not None:
                trusted_outputs[step.id] = output
    for primary in plan_steps:
        substitute_id = str(getattr(primary, "on_failure", "") or "")
        if not substitute_id:
            continue
        primary_records = records_by_id.get(str(getattr(primary, "id", "")), [])
        substitute_records = records_by_id.get(substitute_id, [])
        if not (len(primary_records) == len(substitute_records) == 1):
            continue
        primary_record = primary_records[0]
        substitute_record = substitute_records[0]
        if (
            getattr(primary_record, "status", None) == "substituted"
            and getattr(primary_record, "substitute_step_id", None) == substitute_id
            and getattr(substitute_record, "status", None) == "ok"
        ):
            output = complete_output(substitute_record)
            if output is not None:
                trusted_outputs[primary.id] = output
                trusted_outputs[substitute_id] = output

    paid_owners = [
        step
        for step in plan_steps
        if getattr(step, "on_failure", "") == failed_step_id
        and is_external_paid_step(step)
    ]
    if paid_owners:
        # Replaying the local fallback is safe only when the persisted edge is
        # strong enough for the agent to seed/skip the paid primary. Anything
        # missing, duplicated, or mismatched would let normal DAG scheduling
        # reach the paid step again.
        if len(paid_owners) != 1:
            return (
                "This fallback cannot be replayed safely because its paid "
                "submission history is ambiguous. Start no new generation "
                "until provider history has been reviewed."
            )
        owner = paid_owners[0]
        owner_id = str(getattr(owner, "id", "") or "")
        owner_records = [
            record for record in records if getattr(record, "step_id", None) == owner_id
        ]
        if not (
            len(owner_records) == 1
            and getattr(owner_records[0], "status", None) == "substituted"
            and getattr(owner_records[0], "substitute_step_id", None) == failed_step_id
        ):
            return (
                "This fallback cannot be replayed safely because the paid "
                "primary cannot be proven skipped. Check provider history "
                "before explicitly starting another generation."
            )

    if is_external_paid_step(failed_step):
        matching = [
            record for record in records if getattr(record, "step_id", None) == failed_step_id
        ]
        direct_paid_target_is_safe = (
            len(matching) == 1
            and getattr(matching[0], "status", None) == "failed"
            and paid_replay_is_safe(getattr(matching[0], "error", None))
        )
        if not direct_paid_target_is_safe:
            return (
                "This paid generation may already have been accepted or billed. "
                "Check provider history before explicitly starting another generation."
            )

    # Every other paid execution must be provably reusable or provably skipped.
    # Otherwise the generic seed builder would silently rerun it.
    for step in plan_steps:
        if not is_external_paid_step(step) or step.id == failed_step_id:
            continue
        if any(owner.id == step.id for owner in paid_owners):
            # Exact substituted-edge validation above deliberately allows the
            # failed local fallback to run while this primary remains skipped.
            continue
        step_records = records_by_id.get(step.id, [])
        if len(step_records) > 1:
            return f"Paid step {step.id!r} has ambiguous duplicate replay evidence."
        if not step_records:
            if step.when:
                try:
                    if not evaluate_when(step.when, inputs={}, outputs=trusted_outputs):
                        continue
                except Exception:  # noqa: BLE001 - incomplete history is unsafe
                    pass
            return f"Paid step {step.id!r} cannot be proven reusable or skipped."
        record = step_records[0]
        status = getattr(record, "status", None)
        if status == "skipped":
            continue
        if step.id in trusted_outputs:
            continue
        return f"Paid step {step.id!r} has no complete reusable output."
    return None


def paid_fresh_run_block_reason(*, plan: Any, persisted_steps: Any) -> str | None:
    """Block one-click fresh-run rescue when any paid step lacks no-submit proof."""

    try:
        records = tuple(persisted_steps or ())
    except TypeError:
        records = ()
    for step in tuple(getattr(plan, "steps", ()) or ()):
        if not is_external_paid_step(step):
            continue
        matching = [
            record for record in records if getattr(record, "step_id", None) == step.id
        ]
        if (
            len(matching) == 1
            and getattr(matching[0], "status", None) == "failed"
            and paid_replay_is_safe(getattr(matching[0], "error", None))
        ):
            continue
        return (
            "A fresh retry could repeat a paid provider submission. Use a safe "
            "failed-step replay or review provider history first."
        )
    return None


__all__ = [
    "EXTERNAL_PAID_SUBMIT",
    "PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY",
    "PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY",
    "PAID_SUBMISSION_MAYBE_ACCEPTED",
    "PAID_SUBMISSION_RECEIPT",
    "PAID_SUBMISSION_SAFE_NO_SUBMIT",
    "PaidReceiptProofError",
    "PaidReceiptProofText",
    "SAFE_NO_SUBMIT_EXIT_CODE",
    "encode_paid_submission_dispositions",
    "encode_paid_submission_receipt_proofs",
    "encode_paid_replay_safety",
    "is_external_paid_step",
    "paid_receipt_proof",
    "paid_live_replay_block_reason",
    "paid_fresh_run_block_reason",
    "paid_replay_is_safe",
    "paid_replay_may_duplicate",
    "public_step_error",
]
