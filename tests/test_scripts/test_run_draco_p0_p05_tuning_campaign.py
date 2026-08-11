from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from opensquilla.eval import draco_artifact_integrity as artifact_integrity
from opensquilla.eval import draco_artifact_io as artifact_io
from opensquilla.eval import draco_selection_plan_evidence as plan_evidence
from opensquilla.provider.ranking_router import (
    build_request_context,
    fallback_task_profile,
    frozen_task_analysis_result,
    ranking_config_resolution,
)

CONTROLLER_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "experiments"
    / "run_draco_p0_p05_tuning_campaign.py"
)
if not CONTROLLER_PATH.exists():
    CONTROLLER_PATH = Path(__file__).with_name("controller.py")
SPEC = importlib.util.spec_from_file_location("draco_p0_p05_controller", CONTROLLER_PATH)
assert SPEC is not None and SPEC.loader is not None
controller = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = controller
SPEC.loader.exec_module(controller)

PLAN_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "configs"
    / "benchmarks"
    / "draco_p0_p05_campaign_plan.template.json"
)
if not PLAN_TEMPLATE.exists():
    PLAN_TEMPLATE = Path(__file__).with_name("campaign-plan.template.json")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def sealed_result(task_id: str) -> dict[str, object]:
    row: dict[str, object] = {
        "task_id": task_id,
        "group": "G1",
        "result_evidence_schema": "opensquilla.draco.result-evidence/v1",
    }
    row["result_evidence_sha256"] = "sha256:" + controller.canonical_sha256(
        {
            "schema": row["result_evidence_schema"],
            "result": copy.deepcopy(row),
        }
    )
    return row


def write_compact_dry_bundle(
    root: Path,
    *,
    task_ids: tuple[str, ...] = ("task-0", "task-1"),
    stamp: str = "20260811-230000",
    plan_marker: str = "A",
) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    results_path = root / f"draco_ensemble_{stamp}.jsonl"
    trace_path = root / f"draco_run_{stamp}.trace.jsonl"
    checkpoint_path = root / f"draco_run_{stamp}.checkpoint.json"
    manifest_path = root / f"draco_run_{stamp}.manifest.json"
    pack_path = root / f"draco_run_{stamp}.selection-plan.pack.jsonl"
    capability = artifact_io.durable_artifact_capability_contract()
    compact_rows: list[dict[str, object]] = []
    inline_plans: dict[str, dict[str, object]] = {}
    with plan_evidence.SelectionPlanPackAppender(pack_path) as appender:
        for row_index, task_id in enumerate(task_ids, start=1):
            selection_plan: dict[str, object] = {
                "task_profile_pre_escalation": {
                    "complexity": "medium",
                    "task_id": task_id,
                    "bundle_marker": plan_marker,
                },
                "task_analyzer": {
                    "source": "frozen_replay",
                    "usage": {},
                    "replay": {"physical_request_count": 0},
                },
                "selected_P": ["openrouter:test-proposer"],
                "selected_A": "openrouter:test-aggregator",
            }
            inline_plans[task_id] = copy.deepcopy(selection_plan)
            row: dict[str, object] = {
                "row_index": row_index,
                "task_id": task_id,
                "group": "G1",
                "routing_trace": {
                    "dry_run": True,
                    "selection_plan": selection_plan,
                },
                "ensemble_trace": {
                    "calls": [
                        {"selection_plan": copy.deepcopy(selection_plan)}
                    ]
                },
            }
            compact = plan_evidence.compact_selection_plan_evidence_row(
                row,
                appender=appender,
            )
            compact[artifact_io.DRACO_DURABLE_RESULT_ROW_FIELD] = capability
            compact_rows.append(artifact_integrity.seal_result_row(compact))
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        for row in compact_rows:
            assert writer.append(row, artifact_integrity.trace_row_from_result(row))
    verification = artifact_io.verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    with plan_evidence.SelectionPlanPackReader(pack_path, owner_only=True) as reader:
        binding = plan_evidence.selection_plan_evidence_manifest_binding(
            pack_index=reader.index,
            durable_artifact_verification=verification,
            compact_row_count=sum(
                plan_evidence.selection_plan_reference_signal(row)
                for row in compact_rows
            ),
        )
    manifest = {
        "schema": artifact_io.DRACO_RUN_MANIFEST_SCHEMA_V2,
        "stamp": stamp,
        "status": "complete",
        "dry_run": True,
        "groups": ["G1"],
        "durable_artifact_capability": capability,
        "run_compatibility": {
            "contracts": {
                "G1": {"durable_artifact_capability": capability},
            }
        },
        "artifacts": {
            "results_jsonl": str(results_path),
            "trace_jsonl": str(trace_path),
            "checkpoint_json": str(checkpoint_path),
            "manifest_json": str(manifest_path),
            plan_evidence.SELECTION_PLAN_PACK_ARTIFACT_FIELD: str(pack_path),
        },
        plan_evidence.SELECTION_PLAN_EVIDENCE_ROW_FIELD: (
            plan_evidence.selection_plan_evidence_capability_contract()
        ),
        plan_evidence.SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD: binding,
    }
    write_json(manifest_path, manifest)
    return SimpleNamespace(
        results=results_path,
        trace=trace_path,
        checkpoint=checkpoint_path,
        manifest=manifest_path,
        pack=pack_path,
        plans=inline_plans,
    )


def write_inline_dry_bundle(
    root: Path,
    *,
    task_ids: tuple[str, ...] = ("task-0", "task-1"),
    stamp: str = "20260811-230000",
    plan_marker: str = "legacy",
) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    results_path = root / f"draco_ensemble_{stamp}.jsonl"
    trace_path = root / f"draco_run_{stamp}.trace.jsonl"
    checkpoint_path = root / f"draco_run_{stamp}.checkpoint.json"
    manifest_path = root / f"draco_run_{stamp}.manifest.json"
    capability = artifact_io.durable_artifact_capability_contract()
    rows: list[dict[str, object]] = []
    inline_plans: dict[str, dict[str, object]] = {}
    for row_index, task_id in enumerate(task_ids, start=1):
        selection_plan: dict[str, object] = {
            "task_profile_pre_escalation": {
                "complexity": "medium",
                "task_id": task_id,
                "bundle_marker": plan_marker,
            },
            "task_analyzer": {
                "source": "frozen_replay",
                "usage": {},
                "replay": {"physical_request_count": 0},
            },
            "selected_P": ["openrouter:test-proposer"],
            "selected_A": "openrouter:test-aggregator",
        }
        inline_plans[task_id] = copy.deepcopy(selection_plan)
        row: dict[str, object] = {
            "row_index": row_index,
            "task_id": task_id,
            "group": "G1",
            "routing_trace": {
                "dry_run": True,
                "selection_plan": selection_plan,
            },
            artifact_io.DRACO_DURABLE_RESULT_ROW_FIELD: capability,
        }
        rows.append(artifact_integrity.seal_result_row(row))
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        for row in rows:
            assert writer.append(row, artifact_integrity.trace_row_from_result(row))
    manifest = {
        "schema": artifact_io.DRACO_RUN_MANIFEST_SCHEMA_V2,
        "stamp": stamp,
        "status": "complete",
        "dry_run": True,
        "groups": ["G1"],
        "durable_artifact_capability": capability,
        "run_compatibility": {
            "contracts": {
                "G1": {"durable_artifact_capability": capability},
            }
        },
        "artifacts": {
            "results_jsonl": str(results_path),
            "trace_jsonl": str(trace_path),
            "checkpoint_json": str(checkpoint_path),
            "manifest_json": str(manifest_path),
        },
    }
    write_json(manifest_path, manifest)
    return SimpleNamespace(
        results=results_path,
        trace=trace_path,
        checkpoint=checkpoint_path,
        manifest=manifest_path,
        pack=None,
        plans=inline_plans,
    )


class ControllerTests(unittest.TestCase):
    def test_isolated_snapshot_helper_does_not_write_bytecode(self) -> None:
        program = """
import json
import sys
from pathlib import Path

snapshot = Path(sys.argv[1])
sys.path.insert(0, str(snapshot))
import snapshot_probe

print(json.dumps({"value": snapshot_probe.VALUE}))
"""
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp)
            (snapshot / "snapshot_probe.py").write_text(
                "VALUE = 'snapshot-imported'\n",
                encoding="utf-8",
            )

            result = controller._isolated_snapshot_json(
                snapshot,
                program=program,
                payload={},
                label="bytecode isolation probe",
            )

            self.assertEqual(result, {"value": "snapshot-imported"})
            self.assertEqual(list(snapshot.rglob("__pycache__")), [])
            self.assertEqual(list(snapshot.rglob("*.pyc")), [])

    def test_matrix_freezes_66_arms_controls_modes_and_schedule(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        self.assertEqual(len(arms), 66)
        self.assertEqual(
            len({arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"}),
            31,
        )
        self.assertEqual(sum(arm.analyzer_mode == "frozen_replay" for arm in arms), 61)
        self.assertEqual(sum(arm.analyzer_mode == "live" for arm in arms), 5)
        self.assertEqual(
            [arm.arm_id for arm in arms],
            plan["execution"]["schedule"]["arm_order"],
        )
        r1_index = plan["execution"]["schedule"]["arm_order"].index("common-E0-R1")
        self.assertEqual(
            plan["execution"]["schedule"]["arm_order"][r1_index : r1_index + 3],
            ["common-E0-R1", "P0-20-E3", "P0-20-E2"],
        )
        by_id = {arm.arm_id: arm for arm in arms}
        for arm in arms:
            if arm.experiment_id == "common-E0":
                continue
            control = by_id[arm.control_arm_id]
            self.assertEqual(arm.analyzer_mode, control.analyzer_mode)

    def test_plan_rejects_schedule_control_or_mode_drift(self) -> None:
        template = controller.load_json(PLAN_TEMPLATE)
        mutations = []
        wrong_order = copy.deepcopy(template)
        wrong_order["execution"]["schedule"]["arm_order"][6:8] = reversed(
            wrong_order["execution"]["schedule"]["arm_order"][6:8]
        )
        mutations.append(wrong_order)
        wrong_anchor = copy.deepcopy(template)
        wrong_anchor["execution"]["schedule"]["anchor_by_arm_id"]["P0-12-E1"] = "common-E0-R2"
        mutations.append(wrong_anchor)
        wrong_control = copy.deepcopy(template)
        wrong_control["comparison_controls"]["arm_control_overrides"]["P0-12-E1"] = "common-E0-R2"
        mutations.append(wrong_control)
        strict_interleaving = copy.deepcopy(template)
        strict_interleaving["execution"]["schedule"]["strict_task_interleaving"] = True
        mutations.append(strict_interleaving)
        wrong_mode = copy.deepcopy(template)
        wrong_mode["common_e0"][1]["analyzer_mode"] = "live"
        mutations.append(wrong_mode)
        for plan in mutations:
            with self.subTest(plan=plan):
                with self.assertRaises(controller.ControllerError):
                    controller.validate_plan(plan, allow_placeholders=True)

    def test_anchor_gate_blocks_source_and_replay_tranches_after_anchor_failure(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        by_id = {arm.arm_id: arm for arm in arms}
        status = controller.initialize_status(
            plan,
            arms,
            plan_sha256="plan",
            snapshot_identity={"commit": "commit", "tree": "tree"},
        )
        cases = (
            ("P0-03-E1", "common-E0-source"),
            ("P0-20-E3", "common-E0-R1"),
        )
        for arm_id, anchor_id in cases:
            status["arms"][anchor_id]["state"] = "failed"
            with self.subTest(arm_id=arm_id):
                allowed, failure = controller.schedule_anchor_launch_gate(
                    plan,
                    by_id[arm_id],
                    status=status,
                    authenticated_anchor_ids=set(),
                )
                self.assertFalse(allowed)
                self.assertEqual(failure["reason"], "anchor_not_succeeded")
                self.assertEqual(failure["anchor_arm_id"], anchor_id)
                self.assertEqual(failure["anchor_state"], "failed")

    def test_anchor_gate_allows_restart_anchor_authenticated_earlier_in_schedule(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        by_id = {arm.arm_id: arm for arm in arms}
        status = controller.initialize_status(
            plan,
            arms,
            plan_sha256="plan",
            snapshot_identity={"commit": "commit", "tree": "tree"},
        )
        status["arms"]["common-E0-R1"]["state"] = "succeeded"
        allowed, evidence = controller.schedule_anchor_launch_gate(
            plan,
            by_id["P0-20-E3"],
            status=status,
            authenticated_anchor_ids={"common-E0-R1"},
        )
        self.assertTrue(allowed)
        self.assertTrue(evidence["anchor_authenticated"])

    def test_anchor_gate_rejects_forged_succeeded_status_without_artifacts(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        by_id = {arm.arm_id: arm for arm in arms}
        status = controller.initialize_status(
            plan,
            arms,
            plan_sha256="plan",
            snapshot_identity={"commit": "commit", "tree": "tree"},
        )
        status["arms"]["common-E0-source"]["state"] = "succeeded"
        allowed, failure = controller.schedule_anchor_launch_gate(
            plan,
            by_id["P0-03-E1"],
            status=status,
            authenticated_anchor_ids=set(),
        )
        self.assertFalse(allowed)
        self.assertEqual(failure["reason"], "anchor_not_succeeded")
        self.assertEqual(failure["anchor_state"], "succeeded")
        self.assertFalse(failure["anchor_authenticated"])

    def test_replicate_overrides_deep_merge_and_seed_contract(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        by_id = {arm.arm_id: arm for arm in arms}
        for replicate, seed in enumerate((0, 1, 4), start=1):
            override = by_id[f"P0.5-36-E1-R{replicate}"].override
            self.assertIs(override["ensemble"]["shuffle_candidates"], True)
            self.assertEqual(override["ensemble"]["candidate_order_seed"], seed)

        merge_plan = copy.deepcopy(plan)
        temperature_variant = next(
            experiment for experiment in merge_plan["experiments"] if experiment["id"] == "P0.5-11"
        )["variants"][0]
        temperature_variant["replicate_overrides"] = [
            {"generation": {"max_tokens": value}} for value in (1, 2, 3)
        ]
        merged = {
            arm.arm_id: arm
            for arm in controller.expand_arms(merge_plan)
            if arm.experiment_id == "P0.5-11"
        }
        for replicate in range(1, 4):
            generation = merged[f"P0.5-11-E1-R{replicate}"].override["generation"]
            self.assertEqual(generation["temperature"], 0.2)
            self.assertEqual(generation["max_tokens"], replicate)

    def test_replicate_overrides_and_shuffle_seeds_fail_closed(self) -> None:
        template = controller.load_json(PLAN_TEMPLATE)
        shuffle_variant = next(
            experiment for experiment in template["experiments"] if experiment["id"] == "P0.5-36"
        )["variants"][0]
        invalid: list[dict[str, object]] = []
        for value in (True, -1, 1 << 64):
            plan = copy.deepcopy(template)
            variant = next(
                experiment for experiment in plan["experiments"] if experiment["id"] == "P0.5-36"
            )["variants"][0]
            variant["replicate_overrides"][0]["ensemble"]["candidate_order_seed"] = value
            invalid.append(plan)
        duplicate = copy.deepcopy(template)
        variant = next(
            experiment for experiment in duplicate["experiments"] if experiment["id"] == "P0.5-36"
        )["variants"][0]
        variant["replicate_overrides"][0]["ensemble"]["candidate_order_seed"] = 1
        invalid.append(duplicate)
        shuffle_off = copy.deepcopy(template)
        variant = next(
            experiment for experiment in shuffle_off["experiments"] if experiment["id"] == "P0.5-36"
        )["variants"][0]
        variant["override"]["ensemble"]["shuffle_candidates"] = False
        invalid.append(shuffle_off)
        for plan in invalid:
            with self.subTest(plan=plan):
                with self.assertRaises(controller.ControllerError):
                    controller.validate_plan(plan, allow_placeholders=True)

        for malformed in (
            shuffle_variant["replicate_overrides"][:2],
            [*shuffle_variant["replicate_overrides"], {}],
            "not-a-list",
            [shuffle_variant["replicate_overrides"][0], 1, {}],
        ):
            plan = copy.deepcopy(template)
            variant = next(
                experiment for experiment in plan["experiments"] if experiment["id"] == "P0.5-36"
            )["variants"][0]
            variant["replicate_overrides"] = copy.deepcopy(malformed)
            with self.subTest(malformed=malformed):
                with self.assertRaises(controller.ControllerError):
                    controller.expand_arms(plan)
        single = copy.deepcopy(template)
        variant = next(
            experiment for experiment in single["experiments"] if experiment["id"] == "P0.5-36"
        )["variants"][0]
        variant["replicates"] = 1
        variant["replicate_overrides"] = [variant["replicate_overrides"][0]]
        with self.assertRaises(controller.ControllerError):
            controller.expand_arms(single)

    def test_plan_rejects_judge_or_generation_budget_drift(self) -> None:
        template = controller.load_json(PLAN_TEMPLATE)
        for key, value in (("judge_concurrency", 5), ("generation_max_attempts", 2)):
            with self.subTest(key=key):
                plan = copy.deepcopy(template)
                plan["execution"][key] = value
                with self.assertRaises(controller.ControllerError):
                    controller.validate_plan(plan, allow_placeholders=True)

    def test_replay_overlay_and_runtime_support_bind_declared_schema(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        artifact = {
            "replay_payload": {
                "schema": controller.FROZEN_TASK_ANALYSIS_SCHEMA_V2,
                "entries": {},
            }
        }
        with self.assertRaises(controller.ControllerError):
            controller.make_replay_overlay(plan, artifact)
        artifact["replay_payload"]["schema"] = controller.FROZEN_TASK_ANALYSIS_SCHEMA_V3
        overlay = controller.make_replay_overlay(plan, artifact)
        self.assertEqual(
            overlay["g1_routing"]["task_analysis_execution"]["schema"],
            controller.FROZEN_TASK_ANALYSIS_SCHEMA_V3,
        )
        controller.validate_frozen_replay_runtime_support(
            plan,
            {"frozen_task_analysis_schemas": {controller.FROZEN_TASK_ANALYSIS_SCHEMA_V3}},
        )
        with self.assertRaises(controller.ControllerError):
            controller.validate_frozen_replay_runtime_support(
                plan,
                {"frozen_task_analysis_schemas": {controller.FROZEN_TASK_ANALYSIS_SCHEMA_V2}},
            )

    def test_analyzer_fallback_and_preexisting_source_require_explicit_contracts(self) -> None:
        template = controller.load_json(PLAN_TEMPLATE)
        self.assertFalse(
            controller.analyzer_source_policy(template)["allow_deterministic_router_fallback"]
        )
        self.assertIsNone(controller.preexisting_source_contract(template))

        opted_in = copy.deepcopy(template)
        opted_in["runtime_contract"]["analyzer_source"] = {
            "schema": controller.ANALYZER_SOURCE_POLICY_SCHEMA,
            "allow_deterministic_router_fallback": True,
        }
        opted_in["runtime_contract"]["frozen_replay"]["schema"] = (
            controller.FROZEN_TASK_ANALYSIS_SCHEMA_V1
        )
        with self.assertRaises(controller.ControllerError):
            controller.validate_plan(opted_in, allow_placeholders=True)
        opted_in["runtime_contract"]["frozen_replay"]["schema"] = (
            controller.FROZEN_TASK_ANALYSIS_SCHEMA_V2
        )
        opted_in["runtime_contract"]["preexisting_source"] = {
            "schema": controller.PREEXISTING_SOURCE_SCHEMA,
            "enabled": True,
            "source_plan_path": "TODO_SOURCE_PLAN_PATH",
            "source_plan_raw_sha256": "TODO_SOURCE_PLAN_RAW_SHA256",
            "source_plan_canonical_sha256": "TODO_SOURCE_PLAN_CANONICAL_SHA256",
            "source_snapshot_path": "TODO_SOURCE_SNAPSHOT_PATH",
            "source_snapshot_commit": "TODO_SOURCE_SNAPSHOT_COMMIT",
            "source_snapshot_tree": "TODO_SOURCE_SNAPSHOT_TREE",
            "source_output_dir": "TODO_SOURCE_OUTPUT_DIR",
            "source_manifest_sha256": "TODO_SOURCE_MANIFEST_SHA256",
            "source_results_sha256": "TODO_SOURCE_RESULTS_SHA256",
            "source_trace_sha256": "TODO_SOURCE_TRACE_SHA256",
        }
        controller.validate_plan(opted_in, allow_placeholders=True)
        self.assertTrue(
            controller.analyzer_source_policy(opted_in)["allow_deterministic_router_fallback"]
        )

        malformed = copy.deepcopy(opted_in)
        malformed["runtime_contract"]["preexisting_source"].pop("source_trace_sha256")
        with self.assertRaises(controller.ControllerError):
            controller.validate_plan(malformed, allow_placeholders=True)

    def test_preexisting_source_publication_receipt_is_fail_closed(self) -> None:
        plan = {
            "benchmark": {"task_ids": [f"task-{index}" for index in range(10)]},
            "execution": {"task_concurrency": 6},
        }
        expected = {"output_dir": "/bound/source"}
        base_receipt = {"source_output_dir": "/bound/source"}
        with (
            mock.patch.object(
                controller,
                "preexisting_source_identity",
                return_value=(expected, copy.deepcopy(base_receipt)),
            ),
            mock.patch.object(
                controller,
                "inspect_complete_arm",
                return_value=(True, {"status": "complete"}),
            ),
        ):
            receipt = controller.authenticate_preexisting_source(plan)
        self.assertEqual(receipt["source_output_dir"], "/bound/source")
        self.assertIn("publication_evidence_sha256", receipt)
        self.assertIn("receipt_sha256", receipt)

        with (
            mock.patch.object(
                controller,
                "preexisting_source_identity",
                return_value=(expected, copy.deepcopy(base_receipt)),
            ),
            mock.patch.object(
                controller,
                "inspect_complete_arm",
                return_value=(False, {"reason": "hash_mismatch"}),
            ),
            self.assertRaises(controller.ControllerError),
        ):
            controller.authenticate_preexisting_source(plan)

    def test_preexisting_source_is_frozen_once_and_reused_without_source_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "old-reports" / "common" / "source"
            source.mkdir(parents=True)
            snapshot = root / "old-snapshot"
            module_dir = snapshot / "src" / "opensquilla" / "eval"
            module_dir.mkdir(parents=True)
            (snapshot / "src" / "opensquilla" / "__init__.py").write_text(
                "", encoding="utf-8"
            )
            (module_dir / "__init__.py").write_text("", encoding="utf-8")
            config_module_path = module_dir / "draco_experiment_config.py"
            config_module_source = (
                "class Config:\n"
                "    def model_dump(self, mode=None):\n"
                "        return {\n"
                "            'runner': {'concurrency': 2, 'timeout_seconds': 111.0},\n"
                "            'judge': {'concurrency': 6, 'model': 'judge-model'},\n"
                "            'generation': {'max_attempts': 3, 'retry_backoff_s': 2.0},\n"
                "            'ensemble': {'candidate_order_seed': None, "
                "'shuffle_candidates': False},\n"
                "            'audit_marker': 'source',\n"
                "        }\n"
                "class Loaded:\n"
                "    config = Config()\n"
                "def load_draco_experiment_config(*args, **kwargs):\n"
                "    return Loaded()\n"
            )
            config_module_path.write_text(
                config_module_source,
                encoding="utf-8",
            )
            scripts = snapshot / "scripts"
            scripts.mkdir()
            for runner_name in (
                "run_draco_routing_experiment.py",
                "run_draco_routing_experiment_resume.py",
            ):
                (scripts / runner_name).write_text("# frozen runner\n", encoding="utf-8")
            source_arm = controller.Arm(
                arm_id=controller.ANALYZER_SOURCE_ARM_ID,
                experiment_id="common-E0",
                directory_name="common",
                variant="E0",
                replicate=1,
                analyzer_mode="live",
                override={},
                dynamic=None,
                wire_gate=None,
                output_name="source",
                control_arm_id=None,
            )
            source_plan_payload = {
                "run_id": "old-run",
                "paths": {
                    "experiment_config_relative": "configs/draco.json",
                    "reference_repo": str(root / "reference"),
                    "report_root": str(root / "old-reports"),
                },
                "benchmark": {
                    "input_sha256": "c" * 64,
                    "task_ids": [f"task-{index}" for index in range(10)],
                },
                "execution": {
                    "task_concurrency": 6,
                    "judge_concurrency": 6,
                    "generation_max_attempts": 3,
                },
            }
            source_plan = root / "old-plan.json"
            source_plan.write_text(
                json.dumps(source_plan_payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            for name in (
                "manifest.json",
                "results.jsonl",
                "trace.jsonl",
                "audit.json",
                "openrouter-non-byok-campaign-proof.json",
            ):
                (source / name).write_text(name + "\n", encoding="utf-8")
            contract = {
                "schema": controller.PREEXISTING_SOURCE_SCHEMA,
                "enabled": True,
                "source_plan_path": str(source_plan),
                "source_plan_raw_sha256": controller.file_sha256(source_plan),
                "source_plan_canonical_sha256": controller.canonical_sha256(
                    source_plan_payload
                ),
                "source_snapshot_path": str(snapshot),
                "source_snapshot_commit": "a" * 40,
                "source_snapshot_tree": "b" * 40,
                "source_output_dir": str(source),
                "source_manifest_sha256": controller.file_sha256(source / "manifest.json"),
                "source_results_sha256": controller.file_sha256(source / "results.jsonl"),
                "source_trace_sha256": controller.file_sha256(source / "trace.jsonl"),
            }
            plan = {
                "runtime_contract": {"preexisting_source": contract},
                "paths": {
                    "run_root": str(root / "run"),
                    "report_root": str(root / "new-reports"),
                },
            }
            (root / "run").mkdir()
            git_state = {"commit": "a" * 40, "tree": "b" * 40, "status": ""}
            expected_identity = controller.arm_completion_identity(
                source_plan_payload,
                source_arm,
                snapshot=snapshot,
                snapshot_identity=git_state,
                override={},
                isolated_config=True,
            )
            expected_config_payload = {
                "runner": {"concurrency": 6, "timeout_seconds": 111.0},
                "judge": {"concurrency": 6, "model": "judge-model"},
                "generation": {"max_attempts": 3, "retry_backoff_s": 2.0},
                "ensemble": {
                    "candidate_order_seed": None,
                    "shuffle_candidates": False,
                },
                "audit_marker": "source",
            }
            self.assertEqual(
                expected_identity["effective_config_sha256"],
                controller.canonical_sha256(expected_config_payload),
            )
            for section_name, field_name, invalid_value in (
                ("judge", "concurrency", 5),
                ("generation", "max_attempts", 2),
            ):
                with self.subTest(runtime_field=f"{section_name}.{field_name}"):
                    invalid_config = copy.deepcopy(expected_config_payload)
                    invalid_config[section_name][field_name] = invalid_value
                    with self.assertRaises(controller.ControllerError):
                        controller.launcher_effective_config_projection(
                            source_plan_payload,
                            invalid_config,
                        )
            config_module_path.write_text(
                config_module_source.replace(
                    "'audit_marker': 'source'",
                    "'audit_marker': 'drift'",
                ),
                encoding="utf-8",
            )
            drifted_identity = controller.arm_completion_identity(
                source_plan_payload,
                source_arm,
                snapshot=snapshot,
                snapshot_identity=git_state,
                override={},
                isolated_config=True,
            )
            self.assertNotEqual(
                drifted_identity["effective_config_sha256"],
                expected_identity["effective_config_sha256"],
            )
            config_module_path.write_text(config_module_source, encoding="utf-8")
            publication_evidence = {"reason": "complete"}
            authenticated = {
                **contract,
                "expected_identity_sha256": controller.canonical_sha256(expected_identity),
                "expected_publication_identity": expected_identity,
                "publication_evidence": publication_evidence,
                "publication_evidence_sha256": controller.canonical_sha256(publication_evidence),
                "contract_sha256": controller.canonical_sha256(contract),
            }
            authenticated["receipt_sha256"] = controller.canonical_sha256(authenticated)
            archive_bytes = io.BytesIO()
            with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
                for path in sorted(snapshot.rglob("*")):
                    if not path.is_file():
                        continue
                    payload = path.read_bytes()
                    member = tarfile.TarInfo(path.relative_to(snapshot).as_posix())
                    member.size = len(payload)
                    member.mode = 0o644
                    archive.addfile(member, io.BytesIO(payload))
            completed = SimpleNamespace(returncode=0, stdout=archive_bytes.getvalue())
            real_subprocess_run = subprocess.run

            def run_with_frozen_archive(command, *args, **kwargs):
                if list(command)[:2] == ["git", "archive"]:
                    return completed
                return real_subprocess_run(command, *args, **kwargs)

            with (
                mock.patch.object(
                    controller,
                    "authenticate_preexisting_source",
                    return_value=copy.deepcopy(authenticated),
                ) as authenticate,
                mock.patch.object(
                    controller,
                    "authenticate_published_arm_artifacts",
                    return_value=({}, {}, {}, {}),
                ),
                mock.patch.object(controller, "git_identity", return_value=git_state),
                mock.patch.object(
                    controller.subprocess,
                    "run",
                    side_effect=run_with_frozen_archive,
                ),
                mock.patch.object(controller, "validate_plan", return_value=[source_arm]),
            ):
                first = controller.materialize_preexisting_source(plan)
                (source / "results.jsonl").write_text("changed\n", encoding="utf-8")
                second = controller.materialize_preexisting_source(plan)
            self.assertEqual(first, second)
            self.assertEqual(authenticate.call_count, 1)
            package = Path(first["package_dir"])
            self.assertNotEqual(
                controller.file_sha256(source / "results.jsonl"),
                controller.file_sha256(package / "results.jsonl"),
            )

    def test_preexisting_source_path_rejects_symlink_before_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            alias = root / "alias.json"
            alias.symlink_to(target)
            with self.assertRaises(controller.ControllerError):
                controller.absolute_path_without_symlinks(
                    alias,
                    label="source plan",
                )

    def test_effective_config_import_isolated_between_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            observed = []
            for marker in ("old", "new"):
                snapshot = root / marker
                module_dir = snapshot / "src" / "opensquilla" / "eval"
                module_dir.mkdir(parents=True)
                (snapshot / "src" / "opensquilla" / "__init__.py").write_text("", encoding="utf-8")
                (module_dir / "__init__.py").write_text("", encoding="utf-8")
                (module_dir / "draco_experiment_config.py").write_text(
                    "class _Config:\n"
                    "    def model_dump(self, mode=None):\n"
                    f"        return {{'marker': '{marker}', 'ensemble': {{}}}}\n"
                    "class _Loaded:\n"
                    "    config = _Config()\n"
                    "def load_draco_experiment_config(*args, **kwargs):\n"
                    "    return _Loaded()\n",
                    encoding="utf-8",
                )
                base_config = snapshot / "config.json"
                base_config.write_text("{}\n", encoding="utf-8")
                observed.append(
                    controller.load_effective_experiment_config_isolated(
                        snapshot,
                        base_config,
                        {},
                    )["marker"]
                )
            self.assertEqual(observed, ["old", "new"])

    def test_preexisting_source_identity_binds_plan_snapshot_and_three_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_dir = root / "reports" / "common" / "source-old"
            source_dir.mkdir(parents=True)
            for filename, content in (
                ("manifest.json", "{}\n"),
                ("results.jsonl", "{}\n"),
                ("trace.jsonl", "{}\n"),
            ):
                (source_dir / filename).write_text(content, encoding="utf-8")
            source_plan_path = root / "old-plan.json"
            (root / "snapshot").mkdir()
            benchmark = {
                "task_count": 10,
                "task_ids": [f"task-{index}" for index in range(10)],
                "groups": ["G1"],
            }
            source_plan = {
                "schema": controller.PLAN_SCHEMA,
                "run_id": "old",
                "benchmark": benchmark,
                "runtime_contract": {},
                "paths": {"report_root": str(root / "reports")},
                "freeze": {"snapshot_commit": "a" * 40, "snapshot_tree": "b" * 40},
            }
            write_json(source_plan_path, source_plan)
            source_arm = controller.Arm(
                arm_id=controller.ANALYZER_SOURCE_ARM_ID,
                experiment_id="common-E0",
                directory_name="common",
                variant="E0",
                replicate=1,
                analyzer_mode="live",
                override={},
                dynamic=None,
                wire_gate=None,
                output_name="source-old",
                control_arm_id=None,
            )
            contract = {
                "schema": controller.PREEXISTING_SOURCE_SCHEMA,
                "enabled": True,
                "source_plan_path": str(source_plan_path),
                "source_plan_raw_sha256": controller.file_sha256(source_plan_path),
                "source_plan_canonical_sha256": controller.canonical_sha256(source_plan),
                "source_snapshot_path": str(root / "snapshot"),
                "source_snapshot_commit": "a" * 40,
                "source_snapshot_tree": "b" * 40,
                "source_output_dir": str(source_dir),
                "source_manifest_sha256": controller.file_sha256(source_dir / "manifest.json"),
                "source_results_sha256": controller.file_sha256(source_dir / "results.jsonl"),
                "source_trace_sha256": controller.file_sha256(source_dir / "trace.jsonl"),
            }
            plan = {
                "benchmark": copy.deepcopy(benchmark),
                "runtime_contract": {"preexisting_source": contract},
            }
            expected = {"output_dir": str(source_dir), "identity": "bound"}
            with (
                mock.patch.object(controller, "validate_plan", return_value=[source_arm]),
                mock.patch.object(
                    controller,
                    "git_identity",
                    return_value={
                        "commit": "a" * 40,
                        "tree": "b" * 40,
                        "status": "",
                    },
                ),
                mock.patch.object(
                    controller,
                    "arm_completion_identity",
                    return_value=expected,
                ) as completion_identity,
            ):
                observed = controller.preexisting_source_identity(plan)
                self.assertEqual(observed[0], expected)
                self.assertEqual(
                    observed[1]["source_results_sha256"],
                    contract["source_results_sha256"],
                )
                self.assertTrue(completion_identity.call_args.kwargs["isolated_config"])
                (source_dir / "results.jsonl").write_text('{"changed":true}\n', encoding="utf-8")
                with self.assertRaises(controller.ControllerError):
                    controller.preexisting_source_identity(plan)

    def test_analyzer_ledger_rejects_ambiguous_usage_and_cross_task_ids(self) -> None:
        expected = {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "upstream_provider": "anthropic",
        }
        attempt = {
            "attempt": 1,
            "physical_attempt_id": "1" * 32,
            "requested_provider": "openrouter",
            "requested_model": "anthropic/claude-opus-4.8",
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "usage_unknown": False,
            "input_tokens": 10,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "cached_tokens": 3,
            "cache_write_tokens": 0,
            "billed_cost": 0.01,
            "provider_usage": {
                "usage_unknown": False,
                "physical_attempt_id": "1" * 32,
            },
        }
        analyzer = {
            **expected,
            "source": "llm_provider",
            "schema_valid": True,
            "fallback_reason": "",
            "normalization_warnings": [],
            "usage": {
                "attempt_count": 1,
                "physical_request_count": 1,
                "usage_unknown_count": 0,
                "input_tokens": 10,
                "output_tokens": 2,
                "reasoning_tokens": 0,
                "cached_tokens": 3,
                "cache_write_tokens": 0,
                "billed_cost": 0.01,
                "physical_attempts": [attempt],
            },
        }
        usage, _, _ = controller._validated_analyzer_attempt_ledger(
            task_id="task-a",
            analyzer=analyzer,
            expected_config=expected,
            allow_zero_attempts=False,
        )
        owners: dict[str, str] = {}
        controller.register_analyzer_attempt_owners(owners, task_id="task-a", usage=usage)
        with self.assertRaises(controller.ControllerError):
            controller.register_analyzer_attempt_owners(owners, task_id="task-b", usage=usage)

        bool_aggregate = copy.deepcopy(analyzer)
        bool_aggregate["usage"]["input_tokens"] = True
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-a",
                analyzer=bool_aggregate,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        conflicting_id = copy.deepcopy(analyzer)
        conflicting_id["usage"]["physical_attempts"][0]["reported_physical_attempt_ids"] = [
            "2" * 32
        ]
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-a",
                analyzer=conflicting_id,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        contradictory_unknown = copy.deepcopy(analyzer)
        unknown_attempt = contradictory_unknown["usage"]["physical_attempts"][0]
        unknown_attempt.update(
            {
                "usage_unknown": True,
                "unknown_reason": "TimeoutError",
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "billed_cost": 0.0,
                "provider_usage": {
                    "usage_unknown": True,
                    "unknown_reason": "TimeoutError",
                    "physical_attempt_id": "1" * 32,
                },
            }
        )
        contradictory_unknown["usage"].update(
            {
                "usage_unknown_count": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "billed_cost": 0.0,
            }
        )
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-a",
                analyzer=contradictory_unknown,
                expected_config=expected,
                allow_zero_attempts=False,
            )

    def test_analyzer_chain_ledger_authenticates_order_and_selected_route(self) -> None:
        expected = copy.deepcopy(
            ranking_config_resolution()["effective_config"]["task_analyzer"]
        )
        routes = [
            {
                key: str(expected[key])
                for key in ("provider", "model", "upstream_provider")
            },
            *copy.deepcopy(expected["fallback_chain"]),
        ]

        def attempt(
            ordinal: int,
            route: dict[str, str],
            *,
            known: bool,
        ) -> dict[str, object]:
            physical_id = f"{ordinal:032x}"
            row: dict[str, object] = {
                "attempt": ordinal,
                "physical_attempt_id": physical_id,
                "requested_provider": route["provider"],
                "requested_model": route["model"],
                "input_tokens": 10 if known else 0,
                "output_tokens": 2 if known else 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.01 if known else 0.0,
                "cost_source": "provider_billed" if known else "none",
                "provider_usage": {"physical_attempt_id": physical_id},
            }
            if known:
                row.update(
                    {
                        "provider": route["provider"],
                        "model": route["model"],
                        "usage_unknown": False,
                    }
                )
            else:
                row.update(
                    {
                        "provider": "",
                        "model": "",
                        "usage_unknown": True,
                        "unknown_reason": "transient",
                        "provider_usage": {
                            "physical_attempt_id": physical_id,
                            "usage_unknown": True,
                            "unknown_reason": "transient",
                        },
                    }
                )
            return row

        attempts = [attempt(1, routes[0], known=False), attempt(2, routes[1], known=True)]
        analyzer = {
            "source": "llm_provider",
            "schema_valid": True,
            "fallback_reason": "",
            "provider": routes[1]["provider"],
            "model": routes[1]["model"],
            "normalization_warnings": [],
            "chain": {
                "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
                "configured_routes": routes,
                "attempt_outcomes": [
                    {
                        "candidate_index": 0,
                        **routes[0],
                        "outcome": "failed",
                        "reason": "transient",
                        "physical_request_count": 1,
                    },
                    {
                        "candidate_index": 1,
                        **routes[1],
                        "outcome": "success",
                        "reason": "",
                        "physical_request_count": 1,
                    },
                ],
                "selected_index": 1,
                "exhausted": False,
                "schema_repair_max_retries": 1,
                "deadline": {
                    "configured_seconds": float(expected["total_timeout_seconds"]),
                    "elapsed_seconds": 1.0,
                    "remaining_seconds": float(expected["total_timeout_seconds"]) - 1.0,
                    "expired": False,
                },
            },
            "usage": {
                "attempt_count": 2,
                "usage_unknown_count": 1,
                "input_tokens": 10,
                "output_tokens": 2,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.01,
                "physical_attempts": attempts,
            },
        }

        usage, observed, unknown_count = controller._validated_analyzer_attempt_ledger(
            task_id="task-chain",
            analyzer=analyzer,
            expected_config=expected,
            allow_zero_attempts=False,
        )
        self.assertEqual(usage["attempt_count"], 2)
        self.assertEqual(len(observed), 2)
        self.assertEqual(unknown_count, 1)

        repaired = copy.deepcopy(analyzer)
        repaired_attempts = [
            attempt(1, routes[0], known=False),
            attempt(2, routes[0], known=False),
            attempt(3, routes[1], known=True),
        ]
        repaired["usage"].update(
            {
                "attempt_count": 3,
                "usage_unknown_count": 2,
                "physical_attempts": repaired_attempts,
            }
        )
        repaired["chain"]["attempt_outcomes"][0]["physical_request_count"] = 2
        controller._validated_analyzer_attempt_ledger(
            task_id="task-chain-repair",
            analyzer=repaired,
            expected_config=expected,
            allow_zero_attempts=False,
        )
        repair_disabled = copy.deepcopy(expected)
        repair_disabled["schema_repair_max_retries"] = 0
        repaired["chain"]["schema_repair_max_retries"] = 0
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-chain-repair",
                analyzer=repaired,
                expected_config=repair_disabled,
                allow_zero_attempts=False,
            )

        outside = copy.deepcopy(analyzer)
        outside["usage"]["physical_attempts"][1]["requested_model"] = "other/model"
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-chain",
                analyzer=outside,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        out_of_order = copy.deepcopy(analyzer)
        out_of_order["usage"]["physical_attempts"].reverse()
        for ordinal, row in enumerate(
            out_of_order["usage"]["physical_attempts"], start=1
        ):
            row["attempt"] = ordinal
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-chain",
                analyzer=out_of_order,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        wrong_result = copy.deepcopy(analyzer)
        wrong_result["model"] = routes[0]["model"]
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-chain",
                analyzer=wrong_result,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        undeclared_attempt = copy.deepcopy(analyzer)
        undeclared_attempt.update(
            {
                "source": "router_fallback",
                "schema_valid": False,
                "fallback_reason": "transient",
                "provider": routes[0]["provider"],
                "model": routes[0]["model"],
            }
        )
        undeclared_attempt["chain"].update(
            {
                "attempt_outcomes": [
                    {
                        "candidate_index": 0,
                        **routes[0],
                        "outcome": "failed",
                        "reason": "transient",
                        "physical_request_count": 0,
                    }
                ],
                "selected_index": None,
                "exhausted": True,
            }
        )
        undeclared_attempt["usage"].update(
            {
                "attempt_count": 1,
                "usage_unknown_count": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "billed_cost": 0.0,
                "physical_attempts": [attempt(1, routes[0], known=False)],
            }
        )
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-chain-undeclared-attempt",
                analyzer=undeclared_attempt,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        wrong_exhaustion_reason = copy.deepcopy(undeclared_attempt)
        wrong_exhaustion_reason["chain"]["attempt_outcomes"][0][
            "physical_request_count"
        ] = 1
        wrong_exhaustion_reason["fallback_reason"] = "unsupported"
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-chain-wrong-exhaustion-reason",
                analyzer=wrong_exhaustion_reason,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        unsafe_reason = copy.deepcopy(wrong_exhaustion_reason)
        unsafe_reason["fallback_reason"] = "unsafe reason with spaces"
        unsafe_reason["chain"]["attempt_outcomes"][0]["reason"] = (
            "unsafe reason with spaces"
        )
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-chain-unsafe-reason",
                analyzer=unsafe_reason,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        duplicate_config = copy.deepcopy(expected)
        duplicate_config["fallback_chain"].append(copy.deepcopy(routes[0]))
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-chain",
                analyzer=analyzer,
                expected_config=duplicate_config,
                allow_zero_attempts=False,
            )

    def test_authenticated_analyzer_extract_uses_terminal_physical_attempt(self) -> None:
        task_ids = [f"task-{index}" for index in range(10)]
        ranking_config = ranking_config_resolution()["effective_config"]
        request_context = build_request_context(
            message="controller fallback replay fixture",
            turn_metadata={},
            attachments=[],
            candidate_output_tokens=8192,
            aggregator_output_tokens=8192,
            ranking_config=ranking_config,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            results_path = root / "results.jsonl"
            trace_path = root / "trace.jsonl"
            result_rows = [sealed_result(task_id) for task_id in task_ids]
            results_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in result_rows),
                encoding="utf-8",
            )
            analyzer_config = ranking_config["task_analyzer"]
            analyzer_routes = [
                {
                    key: str(analyzer_config[key])
                    for key in ("provider", "model", "upstream_provider")
                },
                *copy.deepcopy(analyzer_config["fallback_chain"]),
            ]

            def analyzer_attempt(
                *,
                index: int,
                ordinal: int,
                route: dict[str, str],
                output_tokens: int = 0,
            ) -> dict[str, object]:
                physical_id = f"{index * 10 + ordinal + 1:032x}"
                attempt: dict[str, object] = {
                    "attempt": ordinal,
                    "physical_attempt_id": physical_id,
                    "requested_provider": route["provider"],
                    "requested_model": route["model"],
                    "input_tokens": 0,
                    "output_tokens": output_tokens,
                    "reasoning_tokens": 0,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "billed_cost": 0.0,
                    "provider_usage": {"physical_attempt_id": physical_id},
                }
                if output_tokens > 0:
                    attempt.update(
                        {
                            "provider": route["provider"],
                            "model": route["model"],
                            "usage_unknown": False,
                        }
                    )
                else:
                    attempt.update(
                        {
                            "provider": "",
                            "model": "",
                            "usage_unknown": True,
                            "unknown_reason": "transient",
                            "cost_source": "none",
                            "provider_usage": {
                                "physical_attempt_id": physical_id,
                                "usage_unknown": True,
                                "unknown_reason": "transient",
                            },
                        }
                    )
                return attempt

            trace_rows = []
            for index, (task_id, result) in enumerate(zip(task_ids, result_rows)):
                profile = (
                    fallback_task_profile(
                        routed_tier="c1",
                        request_context=request_context,
                        ranking_config=ranking_config,
                    )
                    if index in {0, 1}
                    else {"index": index, "constraints": {"risk": "medium"}}
                )
                selected_route_index = 0 if index >= 2 else 1 if index == 1 else None
                if index == 0:
                    attempts = [
                        analyzer_attempt(
                            index=index,
                            ordinal=ordinal,
                            route=route,
                        )
                        for ordinal, route in enumerate(analyzer_routes, start=1)
                    ]
                    outcomes = [
                        {
                            "candidate_index": route_index,
                            **route,
                            "outcome": "failed",
                            "reason": "transient",
                            "physical_request_count": 1,
                        }
                        for route_index, route in enumerate(analyzer_routes)
                    ]
                elif index == 1:
                    attempts = [
                        analyzer_attempt(
                            index=index,
                            ordinal=1,
                            route=analyzer_routes[0],
                        ),
                        analyzer_attempt(
                            index=index,
                            ordinal=2,
                            route=analyzer_routes[1],
                            output_tokens=100 + index,
                        ),
                    ]
                    outcomes = [
                        {
                            "candidate_index": 0,
                            **analyzer_routes[0],
                            "outcome": "failed",
                            "reason": "transient",
                            "physical_request_count": 1,
                        },
                        {
                            "candidate_index": 1,
                            **analyzer_routes[1],
                            "outcome": "success",
                            "reason": "",
                            "physical_request_count": 1,
                        },
                    ]
                else:
                    attempts = [
                        analyzer_attempt(
                            index=index,
                            ordinal=1,
                            route=analyzer_routes[0],
                        ),
                        analyzer_attempt(
                            index=index,
                            ordinal=2,
                            route=analyzer_routes[0],
                            output_tokens=100 + index,
                        ),
                    ]
                    outcomes = [
                        {
                            "candidate_index": 0,
                            **analyzer_routes[0],
                            "outcome": "success",
                            "reason": "",
                            "physical_request_count": 2,
                        }
                    ]
                result_route = (
                    analyzer_routes[selected_route_index]
                    if selected_route_index is not None
                    else analyzer_routes[-1]
                )
                analyzer = {
                    "source": "llm_provider",
                    "schema_valid": True,
                    "confidence": 0.8,
                    "analyzer_version": "opus-4.8-json-v3",
                    "provider": result_route["provider"],
                    "model": result_route["model"],
                    "fallback_reason": "",
                    "normalization_warnings": [f"warning-{index}"],
                    "chain": {
                        "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
                        "configured_routes": analyzer_routes,
                        "attempt_outcomes": outcomes,
                        "selected_index": selected_route_index,
                        "exhausted": selected_route_index is None,
                        "schema_repair_max_retries": 1,
                        "deadline": {
                            "configured_seconds": float(
                                analyzer_config["total_timeout_seconds"]
                            ),
                            "elapsed_seconds": 1.0,
                            "remaining_seconds": float(
                                analyzer_config["total_timeout_seconds"]
                            )
                            - 1.0,
                            "expired": False,
                        },
                    },
                    "usage": {
                        "attempt_count": len(attempts),
                        "usage_unknown_count": sum(
                            attempt.get("usage_unknown") is True
                            for attempt in attempts
                        ),
                        "input_tokens": 0,
                        "output_tokens": 0 if index == 0 else 100 + index,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.0,
                        "physical_attempts": attempts,
                    },
                }
                if index == 0:
                    analyzer.update(
                        {
                            "source": "router_fallback",
                            "schema_valid": False,
                            "fallback_reason": "transient",
                        }
                    )
                selection = {
                    "task_profile_pre_escalation": profile,
                    "task_analyzer": analyzer,
                    "ranking_parameters": copy.deepcopy(ranking_config),
                    "request_context": copy.deepcopy(request_context),
                    "routed_tier": "c1",
                }
                trace_rows.append(
                    {
                        "task_id": task_id,
                        "group": "G1",
                        "error": None,
                        "task_input_sha256": "sha256:" + f"{index + 201:064x}",
                        "prompt_sha256": f"{index + 301:064x}",
                        "result_evidence_sha256": result["result_evidence_sha256"],
                        "routing_trace": {"selection_plan": selection},
                    }
                )
            trace_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in trace_rows),
                encoding="utf-8",
            )
            audit = {"execution_pass": True, "warnings": ["audit warning retained"]}
            audit["audit_sha256"] = "sha256:" + controller.canonical_sha256(audit)
            proof = {"execution_pass": True, "pass": False, "warnings": ["policy warning"]}
            proof["proof_sha256"] = "sha256:" + controller.canonical_sha256(proof)
            write_json(root / "audit.json", audit)
            write_json(root / "openrouter-non-byok-campaign-proof.json", proof)
            manifest = {
                "status": "complete",
                "audit_sha256": audit["audit_sha256"],
                "openrouter_non_byok_campaign_proof_sha256": proof["proof_sha256"],
                "artifacts": {
                    name: {
                        "path": name,
                        "size_bytes": path.stat().st_size,
                        "sha256": controller.file_sha256(path),
                    }
                    for name, path in (
                        ("results.jsonl", results_path),
                        ("trace.jsonl", trace_path),
                        ("audit.json", root / "audit.json"),
                        (
                            "openrouter-non-byok-campaign-proof.json",
                            root / "openrouter-non-byok-campaign-proof.json",
                        ),
                    )
                },
            }
            manifest["manifest_sha256"] = "sha256:" + controller.canonical_sha256(manifest)
            write_json(root / "manifest.json", manifest)
            source_arm = controller.Arm(
                arm_id="common-E0-source",
                experiment_id="common-E0",
                directory_name="common",
                variant="E0",
                replicate=1,
                analyzer_mode="live",
                override={},
                dynamic=None,
                wire_gate=None,
                output_name="source",
                control_arm_id=None,
            )
            extract_kwargs = {
                "source_arm": source_arm,
                "source_dir": root,
                "destination": root / "artifact.json",
                "expected_task_ids": set(task_ids),
                "snapshot": Path(__file__).resolve().parents[2],
                "snapshot_identity": {"commit": "c", "tree": "t"},
                "plan_sha256": "p",
                "replay_schema": controller.FROZEN_TASK_ANALYSIS_SCHEMA_V2,
            }
            with self.assertRaises(controller.ControllerError):
                controller.extract_analyzer_artifact(
                    **extract_kwargs,
                    allow_deterministic_router_fallback=False,
                )
            v1_kwargs = {
                **extract_kwargs,
                "replay_schema": controller.FROZEN_TASK_ANALYSIS_SCHEMA_V1,
            }
            with self.assertRaises(controller.ControllerError):
                controller.extract_analyzer_artifact(
                    **v1_kwargs,
                    allow_deterministic_router_fallback=True,
                )
            artifact = controller.extract_analyzer_artifact(
                **extract_kwargs,
                allow_deterministic_router_fallback=True,
            )
            observed = sorted(
                row["final_successful_physical_attempt_output_tokens"]
                for row in artifact["profiles"].values()
                if row["origin_outcome"] == "live_success"
            )
            self.assertEqual(observed, list(range(101, 110)))
            replay = artifact["replay_payload"]["entries"]["task-0"]
            self.assertEqual(replay["task_analyzer"]["normalization_warnings"], ["warning-0"])
            self.assertIs(replay["task_analyzer"]["schema_valid"], False)
            self.assertEqual(replay["task_analyzer"]["fallback_reason"], "transient")
            self.assertEqual(replay["origin_outcome"], "deterministic_router_fallback")
            self.assertIn("task_profile_pre_escalation", replay)
            selected_replay = artifact["replay_payload"]["entries"]["task-1"]
            self.assertEqual(
                selected_replay["task_analyzer"]["model"],
                analyzer_routes[1]["model"],
            )
            replay_result = frozen_task_analysis_result(
                artifact["replay_payload"],
                task_id="task-1",
                task_input_sha256=selected_replay["task_input_sha256"],
                prompt_sha256=selected_replay["prompt_sha256"],
                routed_tier="c1",
                request_context=request_context,
                ranking_config=ranking_config,
            )
            self.assertEqual(replay_result.model_id, analyzer_routes[1]["model"])
            self.assertEqual(replay_result.usage, {})
            self.assertEqual(replay_result.replay["physical_request_count"], 0)

            from opensquilla.eval.draco_task_analyzer_execution import (
                task_analyzer_execution_contract_from_g1_registry,
            )

            original_trace_rows = copy.deepcopy(trace_rows)
            original_manifest = copy.deepcopy(manifest)
            live_routes = [
                {**copy.deepcopy(route), "max_attempts": 1}
                for route in analyzer_routes
            ]
            g1_registry_contract = {
                "task_analyzer": copy.deepcopy(analyzer_config),
                "live_task_analyzer_chain": live_routes,
            }
            execution_contract = (
                task_analyzer_execution_contract_from_g1_registry(
                    g1_registry_contract
                )
            )
            for index, trace_row in enumerate(trace_rows):
                selection = trace_row["routing_trace"]["selection_plan"]
                analyzer = selection["task_analyzer"]
                analyzer["chain"]["schema_repair_max_retries"] = 0
                selection["task_analyzer_execution_contract"] = copy.deepcopy(
                    execution_contract
                )
                if index >= 2:
                    terminal_attempt = copy.deepcopy(
                        analyzer["usage"]["physical_attempts"][-1]
                    )
                    terminal_attempt["attempt"] = 1
                    analyzer["usage"].update(
                        {
                            "attempt_count": 1,
                            "usage_unknown_count": 0,
                            "physical_attempts": [terminal_attempt],
                        }
                    )
                    analyzer["chain"]["attempt_outcomes"][0][
                        "physical_request_count"
                    ] = 1
            trace_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in trace_rows),
                encoding="utf-8",
            )
            manifest["artifacts"]["trace.jsonl"].update(
                {
                    "size_bytes": trace_path.stat().st_size,
                    "sha256": controller.file_sha256(trace_path),
                }
            )
            manifest.update(
                {
                    "g1_registry_contract": g1_registry_contract,
                    "task_analyzer_execution_contract": execution_contract,
                    "run_compatibility": {
                        "contracts": {
                            "G1": {
                                "g1_registry_contract": g1_registry_contract,
                                "task_analyzer_execution_contract": execution_contract,
                            }
                        }
                    },
                }
            )
            manifest.pop("manifest_sha256", None)
            manifest["manifest_sha256"] = "sha256:" + controller.canonical_sha256(
                manifest
            )
            write_json(root / "manifest.json", manifest)
            v3_artifact = controller.extract_analyzer_artifact(
                **{
                    **extract_kwargs,
                    "destination": root / "artifact-v3.json",
                    "replay_schema": controller.FROZEN_TASK_ANALYSIS_SCHEMA_V3,
                },
                allow_deterministic_router_fallback=True,
            )
            selected_v3 = v3_artifact["replay_payload"]["entries"]["task-1"]
            v3_result = frozen_task_analysis_result(
                v3_artifact["replay_payload"],
                task_id="task-1",
                task_input_sha256=selected_v3["task_input_sha256"],
                prompt_sha256=selected_v3["prompt_sha256"],
                routed_tier="c1",
                request_context=request_context,
                ranking_config=ranking_config,
            )
            self.assertEqual(v3_result.model_id, analyzer_routes[1]["model"])
            self.assertEqual(v3_result.usage, {})
            self.assertEqual(v3_result.replay["physical_request_count"], 0)
            trace_rows = original_trace_rows
            trace_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in trace_rows),
                encoding="utf-8",
            )
            manifest = original_manifest
            write_json(root / "manifest.json", manifest)

            def isolated_validation(snapshot: Path, **kwargs: object) -> dict[str, object]:
                return {
                    "derived": copy.deepcopy(kwargs["profile"]),
                    "normalized": copy.deepcopy(kwargs["profile"]),
                    "schema_valid": True,
                    "analyzer_version": "opus-4.8-json-v3",
                    "module_path": str(snapshot / "ranking_router.py"),
                    "module_sha256": "a" * 64,
                }

            imported_evidence = {
                "source_snapshot_package_dir": str(root),
                "source_snapshot_commit": "old-commit",
                "source_snapshot_tree": "old-tree",
                "source_output_dir": "/original/source",
                "receipt_sha256": "r" * 64,
            }
            with mock.patch.object(
                controller,
                "validate_fallback_profile_isolated",
                side_effect=isolated_validation,
            ) as isolated:
                controller.extract_analyzer_artifact(
                    **{
                        **extract_kwargs,
                        "destination": root / "imported-artifact.json",
                    },
                    allow_deterministic_router_fallback=True,
                    source_import_evidence=imported_evidence,
                )
            self.assertEqual(isolated.call_count, 2)
            self.assertEqual(Path(isolated.call_args_list[0].args[0]), root)
            self.assertEqual(
                Path(isolated.call_args_list[1].args[0]),
                extract_kwargs["snapshot"],
            )
            receipt = controller.derive_analyzer_p99_receipt(
                artifact,
                destination=root / "p99.json",
                plan_sha256="p",
            )
            self.assertEqual(receipt["ordered_output_tokens"], list(range(101, 110)))
            self.assertEqual(receipt["eligibility"]["eligible_denominator"], 9)
            self.assertEqual(receipt["eligibility"]["excluded_denominator"], 1)
            self.assertEqual(receipt["eligibility"]["excluded_task_ids"], ["task-0"])

    def test_analyzer_p99_requires_eight_live_observations(self) -> None:
        profiles = {
            f"task-{index}": {
                "origin_outcome": (
                    "live_success" if index < 7 else "deterministic_router_fallback"
                ),
                "final_successful_physical_attempt_output_tokens": 100 + index,
            }
            for index in range(10)
        }
        artifact = {"artifact_sha256": "a" * 64, "profiles": profiles}
        with tempfile.TemporaryDirectory() as raw, self.assertRaises(controller.ControllerError):
            controller.derive_analyzer_p99_receipt(
                artifact,
                destination=Path(raw) / "p99.json",
                plan_sha256="p",
            )

    def test_result_and_document_hashes_fail_closed(self) -> None:
        row = sealed_result("task")
        controller.verify_result_row_evidence(row)
        row["group"] = "B0"
        with self.assertRaises(controller.ControllerError):
            controller.verify_result_row_evidence(row)
        document = {"status": "complete"}
        document["manifest_sha256"] = "sha256:" + controller.canonical_sha256(document)
        controller.verify_document_self_hash(document, field="manifest_sha256", label="manifest")
        document["status"] = "failed"
        with self.assertRaises(controller.ControllerError):
            controller.verify_document_self_hash(
                document, field="manifest_sha256", label="manifest"
            )

    def test_behavior_compare_is_task_exact(self) -> None:
        baseline = {"a": {"selected_P": ["p"], "selected_A": "a"}}
        same = controller.compare_behavior_projections(baseline, copy.deepcopy(baseline))
        self.assertEqual(same["changed_task_count"], 0)
        changed = controller.compare_behavior_projections(
            baseline, {"a": {"selected_P": ["q"], "selected_A": "a"}}
        )
        self.assertEqual(changed["changed_task_count"], 1)

    def test_candidate_order_seed_is_request_visible_behavior(self) -> None:
        baseline = controller._candidate_order_seed_projection(
            SimpleNamespace(candidate_order_seed=0, shuffle_candidates=True),
            {
                "configured_candidate_order_seed": 0,
                "effective_candidate_order_seed": 0,
            },
            task_id="task",
        )
        candidate = controller._candidate_order_seed_projection(
            SimpleNamespace(candidate_order_seed=1, shuffle_candidates=True),
            {
                "configured_candidate_order_seed": 1,
                "effective_candidate_order_seed": 1,
            },
            task_id="task",
        )
        comparison = controller.compare_behavior_projections(
            {"task": baseline},
            {"task": candidate},
        )
        self.assertEqual(comparison["changed_task_count"], 1)
        with self.assertRaises(controller.ControllerError):
            controller._candidate_order_seed_projection(
                SimpleNamespace(candidate_order_seed=4, shuffle_candidates=True),
                {
                    "configured_candidate_order_seed": 4,
                    "effective_candidate_order_seed": 0,
                },
                task_id="task",
            )

    def test_candidate_order_seed_execution_evidence_gate(self) -> None:
        expected = {
            "required": True,
            "configured_candidate_order_seed": 0,
            "effective_candidate_order_seed": 0,
        }
        aggregate_call = {
            "final_request_role": "aggregator",
            "total_candidates": 3,
            "selected_candidate_count": 3,
            "shuffle_candidates": True,
            "configured_candidate_order_seed": 0,
            "candidate_order_seed": 0,
            "candidate_order_seed_source": "configured",
            "candidate_display_order": [0, 2, 1],
            "selection_plan": {
                "configured_candidate_order_seed": 0,
                "effective_candidate_order_seed": 0,
            },
            "candidates": [
                {"index": index, "selected_for_aggregation": True} for index in range(3)
            ],
        }
        rows = [
            {
                "task_id": "task",
                "ensemble_trace": {"mode": "agent_loop", "calls": [aggregate_call]},
            }
        ]
        matched = controller.candidate_order_seed_execution_evidence(
            rows,
            expected=expected,
        )
        self.assertTrue(matched["pass"])
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(matched["aggregation_call_count"], 1)

        wrong_seed = copy.deepcopy(rows)
        wrong_seed[0]["ensemble_trace"]["calls"][0]["candidate_order_seed"] = 1
        mismatched = controller.candidate_order_seed_execution_evidence(
            wrong_seed,
            expected=expected,
        )
        self.assertFalse(mismatched["pass"])
        self.assertIn(
            "candidate_order_seed",
            mismatched["failures"][0]["fields"],
        )

        pre_aggregation = copy.deepcopy(rows)
        pre_aggregation[0]["ensemble_trace"]["calls"] = [
            {
                "final_request_role": "fallback_single",
                "total_candidates": 3,
                "fallback_used": True,
            }
        ]
        not_applicable = controller.candidate_order_seed_execution_evidence(
            pre_aggregation,
            expected=expected,
        )
        self.assertTrue(not_applicable["pass"])
        self.assertEqual(not_applicable["status"], "not_applicable")
        self.assertEqual(not_applicable["aggregation_call_count"], 0)

    def test_aggregator_prompt_evidence_is_authoritative_with_optional_ranking_version(
        self,
    ) -> None:
        def prompt_evidence(version: str) -> dict[str, object]:
            payload: dict[str, object] = {
                "schema": controller.AGGREGATOR_PROMPT_SCHEMA,
                "version": version,
                "description": f"prompt contract for {version}",
                "additional_instructions": [],
            }
            return {**payload, "sha256": controller.canonical_sha256(payload)}

        version = "aggregator-v1-current"
        evidence = prompt_evidence(version)
        helpers = {"aggregator_prompt_version_evidence": prompt_evidence}

        without_ranking_version = {
            "aggregator_prompt": copy.deepcopy(evidence),
            "ranking_parameters": {"aggregator": {"candidate_count": 3}},
        }
        self.assertEqual(
            controller._validated_aggregator_prompt(
                without_ranking_version,
                helpers=helpers,
            ),
            evidence,
        )

        with_matching_ranking_version = copy.deepcopy(without_ranking_version)
        with_matching_ranking_version["ranking_parameters"]["aggregator"][
            "prompt_version"
        ] = version
        self.assertEqual(
            controller._validated_aggregator_prompt(
                with_matching_ranking_version,
                helpers=helpers,
            ),
            evidence,
        )

        with_conflicting_ranking_version = copy.deepcopy(without_ranking_version)
        with_conflicting_ranking_version["ranking_parameters"]["aggregator"][
            "prompt_version"
        ] = "aggregator-v2-verify-first"
        with self.assertRaisesRegex(
            controller.ControllerError,
            "differs from ranking parameters",
        ):
            controller._validated_aggregator_prompt(
                with_conflicting_ranking_version,
                helpers=helpers,
            )

    def test_selection_projection_rejects_duplicate_or_mismatched_proposers(self) -> None:
        base = {
            "selected_P": ["openrouter:model-a", "openrouter:model-b"],
            "N_min": 2,
            "N_max": 3,
            "proposer_count": 2,
        }
        invalid = []
        duplicate = copy.deepcopy(base)
        duplicate["selected_P"][1] = "OPENROUTER:MODEL-A"
        invalid.append(duplicate)
        mismatched = copy.deepcopy(base)
        mismatched["proposer_count"] = 3
        invalid.append(mismatched)
        outside_range = copy.deepcopy(base)
        outside_range["N_min"] = 3
        outside_range["N_max"] = 4
        invalid.append(outside_range)
        for selection in invalid:
            with self.subTest(selection=selection):
                with self.assertRaises(controller.ControllerError):
                    controller.request_visible_selection_projection(
                        snapshot=Path("/not-used"),
                        config=None,
                        selections={"task": selection},
                        max_tokens_cap_explicit=False,
                    )

    def test_noop_requires_all_ten_byte_identical_and_uncertainty_runs(self) -> None:
        baseline = {f"task-{index}": {"value": index} for index in range(10)}
        same = controller.compare_behavior_projections(
            baseline,
            copy.deepcopy(baseline),
            expected_task_count=10,
        )
        self.assertTrue(same["all_tasks_byte_identical"])
        self.assertEqual(
            controller.offline_effect_decision(
                comparisons={"actual": same},
                arm_ids=["P0.5-10-E1"],
                budget_gated=True,
                production_budget_projection_complete=True,
                projection_uncertain=False,
            ),
            "deleted_no_live_run",
        )
        self.assertEqual(
            controller.offline_effect_decision(
                comparisons={},
                arm_ids=["P0.5-10-E1"],
                budget_gated=True,
                production_budget_projection_complete=False,
                projection_uncertain=True,
            ),
            "run_conservative_projection_uncertain",
        )
        self.assertEqual(
            controller.offline_effect_decision(
                comparisons={"actual": same},
                arm_ids=["common-E0-R1"],
                budget_gated=False,
                production_budget_projection_complete=True,
                projection_uncertain=False,
            ),
            "run_required_replicate",
        )

    def test_plan_freezes_57_unique_replay_overlays_and_registry_identities(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        replay_overlays = {
            controller.canonical_sha256(arm.override)
            for arm in arms
            if arm.analyzer_mode == "frozen_replay"
        }
        self.assertEqual(len(replay_overlays), 57)
        self.assertEqual(controller.EXPECTED_OFFLINE_UNIQUE_REPLAY_OVERLAYS, 57)
        controller.validate_offline_unique_replay_overlay_count(
            {str(index): {} for index in range(57)}
        )
        with self.assertRaises(controller.ControllerError):
            controller.validate_offline_unique_replay_overlay_count(
                {str(index): {} for index in range(56)}
            )
        registry = plan["freeze"]["model_registry"]
        self.assertEqual(registry["full_model_count"], 79)
        self.assertEqual(registry["formal_model_count"], 79)

    def test_status_freezes_schedule_hash_ordinals_and_anchors(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        status = controller.initialize_status(
            plan,
            arms,
            plan_sha256=controller.canonical_sha256(plan),
            snapshot_identity={"commit": "c", "tree": "t"},
        )
        schedule = plan["execution"]["schedule"]
        self.assertEqual(status["schedule_sha256"], controller.canonical_sha256(schedule))
        self.assertIs(status["strict_task_interleaving"], False)
        for ordinal, arm in enumerate(arms, start=1):
            state = status["arms"][arm.arm_id]
            self.assertEqual(state["schedule_ordinal"], ordinal)
            self.assertEqual(
                state["anchor_arm_id"],
                schedule["anchor_by_arm_id"][arm.arm_id],
            )

    def test_source_selection_plans_materializes_bound_compact_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = write_compact_dry_bundle(Path(raw))
            artifact_evidence: dict[str, str] = {}

            observed = controller._source_selection_plans(
                bundle.trace,
                terminal_manifest_path=bundle.manifest,
                expected_task_ids=set(bundle.plans),
                require_dry_replay=True,
                artifact_evidence_out=artifact_evidence,
            )

            self.assertEqual(observed, bundle.plans)
            self.assertEqual(
                artifact_evidence,
                {
                    "trace_raw_sha256": controller.file_sha256(bundle.trace),
                    "manifest_raw_sha256": controller.file_sha256(bundle.manifest),
                },
            )
            for selection in observed.values():
                self.assertEqual(selection["task_analyzer"]["source"], "frozen_replay")
                self.assertFalse(
                    plan_evidence.selection_plan_reference_signal(selection)
                )
                self.assertFalse(
                    plan_evidence.selection_plan_row_capability_signal(selection)
                )

    def test_source_selection_plans_accepts_bound_standard_inline_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = write_inline_dry_bundle(Path(raw))
            artifact_evidence: dict[str, str] = {}

            observed = controller._source_selection_plans(
                bundle.trace,
                terminal_manifest_path=bundle.manifest,
                expected_task_ids=set(bundle.plans),
                require_dry_replay=True,
                artifact_evidence_out=artifact_evidence,
            )

            self.assertEqual(observed, bundle.plans)
            self.assertEqual(
                artifact_evidence,
                {
                    "trace_raw_sha256": controller.file_sha256(bundle.trace),
                    "manifest_raw_sha256": controller.file_sha256(bundle.manifest),
                },
            )

    def test_source_selection_plans_rejects_legacy_manifest_compact_signal_race(
        self,
    ) -> None:
        from opensquilla.eval.draco_resume_source_index import ResumeSourceIndex

        for mutation in ("partial_capability", "coherent_zero_ref_binding"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                bundle = write_inline_dry_bundle(Path(raw))
                manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
                if mutation == "partial_capability":
                    manifest[plan_evidence.SELECTION_PLAN_EVIDENCE_ROW_FIELD] = (
                        plan_evidence.selection_plan_evidence_capability_contract()
                    )
                else:
                    pack_path = bundle.manifest.with_name(
                        "draco_run_20260811-230000.selection-plan.pack.jsonl"
                    )
                    with plan_evidence.SelectionPlanPackAppender(pack_path):
                        pass
                    verification = artifact_io.verify_durable_draco_artifacts(
                        results_path=bundle.results,
                        trace_path=bundle.trace,
                        checkpoint_path=bundle.checkpoint,
                    )
                    with plan_evidence.SelectionPlanPackReader(
                        pack_path,
                        owner_only=True,
                    ) as reader:
                        binding = plan_evidence.selection_plan_evidence_manifest_binding(
                            pack_index=reader.index,
                            durable_artifact_verification=verification,
                            compact_row_count=0,
                        )
                    manifest["artifacts"][
                        plan_evidence.SELECTION_PLAN_PACK_ARTIFACT_FIELD
                    ] = str(pack_path)
                    manifest[plan_evidence.SELECTION_PLAN_EVIDENCE_ROW_FIELD] = (
                        plan_evidence.selection_plan_evidence_capability_contract()
                    )
                    manifest[
                        plan_evidence.SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD
                    ] = binding
                original = ResumeSourceIndex.source_artifact_evidence
                replaced = False

                def replace_manifest(
                    index: ResumeSourceIndex,
                    *,
                    source_index: int,
                ) -> dict[str, object]:
                    nonlocal replaced
                    evidence = original(index, source_index=source_index)
                    if not replaced:
                        write_json(bundle.manifest, manifest)
                        replaced = True
                    return evidence

                artifact_evidence: dict[str, str] = {}
                with (
                    mock.patch.object(
                        ResumeSourceIndex,
                        "source_artifact_evidence",
                        replace_manifest,
                    ),
                    self.assertRaisesRegex(
                        controller.ControllerError,
                        "undeclared compact manifest evidence",
                    ),
                ):
                    controller._source_selection_plans(
                        bundle.trace,
                        terminal_manifest_path=bundle.manifest,
                        expected_task_ids=set(bundle.plans),
                        require_dry_replay=True,
                        artifact_evidence_out=artifact_evidence,
                    )
                self.assertTrue(replaced)
                self.assertEqual(artifact_evidence, {})

    def test_dry_run_artifact_discovery_does_not_open_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            trace = root / "draco_run_20260811-230000.trace.jsonl"
            manifest = root / "draco_run_20260811-230000.manifest.json"
            trace.write_text("not-json", encoding="utf-8")
            manifest.write_text("not-json", encoding="utf-8")

            with (
                mock.patch.object(
                    controller,
                    "require_regular_file",
                    side_effect=AssertionError("discovery opened trace"),
                ),
                mock.patch.object(
                    controller,
                    "load_json",
                    side_effect=AssertionError("discovery opened manifest"),
                ),
            ):
                self.assertEqual(
                    controller._dry_run_output_artifacts(root),
                    (trace, manifest),
                )

    def test_source_selection_plans_keeps_inline_and_rejects_undeclared_ref(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            trace = root / "formal-trace.jsonl"
            selection = {
                "task_analyzer": {
                    "source": "frozen_replay",
                    "usage": {},
                    "replay": {"physical_request_count": 0},
                }
            }
            inline_row = {
                "task_id": "task-0",
                "group": "G1",
                "routing_trace": {
                    "dry_run": True,
                    "selection_plan": selection,
                },
            }
            trace.write_text(json.dumps(inline_row) + "\n", encoding="utf-8")
            self.assertEqual(
                controller._source_selection_plans(
                    trace,
                    expected_task_ids={"task-0"},
                    require_dry_replay=True,
                ),
                {"task-0": selection},
            )

            inline_row["routing_trace"]["selection_plan"] = {
                "schema": plan_evidence.SELECTION_PLAN_REF_SCHEMA,
            }
            trace.write_text(json.dumps(inline_row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                controller.ControllerError,
                "undeclared compact selection-plan evidence",
            ):
                controller._source_selection_plans(
                    trace,
                    expected_task_ids={"task-0"},
                    require_dry_replay=True,
                )

    def test_source_selection_plans_rejects_compact_bundle_corruption(self) -> None:
        for mutation in (
            "missing_pack",
            "tampered_pack",
            "downgrade",
            "manifest_not_dry",
            "manifest_incomplete",
            "trace_ref_substitution",
            "trace_ahead",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                bundle = write_compact_dry_bundle(Path(raw))
                if mutation == "missing_pack":
                    bundle.pack.unlink()
                elif mutation == "tampered_pack":
                    payload = bytearray(bundle.pack.read_bytes())
                    payload[-2] ^= 1
                    bundle.pack.write_bytes(payload)
                elif mutation == "downgrade":
                    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
                    manifest.pop(plan_evidence.SELECTION_PLAN_EVIDENCE_ROW_FIELD)
                    manifest.pop(plan_evidence.SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD)
                    manifest["artifacts"].pop(
                        plan_evidence.SELECTION_PLAN_PACK_ARTIFACT_FIELD
                    )
                    write_json(bundle.manifest, manifest)
                elif mutation in {"manifest_not_dry", "manifest_incomplete"}:
                    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
                    if mutation == "manifest_not_dry":
                        manifest["dry_run"] = False
                    else:
                        manifest["status"] = "result_incomplete"
                    write_json(bundle.manifest, manifest)
                elif mutation == "trace_ref_substitution":
                    trace_rows = [
                        json.loads(line)
                        for line in bundle.trace.read_text(encoding="utf-8").splitlines()
                    ]
                    trace_rows[0]["routing_trace"]["selection_plan"] = copy.deepcopy(
                        trace_rows[1]["routing_trace"]["selection_plan"]
                    )
                    bundle.trace.write_text(
                        "".join(json.dumps(row) + "\n" for row in trace_rows),
                        encoding="utf-8",
                    )
                else:
                    bundle.trace.write_bytes(bundle.trace.read_bytes() + b" ")

                with self.assertRaises(controller.ControllerError):
                    controller._source_selection_plans(
                        bundle.trace,
                        terminal_manifest_path=bundle.manifest,
                        expected_task_ids=set(bundle.plans),
                        require_dry_replay=True,
                    )

    def test_source_selection_plans_rejects_manifest_replacement_after_bind(self) -> None:
        from opensquilla.eval.draco_resume_source_index import ResumeSourceIndex

        with tempfile.TemporaryDirectory() as raw:
            bundle = write_compact_dry_bundle(Path(raw))
            original = ResumeSourceIndex.source_artifact_evidence
            replaced = False

            def replace_manifest(
                index: ResumeSourceIndex,
                *,
                source_index: int,
            ) -> dict[str, object]:
                nonlocal replaced
                result = original(index, source_index=source_index)
                if not replaced:
                    replacement = bundle.manifest.with_name("replacement.manifest.json")
                    replacement.write_bytes(bundle.manifest.read_bytes())
                    os.replace(replacement, bundle.manifest)
                    replaced = True
                return result

            with (
                mock.patch.object(
                    ResumeSourceIndex,
                    "source_artifact_evidence",
                    replace_manifest,
                ),
                self.assertRaises(controller.ControllerError),
            ):
                controller._source_selection_plans(
                    bundle.trace,
                    terminal_manifest_path=bundle.manifest,
                    expected_task_ids=set(bundle.plans),
                    require_dry_replay=True,
                )
            self.assertTrue(replaced)

    def test_source_selection_plans_rejects_coherent_bundle_replacement(self) -> None:
        from opensquilla.eval.draco_resume_source_index import ResumeSourceIndex

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = write_compact_dry_bundle(root / "target", plan_marker="A")
            replacement = write_compact_dry_bundle(
                root / "replacement",
                plan_marker="B",
            )
            replacement_manifest = json.loads(
                replacement.manifest.read_text(encoding="utf-8")
            )
            for key, path in (
                ("results_jsonl", target.results),
                ("trace_jsonl", target.trace),
                ("checkpoint_json", target.checkpoint),
                ("manifest_json", target.manifest),
                (plan_evidence.SELECTION_PLAN_PACK_ARTIFACT_FIELD, target.pack),
            ):
                replacement_manifest["artifacts"][key] = str(path)
            write_json(replacement.manifest, replacement_manifest)
            original = ResumeSourceIndex.source_artifact_evidence
            replaced = False

            def replace_bundle(
                index: ResumeSourceIndex,
                *,
                source_index: int,
            ) -> dict[str, object]:
                nonlocal replaced
                if not replaced:
                    for source, destination in (
                        (replacement.pack, target.pack),
                        (replacement.trace, target.trace),
                        (replacement.checkpoint, target.checkpoint),
                        (replacement.results, target.results),
                        (replacement.manifest, target.manifest),
                    ):
                        os.replace(source, destination)
                    replaced = True
                return original(index, source_index=source_index)

            with (
                mock.patch.object(
                    ResumeSourceIndex,
                    "source_artifact_evidence",
                    replace_bundle,
                ),
                self.assertRaises(controller.ControllerError),
            ):
                controller._source_selection_plans(
                    target.trace,
                    terminal_manifest_path=target.manifest,
                    expected_task_ids=set(target.plans),
                    require_dry_replay=True,
                )
            self.assertTrue(replaced)

    def test_source_selection_plans_rejects_compact_to_legacy_replacement(self) -> None:
        from opensquilla.eval.draco_resume_source_index import ResumeSourceIndex

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = write_compact_dry_bundle(root / "target", plan_marker="A")
            replacement = write_inline_dry_bundle(
                root / "replacement",
                plan_marker="legacy-B",
            )
            replacement_manifest = json.loads(
                replacement.manifest.read_text(encoding="utf-8")
            )
            for key, path in (
                ("results_jsonl", target.results),
                ("trace_jsonl", target.trace),
                ("checkpoint_json", target.checkpoint),
                ("manifest_json", target.manifest),
            ):
                replacement_manifest["artifacts"][key] = str(path)
            write_json(replacement.manifest, replacement_manifest)
            original = ResumeSourceIndex.source_artifact_evidence
            replaced = False

            def replace_bundle(
                index: ResumeSourceIndex,
                *,
                source_index: int,
            ) -> dict[str, object]:
                nonlocal replaced
                if not replaced:
                    target.pack.unlink()
                    for source, destination in (
                        (replacement.trace, target.trace),
                        (replacement.checkpoint, target.checkpoint),
                        (replacement.results, target.results),
                        (replacement.manifest, target.manifest),
                    ):
                        os.replace(source, destination)
                    replaced = True
                return original(index, source_index=source_index)

            with (
                mock.patch.object(
                    ResumeSourceIndex,
                    "source_artifact_evidence",
                    replace_bundle,
                ),
                self.assertRaises(controller.ControllerError),
            ):
                controller._source_selection_plans(
                    target.trace,
                    terminal_manifest_path=target.manifest,
                    expected_task_ids=set(target.plans),
                    require_dry_replay=True,
                )
            self.assertTrue(replaced)

    def test_source_selection_plans_rejects_read_then_restore_manifest_substitution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = write_compact_dry_bundle(root / "target", plan_marker="A")
            replacement = write_compact_dry_bundle(
                root / "replacement",
                plan_marker="B",
            )
            replacement_manifest = json.loads(
                replacement.manifest.read_text(encoding="utf-8")
            )
            for key, path in (
                ("results_jsonl", target.results),
                ("trace_jsonl", target.trace),
                ("checkpoint_json", target.checkpoint),
                ("manifest_json", target.manifest),
                (plan_evidence.SELECTION_PLAN_PACK_ARTIFACT_FIELD, target.pack),
            ):
                replacement_manifest["artifacts"][key] = str(path)
            write_json(replacement.manifest, replacement_manifest)
            original = controller._read_terminal_file_snapshot
            substituted = False

            def read_substituted_manifest(
                path: Path,
                *,
                label: str,
                max_bytes: int | None = None,
            ) -> tuple[bytes, tuple[int, ...], str]:
                nonlocal substituted
                if path == target.manifest and not substituted:
                    backup = target.manifest.with_name("bound-A.manifest.json")
                    os.replace(target.manifest, backup)
                    os.replace(replacement.manifest, target.manifest)
                    try:
                        observed = original(
                            path,
                            label=label,
                            max_bytes=max_bytes,
                        )
                    finally:
                        os.replace(target.manifest, replacement.manifest)
                        os.replace(backup, target.manifest)
                    substituted = True
                    return observed
                return original(path, label=label, max_bytes=max_bytes)

            with (
                mock.patch.object(
                    controller,
                    "_read_terminal_file_snapshot",
                    read_substituted_manifest,
                ),
                self.assertRaises(controller.ControllerError),
            ):
                controller._source_selection_plans(
                    target.trace,
                    terminal_manifest_path=target.manifest,
                    expected_task_ids=set(target.plans),
                    require_dry_replay=True,
                )
            self.assertTrue(substituted)

    def test_main_dry_replay_blanks_network_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            snapshot = root / "snapshot"
            (snapshot / "scripts").mkdir(parents=True)
            runner = snapshot / "scripts/run_draco_routing_experiment.py"
            runner.write_text("# offline fixture\n", encoding="utf-8")
            reference = root / "reference"
            (reference / "data/draco").mkdir(parents=True)
            (reference / ".local-state").mkdir(parents=True)
            plan = {
                "paths": {
                    "run_root": str(root / "run"),
                    "reference_repo": str(reference),
                    "python": "python3",
                    "experiment_config_relative": "config.json",
                }
            }
            captured: dict[str, object] = {}

            def fake_run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                captured["command"] = command
                captured["env"] = kwargs["env"]
                return subprocess.CompletedProcess(command, 0, "", "")

            fake_trace = root / "trace.jsonl"
            fake_manifest = root / "manifest.json"
            fake_trace.write_text("{}\n", encoding="utf-8")
            fake_manifest.write_text("{}\n", encoding="utf-8")
            plans = {f"task-{index}": {} for index in range(10)}

            def fake_source_selection_plans(
                _trace_path: Path,
                **kwargs: object,
            ) -> dict[str, dict[str, object]]:
                artifact_evidence = kwargs["artifact_evidence_out"]
                assert isinstance(artifact_evidence, dict)
                artifact_evidence.update(
                    {
                        "trace_raw_sha256": "a" * 64,
                        "manifest_raw_sha256": "b" * 64,
                    }
                )
                return plans

            with (
                mock.patch.object(controller, "validate_runtime_freeze"),
                mock.patch.object(controller.subprocess, "run", side_effect=fake_run),
                mock.patch.object(
                    controller,
                    "_dry_run_output_artifacts",
                    return_value=(fake_trace, fake_manifest),
                ),
                mock.patch.object(
                    controller,
                    "_source_selection_plans",
                    side_effect=fake_source_selection_plans,
                ),
                mock.patch.object(
                    controller,
                    "file_sha256",
                    side_effect=AssertionError("run_main performed a post-close hash"),
                ),
            ):
                observed, evidence = controller.run_main_dry_replay(
                    plan,
                    snapshot=snapshot,
                    snapshot_identity={"commit": "c", "tree": "t", "status": ""},
                    overlay={},
                    expected_task_ids=set(plans),
                    label="fixture",
                )
            self.assertEqual(set(observed), set(plans))
            self.assertEqual(evidence["trace_raw_sha256"], "a" * 64)
            self.assertEqual(evidence["manifest_raw_sha256"], "b" * 64)
            command = captured["command"]
            self.assertIn("--dry-run", command)
            env = captured["env"]
            self.assertEqual(env["BRAVE_SEARCH_API_KEY"], "")
            self.assertEqual(env["FIRECRAWL_API_KEY"], "")
            self.assertTrue(env["OPENROUTER_API_KEY"].startswith("sk-or-v1-"))
            self.assertIn("zero Analyzer/provider/Judge calls", evidence["network_contract"])

    def test_publication_identity_accepts_bound_main_then_resume_wave(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = root / "arm"
            snapshot = root / "snapshot"
            main_runner = snapshot / "scripts/run_draco_routing_experiment.py"
            resume_runner = snapshot / "scripts/run_draco_routing_experiment_resume.py"
            benchmark = root / "reference/data/draco/mini.jsonl"
            reference_config = root / "reference/.local-state/config.toml"
            effective = {
                "groups": {"G1": {"enabled": True}},
                "judge": {"concurrency": 6},
                "generation": {"max_attempts": 3},
            }
            task_ids = [f"task-{index}" for index in range(10)]
            expected = {
                "arm_id": "P0.5-11-E1-R1",
                "output_name": "fixture",
                "run_id": "run",
                "output_dir": str(directory),
                "snapshot": str(snapshot),
                "snapshot_commit": "commit",
                "runner_identities": {
                    str(main_runner.resolve()): "main-sha",
                    str(resume_runner.resolve()): "resume-sha",
                },
                "benchmark_path": str(benchmark),
                "reference_config_path": str(reference_config),
                "benchmark_sha256": "benchmark-sha",
                "task_ids": task_ids,
                "task_concurrency": 6,
                "judge_concurrency": 6,
                "generation_max_attempts": 3,
                "override_sha256": "override-sha",
                "effective_config_sha256": controller.canonical_sha256(effective),
            }
            bindings: list[dict[str, object]] = []
            for index, runner in enumerate((main_runner, resume_runner)):
                wave = directory / f"wave-{index + 1}"
                wave.mkdir(parents=True)
                effective_path = wave / "experiment-config.effective.json"
                result_path = wave / "results.jsonl"
                source_path = wave / "manifest.json"
                write_json(effective_path, effective)
                result_path.write_text("{}\n", encoding="utf-8")
                source = {
                    "args": {
                        "input": str(benchmark),
                        "config": str(reference_config),
                        "groups": "G1",
                        "max_tasks": 10,
                        "concurrency": 6,
                        "judge_concurrency": 6,
                        "generation_max_attempts": 3,
                        "dry_run": False,
                        "require_openrouter_non_byok": True,
                        "require_clean_source": True,
                        "output_dir": str(wave),
                    },
                    "command": {"cwd": str(snapshot)},
                    "source_provenance": {
                        "git_head": "commit",
                        "git_dirty": False,
                        "git_tracked_dirty": False,
                        "runner_path": str(runner),
                        "runner_sha256": ("main-sha" if index == 0 else "resume-sha"),
                    },
                    "benchmark_input_validation": {
                        "actual_sha256": "benchmark-sha",
                        "actual_task_count": 10,
                        "task_ids_match": True,
                        "status": "matched",
                    },
                    "artifacts": {"experiment_config_effective_json": str(effective_path)},
                }
                write_json(source_path, source)
                binding: dict[str, object] = {
                    "path": str(source_path),
                    "sha256": controller.file_sha256(source_path),
                    "result_path": str(result_path),
                    "result_sha256": controller.file_sha256(result_path),
                    "resume_schedule_contract_verified": index == 1,
                    "resume_scheduled_pairs": [],
                }
                if index == 1:
                    binding["resume_scheduled_pairs"] = [
                        {
                            "group": "G1",
                            "task_id": task_ids[0],
                            "action": "judge_only",
                        }
                    ]
                bindings.append(binding)
            evidence = controller.verify_arm_publication_identity(
                directory,
                {"source_manifests": bindings},
                expected=expected,
            )
            self.assertEqual(
                [row["runner_kind"] for row in evidence["source_manifests"]],
                ["main", "resume"],
            )
            source = controller.load_json(Path(str(bindings[1]["path"])))
            source["args"]["generation_max_attempts"] = 2
            write_json(Path(str(bindings[1]["path"])), source)
            bindings[1]["sha256"] = controller.file_sha256(Path(str(bindings[1]["path"])))
            with self.assertRaises(controller.ControllerError):
                controller.verify_arm_publication_identity(
                    directory,
                    {"source_manifests": bindings},
                    expected=expected,
                )
            source["args"]["generation_max_attempts"] = 3
            write_json(Path(str(bindings[1]["path"])), source)
            bindings[1]["sha256"] = controller.file_sha256(Path(str(bindings[1]["path"])))
            bindings[1]["resume_scheduled_pairs"] = [
                {"group": "G2", "task_id": task_ids[0], "action": "judge_only"}
            ]
            with self.assertRaises(controller.ControllerError):
                controller.verify_arm_publication_identity(
                    directory,
                    {"source_manifests": bindings},
                    expected=expected,
                )

    def test_terminal_status_is_immutable_and_report_failure_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = {
                "schema": controller.STATUS_SCHEMA,
                "run_id": "run",
                "campaign_plan_sha256": "plan-sha",
                "phase": "succeeded",
                "arms": {
                    "arm": {"state": "succeeded"},
                    "deleted": {"state": "no_op_deleted"},
                },
                "no_op_experiments": {"P0.5-07": {"state": "no_op_deleted"}},
                "reporting": {"mutable": True},
                "terminal_status_input": {"mutable": True},
            }
            self.assertEqual(controller.campaign_terminal_phase(status), "succeeded")
            self.assertEqual(
                controller.campaign_terminal_phase(
                    status,
                    reporting_complete=False,
                ),
                "completed_with_failures",
            )
            descriptor = controller.publish_terminal_status_input(
                {"paths": {"run_root": str(root)}},
                status,
            )
            frozen = controller.load_json(Path(descriptor["path"]))
            controller.verify_bare_document_self_hash(
                frozen,
                field="terminal_status_input_sha256",
                label="terminal status input",
            )
            self.assertNotIn("reporting", frozen)
            self.assertNotIn("terminal_status_input", frozen)
            self.assertEqual(
                descriptor["semantic_sha256"],
                frozen["terminal_status_input_sha256"],
            )
            self.assertEqual(
                descriptor["file_sha256"],
                controller.file_sha256(Path(descriptor["path"])),
            )


if __name__ == "__main__":
    unittest.main()
