"""Skill installer — fetch → quarantine → scan → install → lockfile."""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import structlog

from opensquilla.paths import default_opensquilla_home
from opensquilla.skills.hub.lockfile import LockEntry, Lockfile, compute_sha256
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.scanner import ScanResult, scan_skill_bundle
from opensquilla.skills.hub.source import SkillFetchError
from opensquilla.skills.paths import default_managed_skills_dir

log = structlog.get_logger(__name__)

# Path traversal protection: only allow safe skill names
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _default_managed_dir() -> Path:
    return default_managed_skills_dir()


def _default_quarantine_dir() -> Path:
    return default_opensquilla_home() / "quarantine"


def _default_lockfile() -> Path:
    return default_opensquilla_home() / "skills-lock.json"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


@dataclass
class InstallResult:
    """Result of a skill installation."""

    success: bool
    name: str = ""
    message: str = ""
    scan: ScanResult | None = None
    path: str = ""


class SkillInstaller:
    """Manages the full skill install/uninstall lifecycle."""

    def __init__(
        self,
        router: SourceRouter,
        managed_dir: Path | None = None,
        quarantine_dir: Path | None = None,
        lockfile_path: Path | None = None,
    ) -> None:
        self._router = router
        self._managed_dir = managed_dir if managed_dir is not None else _default_managed_dir()
        self._quarantine_dir = (
            quarantine_dir if quarantine_dir is not None else _default_quarantine_dir()
        )
        self._lockfile_path = lockfile_path if lockfile_path is not None else _default_lockfile()

    async def install(
        self,
        identifier: str,
        source_id: str,
        force: bool = False,
    ) -> InstallResult:
        """Full install lifecycle: fetch → quarantine → scan → install → lockfile."""
        # 1. Fetch
        try:
            bundle = await self._router.fetch(identifier, source_id)
        except SkillFetchError as exc:
            return InstallResult(success=False, name=identifier, message=exc.reason)
        if bundle is None:
            return InstallResult(
                success=False,
                message=(
                    f"Failed to fetch '{identifier}' from {source_id}. "
                    "The skill may not exist or the source is rate-limited. "
                    "Try again later."
                ),
            )

        name = bundle.name
        if not _SAFE_NAME_RE.match(name):
            return InstallResult(success=False, name=name, message=f"Invalid skill name: {name}")

        skill_md = bundle.skill_md
        if not skill_md:
            return InstallResult(success=False, name=name, message="Bundle has no SKILL.md")

        bundle_meta = bundle.meta
        if bundle_meta is None:
            inspect = getattr(self._router, "inspect", None)
            if inspect is not None:
                try:
                    bundle_meta = await inspect(identifier, source_id)
                except Exception:  # pragma: no cover - source adapters are best-effort here
                    bundle_meta = None

        # 2. Quarantine — write to temp dir with Zip Slip protection
        q_dir = self._quarantine_dir / name
        if q_dir.exists():
            shutil.rmtree(q_dir)
        q_dir.mkdir(parents=True, exist_ok=True)
        q_dir_resolved = q_dir.resolve()
        for rel_path, content in bundle.files.items():
            file_path = (q_dir / rel_path).resolve()
            if not _is_relative_to(file_path, q_dir_resolved):
                log.warning("installer.zip_slip_blocked", rel_path=rel_path)
                continue
            file_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                file_path.write_bytes(content)
            else:
                file_path.write_text(content, encoding="utf-8")

        # 3. Security scan
        scan_result = scan_skill_bundle(bundle.files)
        if scan_result.verdict == "dangerous" and not force:
            shutil.rmtree(q_dir, ignore_errors=True)
            return InstallResult(
                success=False,
                name=name,
                message=(
                    f"Security scan: {scan_result.verdict} "
                    f"({len(scan_result.findings)} findings). "
                    "Use force=True to override."
                ),
                scan=scan_result,
            )

        # 4. Install — move from quarantine to managed dir
        install_dir = self._managed_dir / name
        if install_dir.exists():
            if not _is_relative_to(install_dir, self._managed_dir):
                shutil.rmtree(q_dir, ignore_errors=True)
                return InstallResult(
                    success=False,
                    name=name,
                    message=f"Existing install path escapes managed dir: {name}",
                    scan=scan_result,
                )
            shutil.rmtree(install_dir)
        self._managed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(q_dir), str(install_dir))

        # 5. Update lockfile
        sha = compute_sha256(install_dir)
        lockfile = Lockfile.load(self._lockfile_path)
        lockfile.add(
            name,
            LockEntry(
                source=source_id,
                identifier=identifier,
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                path=str(install_dir),
                sha256=sha,
                license=bundle_meta.license if bundle_meta else "",
                upstream_url=bundle_meta.homepage if bundle_meta else "",
                source_trust=bundle_meta.trust_level if bundle_meta else "",
                scan_verdict=scan_result.verdict,
                scan_strategy=scan_result.strategy,
                scan_findings=[asdict(finding) for finding in scan_result.findings],
            ),
        )
        lockfile.save(self._lockfile_path)

        log.info("skill.installed", name=name, source=source_id, verdict=scan_result.verdict)
        return InstallResult(
            success=True,
            name=name,
            message=f"Installed '{name}' from {source_id}",
            scan=scan_result,
            path=str(install_dir),
        )

    async def uninstall(self, name: str) -> InstallResult:
        """Remove an installed skill and its lockfile entry."""
        if not _SAFE_NAME_RE.match(name):
            return InstallResult(success=False, name=name, message=f"Invalid skill name: {name}")

        lockfile = Lockfile.load(self._lockfile_path)
        # Remove from disk (only within managed dir)
        install_dir = (self._managed_dir / name).resolve()
        managed_root = self._managed_dir.resolve()
        if install_dir.exists() and _is_relative_to(install_dir, managed_root):
            shutil.rmtree(install_dir)

        # Remove from lockfile
        removed = lockfile.remove(name)
        if removed:
            lockfile.save(self._lockfile_path)

        if not install_dir.exists() and not removed:
            return InstallResult(success=False, name=name, message=f"Skill '{name}' not found")

        log.info("skill.uninstalled", name=name)
        return InstallResult(success=True, name=name, message=f"Uninstalled '{name}'")

    async def update(self, name: str | None = None) -> list[InstallResult]:
        """Re-install skills from lockfile. If name is None, update all."""
        lockfile = Lockfile.load(self._lockfile_path)
        results = []
        entries = {name: lockfile.get(name)} if name else lockfile.installed
        for skill_name, entry in entries.items():
            if entry is None:
                results.append(
                    InstallResult(success=False, name=skill_name, message="Not in lockfile")
                )
                continue
            result = await self.install(entry.identifier, entry.source, force=True)
            results.append(result)
        return results
