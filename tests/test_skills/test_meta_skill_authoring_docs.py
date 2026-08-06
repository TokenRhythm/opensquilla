from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROOT_GUIDE = ROOT / "META_SKILL_GUIDE.md"
USER_GUIDE = ROOT / "docs" / "features" / "meta-skill-user-guide.md"
FEATURE_GUIDE = ROOT / "docs" / "features" / "meta-skills.md"
AUTHORING_DOC = ROOT / "docs" / "authoring" / "meta-skills.md"
PACKAGE_STUB = ROOT / "src" / "opensquilla" / "skills" / "meta" / "META_SKILL_AUTHORING.md"
README = ROOT / "README.md"


def test_meta_skill_user_guide_contains_user_contract_and_retained_catalog() -> None:
    text = USER_GUIDE.read_text(encoding="utf-8")

    required_snippets = [
        "OpenSquilla MetaSkill User Guide",
        "Important Notice",
        "User Mental Model",
        "Outcome:",
        "Decision standard:",
        "Avoiding Accidental Activation",
        "Reading the Result",
        "Correcting a Bad Run",
        "meta-paper-write",
        "meta-skill-creator",
    ]
    for snippet in required_snippets:
        assert snippet in text

    assert "meta-family-day-coordinator" not in text


def test_meta_skill_authoring_doc_matches_current_runtime_contract() -> None:
    text = AUTHORING_DOC.read_text(encoding="utf-8")

    required_snippets = [
        "Meta-Skill Authoring Guide",
        "Where to Put a MetaSkill",
        "metadata.opensquilla.risk",
        "metadata.opensquilla.capabilities",
        "kind: meta",
        "composition:",
        "agent",
        "llm_chat",
        "llm_classify",
        "user_input",
        "tool_call",
        "skill_exec",
        "final_text_mode",
        "xml_escape",
        "truncate",
        "scripts/live_meta_soft_activation_e2e.py",
        "disable-model-invocation",
    ]
    for snippet in required_snippets:
        assert snippet in text

    assert "composition.final_text" not in text


def test_meta_skill_docs_are_linked_from_compatibility_stubs_and_readme() -> None:
    root_text = ROOT_GUIDE.read_text(encoding="utf-8")
    package_text = PACKAGE_STUB.read_text(encoding="utf-8")
    feature_text = FEATURE_GUIDE.read_text(encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")

    assert "docs/features/meta-skill-user-guide.md" in root_text
    assert "docs/authoring/meta-skills.md" in root_text
    assert "docs/authoring/meta-skills.md" in package_text
    assert "meta-skill-user-guide.md" in feature_text
    assert "docs/features/meta-skill-user-guide.md" in readme_text
    assert "docs/authoring/meta-skills.md" in readme_text

    assert root_text != AUTHORING_DOC.read_text(encoding="utf-8")
    assert package_text != AUTHORING_DOC.read_text(encoding="utf-8")
