"""Record or verify the exact bytes used by Windows package acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

_NUMERIC_VERSION = r"(?:0|[1-9][0-9]*)"
_VERSION_CORE = rf"{_NUMERIC_VERSION}\.{_NUMERIC_VERSION}\.{_NUMERIC_VERSION}"
_PRERELEASE_ID = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_VERSION = re.compile(
    rf"{_VERSION_CORE}(?:-{_PRERELEASE_ID}(?:\.{_PRERELEASE_ID})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def normalized_version(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Candidate version must be a semver string")
    # Windows ProductVersion adds a zero revision to stable three-part versions.
    # Preserve every prerelease/build identifier and reject nonzero revisions.
    if re.fullmatch(rf"{_VERSION_CORE}\.0", value):
        return value[:-2]
    if not _VERSION.fullmatch(value):
        raise ValueError("Candidate version must be valid semver")
    return value


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installed_files(root: Path) -> dict[str, Path]:
    gateways = list((root / "resources/runtime/gateway").rglob("opensquilla-gateway.exe"))
    if len(gateways) != 1:
        raise ValueError("Expected exactly one frozen Gateway executable")
    return {
        "executableSha256": root / "OpenSquilla.exe",
        "asarSha256": root / "resources/app.asar",
        "gatewaySha256": gateways[0],
        "dependencyInventorySha256": root / "resources/runtime/gateway/dependency-inventory.json",
    }


def verify(manifest: dict, installer: Path, source_sha: str,
           installer_sha: str | None = None, root: Path | None = None, *,
           expected_workflow_sha: str | None = None, expected_version: str | None = None,
           installed_version: str | None = None) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Expected candidate source must be a complete Git SHA")
    if manifest.get("sourceSha") != source_sha:
        raise ValueError("Candidate source mismatch")
    workflow_sha = manifest.get("workflowSha")
    if not isinstance(workflow_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", workflow_sha):
        raise ValueError("Candidate workflow SHA must be a complete Git SHA")
    if expected_workflow_sha is not None:
        if not re.fullmatch(r"[0-9a-f]{40}", expected_workflow_sha):
            raise ValueError("Expected workflow SHA must be a complete Git SHA")
        if workflow_sha != expected_workflow_sha:
            raise ValueError("Candidate workflow SHA mismatch")
    version = normalized_version(manifest.get("version"))
    if expected_version is not None and normalized_version(expected_version) != version:
        raise ValueError("Candidate version mismatch")
    if installed_version is not None and normalized_version(installed_version) != version:
        raise ValueError("Installed candidate version mismatch")
    if manifest.get("installerName") != installer.name:
        raise ValueError("Candidate installer name mismatch")
    actual = digest(installer)
    if actual != manifest.get("installerSha256") or (installer_sha and actual != installer_sha):
        raise ValueError("Candidate installer hash mismatch")
    if root is not None:
        for field, path in installed_files(root).items():
            if digest(path) != manifest.get(field):
                raise ValueError(f"Installed candidate mismatch: {field}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--installer-sha256")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--signed", action="store_true")
    parser.add_argument("--workflow-sha")
    parser.add_argument("--expected-version")
    parser.add_argument("--installed-version")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-attempt", default="")
    args = parser.parse_args()
    if args.write:
        if args.root is None:
            parser.error("--write requires --root")
        repository = Path(__file__).resolve().parents[2]
        actual_source = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
        ).strip()
        if actual_source != args.source_sha:
            raise ValueError("Manifest source does not match the build checkout")
        package = json.loads((repository / "desktop/electron/package.json").read_text())
        manifest = {
            "sourceSha": args.source_sha,
            "workflowSha": args.workflow_sha,
            "auditRunId": args.run_id,
            "runAttempt": args.run_attempt,
            "signing": "signed" if args.signed else "unsigned regression only",
            "version": package["version"],
            "installerName": args.installer.name,
            "installerSha256": digest(args.installer),
            **{field: digest(path) for field, path in installed_files(args.root).items()},
        }
    else:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    verify(
        manifest, args.installer, args.source_sha, args.installer_sha256, args.root,
        expected_workflow_sha=args.workflow_sha, expected_version=args.expected_version,
        installed_version=args.installed_version,
    )
    if args.write:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
