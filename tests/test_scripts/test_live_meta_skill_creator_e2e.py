from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script_module():
    path = Path("scripts/live_meta_skill_creator_e2e.py")
    spec = importlib.util.spec_from_file_location("live_meta_skill_creator_e2e", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_live_meta_skill_creator_script_runs_full_flow_with_stub_llm(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_script_module()

    def stub_slots(_prompt: str, **_kwargs) -> str:
        return json.dumps({
            "name": "script-history-summary",
            "description": (
                "Script live harness fixture that reads recent history and "
                "summarizes the operational pattern."
            ),
            "meta_priority": 50,
            "triggers": ["script history summary"],
            "steps": [
                {
                    "id": "history",
                    "skill": "history-explorer",
                    "task": "read recent history",
                    "with_keys": {},
                },
                {
                    "id": "summary",
                    "skill": "docx",
                    "task": "create an operational brief",
                    "with_keys": {},
                },
            ],
        })

    monkeypatch.setattr(module.proposer, "_call_llm_for_slots", stub_slots)

    out = module.run_live_meta_skill_creator_e2e(
        home=tmp_path / "home",
        model="stub-live-model",
        provider="stub-provider",
        auto_enable=True,
        auto_enable_max_risk="medium",
    )

    assert out["ok"] is True
    assert out["llm_slots"]["name"] == "script-history-summary"
    assert out["lint"]["G1"]["passed"] is True
    assert out["lint"]["G2"]["passed"] is True
    assert out["smoke"]["G3"]["passed"] is True
    assert out["smoke"]["G4"]["passed"] is True
    assert out["persist"]["auto_enable"]["status"] == "enabled"
    assert out["managed"] == ["script-history-summary"]
    assert out["pending"] == []
