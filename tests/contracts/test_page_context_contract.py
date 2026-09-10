"""Page annotations travel as ordinary bounded message context."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import GATEWAY_METHOD_CONTRACTS
from scripts.contracts.generate_gateway_contracts import discover_contracts

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads(
    (ROOT / "contracts/gateway/v4/conversation/fixtures/page-context.json").read_text()
)["cases"]
MODULES = (
    "chat_send", "sessions_send", "sessions_pending_inputs_enqueue",
    "sessions_pending_inputs_list", "chat_history",
)
RETIRED = {
    "documents.editSessions.start", "documents.editSessions.heartbeat",
    "documents.editSessions.close", "artifacts.prompt_annotations.create",
    "artifacts.prompt_annotations.focus", "artifacts.prompt_annotations.update",
    "artifacts.prompt_annotations.discard", "artifacts.source.patch",
}


@pytest.mark.parametrize("module_name", MODULES)
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_page_context_preserves_user_annotations(module_name: str, case: dict) -> None:
    module = importlib.import_module(f"opensquilla.contracts.generated.v4.{module_name}")
    if not case["valid"]:
        with pytest.raises(ValidationError):
            module.PageContext.model_validate(case["context"])
        return
    parsed = module.PageContext.model_validate(case["context"])
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == case["context"]


def test_retired_editing_contracts_are_absent() -> None:
    specs = discover_contracts()
    assert RETIRED.isdisjoint(GATEWAY_METHOD_CONTRACTS)
    assert RETIRED.isdisjoint(spec.wire_name for spec in specs)
    targets = json.loads((ROOT / "contracts/gateway/v4/production-targets.json").read_text())
    assert RETIRED.isdisjoint(target["wireName"] for target in targets["targets"])
    assert {
        "artifacts.source.read", "artifacts.prompt_annotations.list", "artifacts.mutations.resolve",
        "artifacts.documents.open", "artifacts.revisions.restore", "artifacts.changes.revert",
    } <= GATEWAY_METHOD_CONTRACTS.keys()


def test_working_file_open_result_round_trips() -> None:
    result = {
        "disposition": "document", "resolution": {}, "resource": {}, "materialized": False,
        "pageContext": {"resourceId": "document:synthetic"},
        "workingFile": "/synthetic/workspace/page.html",
    }
    descriptor = GATEWAY_METHOD_CONTRACTS["workbench.resources.open"]
    parsed = descriptor.result_model.model_validate(result)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == result
    with pytest.raises(ValidationError):
        descriptor.result_model.model_validate({**result, "workingFile": {"path": "page.html"}})
