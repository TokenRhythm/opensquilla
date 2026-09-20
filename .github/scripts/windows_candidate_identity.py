"""Record or verify the exact bytes used by Windows package acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


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
           installer_sha: str | None = None, root: Path | None = None) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Expected candidate source must be a complete Git SHA")
    if manifest.get("sourceSha") != source_sha:
        raise ValueError("Candidate source mismatch")
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
    parser.add_argument("--workflow-sha", default="")
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
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    else:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    verify(manifest, args.installer, args.source_sha, args.installer_sha256, args.root)
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
