from __future__ import annotations

from copy import deepcopy

import pytest

from opensquilla.session.compaction_state import (
    CoverageCheckpoint,
    bind_coverage_manifest,
    build_coverage_obligations,
    build_structured_summary_from_text,
    coverage_with_manifest,
    render_structured_summary,
    verify_summary_coverage,
)
from opensquilla.session.context_view import (
    build_compaction_context_records,
    compaction_context_fingerprint,
)
from opensquilla.session.models import SessionContextState, SessionSummary


def checkpoint(result, text, *, owner="session", boundary=1, compaction_id="cmp_1"):
    payload, _ = build_structured_summary_from_text(
        text,
        result.obligations,
        exact_reference_manifest=result.manifest,
    )
    bound = bind_coverage_manifest(
        payload.model_dump(mode="json"),
        session_id=owner,
        covered_through_id=boundary,
        compaction_id=compaction_id,
    )
    return CoverageCheckpoint(
        text=render_structured_summary(bound),
        session_id=owner,
        covered_through_id=boundary,
        compaction_id=compaction_id,
        manifest=bound["source_coverage"]["exact_reference_manifest"],
    )


def status(result, text):
    return coverage_with_manifest(
        verify_summary_coverage(
            text,
            result.obligations,
            exact_reference_manifest=result.manifest,
        ),
        result.manifest,
    )


def test_missing_canonical_path_cannot_heal_after_repeated_checkpoints():
    first = build_coverage_obligations(
        [{"id": 1, "role": "user", "content": "Inspect apps/backend."}],
        session_id="session",
    )
    for generation in range(3):
        assert status(first, "The work continues.").status == "fail_reported"
        prior = checkpoint(first, "The work continues.", compaction_id=f"cmp_{generation}")
        first = build_coverage_obligations([], session_id="session", previous_checkpoints=(prior,))
        assert first.manifest["entries"][0]["origin"] == "canonical"
        assert first.manifest["entries"][0]["source_entry_id"] == 1


def test_checkpoint_prose_cannot_mint_new_exact_reference_but_canonical_can():
    initial = build_coverage_obligations([], session_id="session")
    prior = checkpoint(initial, "The probe rounds were environment/refresh-recovery checks only.")
    inherited = build_coverage_obligations([], session_id="session", previous_checkpoints=(prior,))
    assert inherited.manifest["entries"] == []
    canonical = build_coverage_obligations(
        [{"id": 2, "role": "user", "content": "Inspect environment/refresh-recovery."}],
        session_id="session",
        previous_checkpoints=(prior,),
    )
    assert status(canonical, "The checks are complete.").status == "fail_reported"


def test_legacy_checkpoint_references_remain_conservative_and_inherit_when_missing():
    legacy = CoverageCheckpoint(text="Inspect apps/backend.", session_id="session")
    first = build_coverage_obligations([], session_id="session", previous_checkpoints=(legacy,))
    assert first.manifest["entries"][0]["origin"] == "legacy_unclassified"
    assert status(first, "Inspect apps/backend.").status == "pass"
    second = build_coverage_obligations(
        [],
        session_id="session",
        previous_checkpoints=(checkpoint(first, "No path retained."),),
    )
    assert status(second, "No path retained.").status == "fail_reported"


@pytest.mark.parametrize(
    "path",
    [
        "C:/work/engine.py",
        "/srv/engine",
        "./apps/backend",
        "../apps/backend",
        "apps/backend",
        "`feature/login`",
        "CI/CD",
        "`@scope/package`",
        '"C:/Long  Folder/engine.py"',
    ],
)
def test_existing_canonical_path_candidates_are_not_excluded_by_prose_heuristics(path):
    result = build_coverage_obligations(
        [{"id": 2, "role": "user", "content": f"Inspect {path}."}],
        session_id="session",
    )
    assert result.manifest["entries"]
    assert status(result, "Finished.").status == "fail_reported"


@pytest.mark.parametrize(
    "damage",
    ["version", "owner", "boundary", "malformed", "oversize", "null", "unhashable"],
)
def test_invalid_manifest_cannot_report_complete_pass(damage):
    result = build_coverage_obligations(
        [{"id": 1, "role": "user", "content": "Inspect apps/backend."}],
        session_id="session",
    )
    prior = checkpoint(result, "Inspect apps/backend.")
    manifest = deepcopy(prior.manifest)
    if damage == "version":
        manifest["schema_version"] = 99
    elif damage == "owner":
        manifest["session_id"] = "other-session"
    elif damage == "boundary":
        manifest["covered_through_id"] = 2
    elif damage == "malformed":
        manifest["entries"].append({"kind": "file_path", "value": 42})
    elif damage == "oversize":
        manifest["entries"] *= 65
    elif damage == "unhashable":
        manifest["entries"][0]["kind"] = []
    else:
        manifest = None
    damaged = CoverageCheckpoint(
        text=prior.text,
        session_id="session",
        covered_through_id=1,
        compaction_id="cmp_1",
        manifest=manifest,
    )
    recovered = build_coverage_obligations(
        [], session_id="session", previous_checkpoints=(damaged,)
    )
    assert recovered.manifest["complete"] is False
    assert status(recovered, prior.text).status == "unknown"


def test_overflow_retains_old_missing_reference_and_never_claims_complete_pass():
    original = build_coverage_obligations(
        [{"id": 1, "role": "user", "content": "Inspect apps/old-missing."}],
        session_id="session",
    )
    prior = checkpoint(original, "No path retained.")
    result = build_coverage_obligations(
        [{"id": 2, "role": "user", "content": "\n".join(f"apps/file_{i}" for i in range(70))}],
        session_id="session",
        previous_checkpoints=(prior,),
    )
    assert len(result.manifest["entries"]) == 64
    assert result.manifest["entries"][0]["value"] == "apps/old-missing"
    assert result.manifest["complete"] is False
    assert (
        status(result, "\n".join(item["value"] for item in result.manifest["entries"])).status
        == "unknown"
    )
    repeated = build_coverage_obligations(
        [], session_id="session", previous_checkpoints=(checkpoint(
            result, "The probe rounds were newly/invented checks only.",
        ),),
    )
    assert repeated.manifest["complete"] is False
    assert repeated.manifest["entries"][0]["origin"] == "canonical"
    assert repeated.manifest["entries"] == result.manifest["entries"]


def test_model_supersession_cannot_retire_exact_refs_or_freeze_dynamic_state():
    original = build_coverage_obligations(
        [{"id": 1, "role": "user", "content": "Inspect apps/backend.\nNext: retry later."}],
        session_id="session",
    )
    prior = checkpoint(original, "apps/backend is obsolete; apps/frontend is current. Next: retry.")
    result = build_coverage_obligations(
        [{"id": 2, "role": "user", "content": "Use apps/replacement now. The error is resolved."}],
        session_id="session",
        previous_checkpoints=(prior,),
    )
    refs = {item["value"] for item in result.manifest["entries"]}
    assert refs == {"apps/backend", "apps/replacement"}
    assert all(
        item["kind"] in {"file_path", "artifact_path_or_name", "important_identifier"}
        for item in result.manifest["entries"]
    )


def test_manifest_is_host_only_and_explicit_fork_preserves_origin_without_mutating_parent():
    result = build_coverage_obligations(
        [{"id": 1, "role": "user", "content": "Inspect apps/backend."}],
        session_id="parent",
    )
    prior = checkpoint(result, "Inspect apps/backend.", owner="parent")
    payload, _ = build_structured_summary_from_text(
        prior.text,
        result.obligations,
        exact_reference_manifest=prior.manifest,
    )
    parent = payload.model_dump(mode="json")
    before = deepcopy(parent)
    child = bind_coverage_manifest(
        parent,
        session_id="child",
        source_session_id="parent",
        compaction_id="cmp_1",
        covered_through_id=1,
    )
    assert parent == before
    manifest = child["source_coverage"]["exact_reference_manifest"]
    assert manifest["session_id"] == "child"
    assert manifest["entries"][0]["source_session_id"] == "parent"
    assert manifest["complete"] is True
    assert "exact_reference_manifest" not in render_structured_summary(child)
    assert "source_sha256" not in render_structured_summary(child)


def test_exact_inventory_cannot_starve_latest_goal_or_constraint_diagnostics():
    result = build_coverage_obligations(
        [
            {
                "id": 1,
                "role": "user",
                "content": (
                    "Goal: finish the current migration.\nConstraint: keep the original records.\n"
                    + "\n".join(f"apps/file_{i}" for i in range(64))
                ),
            }
        ],
        session_id="session",
    )
    all_paths = "\n".join(item["value"] for item in result.manifest["entries"])
    assert len(result.manifest["entries"]) == 64
    coverage = status(result, all_paths)
    assert coverage.status == "fail_reported"
    assert any("finish the current migration" in item for item in coverage.missing_obligations)
    assert any("keep the original records" in item for item in coverage.missing_obligations)
    assert coverage.checked_obligations >= 66


def test_unproven_canonical_prefix_cannot_report_complete_pass():
    result = build_coverage_obligations(
        [{"id": 1, "role": "user", "content": "Inspect apps/backend."}],
        session_id="session", source_complete=False,
    )
    assert result.manifest["complete"] is False
    assert status(result, "Inspect apps/backend.").status == "unknown"


@pytest.mark.parametrize("fallback", [False, True])
def test_selected_summary_manifest_changes_context_fingerprint_without_changing_prose(fallback):
    result = build_coverage_obligations(
        [{"id": 1, "role": "user", "content": "Inspect apps/backend."}],
        session_id="session",
    )
    prior = checkpoint(result, "Same prose.")
    payload, _ = build_structured_summary_from_text(
        "Same prose.",
        (),
        exact_reference_manifest=prior.manifest,
    )
    summary = SessionSummary(
        session_id="session",
        session_key="agent:main:coverage",
        compaction_id="cmp_1",
        summary_text=prior.text,
        summary_payload=payload.model_dump(mode="json"),
        summary_format="structured_v1",
        covered_through_id=1,
    )
    states = (
        [
            SessionContextState(
                session_id="session",
                session_key=summary.session_key,
                provider="portable",
                state_kind="structured_summary_v1",
                covered_through_id=1,
                payload={"compaction_id": "cmp_1"},
                portable=True,
                cacheable=True,
            )
        ]
        if fallback
        else []
    )
    before = compaction_context_fingerprint(context_states=states, summaries=[summary])
    changed = summary.model_copy(deep=True)
    changed.summary_payload["source_coverage"]["exact_reference_manifest"]["entries"] = []
    after = compaction_context_fingerprint(context_states=states, summaries=[changed])
    assert before != after
    records = build_compaction_context_records(context_states=states, summaries=[changed])
    assert records[0].coverage_checkpoint.manifest["entries"] == []


def test_explicit_fork_cannot_heal_tampered_parent_checkpoint_binding():
    result = build_coverage_obligations([], session_id="parent")
    prior = checkpoint(result, "Same prose.", owner="parent")
    payload, _ = build_structured_summary_from_text(
        "Same prose.",
        (),
        exact_reference_manifest=prior.manifest,
    )
    tampered = payload.model_dump(mode="json")
    tampered["source_coverage"]["exact_reference_manifest"]["covered_through_id"] = 999
    child = bind_coverage_manifest(
        tampered,
        session_id="child",
        source_session_id="parent",
        compaction_id="cmp_1",
        covered_through_id=1,
    )
    assert child["source_coverage"]["exact_reference_manifest"]["complete"] is False
