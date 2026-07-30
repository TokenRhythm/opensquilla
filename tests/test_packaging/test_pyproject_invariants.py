"""Invariants for pyproject.toml after the channel install contract cleanup.

Real-used channel SDKs stay in base dependencies. Base-install channels
must not be re-exposed as empty optional extras, because package metadata
is also a user-visible install contract.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
UV_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
FORMULA = Path(__file__).resolve().parents[2] / "Formula" / "opensquilla.rb"


@pytest.fixture(scope="module")
def project_table() -> dict:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]


@pytest.fixture(scope="module")
def lock_package() -> dict:
    data = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return next(package for package in data["package"] if package["name"] == "opensquilla")


def _dep_names(specs: list[str]) -> set[str]:
    """Extract canonical (lowercased) package names from a list of PEP 508 specs."""

    names: set[str] = set()
    for spec in specs:
        head = spec.strip()
        for sep in ("[", " ", ";", "=", ">", "<", "~", "!"):
            head = head.split(sep, 1)[0]
        if head:
            names.add(head.lower())
    return names


def test_channel_sdks_in_base(project_table: dict) -> None:
    """Channel adapters that ``import`` a vendor SDK must keep that SDK in base."""

    base = _dep_names(project_table["dependencies"])
    required = {
        "lark-oapi",
        "python-telegram-bot",
        "dingtalk-stream",
        "qq-botpy",
        "cryptography",
    }
    missing = required - base
    assert not missing, f"channel SDKs missing from base deps: {sorted(missing)}"


def test_no_dead_extras(project_table: dict) -> None:
    """msteams extra is intentionally absent; matrix extra installs matrix-nio."""

    extras = project_table.get("optional-dependencies", {})
    assert "msteams" not in extras, (
        "msteams extra stays absent: the adapter is text-only and not advertised"
    )
    assert "matrix" in extras, "matrix extra must exist and pull matrix-nio"
    matrix_specs = extras["matrix"]
    assert any("matrix-nio" in spec for spec in matrix_specs), (
        "matrix extra must declare matrix-nio (without [e2e]); use matrix-e2e for E2EE"
    )


def test_base_channel_extras_are_not_exposed_as_noop_aliases(
    project_table: dict,
) -> None:
    """Base-install channels must not be exposed as no-op extras."""

    extras = project_table.get("optional-dependencies", {})
    for name in ("feishu", "telegram", "dingtalk", "wecom", "qq"):
        assert name not in extras, f"{name} is installed from base; do not expose a no-op extra"


def test_lockfile_does_not_advertise_removed_base_channel_extras(
    lock_package: dict,
) -> None:
    """uv.lock metadata must match the package install contract."""

    provides_extras = set(lock_package.get("provides-extras", []))
    for name in ("feishu", "telegram", "dingtalk", "wecom", "qq"):
        assert name not in provides_extras


def test_no_duplicate_ml_extra(project_table: dict) -> None:
    """``recommended`` and ``model-router`` historically overlapped — only one survives."""

    extras = project_table.get("optional-dependencies", {})
    has_recommended = "recommended" in extras
    has_model_router = "model-router" in extras
    assert has_recommended, "recommended extra must exist (router users opt in here)"
    assert not has_model_router, (
        "model-router extra duplicates recommended — collapse into one"
    )


def test_alpha_classifier_present(project_table: dict) -> None:
    """0.1.0 stays pre-stable — the classifier must reflect that."""

    classifiers = project_table.get("classifiers", [])
    assert "Development Status :: 3 - Alpha" in classifiers, (
        "Alpha classifier signals to PyPI/uv that this is pre-stable"
    )


def test_readme_points_at_user_facing_file(project_table: dict) -> None:
    """``readme`` must be the user-facing README, not the legacy portable view."""

    assert project_table["readme"] == "README.md", (
        "readme should point at the canonical README.md after the 0.1.0 refactor"
    )


def test_html_coder_reference_files_are_packaged() -> None:
    """html-coder's SKILL.md links to local references that must survive wheels."""

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    force_include = data["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]
    references_dir = (
        PYPROJECT.parent
        / "src"
        / "opensquilla"
        / "skills"
        / "bundled"
        / "html-coder"
        / "references"
    )
    expected_sources = {
        path.relative_to(PYPROJECT.parent).as_posix()
        for path in references_dir.glob("*.md")
    }

    missing_sources = expected_sources - set(force_include)
    assert not missing_sources, (
        "html-coder reference files must be force-included in wheels: "
        f"{sorted(missing_sources)}"
    )
    for source in expected_sources:
        assert (
            force_include[source] == source.removeprefix("src/")
        ), f"{source} should be packaged at its import-time skill path"


def test_standard_distributions_require_verified_generated_webui() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    build = data["build-system"]
    hatch = data["tool"]["hatch"]["build"]
    artifact_rule = "src/opensquilla/gateway/static/dist/**"

    assert build["requires"] == ["hatchling>=1.31,<2"]
    assert "custom" in hatch["hooks"]
    assert artifact_rule in hatch["targets"]["wheel"]["artifacts"]
    assert artifact_rule in hatch["targets"]["sdist"]["artifacts"]
    assert "opensquilla-webui/public/music/**" not in hatch["targets"]["sdist"][
        "artifacts"
    ]
    assert (PYPROJECT.parent / "hatch_build.py").is_file()
    assert (PYPROJECT.parent / "scripts" / "verify_webui_artifact.py").is_file()


def test_homebrew_head_install_builds_the_generated_webui_before_python() -> None:
    formula = FORMULA.read_text(encoding="utf-8")

    assert 'depends_on "node" => :build' in formula
    assert 'require "language/node"' in formula
    npm_ci = formula.index(
        'system "npm", "ci", "--#{Language::Node.npm_cache_config}"'
    )
    npm_build = formula.index('system "npm", "run", "build"')
    pip_install = formula.index("venv.pip_install_and_link buildpath")
    assert npm_ci < npm_build < pip_install
