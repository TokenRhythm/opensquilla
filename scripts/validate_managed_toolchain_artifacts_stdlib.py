#!/usr/bin/env python3
"""Run managed-toolchain artifact validation from a stdlib-only source tree.

The Alpine artifact job intentionally avoids installing OpenSquilla's complete
runtime dependency graph: the locked sqlite-vec package has no musllinux wheel.
This fixed-path bootstrap skips the broad skills
package initializer, then delegates to the canonical validator.  Installation
and probing still use the production ``install_component`` and
``probe_component`` implementations.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType


def _bootstrap_skills_package(repo_root: Path) -> None:
    source_root = repo_root / "src"
    skills_root = source_root / "opensquilla" / "skills"
    sys.path.insert(0, str(source_root))

    import opensquilla

    skills_package = ModuleType("opensquilla.skills")
    skills_package.__file__ = str(skills_root / "__init__.py")
    skills_package.__package__ = "opensquilla.skills"
    skills_package.__path__ = [str(skills_root)]
    sys.modules["opensquilla.skills"] = skills_package
    opensquilla.skills = skills_package


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    _bootstrap_skills_package(repo_root)
    runpy.run_path(
        str(repo_root / "scripts" / "validate_managed_toolchain_artifacts.py"),
        run_name="__main__",
    )


if __name__ == "__main__":
    main()
