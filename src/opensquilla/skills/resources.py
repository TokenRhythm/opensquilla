"""Resource directory support for skills — scripts, references, assets."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath

_RESOURCE_DIRS = ("references", "scripts", "assets", "templates")


class SkillResources:
    """Access skill resource directories: scripts/, references/, assets/."""

    def __init__(
        self,
        skill_dir: Path,
        *,
        managed_manifest_files: Iterable[str] | None = None,
    ) -> None:
        self._dir = skill_dir
        self._managed_manifest_files = (
            None
            if managed_manifest_files is None
            else frozenset(managed_manifest_files)
        )

    @property
    def scripts_dir(self) -> Path:
        return self._dir / "scripts"

    @property
    def references_dir(self) -> Path:
        return self._dir / "references"

    @property
    def assets_dir(self) -> Path:
        return self._dir / "assets"

    @property
    def templates_dir(self) -> Path:
        return self._dir / "templates"

    def has_scripts(self) -> bool:
        return self.scripts_dir.is_dir() and any(self.scripts_dir.iterdir())

    def has_references(self) -> bool:
        return self.references_dir.is_dir() and any(self.references_dir.iterdir())

    def has_assets(self) -> bool:
        return self.assets_dir.is_dir() and any(self.assets_dir.iterdir())

    def list_scripts(self) -> list[Path]:
        if not self.scripts_dir.is_dir():
            return []
        return sorted(p for p in self.scripts_dir.iterdir() if p.is_file())

    def list_references(self) -> list[Path]:
        if not self.references_dir.is_dir():
            return []
        return sorted(p for p in self.references_dir.iterdir() if p.is_file())

    def list_assets(self) -> list[Path]:
        if not self.assets_dir.is_dir():
            return []
        return sorted(p for p in self.assets_dir.iterdir() if p.is_file())

    def read_resource(self, name: str) -> str | None:
        """Read a text resource by skill-relative path.

        Supports SKILL.md plus package resource paths such as
        ``references/foo.md``, ``scripts/tool.py``, ``assets/palette.txt``,
        and ``templates/report.md``. Bare filenames keep the legacy lookup
        behavior by trying references/, scripts/, assets/, then templates/.
        """

        relative = _normalise_resource_path(name)
        if relative is None:
            return None

        parts = relative.parts
        if parts == ("SKILL.md",):
            return self._read_text_under(self._dir, relative)

        if self._managed_manifest_files is not None:
            if any(part.startswith(".") for part in parts):
                return None
            if relative.as_posix() in self._managed_manifest_files:
                return self._read_text_under(
                    self._dir,
                    relative,
                    reject_symlinks=True,
                    reject_nul=True,
                )
            if len(parts) == 1:
                for root_name in _RESOURCE_DIRS:
                    candidate = Path(root_name) / relative
                    if candidate.as_posix() not in self._managed_manifest_files:
                        continue
                    return self._read_text_under(
                        self._dir,
                        candidate,
                        reject_symlinks=True,
                        reject_nul=True,
                    )
            return None

        if parts and parts[0] in _RESOURCE_DIRS:
            root = self._dir / parts[0]
            return self._read_text_under(root, Path(*parts[1:]))

        if len(parts) == 1:
            for root_name in _RESOURCE_DIRS:
                content = self._read_text_under(self._dir / root_name, relative)
                if content is not None:
                    return content
        return None

    def read_reference(self, name: str) -> str | None:
        """Read a reference file by name. Returns None if not found."""
        relative = _normalise_resource_path(name)
        if relative is None:
            return None
        if relative.parts[:1] == ("references",):
            relative = Path(*relative.parts[1:])
        return self._read_text_under(self.references_dir, relative)

    def read_script(self, name: str) -> str | None:
        """Read a script file by name. Returns None if not found."""
        relative = _normalise_resource_path(name)
        if relative is None:
            return None
        if relative.parts[:1] == ("scripts",):
            relative = Path(*relative.parts[1:])
        return self._read_text_under(self.scripts_dir, relative)

    def _read_text_under(
        self,
        root: Path,
        relative: Path,
        *,
        reject_symlinks: bool = False,
        reject_nul: bool = False,
    ) -> str | None:
        if not relative.parts:
            return None
        path = root / relative
        if reject_symlinks and _contains_symlink(path, root):
            return None
        try:
            resolved_root = root.resolve()
            resolved_path = path.resolve()
            resolved_path.relative_to(resolved_root)
        except (OSError, ValueError):
            return None
        if not resolved_path.is_file():
            return None
        try:
            content = resolved_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None
        if reject_nul and "\x00" in content:
            return None
        return content


def _contains_symlink(path: Path, root: Path) -> bool:
    current = path
    while current != root:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        if current.parent == current:
            return True
        current = current.parent
    return False


def _normalise_resource_path(name: str) -> Path | None:
    raw = name.strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw:
        return None

    posix = PurePosixPath(raw)
    if posix.is_absolute():
        return None
    parts = posix.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return Path(*parts)
