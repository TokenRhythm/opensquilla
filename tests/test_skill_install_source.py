from __future__ import annotations

import pytest

from opensquilla.application.skill_management import InstallSkill
from opensquilla.skills.install_source import resolve_install_source


@pytest.mark.parametrize(("identifier", "source", "expected"), [
    ("https://github.com/acme/pack", None, "github"),
    ("https://www.github.com/acme/pack", None, "github"),
    ("github.com/acme/pack", None, "github"),
    ("https://raw.githubusercontent.com/acme/pack/main/SKILL.md", None, "github"),
    ("demo", None, "clawhub"),
    ("acme/pack", None, "clawhub"),
    ("https://github.com.evil.example/acme/pack", None, "clawhub"),
    ("https://github.com@evil.example/acme/pack", None, "clawhub"),
    ("https://github.com/acme/pack", "clawhub", "clawhub"),
    ("https://github.com/acme/pack", "", "clawhub"),
    ("demo", "custom", "custom"),
])
def test_source_inference_preserves_explicit_source(identifier, source, expected):
    assert resolve_install_source(identifier, source) == expected
    assert InstallSkill(identifier, source=source).source == expected
