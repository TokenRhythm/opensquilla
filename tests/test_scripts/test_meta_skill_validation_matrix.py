from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script_module():
    path = Path("scripts/meta_skill_validation_matrix.py")
    spec = importlib.util.spec_from_file_location("meta_skill_validation_matrix", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_meta_skill_validation_matrix_materials_are_present() -> None:
    module = _load_script_module()
    result = module.check_materials(module.load_cases())

    assert result["ok"] is True
    assert len(result["cases"]) >= 10
    assert all(not row["missing"] for row in result["cases"])


def test_meta_skill_validation_matrix_writes_judge_bundle_template(
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    out = tmp_path / "bundle.json"

    result = module.write_empty_bundle("B2_pdf_intelligence", out)

    assert result == {"ok": True, "bundle": str(out)}
    bundle = json.loads(out.read_text(encoding="utf-8"))
    assert bundle["case_id"] == "B2_pdf_intelligence"
    assert bundle["skill_name"] == "meta-pdf-intelligence"
    assert "router-evaluation-summary.pdf" in "\n".join(bundle["materials"])
    assert bundle["selected_meta_skill"] == ""
    assert bundle["step_trace"] == []
