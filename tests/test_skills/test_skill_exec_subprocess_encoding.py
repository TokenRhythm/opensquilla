"""Encoding contracts for ``skill_exec`` subprocess boundaries."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla import subprocess_encoding
from opensquilla.skills.meta.executors import skill_exec
from opensquilla.skills.meta.types import MetaStep
from opensquilla.skills.types import SkillLayer, SkillSpec


class _Loader:
    def __init__(self, spec: SkillSpec) -> None:
        self._spec = spec

    def get_by_name(self, name: str) -> SkillSpec | None:
        return self._spec if name == self._spec.name else None


class _FakeOwnedProcess:
    pid = 4242

    def __init__(self, *, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdin: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin = input
        return self._stdout, self._stderr


def _mock_owned_launcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_spawn(*argv: str, **kwargs: Any) -> _FakeOwnedProcess:
        process = _FakeOwnedProcess(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        captured.update(argv=argv, kwargs=kwargs, process=process)
        return process

    monkeypatch.setattr(skill_exec, "create_owned_subprocess_exec", fake_spawn)
    return captured


def _assert_canonical_spawn(captured: dict[str, Any], base_dir: Path) -> None:
    assert captured["argv"] == (sys.executable, str(base_dir / "unused.py"))
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == str(base_dir)
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE
    assert kwargs["stdin"] is None
    assert isinstance(kwargs["env"], dict)
    process = captured["process"]
    assert isinstance(process, _FakeOwnedProcess)
    assert process.stdin is None


def _spec(
    base_dir: Path,
    *,
    parse: str = "text",
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    script: Path | None = None,
) -> SkillSpec:
    entrypoint: dict[str, object] = {
        "command": "python",
        "args": [str(script or (base_dir / "unused.py"))],
        "parse": parse,
        "timeout": 10.0,
    }
    if stdin is not None:
        entrypoint["stdin"] = stdin
    if env is not None:
        entrypoint["env"] = env
    return SkillSpec(
        name="encoding-skill",
        description="synthetic encoding probe",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        base_dir=str(base_dir),
        entrypoint=entrypoint,
    )


async def _run(spec: SkillSpec, base_dir: Path) -> str:
    return await skill_exec.run_skill_exec_step(
        MetaStep(id="encoding", kind="skill_exec", skill=spec.name),
        effective_skill=spec.name,
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(base_dir),
    )


@pytest.mark.asyncio
async def test_skill_exec_forces_utf8_for_windows_python_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GBK-default child must emit lossless Unicode JSON through stdout."""

    script = tmp_path / "unicode_json.py"
    script.write_text(
        "import json, os, sys\n"
        "if 'PYTHONIOENCODING' not in os.environ:\n"
        "    sys.stdout.reconfigure(encoding='gbk', errors='strict')\n"
        "payload = json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
        "sys.stdout.write(json.dumps(payload, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    payload = {
        "author": "Badura, ś",
        "title": "大型语言模型与 café – verified",
    }
    spec = _spec(
        tmp_path,
        parse="json",
        stdin=json.dumps(payload, ensure_ascii=False),
        script=script,
    )

    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    # Patch only the helper module's platform view. Mutating the shared
    # ``os.name`` object would make pathlib try to construct WindowsPath on POSIX.
    monkeypatch.setattr(subprocess_encoding, "os", SimpleNamespace(name="nt"))

    output = await _run(spec, tmp_path)

    assert json.loads(output) == payload


@pytest.mark.asyncio
async def test_skill_exec_forces_utf8_for_windows_python_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UTF-8 manifest stdin must not be decoded through a child's GBK default."""

    script = tmp_path / "unicode_stdin.py"
    script.write_text(
        "import os, sys\n"
        "if 'PYTHONIOENCODING' not in os.environ:\n"
        "    sys.stdin.reconfigure(encoding='gbk', errors='strict')\n"
        "text = sys.stdin.read()\n"
        "sys.stdout.buffer.write(text.encode('utf-8'))\n",
        encoding="utf-8",
    )
    expected = "大型语言模型对大学生批判性思维的影响"
    spec = _spec(tmp_path, stdin=expected, script=script)

    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.setattr(subprocess_encoding, "os", SimpleNamespace(name="nt"))

    output = await _run(spec, tmp_path)

    assert output == expected


@pytest.mark.asyncio
async def test_skill_exec_decodes_legacy_gbk_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"query": "中文检索", "title": "大学生批判性思维"}
    stdout = json.dumps(payload, ensure_ascii=False).encode("gbk")
    decoded: list[bytes | None] = []

    def decode_gbk(raw: bytes | None) -> str:
        decoded.append(raw)
        return subprocess_encoding.decode_subprocess_output(raw, fallback_encoding="gbk")

    monkeypatch.setattr(skill_exec, "decode_subprocess_output", decode_gbk)
    captured = _mock_owned_launcher(
        monkeypatch,
        returncode=0,
        stdout=stdout,
        stderr=b"",
    )

    output = await _run(_spec(tmp_path, parse="json"), tmp_path)

    assert json.loads(output) == payload
    assert decoded == [stdout, b""]
    _assert_canonical_spawn(captured, tmp_path)


@pytest.mark.asyncio
async def test_skill_exec_decodes_legacy_gbk_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "检索失败：上游不可用".encode("gbk")

    def decode_gbk(raw: bytes | None) -> str:
        return subprocess_encoding.decode_subprocess_output(raw, fallback_encoding="gbk")

    monkeypatch.setattr(skill_exec, "decode_subprocess_output", decode_gbk)
    captured = _mock_owned_launcher(
        monkeypatch,
        returncode=7,
        stdout=b"",
        stderr=stderr,
    )

    with pytest.raises(RuntimeError, match="检索失败：上游不可用"):
        await _run(_spec(tmp_path), tmp_path)
    _assert_canonical_spawn(captured, tmp_path)


@pytest.mark.parametrize("source", ["ambient", "entrypoint"])
@pytest.mark.asyncio
async def test_skill_exec_preserves_explicit_python_encoding_env(
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = {"PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"}
    captured: dict[str, str] = {}

    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    if source == "ambient":
        for key, value in explicit.items():
            monkeypatch.setenv(key, value)
        entrypoint_env = None
    else:
        entrypoint_env = explicit
    monkeypatch.setattr(subprocess_encoding, "os", SimpleNamespace(name="nt"))

    spawned = _mock_owned_launcher(
        monkeypatch,
        returncode=0,
        stdout=b"ok\n",
        stderr=b"",
    )

    output = await _run(_spec(tmp_path, env=entrypoint_env), tmp_path)

    child_env = spawned["kwargs"]["env"]
    assert isinstance(child_env, dict)
    captured.update(child_env)
    assert output == "ok"
    assert {key: captured[key] for key in explicit} == explicit
    _assert_canonical_spawn(spawned, tmp_path)
