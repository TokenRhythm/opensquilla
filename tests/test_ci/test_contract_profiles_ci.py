"""Both Contract profiles must participate in required cross-platform CI."""

from pathlib import Path

import yaml


def test_verification_profile_is_required_and_uses_separate_output() -> None:
    jobs = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())["jobs"]
    linux = jobs["gateway-contract-verification-linux"]
    assert "'frontend-validation'" in linux["if"]
    assert linux["timeout-minutes"] == 20
    assert "gateway-contract-verification-linux" in jobs["ci-result"]["needs"]
    commands = "\n".join(step.get("run", "") for step in linux["steps"])
    for required in (
        "--profile verification",
        "--output-root",
        "--write",
        "--verify-determinism",
        "verify_gateway_validator_profiles.mjs",
        "--verification-root",
        "--hash-manifest",
        "test:contract-tooling",
    ):
        assert required in commands
    windows = jobs["gateway-contract-windows"]
    assert "gateway-contract-verification-linux" in windows["needs"]
    windows_commands = "\n".join(step.get("run", "") for step in windows["steps"])
    assert "--profile verification" in windows_commands
    assert "--verification-root" in windows_commands
    assert "gateway-contract-verification-hashes-linux" in windows_commands
    assert "--compare-hash-manifests" in windows_commands
