from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
_RUNNER_MODULE = "opensquilla.sandbox.python_code_runner"


def _run_runner(
    workspace: Path,
    *operands: str,
    python_flags: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    }
    home = workspace / "home"
    temporary = workspace / "temp"
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    environment.update(
        {
            "PYTHONPATH": str(_SOURCE_ROOT),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "TMP": str(temporary),
            "TEMP": str(temporary),
            "TMPDIR": str(temporary),
            "OPENSQUILLA_STATE_DIR": str(home / "opensquilla"),
        }
    )
    return subprocess.run(
        [sys.executable, *python_flags, "-m", _RUNNER_MODULE, *operands],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


def test_code_uses_a_clean_registered_main_module(tmp_path: Path) -> None:
    result = _run_runner(
        tmp_path,
        "import __main__, json, pickle, sys\n"
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class SyntheticValue:\n"
        "    number: int\n"
        "value = pickle.loads(pickle.dumps(SyntheticValue(42)))\n"
        "print(json.dumps({\n"
        "    'name': __name__, 'argv': sys.argv,\n"
        "    'main_identity': vars(__main__) is globals(),\n"
        "    'value': value.number, 'class_module': type(value).__module__,\n"
        "    'has_file': '__file__' in globals(),\n"
        "    'has_runner_main': 'main' in globals(),\n"
        "    'spec_is_none': __spec__ is None,\n"
        "}))\n",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "name": "__main__",
        "argv": ["-c"],
        "main_identity": True,
        "value": 42,
        "class_module": "__main__",
        "has_file": False,
        "has_runner_main": False,
        "spec_is_none": True,
    }
    assert result.stderr == ""


def test_code_imports_a_sibling_module_from_its_working_directory(tmp_path: Path) -> None:
    (tmp_path / "synthetic_sibling.py").write_text("VALUE = 'sibling imported'\n", encoding="utf-8")

    result = _run_runner(tmp_path, "import synthetic_sibling; print(synthetic_sibling.VALUE)")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "sibling imported\n"
    assert result.stderr == ""


def test_safe_path_does_not_add_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / "synthetic_sibling.py").write_text("VALUE = 42\n", encoding="utf-8")

    result = _run_runner(
        tmp_path,
        "import synthetic_sibling",
        python_flags=("-P",),
    )

    assert result.returncode != 0
    assert "ModuleNotFoundError: No module named 'synthetic_sibling'" in result.stderr


def test_code_does_not_inherit_future_annotations_from_the_runner(tmp_path: Path) -> None:
    result = _run_runner(
        tmp_path,
        "def identity(value: int) -> int:\n"
        "    return value\n"
        "assert identity.__annotations__ == {'value': int, 'return': int}\n"
        "print('evaluated annotations')\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "evaluated annotations\n"


def test_code_can_explicitly_request_future_annotations(tmp_path: Path) -> None:
    result = _run_runner(
        tmp_path,
        "from __future__ import annotations\n"
        "def identity(value: UndefinedType) -> UndefinedType:\n"
        "    return value\n"
        "assert identity.__annotations__ == {'value': 'UndefinedType', 'return': 'UndefinedType'}\n"
        "print('postponed annotations')\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "postponed annotations\n"


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("raise RuntimeError('synthetic failure')", "RuntimeError: synthetic failure"),
        ("if:", "SyntaxError: invalid syntax"),
    ],
)
def test_python_errors_produce_tracebacks_and_nonzero_exit(
    tmp_path: Path,
    code: str,
    error: str,
) -> None:
    result = _run_runner(tmp_path, code)

    assert result.returncode != 0
    assert 'File "<string>", line 1' in result.stderr
    assert error in result.stderr
    assert result.stdout == ""


def test_system_exit_preserves_its_status_and_prior_output(tmp_path: Path) -> None:
    result = _run_runner(tmp_path, "print('before exit', flush=True); raise SystemExit(7)")

    assert result.returncode == 7
    assert result.stdout == "before exit\n"
    assert result.stderr == ""


def test_unicode_stdout_and_stderr_are_preserved(tmp_path: Path) -> None:
    result = _run_runner(
        tmp_path,
        "import sys; print('合成输出'); print('合成错误', file=sys.stderr)",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "合成输出\n"
    assert result.stderr == "合成错误\n"


@pytest.mark.parametrize(
    "operands",
    [(), ("print('must not execute')", "unexpected")],
    ids=["missing-code", "extra-operand"],
)
def test_invalid_operand_count_exits_before_running_code(
    tmp_path: Path,
    operands: tuple[str, ...],
) -> None:
    result = _run_runner(tmp_path, *operands)

    assert result.returncode == 2
    assert result.stdout == ""
