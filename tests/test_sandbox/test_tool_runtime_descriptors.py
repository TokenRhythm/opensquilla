from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import opensquilla.tools.dispatch as dispatch_mod
from opensquilla.sandbox.operation_runtime import (
    CustomOperationRequest,
    ProcessOperationRequest,
    SandboxToolDescriptor,
)
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, ToolSpec


def test_tool_spec_always_has_sandbox_descriptor(tmp_path: Path) -> None:
    spec = ToolSpec(name="plain", description="plain", parameters={})

    assert isinstance(spec.sandbox, SandboxToolDescriptor)

    operation = spec.sandbox.build_operation(
        tool_name=spec.name,
        arguments={"value": tmp_path / "x.txt"},
        workspace=tmp_path,
        run_mode="trusted",
    )

    assert operation.domain == "custom"
    assert operation.kind == "plain"
    assert isinstance(operation.request, CustomOperationRequest)
    assert operation.request.data["arguments"]["value"] == str(tmp_path / "x.txt")


@pytest.mark.asyncio
async def test_dispatch_uses_sandbox_descriptor_guard(monkeypatch, tmp_path: Path) -> None:
    registry = ToolRegistry()

    async def handler(command: str) -> str:
        return f"handled:{command}"

    descriptor = SandboxToolDescriptor.process(
        kind="shell.exec",
        argv_factory=lambda args: ("exec_command", str(args["command"])),
        enforce=True,
        record_payload=False,
    )
    registry.register(
        ToolSpec(
            name="exec_command",
            description="exec",
            parameters={},
            sandbox=descriptor,
        ),
        handler,
    )

    calls: list[object] = []

    async def fake_prepare(descriptor, **kwargs):
        calls.append(("prepare", descriptor, kwargs))
        operation = descriptor.build_operation(
            tool_name=kwargs["tool_name"],
            arguments=kwargs["arguments"],
            workspace=kwargs["workspace"],
            run_mode=kwargs["run_mode"],
        )
        assert isinstance(operation.request, ProcessOperationRequest)
        assert operation.request.argv == ("exec_command", "echo ok")
        return SimpleNamespace(denial_payload=None, request=None, record_payload=False)

    async def fake_run(handler, arguments, guard):
        calls.append(("run", arguments, guard))
        return await handler(**dict(arguments))

    async def fake_record(*args, **kwargs):
        calls.append(("record", args, kwargs))

    monkeypatch.setattr(dispatch_mod, "prepare_tool_operation_guard", fake_prepare)
    monkeypatch.setattr(dispatch_mod, "run_tool_handler_with_operation_guard", fake_run)
    monkeypatch.setattr(dispatch_mod, "record_tool_operation_success", fake_record)

    handler_fn = dispatch_mod.build_tool_handler(
        registry,
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            run_mode="trusted",
        ),
    )
    result = await handler_fn(
        ToolCall(
            tool_use_id="t1",
            tool_name="exec_command",
            arguments={"command": "echo ok"},
        )
    )

    assert result.content == "handled:echo ok"
    assert [call[0] for call in calls] == ["prepare", "run"]


def test_builtin_tools_no_longer_use_sandboxed_decorator() -> None:
    builtin_root = Path("src/opensquilla/tools/builtin")
    offenders = []
    for path in builtin_root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "@sandboxed" in text:
            offenders.append(str(path))

    assert offenders == []


def test_builtin_local_artifact_and_media_tools_have_explicit_descriptors() -> None:
    import opensquilla.tools.builtin  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    expected = {
        "publish_artifact": ("artifact", "artifact.publish"),
        "image": ("media", "media.analyze"),
        "image_generate": ("media", "media.generate_image"),
        "pdf": ("media", "media.read_pdf"),
        "voice_clone": ("media", "media.voice_clone"),
        "voice_convert": ("media", "media.voice_convert"),
        "dubbing_generate": ("media", "media.dubbing_generate"),
        "dubbing_status": ("media", "media.dubbing_status"),
        "dubbing_download": ("media", "media.dubbing_download"),
        "music_generate": ("media", "media.music_generate"),
        "song_generate": ("media", "media.song_generate"),
        "audio_provider_capabilities": ("media", "media.audio_capabilities"),
        "voice_search": ("media", "media.voice_search"),
        "tts": ("media", "media.tts"),
    }
    registry = get_default_registry()

    for name, (domain, kind) in expected.items():
        registered = registry.get(name)
        assert registered is not None, name
        descriptor = registered.spec.sandbox
        assert (descriptor.domain, descriptor.kind) == (domain, kind), name
        assert descriptor.enforce is False, name
