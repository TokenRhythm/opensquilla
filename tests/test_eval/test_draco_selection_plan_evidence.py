from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import zlib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import opensquilla.eval.draco_selection_plan_evidence as evidence
from opensquilla.eval.draco_selection_plan_evidence import (
    DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
    SELECTION_PLAN_PACK_RECORD_SCHEMA,
    SELECTION_PLAN_REF_SCHEMA,
    SelectionPlanEvidenceError,
    SelectionPlanEvidenceLimitError,
    SelectionPlanPackAppender,
    SelectionPlanPackReader,
    canonical_selection_plan_json_bytes,
    compact_selection_plan_evidence_row,
    expand_selection_plan,
    materialize_selection_plan_row_view,
    parse_selection_plan_reference,
    selection_plan_evidence_manifest_binding,
    validate_compact_selection_plan_evidence_row,
    validate_selection_plan_evidence_manifest_binding,
)


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_line(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _plan(
    decision_id: str = "decision-1",
    *,
    request_marker: str = "request-1",
) -> dict[str, object]:
    registry = {
        "schema_version": "opensquilla.model-registry/v2",
        "snapshot_version": "snapshot-20260811",
        "models": [
            {
                "identity": f"openrouter:model-{index}",
                "description": "public routing fact " * 64,
                "score": index,
            }
            for index in range(20)
        ],
    }
    ranking = {
        "schema_version": "step2-ranking-config-v4",
        "config_version": "ranking-20260811",
        "weights": {"quality": 0.7, "cost": 0.2, "stability": 0.1},
    }
    request_context = {
        "schema_version": "request-context/v1",
        "marker": request_marker,
        "snapshot_hash": "f" * 64,
    }
    return {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "decision_id": decision_id,
        "ranking_version": "step2-ranking-v2",
        "ranking_config_schema_version": ranking["schema_version"],
        "ranking_config_version": ranking["config_version"],
        "ranking_config_hash": "a" * 64,
        "registry_snapshot_version": registry["snapshot_version"],
        "registry_snapshot_hash": "b" * 64,
        "request_context_hash": "c" * 64,
        "registry_snapshot": registry,
        "ranking_parameters": ranking,
        "request_context": request_context,
        "candidate_pool": [
            {"identity": f"openrouter:model-{index}", "score": index / 20}
            for index in range(20)
        ],
        "selected_P": ["openrouter:model-1", "openrouter:model-2"],
        "backup_P": ["openrouter:model-3"],
        "selected_A": "openrouter:model-4",
        "aggregator_candidates": ["openrouter:model-4", "openrouter:model-5"],
        "proposer_count": 2,
        "N_min": 2,
        "N_max": 3,
        "stop_reason": "coverage_reached",
        "analyzer_failure_fallback": False,
        "ranking_thinking_assignment_enabled": True,
    }


def _pack_lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


def _rewrite_record(
    path: Path,
    record_index: int,
    mutate,
) -> None:
    lines = _pack_lines(path)
    record = json.loads(lines[record_index])
    mutate(record)
    lines[record_index] = _canonical_line(record)
    path.write_bytes(b"".join(lines))


def test_root_leaf_dag_roundtrip_deduplicates_and_retains_only_offsets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    first = _plan()
    second = _plan("decision-2", request_marker="request-2")
    # Exercise cross-plan leaf sharing rather than merely equal-looking fixtures.
    second["registry_snapshot"] = first["registry_snapshot"]
    second["ranking_parameters"] = first["ranking_parameters"]

    with SelectionPlanPackAppender(path) as appender:
        first_ref = appender.store_selection_plan(first)
        second_ref = appender.store_selection_plan(second)
        assert appender.index.object_count == 6
        assert appender.expand_selection_plan(first_ref) == first
        assert appender.expand_selection_plan(second_ref) == second
        root_location = appender.index.location(first_ref["sha256"])
        assert [item.kind for item in root_location.dependencies] == [
            "registry_snapshot",
            "ranking_parameters",
            "request_context",
        ]
        assert all(
            not hasattr(location, "payload")
            for location in appender.index.objects.values()
        )

    inline_bytes = len(canonical_selection_plan_json_bytes(first)) + len(
        canonical_selection_plan_json_bytes(second)
    )
    assert path.stat().st_size < inline_bytes // 5
    assert len(canonical_selection_plan_json_bytes(first_ref)) < 32 * 1024

    with SelectionPlanPackReader(path) as reader:
        assert reader.expand_selection_plan(first_ref) == first
        assert reader.expand_selection_plan(second_ref) == second
        # Resolves are detached and the reader gains no decoded-object cache.
        assert reader.expand_selection_plan(first_ref) is not reader.expand_selection_plan(
            first_ref
        )
        assert not any("cache" in key or "payload" in key for key in vars(reader))
        reader.verify_snapshot()


def test_legacy_inline_plan_is_returned_by_identity_and_ref_requires_pack(
    tmp_path: Path,
) -> None:
    inline = _plan()
    assert expand_selection_plan(inline) is inline

    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        ref = appender.store_selection_plan(inline)
    with SelectionPlanPackReader(path) as reader:
        assert reader.expand_selection_plan(inline) is inline
        assert expand_selection_plan(ref, reader=reader) == inline
    with pytest.raises(SelectionPlanEvidenceError, match="requires its bound pack"):
        expand_selection_plan(ref)


def test_row_view_is_detached_and_expands_each_ref_once_per_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    plan = _plan()
    with SelectionPlanPackAppender(path) as appender:
        ref = appender.store_selection_plan(plan)
    compact = {
        "routing_trace": {"selection_plan": deepcopy(ref)},
        "ensemble_trace": {
            "calls": [
                {"selection_plan": deepcopy(ref)},
                {"selection_plan": deepcopy(ref)},
            ]
        },
    }

    with SelectionPlanPackReader(path) as reader:
        original_expand = reader.expand_selection_plan
        expanded_refs: list[object] = []

        def counted_expand(value: object) -> object:
            expanded_refs.append(value)
            return original_expand(value)

        monkeypatch.setattr(reader, "expand_selection_plan", counted_expand)
        view = materialize_selection_plan_row_view(
            compact,
            reader=reader,
            require_references=True,
        )

    assert len(expanded_refs) == 1
    assert compact["routing_trace"]["selection_plan"] == ref
    routing_plan = view["routing_trace"]["selection_plan"]
    call_plans = [
        call["selection_plan"] for call in view["ensemble_trace"]["calls"]
    ]
    assert routing_plan == plan
    assert call_plans == [plan, plan]
    assert routing_plan is not call_plans[0]
    call_plans[0]["decision_id"] = "detached-mutation"
    assert routing_plan["decision_id"] == "decision-1"


def test_row_view_preserves_old_inline_identity_and_rejects_compact_downgrade(
    tmp_path: Path,
) -> None:
    inline_row = {"routing_trace": {"selection_plan": _plan()}}
    assert (
        materialize_selection_plan_row_view(
            inline_row,
            reader=None,
            require_references=False,
        )
        is inline_row
    )

    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path):
        pass
    with SelectionPlanPackReader(path) as reader:
        with pytest.raises(SelectionPlanEvidenceError, match="inline plan"):
            materialize_selection_plan_row_view(
                inline_row,
                reader=reader,
                require_references=True,
            )


@pytest.mark.parametrize("inline_value", [[{"inline": True}], "inline", 7])
def test_compact_row_rejects_non_mapping_inline_selection_plan_values(
    tmp_path: Path,
    inline_value: object,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path):
        pass
    row = {"routing_trace": {"selection_plan": inline_value}}

    with SelectionPlanPackReader(path) as reader:
        with pytest.raises(SelectionPlanEvidenceError, match="inline plan"):
            materialize_selection_plan_row_view(
                row,
                reader=reader,
                require_references=True,
            )


def test_reopened_appender_is_content_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "selection-plans.jsonl"
    first = _plan()
    with SelectionPlanPackAppender(path) as appender:
        first_ref = appender.store_selection_plan(first)
        first_size = appender.index.pack_bytes
        first_count = appender.index.object_count

    with SelectionPlanPackAppender(path, create=False) as appender:
        assert appender.store_selection_plan(first) == first_ref
        assert appender.index.pack_bytes == first_size
        assert appender.index.object_count == first_count
        second = _plan("decision-2", request_marker="request-2")
        second["registry_snapshot"] = first["registry_snapshot"]
        second["ranking_parameters"] = first["ranking_parameters"]
        appender.store_selection_plan(second)
        assert appender.index.object_count == first_count + 2


def test_canonical_json_rejects_lossy_python_types(tmp_path: Path) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        with pytest.raises(SelectionPlanEvidenceError, match="keys must be strings"):
            appender.append_object("registry_snapshot", {1: "integer-key"})
        with pytest.raises(SelectionPlanEvidenceError, match="native JSON"):
            appender.append_object(
                "registry_snapshot",
                {"tuple": ("silently", "became", "a-list")},
            )
        assert appender.index.object_count == 0


def test_appender_index_is_immutable_and_writer_snapshot_is_exclusive(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        initial = appender.index
        appender.append_object("registry_snapshot", {"version": 1})
        assert initial.object_count == 0
        assert len(initial.objects) == 0
        with pytest.raises(TypeError):
            initial.objects["sha256:" + "0" * 64] = appender.index.objects[
                next(iter(appender.index.objects))
            ]

        with pytest.raises(SelectionPlanEvidenceError, match="locked"):
            SelectionPlanPackAppender(path, create=False)

        raw_fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(raw_fd, b"external-mutation")
            os.fsync(raw_fd)
        finally:
            os.close(raw_fd)
        with pytest.raises(SelectionPlanEvidenceError, match="outside"):
            appender.append_object("registry_snapshot", {"version": 2})


def test_failed_rollback_poison_closes_the_appender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        def partial_write(fd: int, payload: bytes) -> None:
            os.write(fd, bytes(payload[:8]))
            raise OSError("synthetic write failure")

        monkeypatch.setattr(evidence, "_write_all", partial_write)
        monkeypatch.setattr(
            evidence.os,
            "ftruncate",
            lambda _fd, _size: (_ for _ in ()).throw(
                OSError("synthetic rollback failure")
            ),
        )
        with pytest.raises(SelectionPlanEvidenceError, match="rollback failed"):
            appender.append_object("registry_snapshot", {"version": 1})
        with pytest.raises(SelectionPlanEvidenceError, match="closed"):
            _ = appender.index


def test_create_failure_never_unlinks_a_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    original = tmp_path / "opened-inode.jsonl"

    def replace_path_then_fail(_fd: int, *, limits) -> None:
        del limits
        path.rename(original)
        path.write_bytes(b"replacement-must-survive")
        raise RuntimeError("synthetic scan failure")

    monkeypatch.setattr(evidence, "_scan_pack_fd", replace_path_then_fail)
    with pytest.raises(RuntimeError, match="scan failure"):
        SelectionPlanPackAppender(path)
    assert path.read_bytes() == b"replacement-must-survive"
    assert original.read_bytes().endswith(b"\n")


def test_create_fsyncs_the_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []
    real_fsync_directory = evidence._fsync_directory

    def record_fsync(path: Path) -> None:
        calls.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(evidence, "_fsync_directory", record_fsync)
    with SelectionPlanPackAppender(tmp_path / "selection-plans.jsonl"):
        pass
    assert calls == [tmp_path]


def test_root_objects_require_complete_existing_leaf_dependencies(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    missing_leaf = {
        "schema": SELECTION_PLAN_REF_SCHEMA,
        "kind": "registry_snapshot",
        "sha256": "sha256:" + "0" * 64,
        "uncompressed_bytes": 1,
    }
    with SelectionPlanPackAppender(path) as appender:
        with pytest.raises(SelectionPlanEvidenceError, match="must be stored"):
            appender.append_object("selection_plan", {"strategy": "invalid-root-api"})
        with pytest.raises(SelectionPlanEvidenceError, match="dangling"):
            appender._append_object(
                "selection_plan",
                {"registry_snapshot": missing_leaf},
            )

    line, _decoded = evidence._record_line(
        "selection_plan",
        {"registry_snapshot": missing_leaf},
        limits=DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
    )
    with path.open("ab") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(SelectionPlanEvidenceError, match="dangling"):
        SelectionPlanPackReader(path)


def test_ref_summary_and_header_tampering_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        ref = appender.store_selection_plan(_plan())

    tampered_summary = deepcopy(ref)
    tampered_summary["summary"]["decision_id"] = "different-decision"
    with SelectionPlanPackReader(path) as reader:
        with pytest.raises(SelectionPlanEvidenceError, match="summary differs"):
            reader.expand_selection_plan(tampered_summary)

        tampered_size = deepcopy(ref)
        tampered_size["uncompressed_bytes"] += 1
        with pytest.raises(SelectionPlanEvidenceError, match="header differs"):
            reader.expand_selection_plan(tampered_size)

        missing_field = deepcopy(ref)
        missing_field.pop("expanded_sha256")
        with pytest.raises(SelectionPlanEvidenceError, match="shape is not canonical"):
            parse_selection_plan_reference(missing_field)


def test_indexed_record_tamper_and_snapshot_change_are_detected(tmp_path: Path) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        ref = appender.store_selection_plan(_plan())

    with SelectionPlanPackReader(path) as reader:
        location = reader.index.location(ref["sha256"])
        payload = bytearray(path.read_bytes())
        payload[location.start_offset + 5] ^= 1
        path.write_bytes(payload)
        with pytest.raises(SelectionPlanEvidenceError, match="changed after indexing"):
            reader.expand_selection_plan(ref)
        with pytest.raises(SelectionPlanEvidenceError):
            reader.verify_snapshot()


def test_scan_rejects_compression_bomb_before_object_hash(tmp_path: Path) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        appender.append_object("registry_snapshot", {"small": True})

    compressed = zlib.compress(b"x" * 4096, level=9)

    def mutate(record: dict[str, object]) -> None:
        record["uncompressed_bytes"] = 8
        record["compressed_bytes"] = len(compressed)
        record["compressed_sha256"] = _sha256(compressed)
        record["payload_base64"] = base64.b64encode(compressed).decode("ascii")

    _rewrite_record(path, 1, mutate)
    with pytest.raises(SelectionPlanEvidenceError, match="decompression boundary"):
        SelectionPlanPackReader(path)


def test_scan_rejects_unused_zlib_member_even_when_compressed_hash_matches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        appender.append_object("registry_snapshot", {"small": True})

    def mutate(record: dict[str, object]) -> None:
        original = base64.b64decode(str(record["payload_base64"]), validate=True)
        conflict = original + zlib.compress(b"unused-member", level=9)
        record["compressed_bytes"] = len(conflict)
        record["compressed_sha256"] = _sha256(conflict)
        record["payload_base64"] = base64.b64encode(conflict).decode("ascii")

    _rewrite_record(path, 1, mutate)
    with pytest.raises(SelectionPlanEvidenceError, match="decompression boundary"):
        SelectionPlanPackReader(path)


def test_scan_rejects_duplicate_content_address_and_noncanonical_record(
    tmp_path: Path,
) -> None:
    duplicate_path = tmp_path / "duplicate.jsonl"
    with SelectionPlanPackAppender(duplicate_path) as appender:
        appender.append_object("registry_snapshot", {"small": True})
    lines = _pack_lines(duplicate_path)
    duplicate_path.write_bytes(b"".join([*lines, lines[1]]))
    with pytest.raises(SelectionPlanEvidenceError, match="duplicate content address"):
        SelectionPlanPackReader(duplicate_path)

    noncanonical_path = tmp_path / "noncanonical.jsonl"
    with SelectionPlanPackAppender(noncanonical_path) as appender:
        appender.append_object("registry_snapshot", {"small": True})
    lines = _pack_lines(noncanonical_path)
    record = json.loads(lines[1])
    lines[1] = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    noncanonical_path.write_bytes(b"".join(lines))
    with pytest.raises(SelectionPlanEvidenceError, match="not canonical JSON"):
        SelectionPlanPackReader(noncanonical_path)


def test_partial_line_and_unknown_record_schema_fail_closed(tmp_path: Path) -> None:
    partial_path = tmp_path / "partial.jsonl"
    with SelectionPlanPackAppender(partial_path) as appender:
        appender.append_object("registry_snapshot", {"small": True})
    partial_path.write_bytes(partial_path.read_bytes()[:-1])
    with pytest.raises(SelectionPlanEvidenceError, match="partial line"):
        SelectionPlanPackReader(partial_path)

    schema_path = tmp_path / "schema.jsonl"
    with SelectionPlanPackAppender(schema_path) as appender:
        appender.append_object("registry_snapshot", {"small": True})

    def mutate(record: dict[str, object]) -> None:
        assert record["schema"] == SELECTION_PLAN_PACK_RECORD_SCHEMA
        record["schema"] = "opensquilla.draco-selection-plan-pack-record/v999"

    _rewrite_record(schema_path, 1, mutate)
    with pytest.raises(SelectionPlanEvidenceError, match="shape differs"):
        SelectionPlanPackReader(schema_path)


def test_cyclic_input_and_wrong_leaf_kind_are_rejected_without_partial_batch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        cyclic: dict[str, object] = {"strategy": "router_dynamic"}
        cyclic["request_context"] = cyclic
        with pytest.raises(SelectionPlanEvidenceError, match="acyclic JSON"):
            appender.store_selection_plan(cyclic)
        assert appender.index.object_count == 0

        ranking_ref = appender.append_object(
            "ranking_parameters",
            {"schema_version": "v4"},
        )
        with pytest.raises(SelectionPlanEvidenceError, match="differs from"):
            appender._append_object(
                "selection_plan",
                {"registry_snapshot": ranking_ref},
            )
        assert appender.index.object_count == 1


def test_fixed_object_dag_rejects_refs_outside_direct_root_leaf_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        leaf_ref = appender.append_object(
            "registry_snapshot",
            {"snapshot": "one"},
        )
        with pytest.raises(SelectionPlanEvidenceError, match="fixed leaf fields"):
            appender.store_selection_plan({"opaque": leaf_ref})
        with pytest.raises(SelectionPlanEvidenceError, match="leaf contains"):
            appender.append_object(
                "ranking_parameters",
                {"opaque": {"nested": leaf_ref}},
            )
        assert appender.index.object_count == 1


def test_expanded_summary_object_count_and_pack_caps_are_fail_closed(
    tmp_path: Path,
) -> None:
    expanded_limits = replace(
        DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
        expanded_plan_bytes=512,
    )
    expanded_path = tmp_path / "expanded-cap.jsonl"
    with SelectionPlanPackAppender(expanded_path, limits=expanded_limits) as appender:
        with pytest.raises(SelectionPlanEvidenceLimitError, match="expanded selection plan"):
            appender.store_selection_plan(_plan())
        assert appender.index.object_count == 0

    summary_limits = replace(
        DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
        summary_identity_count=1,
    )
    summary_path = tmp_path / "summary-cap.jsonl"
    with SelectionPlanPackAppender(summary_path, limits=summary_limits) as appender:
        with pytest.raises(SelectionPlanEvidenceLimitError, match="count cap"):
            appender.store_selection_plan(_plan())
        assert appender.index.object_count == 0

    object_limits = replace(
        DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
        pack_object_count=1,
    )
    object_path = tmp_path / "object-cap.jsonl"
    with SelectionPlanPackAppender(object_path, limits=object_limits) as appender:
        with pytest.raises(SelectionPlanEvidenceLimitError, match="object-count cap"):
            appender.store_selection_plan(_plan())
        assert appender.index.object_count == 0

    pack_limits = replace(
        DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
        pack_bytes=1024,
    )
    pack_path = tmp_path / "pack-cap.jsonl"
    with SelectionPlanPackAppender(pack_path, limits=pack_limits) as appender:
        with pytest.raises(SelectionPlanEvidenceLimitError, match="total byte cap"):
            appender.append_object(
                "registry_snapshot",
                {"payload": os.urandom(4096).hex()},
            )
        assert appender.index.object_count == 0


def test_cumulative_uncompressed_cap_stops_before_next_record_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "aggregate-uncompressed.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        appender.append_object("registry_snapshot", {"payload": "x" * 512})
        appender.append_object("ranking_parameters", {"payload": "y" * 512})
        locations = tuple(appender.index.objects.values())
        first_size = locations[0].uncompressed_bytes
        total_size = appender.index.uncompressed_bytes

    low_limits = replace(
        DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
        pack_uncompressed_bytes=total_size - 1,
    )
    original_decompress = evidence._bounded_zlib_decompress
    decompressions: list[int] = []

    def counted_decompress(*args: object, **kwargs: object) -> bytes:
        decompressions.append(1)
        return original_decompress(*args, **kwargs)

    monkeypatch.setattr(evidence, "_bounded_zlib_decompress", counted_decompress)
    with pytest.raises(SelectionPlanEvidenceLimitError, match="cumulative"):
        SelectionPlanPackReader(path, limits=low_limits)
    assert len(decompressions) == 1

    append_path = tmp_path / "aggregate-append.jsonl"
    append_limits = replace(
        DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
        pack_uncompressed_bytes=first_size + 1,
    )
    with SelectionPlanPackAppender(append_path, limits=append_limits) as appender:
        appender.append_object("registry_snapshot", {"payload": "x" * 512})
        with pytest.raises(SelectionPlanEvidenceLimitError, match="cumulative"):
            appender.append_object("ranking_parameters", {"payload": "y" * 512})
        assert appender.index.object_count == 1
        assert appender.index.uncompressed_bytes == first_size


def test_object_uncompressed_and_record_line_caps_are_enforced(tmp_path: Path) -> None:
    object_limits = replace(
        DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
        object_uncompressed_bytes=256,
    )
    object_path = tmp_path / "object-bytes.jsonl"
    with SelectionPlanPackAppender(object_path, limits=object_limits) as appender:
        with pytest.raises(SelectionPlanEvidenceLimitError, match="uncompressed byte cap"):
            appender.append_object("registry_snapshot", {"payload": "x" * 1024})

    line_limits = replace(
        DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
        record_line_bytes=512,
    )
    line_path = tmp_path / "line-bytes.jsonl"
    with SelectionPlanPackAppender(line_path, limits=line_limits) as appender:
        with pytest.raises(SelectionPlanEvidenceLimitError, match="record exceeds"):
            appender.append_object(
                "registry_snapshot",
                {"payload": os.urandom(1024).hex()},
            )


def test_fd_bound_reader_survives_path_replacement(tmp_path: Path) -> None:
    path = tmp_path / "selection-plans.jsonl"
    original = _plan()
    with SelectionPlanPackAppender(path) as appender:
        ref = appender.store_selection_plan(original)

    fd = os.open(path, os.O_RDONLY)
    try:
        with SelectionPlanPackReader.from_fd(fd) as reader:
            moved = tmp_path / "original-pack.jsonl"
            path.rename(moved)
            with SelectionPlanPackAppender(path):
                pass
            assert reader.expand_selection_plan(ref) == original
            reader.verify_snapshot()
    finally:
        os.close(fd)


def test_path_bound_reader_resolves_original_fd_but_rejects_path_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    original = _plan()
    with SelectionPlanPackAppender(path) as appender:
        ref = appender.store_selection_plan(original)

    with SelectionPlanPackReader(path) as reader:
        moved = tmp_path / "original-pack.jsonl"
        path.rename(moved)
        with SelectionPlanPackAppender(path):
            pass
        assert reader.expand_selection_plan(ref) == original
        with pytest.raises(SelectionPlanEvidenceError, match="path was replaced"):
            reader.verify_snapshot()


def test_owner_only_reader_checks_mode_on_the_opened_pack_fd(tmp_path: Path) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        appender.store_selection_plan(_plan())
    path.chmod(0o640)

    with pytest.raises(SelectionPlanEvidenceError, match="must be owner-only"):
        SelectionPlanPackReader(path, owner_only=True)


def test_fifo_pack_fails_without_blocking_and_closes_the_opened_fd(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selection-plan.fifo"
    os.mkfifo(path, 0o600)
    script = """
import json
import os
import sys
from pathlib import Path
import opensquilla.eval.draco_selection_plan_evidence as evidence

opened = []
closed = []
original_open = os.open
original_close = os.close

def tracked_open(*args, **kwargs):
    fd = original_open(*args, **kwargs)
    opened.append(fd)
    return fd

def tracked_close(fd):
    closed.append(fd)
    return original_close(fd)

evidence.os.open = tracked_open
evidence.os.close = tracked_close
try:
    evidence.SelectionPlanPackReader(Path(sys.argv[1]), owner_only=True)
except evidence.SelectionPlanEvidenceError as exc:
    print(json.dumps({"closed": bool(opened) and opened[-1] in closed, "error": str(exc)}))
else:
    raise SystemExit("FIFO was accepted")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    result = json.loads(completed.stdout)
    assert result["closed"] is True
    assert "not a regular file" in result["error"]


def test_owner_only_mode_is_rechecked_for_reader_and_reopened_appender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        appender.store_selection_plan(_plan())

    with SelectionPlanPackReader(path, owner_only=True) as reader:
        path.chmod(0o644)
        with pytest.raises(SelectionPlanEvidenceError, match="must be owner-only"):
            reader.verify_snapshot()

    opened_fds: list[int] = []
    original_open_regular = evidence._open_regular

    def tracked_open_regular(*args: object, **kwargs: object) -> int:
        fd = original_open_regular(*args, **kwargs)
        opened_fds.append(fd)
        return fd

    monkeypatch.setattr(evidence, "_open_regular", tracked_open_regular)
    with pytest.raises(SelectionPlanEvidenceError, match="must be owner-only"):
        SelectionPlanPackAppender(path, create=False)
    assert opened_fds
    with pytest.raises(OSError):
        os.fstat(opened_fds[-1])


def test_ref_schema_and_expanded_hash_are_domain_separated(tmp_path: Path) -> None:
    path = tmp_path / "selection-plans.jsonl"
    plan = _plan()
    with SelectionPlanPackAppender(path) as appender:
        ref = appender.store_selection_plan(plan)
    assert ref["schema"] == SELECTION_PLAN_REF_SCHEMA
    assert ref["sha256"] != ref["expanded_sha256"]
    assert ref["expanded_sha256"] != _sha256(canonical_selection_plan_json_bytes(plan))

    tampered = deepcopy(ref)
    tampered["expanded_sha256"] = _sha256(canonical_selection_plan_json_bytes(plan))
    with SelectionPlanPackReader(path) as reader:
        with pytest.raises(SelectionPlanEvidenceError, match="expanded selection plan differs"):
            reader.expand_selection_plan(tampered)


def _durable_verification() -> dict[str, object]:
    return {
        "results_sha256": "1" * 64,
        "trace_sha256": "2" * 64,
        "checkpoint_sha256": "3" * 64,
    }


def test_writer_compacts_reserved_selection_plan_fields_once_per_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "selection-plans.jsonl"
    plan = _plan()
    row = {
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "execution": {
            "generation_attempts": [
                {"run": {"routing_trace": {"selection_plan": deepcopy(plan)}}}
            ]
        },
        "ensemble_trace": {"calls": [{"selection_plan": deepcopy(plan)}]},
    }
    with SelectionPlanPackAppender(path) as appender:
        calls = 0
        original_store = appender.store_selection_plan

        def counted_store(value: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return original_store(value)  # type: ignore[arg-type]

        monkeypatch.setattr(appender, "store_selection_plan", counted_store)
        compact = compact_selection_plan_evidence_row(row, appender=appender)
        assert calls == 1
        assert appender.index.object_count == 4
        assert validate_compact_selection_plan_evidence_row(
            compact,
            reader=appender,
        ) is True

    first_ref = compact["routing_trace"]["selection_plan"]
    assert compact["execution"]["generation_attempts"][0]["run"][
        "routing_trace"
    ]["selection_plan"] == first_ref
    assert compact["ensemble_trace"]["calls"][0]["selection_plan"] == first_ref
    assert row["routing_trace"]["selection_plan"] == plan


def test_writer_header_only_row_and_zero_object_binding(tmp_path: Path) -> None:
    path = tmp_path / "selection-plans.jsonl"
    with SelectionPlanPackAppender(path) as appender:
        compact = compact_selection_plan_evidence_row(
            {"group": "B0", "task_id": "task-1"},
            appender=appender,
        )
        assert validate_compact_selection_plan_evidence_row(
            compact,
            reader=appender,
        ) is False
        binding = selection_plan_evidence_manifest_binding(
            pack_index=appender.index,
            durable_artifact_verification=_durable_verification(),
            compact_row_count=0,
        )
        assert binding["pack_object_count"] == 0
        assert binding["compact_row_count"] == 0
        assert validate_selection_plan_evidence_manifest_binding(
            binding,
            pack_index=appender.index,
            durable_artifact_verification=_durable_verification(),
            compact_row_count=0,
        ) == binding


@pytest.mark.parametrize("inline_value", [None, "business text", [], 7])
def test_writer_rejects_non_mapping_reserved_selection_plan(
    tmp_path: Path,
    inline_value: object,
) -> None:
    with SelectionPlanPackAppender(tmp_path / "selection-plans.jsonl") as appender:
        with pytest.raises(
            SelectionPlanEvidenceError,
            match="requires an inline JSON object",
        ):
            compact_selection_plan_evidence_row(
                {"metadata": {"selection_plan": inline_value}},
                appender=appender,
            )


def test_terminal_binding_rejects_tamper(tmp_path: Path) -> None:
    with SelectionPlanPackAppender(tmp_path / "selection-plans.jsonl") as appender:
        binding = selection_plan_evidence_manifest_binding(
            pack_index=appender.index,
            durable_artifact_verification=_durable_verification(),
            compact_row_count=0,
        )
        binding["results_sha256"] = "f" * 64
        with pytest.raises(
            SelectionPlanEvidenceError,
            match="differs from durable evidence",
        ):
            validate_selection_plan_evidence_manifest_binding(
                binding,
                pack_index=appender.index,
                durable_artifact_verification=_durable_verification(),
                compact_row_count=0,
            )
