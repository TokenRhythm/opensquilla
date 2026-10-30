from __future__ import annotations

import hashlib
import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest
import yaml


@pytest.fixture
def mirror_env(tmp_path, monkeypatch):
    spec = spec_from_file_location("test_mirror", ".github/scripts/mirror-windows-test-to-oss.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "ALIYUN_OSS_BUCKET": "test-bucket",
        "ALIYUN_OSS_PREFIX": "/releases/",
        "OSS_REGION": "cn-beijing",
        "OSS_ACCESS_KEY_ID": "test-id",
        "OSS_ACCESS_KEY_SECRET": "test-secret",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "abc123",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "OSS_ENDPOINT": "https://upload.example.com",
        "OSS_ADDRESSING_STYLE": "",
    }.items():
        monkeypatch.setenv(key, value)
    assets = tmp_path / "assets"
    assets.mkdir()
    installer = assets / "OpenSquilla-0.5.8-test-20261026-165954-win-x64.exe"
    installer.write_bytes(b"test installer bytes")
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    installer.with_suffix(".exe.sha256").write_text(
        f"{digest}  {installer.name}\n", encoding="utf-8-sig"
    )
    remote = {}
    calls = []

    def run(args, *, check):
        assert check
        calls.append(args)
        if "cp" in args:
            remote[args[-1]] = Path(args[-2]).read_bytes()

    def urlopen(request, *, timeout):
        assert timeout == 30
        url = request.full_url if isinstance(request, Request) else request
        key = url.replace("https://test-bucket.oss-cn-beijing.aliyuncs.com/", "oss://test-bucket/")
        response = BytesIO(remote[key])
        response.headers = {"Content-Length": str(len(remote[key]))}
        return response

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "urlopen", urlopen)
    return module, assets, calls, remote, tmp_path / "summary.md"


def test_mirrors_checked_artifact_and_only_prunes_old_test_objects(mirror_env):
    module, assets, calls, remote, summary = mirror_env
    module.mirror(assets)
    assert len(remote) == 2
    assert all(key.startswith("oss://test-bucket/releases/test/windows/12345-2/") for key in remote)
    assert all(command[:3] == ["ossutil", "--addressing-style", "cname"] for command in calls)
    assert calls[-1][3:] == [
        "rm",
        "oss://test-bucket/releases/test/windows/",
        "--recursive",
        "--force",
        "--min-age",
        "14d",
    ]
    assert (
        "https://test-bucket.oss-cn-beijing.aliyuncs.com/releases/test/windows/12345-2/"
        in summary.read_text()
    )
    assert "abc123" in summary.read_text()
    assert "test-secret" not in summary.read_text()


def test_rejects_wrong_sha256_before_upload(mirror_env):
    module, assets, calls, _, summary = mirror_env
    next(assets.glob("*.exe")).write_bytes(b"corrupt download")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        module.mirror(assets)
    assert not calls
    assert not summary.exists()


def test_mirrors_production_test_installer_in_the_test_prefix(mirror_env):
    module, assets, _, remote, summary = mirror_env
    installer = next(assets.glob("*.exe"))
    old_checksum = installer.with_suffix(".exe.sha256")
    installer = installer.rename(assets / installer.name.replace("-test-", "-test-production-"))
    old_checksum.unlink()
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    installer.with_suffix(".exe.sha256").write_text(f"{digest}  {installer.name}\n")
    module.mirror(assets)
    assert len(remote) == 2
    assert all(key.startswith("oss://test-bucket/releases/test/windows/12345-2/") for key in remote)
    assert installer.name in summary.read_text(encoding="utf-8")
    assert "生产环境" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("OSS_ACCESS_KEY_SECRET", ""),
        ("ALIYUN_OSS_PREFIX", "releases/../latest"),
        ("GITHUB_RUN_ID", "../latest"),
        ("ALIYUN_OSS_BUCKET", "bucket/../../latest"),
    ],
)
def test_rejects_invalid_configuration_without_remote_changes(mirror_env, monkeypatch, key, value):
    module, assets, calls, _, _ = mirror_env
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        module.mirror(assets)
    assert not calls


def test_public_download_failure_does_not_publish_links_or_prune(mirror_env, monkeypatch):
    module, assets, calls, _, summary = mirror_env

    def inaccessible(*args, **kwargs):
        raise OSError("public downloads forbidden")

    monkeypatch.setattr(module, "urlopen", inaccessible)
    with pytest.raises(OSError, match="forbidden"):
        module.mirror(assets)
    assert len(calls) == 2
    assert not summary.exists()


def test_cleanup_failure_preserves_download_links_and_reports_warning(
    mirror_env, monkeypatch, capsys
):
    module, assets, calls, _, summary = mirror_env
    original_run = module.subprocess.run

    def run(args, *, check):
        if "rm" in args:
            raise subprocess.CalledProcessError(1, args)
        return original_run(args, check=check)

    monkeypatch.setattr(module.subprocess, "run", run)
    module.mirror(assets)
    assert len(calls) == 2
    assert "下载安装包" in summary.read_text()
    assert "storage may accumulate" in summary.read_text()
    assert "::warning::" in capsys.readouterr().out


def test_workflow_only_mirrors_successful_build_and_keeps_credentials_in_upload_step():
    workflow = yaml.safe_load(Path(".github/workflows/windows-test-installer.yml").read_text())
    job = workflow["jobs"]["mirror-to-oss"]
    assert job["needs"] == "build"
    assert job["if"] == "github.repository == 'TokenRhythm/opensquilla'"
    assert "env" not in job
    download = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert download["with"]["name"] == "${{ needs.build.outputs.artifact_name }}"
    build = workflow["jobs"]["build"]
    assert build["outputs"]["artifact_name"] == "${{ steps.target.outputs.artifact_name }}"
    artifact_upload = next(
        step for step in build["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert artifact_upload["with"]["name"] == "${{ steps.target.outputs.artifact_name }}"
    assert "test-windows-production-" in build["env"]["OPENSQUILLA_TEST_ENVIRONMENT"]
    upload = job["steps"][-1]
    assert "mirror-windows-test-to-oss.py" in upload["run"]
    assert "secrets.ALIYUN_OSS_ACCESS_KEY_SECRET" in upload["env"]["OSS_ACCESS_KEY_SECRET"]
