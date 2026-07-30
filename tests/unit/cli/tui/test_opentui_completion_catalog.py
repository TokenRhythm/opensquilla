from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from opensquilla.cli.tui.opentui import completion
from opensquilla.cli.tui.opentui.completion import (
    CompletionCandidate,
    build_completion_catalog,
)
from opensquilla.engine.commands import Surface


@dataclass(frozen=True)
class FakeSkill:
    name: str
    description: str
    disable_model_invocation: bool = False


class FakeSkillLoader:
    def get_user_invocable(self) -> list[FakeSkill]:
        return [
            FakeSkill("code-review", "Review code for regressions."),
            FakeSkill("internal-only", "Hidden from model.", disable_model_invocation=True),
        ]


def _by_label(items: list[CompletionCandidate]) -> dict[str, CompletionCandidate]:
    return {item.label: item for item in items}


def test_build_completion_catalog_includes_commands_and_skills() -> None:
    catalog = build_completion_catalog(surface="tui", skill_loader=FakeSkillLoader())
    items = _by_label(catalog)

    assert items["/compact"].category == "control"
    assert items["/compact"].insert_text == "/compact "
    assert items["/compact"].description

    assert items["/skill:code-review"].category == "skill"
    assert items["/skill:code-review"].insert_text == "use the code-review skill: "
    assert "/skill:internal-only" not in items


def test_build_completion_catalog_has_one_row_per_registered_command() -> None:
    catalog = build_completion_catalog(surface=Surface.CLI_GATEWAY, skill_loader=FakeSkillLoader())
    items = _by_label(catalog)

    assert items["/model"].category == "control"
    assert items["/cost"].category == "query"
    assert "Model" not in items
    assert "Permissions" not in items
    assert "Cost" not in items
    assert "Resume" not in items

    inserts = [candidate.insert_text.strip() for candidate in catalog]
    assert len(inserts) == len(set(inserts)), "duplicate insert targets in catalog"


def test_standalone_catalog_does_not_advertise_unregistered_gateway_commands() -> None:
    catalog = build_completion_catalog(
        surface=Surface.CLI_STANDALONE, skill_loader=FakeSkillLoader()
    )
    items = _by_label(catalog)

    assert "/model" in items
    assert "/cost" in items
    assert "/permissions" not in items
    assert "/resume" not in items
    assert "Permissions" not in items
    assert "Resume" not in items


def test_build_completion_catalog_keeps_commands_when_skill_loader_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_loader(*, workspace_dir: Path | None = None) -> object:
        raise RuntimeError("no config")

    monkeypatch.setattr(completion, "_build_skill_loader", fail_loader)

    catalog = build_completion_catalog(surface=Surface.CLI_STANDALONE)
    items = _by_label(catalog)

    assert items["/compact"].category == "control"
    assert "/permissions" not in items
    assert all(item.category != "skill" for item in catalog)


def test_gateway_catalog_projects_curated_order_aliases_and_arguments() -> None:
    catalog = build_completion_catalog(surface=Surface.CLI_GATEWAY, skill_loader=FakeSkillLoader())
    commands = [item for item in catalog if item.category != "skill"]
    default_labels = [
        item.label for item in commands if item.visible_by_default and not item.deprecated
    ]

    assert default_labels[:12] == [
        "/model",
        "/strategy",
        "/sessions",
        "/new",
        "/permissions",
        "/status",
        "/compact",
        "/usage",
        "/theme",
        "/help",
        "/keys",
        "/exit",
    ]

    items = _by_label(catalog)
    assert items["/reset"].aliases == ("/clear",)
    assert [choice.value for choice in items["/strategy"].argument_choices] == [
        "direct",
        "router",
        "ensemble",
        "status",
    ]
    assert items["/resume"].submit_behavior == "submit"
    assert items["/delete"].submit_behavior == "complete"
    assert items["/models"].visible_by_default is False
    assert items["/models"].deprecated is True
    assert items["/router"].visible_by_default is False
    assert items["/ensemble"].visible_by_default is False
    assert "/models" not in default_labels
    assert "/router" not in default_labels
    assert "/ensemble" not in default_labels
    assert default_labels.count("/model") == 1


def test_standalone_catalog_does_not_claim_gateway_only_strategy_capability() -> None:
    catalog = build_completion_catalog(
        surface=Surface.CLI_STANDALONE, skill_loader=FakeSkillLoader()
    )
    items = _by_label(catalog)

    assert "/strategy" not in items
    assert "/router" not in items
    assert "/ensemble" not in items
    assert "/meta" not in items
    assert "/models" not in items
    assert "/model" in items
