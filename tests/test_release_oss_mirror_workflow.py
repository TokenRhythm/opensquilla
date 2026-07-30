from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml


def _upload_step_script() -> str:
    workflow = yaml.safe_load(
        Path(".github/workflows/mirror-release-to-oss.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["mirror-release-assets"]["steps"]
    return next(step["run"] for step in steps if step["name"] == "Upload release assets to OSS")


def _bash_executable() -> str:
    if os.name != "nt":
        return "bash"

    candidates: list[Path] = []
    git = shutil.which("git")
    if git is not None:
        for parent in Path(git).resolve().parents:
            candidates.extend((parent / "bin" / "bash.exe", parent / "usr" / "bin" / "bash.exe"))
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        git_root = Path(program_files) / "Git"
        candidates.extend((git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("Git for Windows bash.exe is required for this workflow contract test")


def _install_fake_ossutil(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    poison_python3 = fake_bin / "python3"
    poison_python3.write_text(
        "#!/usr/bin/env bash\necho 'unexpected python3 PATH lookup' >&2\nexit 97\n",
        encoding="utf-8",
        newline="\n",
    )
    poison_python3.chmod(0o755)
    remote_root = tmp_path / "oss"
    call_log = tmp_path / "ossutil-calls.jsonl"
    fake_script = fake_bin / "ossutil.py"
    fake_script.write_text(
        textwrap.dedent(
            """\
            import json
            import os
            import shutil
            import sys
            from pathlib import Path

            if os.name == "nt":
                sys.stdout.reconfigure(newline="\\n")

            args = sys.argv[1:]
            with Path(os.environ["FAKE_OSS_LOG"]).open("a", encoding="utf-8") as log:
                log.write(json.dumps(args) + "\\n")

            remote_root = Path(os.environ["FAKE_OSS_ROOT"])

            def native_path(value: str) -> Path:
                if (
                    os.name == "nt"
                    and len(value) >= 3
                    and value[0] == "/"
                    and value[1].isascii()
                    and value[1].isalpha()
                    and value[2] == "/"
                ):
                    value = f"{value[1]}:{value[2:]}"
                return Path(value)

            def mapped(value: str) -> Path:
                if not value.startswith("oss://"):
                    return native_path(value)
                bucket_and_key = value.removeprefix("oss://")
                bucket, _, key = bucket_and_key.partition("/")
                return remote_root / bucket / key

            def option(name: str) -> str:
                return args[args.index(name) + 1]

            if args[:2] == ["api", "get-bucket-versioning"]:
                # Real ossutil follows the JSON body with extra output (an
                # elapsed-time trailer); reproduce that shape so the parser
                # is exercised against production output, not idealized JSON.
                status = os.environ.get("FAKE_OSS_VERSIONING_STATUS", "")
                print(json.dumps({"Status": status} if status else {}))
                print("0.062000(s) elapsed")
                raise SystemExit(0)

            if args[:2] == ["api", "put-object"]:
                destination = remote_root / option("--bucket") / option("--key")
                source_url = option("--body")
                assert source_url.startswith("file://")
                assert option("--forbid-overwrite") == "true"
                if destination.exists():
                    raise SystemExit(9)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(native_path(source_url.removeprefix("file://")), destination)
                raise SystemExit(0)

            if args[0] == "cp":
                source = mapped(args[-2])
                destination = mapped(args[-1])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                raise SystemExit(0)

            if args[0] == "ls":
                object_url = args[-1]
                destination = mapped(object_url)
                race_object = os.environ.get("FAKE_OSS_RACE_OBJECT")
                race_marker = remote_root / ".race-created"
                if (
                    race_object == object_url
                    and not destination.exists()
                    and not race_marker.exists()
                ):
                    race_marker.parent.mkdir(parents=True, exist_ok=True)
                    race_marker.write_text("created", encoding="utf-8")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(b"concurrent-writer")
                    raise SystemExit(0)
                if destination.is_file():
                    print(object_url)
                raise SystemExit(0)

            raise SystemExit(f"unsupported fake ossutil command: {args}")
            """
        ),
        encoding="utf-8",
        newline="\n",
    )
    fake = fake_bin / "ossutil"
    fake.write_text(
        '#!/usr/bin/env bash\nexec "$FAKE_OSS_PYTHON" "$FAKE_OSS_SCRIPT" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    fake.chmod(0o755)
    return fake_bin, remote_root, call_log


def _run_upload_step(
    tmp_path: Path,
    fake_bin: Path,
    remote_root: Path,
    call_log: Path,
    *,
    attempt: int,
    race_object: str | None = None,
    versioning_status: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "ALIYUN_OSS_BUCKET": "release-bucket",
            "ALIYUN_OSS_PREFIX_NORMALIZED": "releases",
            "FAKE_OSS_LOG": str(call_log),
            "FAKE_OSS_PYTHON": Path(sys.executable).as_posix(),
            "FAKE_OSS_ROOT": str(remote_root),
            "FAKE_OSS_SCRIPT": (fake_bin / "ossutil.py").as_posix(),
            "GITHUB_RUN_ATTEMPT": str(attempt),
            "GITHUB_RUN_ID": "12345",
            "OSS_ADDRESSING_STYLE_NORMALIZED": "virtual",
            "OSS_REGION": "cn-beijing",
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "TAG": "v0.5.0rc4",
        }
    )
    if race_object is not None:
        env["FAKE_OSS_RACE_OBJECT"] = race_object
    if versioning_status is not None:
        env["FAKE_OSS_VERSIONING_STATUS"] = versioning_status
    # The production job runs on Ubuntu, where ``python3`` is guaranteed. Keep
    # this cross-platform contract test on the active pytest interpreter rather
    # than the Windows Store ``python3.exe`` app-execution alias.
    script = 'python3() { "$FAKE_OSS_PYTHON" "$@"; }\n' + _upload_step_script()
    # Windows process creation cannot reliably carry this large, heavily quoted
    # workflow body as a ``bash -c`` argument. Execute an LF-normalized file.
    script_path = tmp_path / "upload-step.sh"
    script_path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run(
        [_bash_executable(), script_path.as_posix()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_aliyun_oss_release_mirror_workflow_contract() -> None:
    workflow = Path(".github/workflows/mirror-release-to-oss.yml").read_text(encoding="utf-8")

    assert "name: Mirror Release Assets to Aliyun OSS" in workflow
    assert "release:\n    types: [published]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "group: oss-release-mirror-latest-aliases" in workflow
    assert "uses: actions/checkout@v4" in workflow
    assert "ref: ${{ github.workflow_sha }}" in workflow
    assert "MANUAL_RELEASE_TAG: ${{ inputs.tag }}" in workflow
    assert 'tag="${MANUAL_RELEASE_TAG}"' in workflow
    assert 'tag="${{ inputs.tag }}"' not in workflow
    assert "gh release download" in workflow
    assert "gh release view" in workflow
    assert "gh release list" in workflow
    assert "--limit 1000" in workflow
    assert "--json tagName,isDraft,isPrerelease,publishedAt,url" in workflow
    assert "--json tagName,isDraft,isPrerelease,publishedAt" in workflow
    assert "sha256sum --strict -c SHA256SUMS" in workflow
    assert "CHECKSUMMED_ASSETS" in workflow
    assert "Release assets missing from SHA256SUMS" in workflow
    assert "Duplicate SHA256SUMS filename" in workflow
    assert "ossutil-2.3.0-linux-amd64.zip" in workflow
    assert "OSSUTIL_SHA256" in workflow
    assert "ALIYUN_OSS_ACCESS_KEY_ID" in workflow
    assert "ALIYUN_OSS_ACCESS_KEY_SECRET" in workflow
    assert "ALIYUN_OSS_BUCKET" in workflow
    assert "OSS_REGION" in workflow
    assert "OSS_ENDPOINT" in workflow
    assert "OSS_ADDRESSING_STYLE" in workflow
    assert "ALIYUN_OSS_PREFIX_NORMALIZED" in workflow
    assert "OSS_ADDRESSING_STYLE_NORMALIZED" in workflow
    assert "--addressing-style" in workflow
    assert (
        'dest_prefix="oss://${ALIYUN_OSS_BUCKET}/${ALIYUN_OSS_PREFIX_NORMALIZED}/${TAG}"'
    ) in workflow
    assert "local -a options=(" in workflow
    assert '--cache-control "${cache_control}"' in workflow
    assert '"release-assets/SHA256SUMS" "SHA256SUMS"' in workflow
    assert "Build moving installer aliases" in workflow
    assert 'make_alias "OpenSquilla-*-mac-arm64.dmg" "OpenSquilla-mac-arm64.dmg"' in workflow
    assert 'make_alias "OpenSquilla-*-win-x64.exe" "OpenSquilla-win-x64.exe"' in workflow
    assert 'latest_prefix="${mirror_root}/latest"' in workflow
    assert 'channels_prefix="${mirror_root}/channels"' in workflow
    assert (
        'backup_prefix="${mirror_root}/.promotion-backups/${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"'
    ) in workflow
    assert "rollback_promotions" in workflow
    assert "Rollback backup preserved for manual recovery" in workflow
    assert "local listing" in workflow
    assert "return 2" in workflow
    assert "if (( exists_status != 1 )); then" in workflow
    assert "if (( latest_html_status != 1 )); then" in workflow
    assert 'moving_cache_control="no-cache,max-age=0,must-revalidate"' in workflow
    assert 'versioned_cache_control="public,max-age=31536000,immutable"' in workflow
    assert 'json_content_type="application/json; charset=utf-8"' in workflow
    assert 'html_content_type="text/html; charset=utf-8"' in workflow
    assert "scripts/release_channel_manifest.py build" in workflow
    assert "scripts/release_channel_manifest.py should-promote" in workflow
    assert "scripts/release_channel_manifest.py is-release-head" in workflow
    assert "channel-assets/releases.json" in workflow
    assert "channel-assets/manifest.json" in workflow
    assert "channel-assets/TARGETS" in workflow
    assert "promote_latest_aliases" in workflow
    assert '[[ "${target}" == "latest.json" ]] && promote_latest_aliases=1' in workflow
    assert "verify_immutable_asset" in workflow
    assert "put_immutable_asset" in workflow
    assert "upload_immutable_asset" in workflow
    assert "--forbid-overwrite true" in workflow
    immutable_put = workflow.split("put_immutable_asset()", 1)[1].split(
        "upload_immutable_asset()", 1
    )[0]
    assert "--force" not in immutable_put
    assert "get-bucket-versioning" in workflow
    assert "OSS bucket versioning is " in workflow
    assert "SHA256 verification before every upload" in workflow
    assert "Refusing to replace immutable OSS release object" in workflow
    assert "Publish corrected release assets under a new release tag" in workflow
    assert 'local_digest="$(sha256sum -- "${source}"' in workflow
    assert 'remote_digest="$(sha256sum -- "${remote_copy}"' in workflow
    assert workflow.index("upload_immutable_asset") < workflow.index(
        "scripts/release_channel_manifest.py is-release-head"
    )
    assert '"${moving_cache_control}" "${json_content_type}"' in workflow
    assert "Skipping non-head release" in workflow
    assert "Skipping older channel candidate" in workflow
    assert "backed_up_manifests" in workflow
    assert "promoted_manifests" in workflow
    assert "backed_up_latest_html" in workflow
    assert "removed_latest_html" in workflow
    assert '"${backup_prefix}/legacy/latest.html" "latest.html"' in workflow
    assert '"${mirror_root}/latest.html"' in workflow
    assert workflow.index("scripts/release_channel_manifest.py is-release-head") < workflow.index(
        'remote_manifest="${channels_prefix}/${target}"'
    )
    assert workflow.index('remote_manifest="${channels_prefix}/${target}"') < workflow.index(
        'for target in "${promote_targets[@]}"'
    )
    assert workflow.index('"${latest_prefix}" "${moving_cache_control}"') < workflow.index(
        "# Publish manifests last"
    )
    published_manifests = workflow.index("# Publish manifests last")
    removed_legacy_page = workflow.index('"${mirror_root}/latest.html"', published_manifests)
    assert published_manifests < removed_legacy_page
    legacy_latest_guard = workflow.index(
        "if (( promote_latest_aliases && backed_up_latest_html )); then",
        workflow.index("# Publish manifests last"),
    )
    assert legacy_latest_guard < removed_legacy_page
    assert workflow.index("# Verify the committed view") < workflow.index(
        "trap - EXIT\n          if (("
    )
    verification = workflow.split("# Verify the committed view", 1)[1].split("trap - EXIT", 1)[0]
    assert "if (( promote_latest_aliases )); then" in verification
    assert "if (( ${#promote_targets[@]} )); then" in verification
    assert "::warning::Promotion succeeded, but temporary OSS backups" in workflow


def test_version_scoped_oss_objects_are_write_once_and_race_safe(tmp_path: Path) -> None:
    fake_bin, remote_root, call_log = _install_fake_ossutil(tmp_path)
    release_assets = tmp_path / "release-assets"
    channel_assets = tmp_path / "channel-assets"
    release_assets.mkdir()
    channel_assets.mkdir()
    payload = release_assets / "payload.bin"
    checksums = release_assets / "SHA256SUMS"
    payload.write_bytes(b"first-published-payload")
    checksums.write_text("first-published-checksums\n", encoding="utf-8", newline="\n")
    (release_assets / "CHECKSUMMED_ASSETS").write_text(
        "payload.bin\n", encoding="utf-8", newline="\n"
    )
    (channel_assets / "TARGETS").write_text("", encoding="utf-8", newline="\n")

    first = _run_upload_step(
        tmp_path,
        fake_bin,
        remote_root,
        call_log,
        attempt=1,
    )
    assert first.returncode == 0, f"stdout:\n{first.stdout}\nstderr:\n{first.stderr}"
    remote_release = remote_root / "release-bucket" / "releases" / "v0.5.0rc4"
    assert (remote_release / "payload.bin").read_bytes() == b"first-published-payload"
    assert (remote_release / "SHA256SUMS").read_text(encoding="utf-8") == (
        "first-published-checksums\n"
    )

    call_log.write_text("", encoding="utf-8")
    identical = _run_upload_step(
        tmp_path,
        fake_bin,
        remote_root,
        call_log,
        attempt=2,
    )
    assert identical.returncode == 0, identical.stderr
    identical_calls = [json.loads(line) for line in call_log.read_text().splitlines()]
    assert not any(call[:2] == ["api", "put-object"] for call in identical_calls)

    payload.write_bytes(b"mutated-payload")
    checksums.write_text("mutated-checksums\n", encoding="utf-8", newline="\n")
    changed = _run_upload_step(
        tmp_path,
        fake_bin,
        remote_root,
        call_log,
        attempt=3,
    )
    assert changed.returncode != 0
    assert "Refusing to replace immutable OSS release object" in changed.stderr
    assert (remote_release / "payload.bin").read_bytes() == b"first-published-payload"
    assert (remote_release / "SHA256SUMS").read_text(encoding="utf-8") == (
        "first-published-checksums\n"
    )

    racy_payload = release_assets / "racy.bin"
    racy_payload.write_bytes(b"workflow-payload")
    (release_assets / "CHECKSUMMED_ASSETS").write_text("racy.bin\n", encoding="utf-8", newline="\n")
    race_url = "oss://release-bucket/releases/v0.5.0rc4/racy.bin"
    raced = _run_upload_step(
        tmp_path,
        fake_bin,
        remote_root,
        call_log,
        attempt=4,
        race_object=race_url,
    )
    assert raced.returncode != 0
    assert "Refusing to replace immutable OSS release object" in raced.stderr
    assert (remote_release / "racy.bin").read_bytes() == b"concurrent-writer"

    # Versioned buckets no longer block the mirror: forbid-overwrite is
    # advisory there, and immutability rests on the existence check plus
    # SHA256 verification. An identical re-run stays idempotent and the run
    # records which versioning regime it executed under.
    racy_payload.write_bytes(b"concurrent-writer")
    checksums.write_text("first-published-checksums\n", encoding="utf-8", newline="\n")
    for attempt, status in enumerate(("Enabled", "Suspended"), start=5):
        call_log.write_text("", encoding="utf-8")
        versioned = _run_upload_step(
            tmp_path,
            fake_bin,
            remote_root,
            call_log,
            attempt=attempt,
            versioning_status=status,
        )
        assert versioned.returncode == 0, versioned.stderr
        assert f"OSS bucket versioning is {status!r}" in versioned.stdout
        versioned_calls = [json.loads(line) for line in call_log.read_text().splitlines()]
        assert not any(call[:2] == ["api", "put-object"] for call in versioned_calls)

    # The write-once contract survives on a versioned bucket: divergent bytes
    # for an already-mirrored object are still refused before any upload.
    racy_payload.write_bytes(b"tampered-payload")
    tampered = _run_upload_step(
        tmp_path,
        fake_bin,
        remote_root,
        call_log,
        attempt=7,
        versioning_status="Enabled",
    )
    assert tampered.returncode != 0
    assert "Refusing to replace immutable OSS release object" in tampered.stderr
    assert (remote_release / "racy.bin").read_bytes() == b"concurrent-writer"
