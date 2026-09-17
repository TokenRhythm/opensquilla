from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

_VERSION = "0.5.4"
_TAG = f"v{_VERSION}"
_ASSETS = (
    f"OpenSquilla-{_VERSION}-mac-arm64.dmg",
    f"OpenSquilla-{_VERSION}-mac-arm64.zip",
    f"OpenSquilla-{_VERSION}-mac-arm64.dmg.blockmap",
    f"OpenSquilla-{_VERSION}-mac-arm64.zip.blockmap",
    "latest-mac.yml",
    f"OpenSquilla-{_VERSION}-win-x64.exe",
    f"OpenSquilla-{_VERSION}-win-x64.exe.blockmap",
    "latest.yml",
    f"opensquilla-{_VERSION}-py3-none-any.whl",
)


def _write_release_assets(root: Path, *, changed_dmg: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name in _ASSETS:
        payload = f"synthetic-{name}".encode()
        if changed_dmg and name.endswith(".dmg"):
            payload += b"-changed"
        (root / name).write_bytes(payload)
        lines.append(f"{hashlib.sha256(payload).hexdigest()}  {name}")
    checksum_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    (root / "SHA256SUMS").write_bytes(checksum_bytes)


def _install_fake_ossutil(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    remote_root = tmp_path / "oss"
    call_log = tmp_path / "calls.jsonl"
    implementation = fake_bin / "ossutil.py"
    implementation.write_text(
        textwrap.dedent(
            """\
            import json
            import os
            import shutil
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            with Path(os.environ["FAKE_OSS_LOG"]).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(args) + "\\n")
            root = Path(os.environ["FAKE_OSS_ROOT"])

            def native_path(value: str) -> Path:
                if (
                    os.name == "nt"
                    and len(value) >= 3
                    and value[0] == "/"
                    and value[1].isalpha()
                    and value[2] == "/"
                ):
                    value = f"{value[1].upper()}:{value[2:]}"
                return Path(value)

            def mapped(value: str) -> Path:
                if not value.startswith("oss://"):
                    return native_path(value)
                bucket_key = value.removeprefix("oss://")
                bucket, _, key = bucket_key.partition("/")
                return root / bucket / key

            def option(name: str) -> str:
                return args[args.index(name) + 1]

            if args[:2] == ["api", "put-object"]:
                destination = root / option("--bucket") / option("--key")
                if destination.exists():
                    raise SystemExit(9)
                assert option("--forbid-overwrite") == "true"
                source = native_path(option("--body").removeprefix("file://"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                raise SystemExit(0)

            if args[0] == "cp":
                source = mapped(args[-2])
                destination = mapped(args[-1])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                raise SystemExit(0)

            if args[0] == "ls":
                object_url = args[-1]
                destination = mapped(object_url.rstrip("/"))
                if "--short-format" in args:
                    if destination.is_file():
                        print(object_url)
                elif destination.is_dir():
                    for path in sorted(destination.iterdir()):
                        print(f"{object_url.rstrip('/')}/{path.name}")
                raise SystemExit(0)

            raise SystemExit(f"unsupported fake ossutil command: {args}")
            """
        ),
        encoding="utf-8",
    )
    executable = fake_bin / "ossutil"
    executable.write_text(
        '#!/usr/bin/env bash\nexec "$PYTHON_BIN" "$FAKE_OSSUTIL" "$@"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    python = fake_bin / "opensquilla-test-python"
    python.write_text(
        "#!/usr/bin/env bash\n"
        'python_path="$FAKE_PYTHON"\n'
        "if command -v cygpath >/dev/null 2>&1; then\n"
        '  python_path="$(cygpath -u "$python_path")"\n'
        "fi\n"
        'exec "$python_path" "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    return fake_bin, remote_root, call_log


def _bash_executable() -> str:
    if os.name != "nt":
        return shutil.which("bash") or "bash"
    git = shutil.which("git")
    if git is not None:
        candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidate = Path(program_files) / "Git" / "bin" / "bash.exe"
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError("Git for Windows Bash is required for the OSS prestage contract test")


def _run(tmp_path: Path, assets: Path) -> subprocess.CompletedProcess[str]:
    fake_bin, remote_root, call_log = (
        _install_fake_ossutil(tmp_path)
        if not (tmp_path / "bin").exists()
        else (tmp_path / "bin", tmp_path / "oss", tmp_path / "calls.jsonl")
    )
    env = os.environ.copy()
    env.update(
        {
            "ALIYUN_OSS_BUCKET": "release-bucket",
            "ALIYUN_OSS_PREFIX_NORMALIZED": "releases",
            "FAKE_OSS_LOG": str(call_log),
            "FAKE_OSS_ROOT": str(remote_root),
            "FAKE_OSSUTIL": str(fake_bin / "ossutil.py"),
            "FAKE_PYTHON": sys.executable,
            "OSS_ADDRESSING_STYLE_NORMALIZED": "virtual",
            "OSSUTIL_BIN": (fake_bin / "ossutil").as_posix(),
            "PYTHON_BIN": (fake_bin / "opensquilla-test-python").as_posix(),
        }
    )
    return subprocess.run(
        [
            _bash_executable(),
            ".github/scripts/prestage-release-to-oss.sh",
            str(assets),
            _TAG,
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_prestage_is_version_scoped_write_once_and_idempotent(tmp_path: Path) -> None:
    assets = tmp_path / "release-assets"
    _write_release_assets(assets)

    first = _run(tmp_path, assets)
    assert first.returncode == 0, first.stderr
    remote = tmp_path / "oss" / "release-bucket" / "releases" / _TAG
    assert {path.name for path in remote.iterdir()} == {*_ASSETS, "SHA256SUMS"}

    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    keys = [call[call.index("--key") + 1] for call in calls if call[:2] == ["api", "put-object"]]
    assert len(keys) == len(_ASSETS) + 1
    assert all(key.startswith(f"releases/{_TAG}/") for key in keys)
    assert all("/channels/" not in key and "/latest/" not in key for key in keys)

    (tmp_path / "calls.jsonl").write_text("", encoding="utf-8")
    identical = _run(tmp_path, assets)
    assert identical.returncode == 0, identical.stderr
    identical_calls = [
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    assert not any(call[:2] == ["api", "put-object"] for call in identical_calls)

    _write_release_assets(assets, changed_dmg=True)
    changed = _run(tmp_path, assets)
    assert changed.returncode != 0
    assert "Refusing to replace immutable OSS Draft asset" in changed.stderr


def test_prestage_rejects_non_exact_release_asset_set(tmp_path: Path) -> None:
    assets = tmp_path / "release-assets"
    _write_release_assets(assets)
    (assets / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    result = _run(tmp_path, assets)
    assert result.returncode != 0
    assert "Draft release asset set is not exact" in result.stderr
    assert not (tmp_path / "oss").exists()
