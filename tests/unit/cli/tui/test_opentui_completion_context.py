from __future__ import annotations

from opensquilla.cli.tui.opentui.completion import build_completion_context
from opensquilla.engine.commands import Surface
from opensquilla.skills.types import SkillLayer, SkillSpec


class _SkillLoader:
    def get_user_invocable(self) -> list[SkillSpec]:
        return [
            SkillSpec(
                name="visible-skill",
                description="Complete visible work.",
                layer=SkillLayer.WORKSPACE,
                always=False,
                triggers=["visible"],
                content="",
            ),
            SkillSpec(
                name="hidden-from-model",
                description="Do not surface to model-driven completion.",
                layer=SkillLayer.WORKSPACE,
                always=False,
                triggers=["hidden"],
                content="",
                disable_model_invocation=True,
            ),
        ]


def test_completion_context_includes_surface_commands_skills_and_safe_files(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")

    context = build_completion_context(
        Surface.CLI_GATEWAY,
        skill_loader=_SkillLoader(),
        workspace_dir=tmp_path,
    )

    catalog = {candidate.label: candidate for candidate in context.catalog}
    assert "/compact" in catalog
    assert catalog["/compact"].insert_text == "/compact "

    assert "/skill:visible-skill" in catalog
    assert catalog["/skill:visible-skill"].insert_text == "use the visible-skill skill: "
    assert "/skill:hidden-from-model" not in catalog

    assert context.files == ("src/main.py",)
    assert context.filters_sensitive_paths is True
