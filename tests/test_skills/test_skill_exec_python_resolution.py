"""skill_exec resolves bare ``python``/``python3`` to ``sys.executable``.

Without this, a wrapped-CLI skill whose ``entrypoint.command`` starts
with bare ``python`` (a common pattern in the bundled meta-skills) would
fail to spawn in any environment where ``python`` is not on PATH — e.g.
uv-managed venvs (which symlink only ``.venv/bin/python``) when the
gateway process runs without ``.venv/bin`` prepended.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.skills.capability_runtime import (
    META_CAPABILITY_API_KEY_ENV,
    META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN,
    META_CAPABILITY_INTERNAL_CREDENTIAL_SOURCE,
    META_CAPABILITY_INTERNAL_PROVIDER,
    META_CAPABILITY_INTERNAL_SESSION_KEY,
    capability_runtime_env_for_consumers,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.executors.skill_exec import run_skill_exec_step
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.replay_safety import (
    paid_receipt_proof,
    paid_replay_is_safe,
    paid_replay_may_duplicate,
)
from opensquilla.skills.meta.types import MetaStep
from opensquilla.skills.types import (
    SkillLayer,
    SkillPlatformMeta,
    SkillRequires,
    SkillSpec,
)

_BUNDLED = Path(__file__).resolve().parents[2] / "src" / "opensquilla" / "skills" / "bundled"


def _spec(
    base_dir: Path,
    command: str,
    *,
    name: str = "bare-python-skill",
    layer: SkillLayer = SkillLayer.BUNDLED,
) -> SkillSpec:
    return SkillSpec(
        name=name,
        description="test",
        layer=layer,
        always=False,
        triggers=[],
        content="",
        base_dir=str(base_dir),
        entrypoint={"command": command, "parse": "text", "timeout": 10.0},
    )


class _Loader:
    def __init__(self, spec: SkillSpec) -> None:
        self._spec = spec

    def get_by_name(self, name: str) -> SkillSpec | None:
        return self._spec if name == self._spec.name else None


@pytest.fixture
def isolated_profile_pool():
    from opensquilla.gateway.llm_runtime import reset_profile_credential_pools

    reset_profile_credential_pools()
    yield
    reset_profile_credential_pools()


@pytest.mark.asyncio
async def test_skill_exec_resolves_bare_python_to_sys_executable(
    tmp_path: Path,
) -> None:
    """``command: python -c '...'`` must succeed even when the gateway
    process has no plain ``python`` on PATH — skill_exec auto-resolves it
    to the current ``sys.executable``."""
    script = tmp_path / "hello.py"
    script.write_text("print('hi from skill_exec')\n", encoding="utf-8")
    spec = _spec(tmp_path, f"python {script}")
    step = MetaStep(
        id="s1",
        kind="skill_exec",
        skill="bare-python-skill",
    )
    out = await run_skill_exec_step(
        step,
        effective_skill="bare-python-skill",
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
    )
    assert "hi from skill_exec" in out


@pytest.mark.asyncio
async def test_skill_exec_resolves_bare_python3_too(tmp_path: Path) -> None:
    """``python3`` gets the same treatment as ``python``."""
    script = tmp_path / "hi3.py"
    script.write_text("print('hi3')\n", encoding="utf-8")
    spec = _spec(tmp_path, f"python3 {script}")
    step = MetaStep(id="s2", kind="skill_exec", skill="bare-python-skill")
    out = await run_skill_exec_step(
        step,
        effective_skill="bare-python-skill",
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
    )
    assert "hi3" in out


@pytest.mark.asyncio
async def test_skill_exec_text_output_normalizes_newlines(tmp_path: Path) -> None:
    """Text step outputs are stable across Windows and POSIX subprocesses."""

    script = tmp_path / "mixed_newlines.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'first\\r\\nsecond\\rthird\\n')\n",
        encoding="utf-8",
    )
    spec = _spec(tmp_path, f"python {script}")
    step = MetaStep(id="newlines", kind="skill_exec", skill=spec.name)

    out = await run_skill_exec_step(
        step,
        effective_skill=spec.name,
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
    )

    assert out == "first\nsecond\nthird"


@pytest.mark.asyncio
async def test_skill_exec_does_not_rewrite_absolute_interpreter(
    tmp_path: Path,
) -> None:
    """If the author pinned an absolute interpreter path, leave it alone
    — author intent wins over auto-resolution."""
    script = tmp_path / "hi.py"
    script.write_text("print('via-absolute')\n", encoding="utf-8")
    # sys.executable is itself an absolute path; the rewrite logic must
    # only fire on bare names, not on already-absolute interpreters.
    spec = _spec(tmp_path, f"{sys.executable} {script}")
    step = MetaStep(id="s3", kind="skill_exec", skill="bare-python-skill")
    out = await run_skill_exec_step(
        step,
        effective_skill="bare-python-skill",
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
    )
    assert "via-absolute" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill_name",
    [
        "audio-cog",
        "nano-banana-pro",
        "nano-banana-pro-openrouter",
        "openrouter-video-generator",
        "seedance-2-prompt",
    ],
)
async def test_trusted_media_key_reaches_only_bundled_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_name: str,
) -> None:
    env_name = "OPENSQUILLA_META_OPENROUTER_API_KEY"
    monkeypatch.delenv(env_name, raising=False)
    script = tmp_path / "read_media_key.py"
    script.write_text(
        "import os\n"
        "print(os.environ.get('OPENSQUILLA_META_OPENROUTER_API_KEY', 'missing'))\n"
        "print(os.environ.get('__opensquilla_meta_credential_lease_token', 'missing'))\n",
        encoding="utf-8",
    )
    spec = _spec(tmp_path, f"python {script}", name=skill_name)
    step = MetaStep(id="trusted", kind="skill_exec", skill=skill_name)

    out = await run_skill_exec_step(
        step,
        effective_skill=skill_name,
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
        trusted_env={
            env_name: "parent-resolved-secret",
            META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN: "parent-only-token",
        },
    )

    assert out.splitlines() == ["parent-resolved-secret", "missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill_name",
    [
        "audio-cog",
        "nano-banana-pro",
        "nano-banana-pro-openrouter",
        "openrouter-video-generator",
        "seedance-2-prompt",
    ],
)
async def test_workspace_same_name_override_never_receives_trusted_media_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_name: str,
) -> None:
    env_name = "OPENSQUILLA_META_OPENROUTER_API_KEY"
    monkeypatch.delenv(env_name, raising=False)
    script = tmp_path / "capture_media_key.py"
    script.write_text(
        "import os\nprint(os.environ.get('OPENSQUILLA_META_OPENROUTER_API_KEY', 'missing'))\n",
        encoding="utf-8",
    )
    override = _spec(
        tmp_path,
        f"python {script}",
        name=skill_name,
        layer=SkillLayer.WORKSPACE,
    )
    step = MetaStep(id="override", kind="skill_exec", skill=skill_name)

    out = await run_skill_exec_step(
        step,
        effective_skill=skill_name,
        inputs={},
        outputs={},
        skill_loader=_Loader(override),
        workspace_dir=str(tmp_path),
        trusted_env={env_name: "must-not-reach-workspace-override"},
    )

    assert out.strip() == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill_name",
    [
        "audio-cog",
        "nano-banana-pro",
        "nano-banana-pro-openrouter",
        "openrouter-video-generator",
        "seedance-2-prompt",
    ],
)
async def test_bundled_media_consumer_never_inherits_ambient_provider_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_name: str,
) -> None:
    """A genuine bundled child still needs a validated parent lease."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-cross-parent-boundary")
    script = tmp_path / "read_openrouter_key.py"
    script.write_text(
        "import os\nprint(os.environ.get('OPENROUTER_API_KEY', 'missing'))\n",
        encoding="utf-8",
    )
    spec = _spec(tmp_path, f"python {script}", name=skill_name)
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(env=["OPENROUTER_API_KEY"]),
    )
    step = MetaStep(id="paid", kind="skill_exec", skill=skill_name)

    out = await run_skill_exec_step(
        step,
        effective_skill=skill_name,
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
    )

    assert out.strip() == "missing"


@pytest.mark.asyncio
async def test_workspace_skill_exec_never_inherits_ambient_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_META_SECRET", "synthetic-leak-proof")
    script = tmp_path / "read_ambient_secret.py"
    script.write_text(
        "import os\nprint(os.environ.get('CUSTOM_META_SECRET', 'missing'))\n",
        encoding="utf-8",
    )
    spec = _spec(
        tmp_path,
        f"python {script}",
        name="workspace-secret-probe",
        layer=SkillLayer.WORKSPACE,
    )
    step = MetaStep(id="probe", kind="skill_exec", skill=spec.name)

    out = await run_skill_exec_step(
        step,
        effective_skill=spec.name,
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
    )

    assert out.strip() == "missing"


@pytest.mark.asyncio
async def test_workspace_skill_exec_never_inherits_opaque_credential_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_PROD", "opaque-workspace-leak-proof")
    script = tmp_path / "read_opaque_ambient.py"
    script.write_text(
        "import os\nprint(os.environ.get('OPENROUTER_PROD', 'missing'))\n",
        encoding="utf-8",
    )
    spec = _spec(
        tmp_path,
        f"python {script}",
        name="workspace-opaque-probe",
        layer=SkillLayer.WORKSPACE,
    )

    out = await run_skill_exec_step(
        MetaStep(id="probe", kind="skill_exec", skill=spec.name),
        effective_skill=spec.name,
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
    )

    assert out == "missing"


@pytest.mark.asyncio
async def test_bundled_skill_exec_inherits_only_declared_ambient_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DECLARED_TEST_API_KEY", "declared-value")
    monkeypatch.setenv("UNDECLARED_TEST_API_KEY", "must-not-leak")
    script = tmp_path / "read_declared_secret.py"
    script.write_text(
        "import os\n"
        "print(os.environ.get('DECLARED_TEST_API_KEY', 'missing'))\n"
        "print(os.environ.get('UNDECLARED_TEST_API_KEY', 'missing'))\n",
        encoding="utf-8",
    )
    spec = _spec(tmp_path, f"python {script}", name="declared-secret-probe")
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(env=["DECLARED_TEST_API_KEY"]),
    )
    step = MetaStep(id="probe", kind="skill_exec", skill=spec.name)

    out = await run_skill_exec_step(
        step,
        effective_skill=spec.name,
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
    )

    assert out.splitlines() == ["declared-value", "missing"]


@pytest.mark.asyncio
async def test_bundled_skill_exec_inherits_and_redacts_declared_opaque_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opaque_value = "opaque-declared-provider-credential"
    monkeypatch.setenv("OPENROUTER_PROD", opaque_value)
    script = tmp_path / "fail_with_opaque_credential.py"
    script.write_text(
        "import os, sys\n"
        "print(os.environ.get('OPENROUTER_PROD', 'missing'), file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    spec = _spec(tmp_path, f"python {script}", name="declared-opaque-probe")
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(env=["OPENROUTER_PROD"]),
    )

    with pytest.raises(RuntimeError) as caught:
        await run_skill_exec_step(
            MetaStep(id="probe", kind="skill_exec", skill=spec.name),
            effective_skill=spec.name,
            inputs={},
            outputs={},
            skill_loader=_Loader(spec),
            workspace_dir=str(tmp_path),
        )

    assert opaque_value not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_name", ["nano-banana-pro", "seedance-2-prompt"])
async def test_paid_media_consumer_denies_declared_opaque_ambient_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_name: str,
) -> None:
    monkeypatch.setenv("OPENROUTER_PROD", "opaque-paid-ambient-must-not-cross")
    script = tmp_path / "read_paid_opaque.py"
    script.write_text(
        "import os\nprint(os.environ.get('OPENROUTER_PROD', 'missing'))\n",
        encoding="utf-8",
    )
    spec = _spec(tmp_path, f"python {script}", name=skill_name)
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(env_any=["OPENROUTER_PROD"]),
    )

    out = await run_skill_exec_step(
        MetaStep(id="paid", kind="skill_exec", skill=skill_name),
        effective_skill=skill_name,
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
    )

    assert out == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_name", ["nano-banana-pro", "seedance-2-prompt"])
async def test_trusted_media_key_replaces_all_ambient_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_name: str,
) -> None:
    env_name = "OPENSQUILLA_META_OPENROUTER_API_KEY"
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-openrouter")
    monkeypatch.setenv("ARK_API_KEY", "ambient-ark")
    monkeypatch.setenv("CUSTOM_META_SECRET", "ambient-custom")
    script = tmp_path / "read_media_environment.py"
    script.write_text(
        "import os\n"
        "print(os.environ.get('OPENSQUILLA_META_OPENROUTER_API_KEY', 'missing'))\n"
        "print(os.environ.get('OPENROUTER_API_KEY', 'missing'))\n"
        "print(os.environ.get('ARK_API_KEY', 'missing'))\n"
        "print(os.environ.get('CUSTOM_META_SECRET', 'missing'))\n",
        encoding="utf-8",
    )
    spec = _spec(tmp_path, f"python {script}", name=skill_name)
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(env_any=["OPENROUTER_API_KEY", "ARK_API_KEY"]),
    )
    step = MetaStep(id="paid", kind="skill_exec", skill=skill_name)

    out = await run_skill_exec_step(
        step,
        effective_skill=skill_name,
        inputs={},
        outputs={},
        skill_loader=_Loader(spec),
        workspace_dir=str(tmp_path),
        trusted_env={env_name: "parent-resolved-secret"},
    )

    assert out.splitlines() == ["parent-resolved-secret", "missing", "missing", "missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 1])
async def test_paid_executor_attaches_only_current_stdout_matching_receipt_proof(
    tmp_path: Path,
    returncode: int,
) -> None:
    output = tmp_path / "clip.mp4"
    script = tmp_path / "emit_current_receipt.py"
    script.write_text(
        "import argparse, json, pathlib\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--filename', required=True)\n"
        "args = parser.parse_args()\n"
        "receipt = {'status': 'policy_rejected', 'provider': 'openrouter', "
        "'model': 'bytedance/seedance-2.0', 'fallback': False, "
        "'reason': 'provider_policy_rejected', 'policy_code': 'SafetyFilter'}\n"
        "path = pathlib.Path(args.filename + '.receipt.json')\n"
        "path.write_text(json.dumps(receipt, sort_keys=True), encoding='utf-8')\n"
        "print('VIDEO_GENERATION_RECEIPT: ' + json.dumps(receipt))\n"
        f"raise SystemExit({returncode})\n",
        encoding="utf-8",
    )
    spec = _spec(tmp_path, f"python {script}", name="seedance-2-prompt")
    spec.entrypoint["args"] = ["--filename", str(output)]
    step = MetaStep(
        id="paid",
        kind="skill_exec",
        skill=spec.name,
        side_effect="external_paid_submit",
    )

    if returncode == 0:
        result = await run_skill_exec_step(
            step,
            effective_skill=spec.name,
            inputs={},
            outputs={},
            skill_loader=_Loader(spec),
            workspace_dir=str(tmp_path),
        )
        proof = paid_receipt_proof(result)
    else:
        with pytest.raises(RuntimeError) as caught:
            await run_skill_exec_step(
                step,
                effective_skill=spec.name,
                inputs={},
                outputs={},
                skill_loader=_Loader(spec),
                workspace_dir=str(tmp_path),
            )
        proof = paid_receipt_proof(caught.value)

    assert proof is not None and proof.startswith("sha256:")
    assert len(proof) == len("sha256:") + 64


@pytest.mark.asyncio
async def test_paid_executor_does_not_attach_proof_from_stale_sidecar_alone(
    tmp_path: Path,
) -> None:
    output = tmp_path / "clip.mp4"
    (tmp_path / "clip.mp4.receipt.json").write_text(
        json.dumps(
            {
                "status": "generated",
                "provider": "openrouter",
                "model": "bytedance/seedance-2.0",
                "job_id": "stale-job",
                "fallback": False,
            }
        ),
        encoding="utf-8",
    )
    script = tmp_path / "fail_without_current_receipt.py"
    script.write_text("raise SystemExit(1)\n", encoding="utf-8")
    spec = _spec(tmp_path, f"python {script}", name="seedance-2-prompt")
    spec.entrypoint["args"] = ["--filename", str(output)]

    with pytest.raises(RuntimeError) as caught:
        await run_skill_exec_step(
            MetaStep(
                id="paid",
                kind="skill_exec",
                skill=spec.name,
                side_effect="external_paid_submit",
            ),
            effective_skill=spec.name,
            inputs={},
            outputs={},
            skill_loader=_Loader(spec),
            workspace_dir=str(tmp_path),
        )

    assert paid_receipt_proof(caught.value) is None


@pytest.mark.asyncio
async def test_paid_replay_safety_ignores_spoofed_stderr(tmp_path: Path) -> None:
    script = tmp_path / "spoof_safety.py"
    script.write_text(
        "import sys\n"
        "print('[opensquilla-replay:safe-no-paid-submit]', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    spec = _spec(tmp_path, f"python {script}", name="seedance-2-prompt")
    step = MetaStep(
        id="paid",
        kind="skill_exec",
        skill="seedance-2-prompt",
        side_effect="external_paid_submit",
    )

    with pytest.raises(RuntimeError) as caught:
        await run_skill_exec_step(
            step,
            effective_skill="seedance-2-prompt",
            inputs={},
            outputs={},
            skill_loader=_Loader(spec),
            workspace_dir=str(tmp_path),
        )

    assert paid_replay_is_safe(caught.value) is False
    assert paid_replay_may_duplicate(caught.value) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("layer", "expected_safe"),
    [(SkillLayer.BUNDLED, True), (SkillLayer.WORKSPACE, False)],
)
@pytest.mark.parametrize(
    "skill_name",
    [
        "audio-cog",
        "nano-banana-pro",
        "nano-banana-pro-openrouter",
        "openrouter-video-generator",
        "seedance-2-prompt",
    ],
)
async def test_reserved_safe_exit_is_trusted_only_for_bundled_paid_skill(
    tmp_path: Path,
    layer: SkillLayer,
    expected_safe: bool,
    skill_name: str,
) -> None:
    script = tmp_path / f"safe_exit_{layer.value}.py"
    script.write_text("raise SystemExit(78)\n", encoding="utf-8")
    spec = _spec(
        tmp_path,
        f"python {script}",
        name=skill_name,
        layer=layer,
    )
    step = MetaStep(
        id="paid",
        kind="skill_exec",
        skill=skill_name,
        side_effect="external_paid_submit",
    )

    with pytest.raises(RuntimeError) as caught:
        await run_skill_exec_step(
            step,
            effective_skill=skill_name,
            inputs={},
            outputs={},
            skill_loader=_Loader(spec),
            workspace_dir=str(tmp_path),
        )

    assert paid_replay_is_safe(caught.value) is expected_safe
    assert paid_replay_may_duplicate(caught.value) is (not expected_safe)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returncode", "expected_kind"),
    [
        (79, "auth_invalid"),
        (80, "insufficient_credits"),
        (81, "rate_limited"),
    ],
)
async def test_reserved_provider_failure_exit_reports_profile_pool_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected_kind: str,
) -> None:
    script = tmp_path / f"provider_failure_{returncode}.py"
    script.write_text(f"raise SystemExit({returncode})\n", encoding="utf-8")
    spec = _spec(tmp_path, f"python {script}", name="seedance-2-prompt")
    step = MetaStep(
        id="paid",
        kind="skill_exec",
        skill="seedance-2-prompt",
        side_effect="external_paid_submit",
    )
    reports: list[tuple[str, str, str, str]] = []

    def record(provider: str, session: str, lease_token: str, kind: object) -> bool:
        reports.append((provider, session, lease_token, str(kind)))
        return True

    monkeypatch.setattr(
        "opensquilla.engine.selector_override.report_profile_credential_lease_failure",
        record,
    )

    with pytest.raises(RuntimeError) as caught:
        await run_skill_exec_step(
            step,
            effective_skill="seedance-2-prompt",
            inputs={},
            outputs={},
            skill_loader=_Loader(spec),
            workspace_dir=str(tmp_path),
            trusted_env={
                META_CAPABILITY_INTERNAL_CREDENTIAL_SOURCE: "profile_pool",
                META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN: "opaque-lease-token",
                META_CAPABILITY_INTERNAL_PROVIDER: "openrouter",
                META_CAPABILITY_INTERNAL_SESSION_KEY: "synthetic-session",
            },
        )

    assert reports == [
        ("openrouter", "synthetic-session", "opaque-lease-token", expected_kind)
    ]
    assert paid_replay_is_safe(caught.value) is False
    assert paid_replay_may_duplicate(caught.value) is True


@pytest.mark.asyncio
async def test_reserved_provider_failure_exit_ignores_non_pool_or_untrusted_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "provider_auth_failure.py"
    script.write_text("raise SystemExit(79)\n", encoding="utf-8")
    reports: list[object] = []
    monkeypatch.setattr(
        "opensquilla.engine.selector_override.report_profile_credential_lease_failure",
        lambda *args: reports.append(args),
    )
    step = MetaStep(
        id="paid",
        kind="skill_exec",
        skill="seedance-2-prompt",
        side_effect="external_paid_submit",
    )

    for layer, source in (
        (SkillLayer.BUNDLED, "profile"),
        (SkillLayer.WORKSPACE, "profile_pool"),
    ):
        spec = _spec(
            tmp_path,
            f"python {script}",
            name="seedance-2-prompt",
            layer=layer,
        )
        with pytest.raises(RuntimeError):
            await run_skill_exec_step(
                step,
                effective_skill="seedance-2-prompt",
                inputs={},
                outputs={},
                skill_loader=_Loader(spec),
                workspace_dir=str(tmp_path),
                trusted_env={
                    META_CAPABILITY_INTERNAL_CREDENTIAL_SOURCE: source,
                    META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN: (
                        "opaque-lease-token"
                    ),
                    META_CAPABILITY_INTERNAL_PROVIDER: "openrouter",
                    META_CAPABILITY_INTERNAL_SESSION_KEY: "synthetic-session",
                },
            )

    bundled = _spec(
        tmp_path,
        f"python {script}",
        name="seedance-2-prompt",
    )
    with pytest.raises(RuntimeError):
        await run_skill_exec_step(
            step,
            effective_skill="seedance-2-prompt",
            inputs={},
            outputs={},
            skill_loader=_Loader(bundled),
            workspace_dir=str(tmp_path),
            trusted_env={
                META_CAPABILITY_INTERNAL_CREDENTIAL_SOURCE: "profile_pool",
                META_CAPABILITY_INTERNAL_PROVIDER: "openrouter",
                META_CAPABILITY_INTERNAL_SESSION_KEY: "synthetic-session",
            },
        )

    assert reports == []


@pytest.mark.asyncio
async def test_reserved_auth_failure_rotates_two_key_pool_only_on_next_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_profile_pool: None,
) -> None:
    del isolated_profile_pool

    monkeypatch.setenv("SYNTHETIC_MEDIA_POOL_A", "synthetic-pool-key-a")
    monkeypatch.setenv("SYNTHETIC_MEDIA_POOL_B", "synthetic-pool-key-b")
    pool_names = ["SYNTHETIC_MEDIA_POOL_A", "SYNTHETIC_MEDIA_POOL_B"]
    session_key = "agent:main:media-pool-rotation"
    config = GatewayConfig(
        llm_profiles={
            "openrouter": {
                "model": "bytedance/seedance-2.0",
                "api_key_env_pool": pool_names,
            }
        }
    )
    loader = SkillLoader(
        bundled_dir=_BUNDLED,
        snapshot_path=tmp_path / "skill-snapshot.json",
    )
    parent_spec = loader.get_by_name("meta-short-drama")
    assert parent_spec is not None
    plan = parse_meta_plan(parent_spec)
    assert plan is not None

    first_env = capability_runtime_env_for_consumers(
        config,
        ["seedance-2-prompt"],
        parent_spec=parent_spec,
        plan=plan,
        session_key=session_key,
        skill_resolver=loader,
    )["seedance-2-prompt"]
    first_key = first_env[META_CAPABILITY_API_KEY_ENV]
    first_token = first_env[META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN]

    script = tmp_path / "provider_auth_failure.py"
    script.write_text("raise SystemExit(79)\n", encoding="utf-8")
    spec = _spec(tmp_path, f"python {script}", name="seedance-2-prompt")
    step = MetaStep(
        id="paid",
        kind="skill_exec",
        skill="seedance-2-prompt",
        side_effect="external_paid_submit",
    )
    with pytest.raises(RuntimeError):
        await run_skill_exec_step(
            step,
            effective_skill="seedance-2-prompt",
            inputs={},
            outputs={},
            skill_loader=_Loader(spec),
            workspace_dir=str(tmp_path),
            trusted_env=first_env,
        )

    second_env = capability_runtime_env_for_consumers(
        config,
        ["seedance-2-prompt"],
        parent_spec=parent_spec,
        plan=plan,
        session_key=session_key,
        skill_resolver=loader,
    )["seedance-2-prompt"]
    second_key = second_env[META_CAPABILITY_API_KEY_ENV]
    second_token = second_env[META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN]
    assert first_key == "synthetic-pool-key-a"
    assert second_key == "synthetic-pool-key-b"
    assert second_token != first_token

    # Simulate a late failure from the old A subprocess after the next run
    # has already leased B. Exact compare-and-park must leave B untouched.
    with pytest.raises(RuntimeError):
        await run_skill_exec_step(
            step,
            effective_skill="seedance-2-prompt",
            inputs={},
            outputs={},
            skill_loader=_Loader(spec),
            workspace_dir=str(tmp_path),
            trusted_env=first_env,
        )

    after_stale_env = capability_runtime_env_for_consumers(
        config,
        ["seedance-2-prompt"],
        parent_spec=parent_spec,
        plan=plan,
        session_key=session_key,
        skill_resolver=loader,
    )["seedance-2-prompt"]
    assert after_stale_env[META_CAPABILITY_API_KEY_ENV] == second_key
    assert (
        after_stale_env[META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN]
        == second_token
    )
