#!/usr/bin/env python3
"""Audit immutable dependency inventories; incomplete evidence fails closed.

Exit 0: complete, no known vulnerabilities; 1: vulnerabilities; 2: audit error.
No project dependencies are installed and no dependency files are rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
POLICY = ".github/ci/dependency-audit.v1.json"
NPM_PROJECTS = {"webui": "opensquilla-webui", "electron": "desktop/electron"}


class AuditError(Exception):
    """The available evidence cannot establish a complete audit."""


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_command(args: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(args[0])
    if executable is None:
        raise AuditError(f"Required executable is unavailable: {args[0]}")
    try:
        return subprocess.run(
            [executable, *args[1:]], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError(f"{args[0]} failed or timed out: {exc}") from exc


def require_success(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise AuditError(f"{label} exited {result.returncode}: {result.stderr.strip()}")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuditError(f"Missing or invalid JSON evidence: {path.name}") from exc


def package_key(package: Any) -> tuple[str, str]:
    if not isinstance(package, dict):
        raise AuditError("Package record must be an object")
    name, version = package.get("name"), package.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise AuditError("Package inventory contains a missing name or version")
    return normalized_name(name), version


def pylock_inventory(path: Path) -> set[tuple[str, str]]:
    lock = tomllib.loads(path.read_text(encoding="utf-8"))
    packages = lock.get("packages")
    if not isinstance(packages, list) or not packages:
        raise AuditError("Exported pylock has no packages")
    return {package_key(package) for package in packages}


def validate_python_report(report: Any, expected: set[tuple[str, str]]) -> dict[str, Any]:
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        raise AuditError("Python audit response has no dependency inventory")
    observed: set[tuple[str, str]] = set()
    findings = []
    seen_vulnerabilities: set[tuple[str, str, str]] = set()
    for package in report["dependencies"]:
        key = package_key(package)
        if package.get("skip_reason") or not isinstance(package.get("vulns"), list):
            raise AuditError(f"Python audit skipped or did not audit {key[0]}=={key[1]}")
        observed.add(key)
        for vuln in package["vulns"]:
            if not isinstance(vuln, dict) or not isinstance(vuln.get("id"), str):
                raise AuditError("Malformed Python vulnerability record")
            finding_key = (*key, vuln["id"])
            if finding_key not in seen_vulnerabilities:
                findings.append({"name": key[0], "version": key[1], **vuln})
                seen_vulnerabilities.add(finding_key)
    if observed != expected:
        missing, extra = sorted(expected - observed), sorted(observed - expected)
        raise AuditError(f"Python audit coverage mismatch: missing={missing}, extra={extra}")
    return {
        "package_versions": len(expected), "coverage_complete": True,
        "findings": findings, "vulnerability_count": len(findings),
    }


def validate_npm_report(report: Any, lock: Any) -> dict[str, Any]:
    if not isinstance(report, dict) or report.get("error"):
        raise AuditError("npm audit returned an error instead of a complete report")
    if report.get("auditReportVersion") != 2 or not isinstance(report.get("vulnerabilities"), dict):
        raise AuditError("Unsupported or missing npm audit report schema")
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict) or not packages or lock.get("lockfileVersion") not in (2, 3):
        raise AuditError("Unsupported or empty npm lockfile")
    if any(not isinstance(package, dict) for package in packages.values()):
        raise AuditError("Malformed npm lockfile package record")
    metadata = report.get("metadata", {})
    counts = metadata.get("vulnerabilities", {}) if isinstance(metadata, dict) else {}
    dependencies = metadata.get("dependencies", {}) if isinstance(metadata, dict) else {}
    count = counts.get("total") if isinstance(counts, dict) else None
    if type(count) is not int or count < 0 or count != len(report["vulnerabilities"]):
        raise AuditError("npm vulnerability count is inconsistent")
    total = dependencies.get("total") if isinstance(dependencies, dict) else None
    # npm excludes the root package from metadata.total. Link records do not
    # add a separate audited package. Include all dev/optional/peer categories.
    expected_count = sum(
        bool(path) and not package.get("link", False) for path, package in packages.items()
    )
    if type(total) is not int or total != expected_count:
        raise AuditError(f"npm audit coverage mismatch: expected {expected_count}, got {total}")
    findings = []
    for name, vulnerability in report["vulnerabilities"].items():
        if not isinstance(vulnerability, dict) or not isinstance(vulnerability.get("via"), list):
            raise AuditError("Malformed npm vulnerability record")
        nodes = vulnerability.get("nodes")
        if not isinstance(nodes, list) or not nodes or any(node not in packages for node in nodes):
            raise AuditError("npm vulnerability refers to absent lockfile nodes")
        findings.append({
            "name": name, **vulnerability,
            "packages": [
                {"path": node, "version": packages[node].get("version"),
                 "dev": bool(packages[node].get("dev")),
                 "optional": bool(packages[node].get("optional"))}
                for node in nodes
            ],
        })
    advisories = {
        str(advisory.get("source", advisory.get("url")))
        for finding in findings for advisory in finding["via"] if isinstance(advisory, dict)
    }
    return {
        "package_locations": expected_count, "coverage_complete": True,
        "findings": findings, "vulnerability_count": count,
        "distinct_advisory_count": len(advisories),
    }


def validate_npm_manifest(manifest: Any, lock: Any) -> None:
    if not isinstance(manifest, dict) or not isinstance(lock, dict):
        raise AuditError("npm manifest and lock must be objects")
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise AuditError("npm lock package inventory must be an object")
    root = packages.get("")
    if not isinstance(root, dict):
        raise AuditError("npm lock is missing the root package")
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        if manifest.get(section, {}) != root.get(section, {}):
            raise AuditError(f"npm manifest and lock disagree on {section}")


def inventory_pylock(source: Path, destination: Path, lock_digest: str) -> list[dict[str, Any]]:
    inventory = read_json(source)
    if (
        not isinstance(inventory, dict) or inventory.get("schemaVersion") != 1
        or inventory.get("kind") not in ("pyinstaller", "wheelhouse")
        or inventory.get("lockSha256") != lock_digest
        or not isinstance(inventory.get("packages"), list)
    ):
        raise AuditError("Artifact inventory schema, kind, or lock SHA256 is invalid")
    included: set[tuple[str, str]] = set()
    excluded = []
    for package in inventory["packages"]:
        name, version = package_key(package)
        if type(package.get("bundled")) is not bool:
            raise AuditError("Artifact package must declare bundled as a boolean")
        if not package["bundled"]:
            excluded.append({"name": name, "version": version, "reason": "build-only; not bundled"})
        elif name == "opensquilla":
            excluded.append({
                "name": name, "version": version,
                "reason": "local opensquilla project; no upstream distribution audit",
            })
        else:
            included.add((name, version))
    if not included:
        raise AuditError("Artifact inventory contains no auditable bundled packages")
    text = 'lock-version = "1.0"\n'
    for name, version in sorted(included):
        text += f"\n[[packages]]\nname = {json.dumps(name)}\nversion = {json.dumps(version)}\n"
    destination.write_text(text, encoding="utf-8")
    return excluded


def audit_python(
    repo: Path, output: Path, policy: dict[str, Any], inventory: Path | None,
) -> dict[str, Any]:
    timeout = policy["timeout_seconds"]
    with tempfile.TemporaryDirectory(prefix="opensquilla-python-audit-") as tmp:
        workspace = Path(tmp)
        pylock = workspace / "pylock.toml"
        excluded = []
        if inventory:
            excluded = inventory_pylock(inventory, pylock, sha256(repo / "uv.lock"))
        else:
            for name in ("pyproject.toml", "uv.lock"):
                shutil.copyfile(repo / name, workspace / name)
            result = run_command([
                "uvx", "--from", f"uv=={policy['uv_version']}", "uv", "export", "--locked",
                "--all-extras", "--all-groups", "--no-emit-project", "--format", "pylock.toml",
                "--output-file", str(pylock),
            ], cwd=workspace, timeout=timeout)
            (output / "python-export.stderr.txt").write_text(result.stderr, encoding="utf-8")
            require_success(result, "uv locked export")
            # A pip-audit project directory must contain only the exported lock;
            # leave no pyproject for a tool to resolve instead of using the lock.
            (workspace / "pyproject.toml").unlink()
            (workspace / "uv.lock").unlink()
        expected = pylock_inventory(pylock)
        shutil.copyfile(pylock, output / "pylock.toml")
        (output / "python.json").unlink(missing_ok=True)
        result = run_command([
            "uvx", "--from", f"pip-audit=={policy['pip_audit_version']}", "pip-audit",
            "--locked", str(workspace), "--format", "json", "--progress-spinner", "off",
            "--strict", "--cache-dir", str(workspace / "advisory-cache"),
            "--output", str(output / "python.json"),
        ], cwd=workspace, timeout=timeout)
        (output / "python.stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode not in (0, 1):
            require_success(result, "pip-audit")
        report = validate_python_report(read_json(output / "python.json"), expected)
        if (result.returncode == 0) != (report["vulnerability_count"] == 0):
            raise AuditError("pip-audit exit status disagrees with its findings")
        report["excluded"] = excluded
        return report


def audit_npm(repo: Path, output: Path, policy: dict[str, Any], ecosystem: str) -> dict[str, Any]:
    source = repo / NPM_PROJECTS[ecosystem]
    with tempfile.TemporaryDirectory(prefix=f"opensquilla-{ecosystem}-audit-") as tmp:
        workspace = Path(tmp)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(source / name, workspace / name)
        validate_npm_manifest(
            read_json(workspace / "package.json"), read_json(workspace / "package-lock.json"),
        )
        result = run_command([
            "npm", "audit", "--package-lock-only", "--ignore-scripts", "--json",
            "--include=dev", "--include=optional", "--include=peer",
            "--registry=https://registry.npmjs.org", "--audit-level=low",
        ], cwd=workspace, timeout=policy["timeout_seconds"])
        (output / f"{ecosystem}.json").write_text(result.stdout, encoding="utf-8")
        (output / f"{ecosystem}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode not in (0, 1):
            require_success(result, "npm audit")
        report = validate_npm_report(
            read_json(output / f"{ecosystem}.json"), read_json(workspace / "package-lock.json"),
        )
        if (result.returncode == 0) != (report["vulnerability_count"] == 0):
            raise AuditError("npm audit exit status disagrees with its findings")
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--python-only", action="store_true")
    parser.add_argument("--python-inventory", type=Path)
    args = parser.parse_args(argv)
    if args.python_inventory and not args.python_only:
        parser.error("--python-inventory requires --python-only")
    repo, output = args.repo.resolve(), args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": 1, "started_at": datetime.now(UTC).isoformat(),
        "mode": "artifact" if args.python_inventory else "locked-dependencies",
        "tools": {}, "inputs": {}, "results": {}, "errors": [],
    }
    errors = summary["errors"]
    try:
        policy = read_json(ROOT / POLICY)
        if not isinstance(policy, dict) or policy.get("schema_version") != 1:
            raise AuditError("Invalid dependency audit tool policy")
        summary["tools"] = policy
        summary["inputs"][POLICY] = sha256(ROOT / POLICY)
        names = ["pyproject.toml", "uv.lock"]
        if not args.python_only:
            names += [f"{path}/{name}" for path in NPM_PROJECTS.values()
                      for name in ("package.json", "package-lock.json")]
        summary["inputs"].update({name: sha256(repo / name) for name in names})
        if args.python_inventory:
            args.python_inventory = args.python_inventory.resolve()
            summary["inputs"]["python_inventory"] = sha256(args.python_inventory)
        commit = run_command(["git", "rev-parse", "HEAD"], cwd=repo, timeout=30)
        require_success(commit, "git revision")
        summary["commit"] = commit.stdout.strip()
        if not args.python_only:
            version = run_command(["npm", "--version"], cwd=repo, timeout=30)
            require_success(version, "npm version")
            if version.stdout.strip() != policy["npm_version"]:
                raise AuditError(
                    f"Expected npm {policy['npm_version']}; got {version.stdout.strip()}",
                )
        ecosystems = ["python"] if args.python_only else ["python", *NPM_PROJECTS]
        for ecosystem in ecosystems:
            try:
                result = (audit_python(repo, output, policy, args.python_inventory)
                          if ecosystem == "python" else audit_npm(repo, output, policy, ecosystem))
                summary["results"][ecosystem] = result
            except (AuditError, OSError, ValueError, KeyError, TypeError) as exc:
                errors.append({"ecosystem": ecosystem, "message": str(exc)})
        for name in names:
            if sha256(repo / name) != summary["inputs"][name]:
                errors.append({"message": f"Input changed during audit: {name}"})
        if args.python_inventory:
            if sha256(args.python_inventory) != summary["inputs"]["python_inventory"]:
                errors.append({"message": "Artifact inventory changed during audit"})
    except (AuditError, OSError, ValueError, KeyError, TypeError) as exc:
        errors.append({"message": str(exc)})
    findings = sum(result["vulnerability_count"] for result in summary["results"].values())
    exit_code = 2 if errors else (1 if findings else 0)
    summary.update(finished_at=datetime.now(UTC).isoformat(), exit_code=exit_code)
    write_json(output / "summary.json", summary)
    print(
        f"Dependency audit: {len(errors)} errors, {findings} vulnerable package records; "
        f"{output / 'summary.json'}",
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
