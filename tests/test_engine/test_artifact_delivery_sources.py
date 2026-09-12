from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from opensquilla.artifacts import ArtifactStore
from opensquilla.engine.artifact_delivery import auto_publish_omitted_workspace_artifacts
from opensquilla.tools.builtin.artifacts import publish_artifact
from opensquilla.tools.builtin.filesystem import write_file
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


@pytest.fixture
def artifact_context(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        run_mode="full",
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="source-version-session",
        session_key="agent:main:webchat:source-version",
    )


@contextmanager
def _active_context(ctx: ToolContext) -> Iterator[None]:
    token = current_tool_context.set(ctx)
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize("path_form", ["relative", "absolute", "workspace_alias"])
async def test_custom_name_publication_satisfies_same_source_backstop(
    artifact_context: ToolContext, path_form: str,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    target = workspace / "index.html"
    publish_path = {
        "relative": "index.html",
        "absolute": str(target),
        "workspace_alias": str(workspace.parent / "model" / "workspace" / "index.html"),
    }[path_form]
    with _active_context(ctx):
        await write_file("index.html", "<html><title>北京旅游攻略</title></html>")
        explicit = json.loads(await publish_artifact(publish_path, name="北京旅游攻略.html"))

    assert any(record["created"] for record in ctx.workspace_file_writes)
    assert any(
        source.path == str(target.resolve()) for source in ctx.artifact_source_paths.values()
    )
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="网页已生成：index.html")

    assert result.artifacts == []
    assert result.failure_summaries == []
    assert [item["id"] for item in ctx.published_artifacts] == [explicit["artifact"]["id"]]


async def test_failed_publication_of_rewritten_source_does_not_suppress_new_bytes(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    first_bytes = "<html><title>Version 1</title></html>"
    second_bytes = "<html><title>Version 2</title></html>"
    with _active_context(ctx):
        await write_file("index.html", first_bytes)
        first = json.loads(await publish_artifact("index.html", name="旅游攻略.html"))
        await write_file("index.html", second_bytes)
        ctx.artifact_max_bytes = 1
        with pytest.raises(ToolError, match="per-file budget"):
            await publish_artifact("index.html", name="旅游攻略.html")
        ctx.artifact_max_bytes = None

    assert {source.artifact_id for source in ctx.artifact_source_paths.values()} == {
        first["artifact"]["id"],
    }
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Updated index.html")

    assert result.failure_summaries == []
    assert len(result.artifacts) == 1
    latest = result.artifacts[0]
    assert latest["id"] != first["artifact"]["id"]
    assert latest["sha256"] == hashlib.sha256(second_bytes.encode()).hexdigest()
    store = ArtifactStore(ctx.artifact_media_root)
    _, old_path = store.resolve_for_download(
        first["artifact"]["id"], session_id=ctx.artifact_session_id,
    )
    _, new_path = store.resolve_for_download(latest["id"], session_id=ctx.artifact_session_id)
    assert old_path.read_bytes() == first_bytes.encode()
    assert new_path.read_bytes() == second_bytes.encode()


@pytest.mark.parametrize("same_inode", [False, True], ids=["copied-bytes", "hardlink"])
async def test_same_bytes_at_a_different_source_still_need_delivery(
    artifact_context: ToolContext, same_inode: bool,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    first = workspace / "a.html"
    second = workspace / "b.html"
    with _active_context(ctx):
        await write_file("a.html", "<html><title>Same bytes, separate files</title></html>")
        await write_file("b.html", first.read_text(encoding="utf-8"))
        if same_inode:
            second.unlink()
            try:
                second.hardlink_to(first)
            except OSError as exc:
                pytest.skip(f"the test filesystem cannot create hard links: {exc}")
            assert first.samefile(second)
        # The display name intentionally collides with the OTHER source's name.
        await publish_artifact("a.html", name="b.html")

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created a.html and b.html")

    assert result.failure_summaries == []
    assert [item["name"] for item in result.artifacts] == ["b.html"]
    assert len(ctx.published_artifacts) == 2


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"the test filesystem cannot create symbolic links: {exc}")


async def test_checked_symlink_to_same_workspace_source_does_not_duplicate(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    with _active_context(ctx):
        await write_file("index.html", "<html><title>Linked source</title></html>")
        _symlink_or_skip(workspace / "alias.html", workspace / "index.html")
        await publish_artifact("alias.html", name="旅游攻略.html", bundle="none")

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")

    assert result.artifacts == []
    assert result.failure_summaries == []
    assert len(ctx.published_artifacts) == 1


@pytest.mark.parametrize("path_form", ["absolute", "traversal", "symlink"])
async def test_rejected_outside_source_cannot_suppress_workspace_delivery(
    artifact_context: ToolContext, path_form: str,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    outside = workspace.parent / "outside.html"
    with _active_context(ctx):
        await write_file("index.html", "<html><title>Same bytes at unsafe source</title></html>")
        outside.write_bytes((workspace / "index.html").read_bytes())
        if path_form == "symlink":
            _symlink_or_skip(workspace / "outside-link.html", outside)
            publish_path = "outside-link.html"
        elif path_form == "traversal":
            publish_path = "../outside.html"
        else:
            publish_path = str(outside)
        with pytest.raises(ToolError, match="outside workspace"):
            await publish_artifact(publish_path, name="index.html")

    assert ctx.artifact_source_paths == {}
    assert ctx.published_artifacts == []
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")

    assert [item["name"] for item in result.artifacts] == ["index.html"]
    assert result.failure_summaries == []


async def test_backstop_rechecks_workspace_boundary_after_source_becomes_symlink(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    target = workspace / "index.html"
    outside = workspace.parent / "outside.html"
    with _active_context(ctx):
        await write_file("index.html", "<html><title>Initially in workspace</title></html>")
    outside.write_text("<html><title>Outside content</title></html>", encoding="utf-8")
    target.unlink()
    _symlink_or_skip(target, outside)

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")

    assert result.artifacts == []
    assert ctx.published_artifacts == []


@pytest.mark.skipif(os.name != "nt", reason="Windows path spelling on the native filesystem")
@pytest.mark.parametrize("path_form", ["uppercase", "forward_slashes", "backslashes"])
async def test_windows_native_path_spellings_share_successful_publication(
    artifact_context: ToolContext, path_form: str,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    target = workspace / "reports" / "index.html"
    with _active_context(ctx):
        await write_file("reports/index.html", "<html><title>Windows path</title></html>")
        publish_path = {
            "uppercase": str(target).upper(),
            "forward_slashes": target.as_posix(),
            "backslashes": r"reports\index.html",
        }[path_form]
        if not Path(publish_path if path_form != "backslashes" else target).exists():
            pytest.skip("this Windows filesystem uses case-sensitive names")
        await publish_artifact(publish_path, name="旅游攻略.html")

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created reports/index.html")

    assert result.artifacts == []
    assert result.failure_summaries == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX case behavior on the native filesystem")
async def test_posix_native_case_behavior_preserves_source_identity(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    lower = workspace / "index.html"
    upper = workspace / "INDEX.html"
    with _active_context(ctx):
        await write_file(lower.name, "<html><title>Native case behavior</title></html>")
        case_insensitive = upper.exists()
        if not case_insensitive:
            await write_file(upper.name, lower.read_text(encoding="utf-8"))
        await publish_artifact(upper.name, name="旅游攻略.html")

    result = auto_publish_omitted_workspace_artifacts(
        ctx, final_text="Created index.html and INDEX.html",
    )

    assert result.failure_summaries == []
    assert [item["name"] for item in result.artifacts] == ([] if case_insensitive else [lower.name])


@pytest.mark.parametrize("name", [None, "旅游攻略.html"], ids=["default-name", "custom-name"])
async def test_changed_bundle_sidecar_does_not_count_as_already_delivered(
    artifact_context: ToolContext, name: str | None,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    entry = '<html><link rel="stylesheet" href="style.css"><title>Tour</title></html>'
    with _active_context(ctx):
        await write_file("index.html", entry)
        await write_file("style.css", "body { color: blue; }")
        explicit = json.loads(await publish_artifact("index.html", name=name))

    store = ArtifactStore(ctx.artifact_media_root)
    old_id = explicit["artifact"]["id"]
    old_bundle = store.describe_preview_bundle(old_id, session_id=ctx.artifact_session_id)
    assert old_bundle is not None
    assert {item.path for item in old_bundle.files} == {"index.html", "style.css"}
    unchanged = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")
    assert unchanged.artifacts == []
    assert unchanged.failure_summaries == []

    with _active_context(ctx):
        await write_file("style.css", "body { color: green; }")
    assert (workspace / "index.html").read_text(encoding="utf-8") == entry
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Updated index.html")

    assert result.failure_summaries == []
    assert len(result.artifacts) == 1
    fallback = result.artifacts[0]
    assert fallback["id"] != old_id
    assert fallback["sha256"] == explicit["artifact"]["sha256"]
    # The existing backstop delivers the entry file, not a rebuilt bundle.
    assert store.describe_preview_bundle(
        fallback["id"], session_id=ctx.artifact_session_id,
    ) is None
    _, fallback_path = store.resolve_for_download(
        fallback["id"], session_id=ctx.artifact_session_id,
    )
    assert fallback_path.read_text(encoding="utf-8") == entry
    old_style = store.resolve_preview_resource(
        old_id, session_id=ctx.artifact_session_id, logical_path="style.css",
    )
    assert old_style.path.read_text(encoding="utf-8") == "body { color: blue; }"


async def test_case_only_hardlinks_remain_distinct_sources_on_case_sensitive_filesystem(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    first = workspace / "A.html"
    second = workspace / "a.html"
    with _active_context(ctx):
        await write_file(first.name, "<html><title>Two case-only hardlinks</title></html>")
        if second.exists():
            pytest.skip("this filesystem does not support case-distinct directory entries")
        await write_file(second.name, first.read_text(encoding="utf-8"))
        second.unlink()
        try:
            second.hardlink_to(first)
        except OSError as exc:
            pytest.skip(f"the test filesystem cannot create hard links: {exc}")
        assert first.samefile(second)
        assert {item.name for item in workspace.iterdir()} == {"A.html", "a.html"}
        await publish_artifact(first.name, name="旅游攻略.html")

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created A.html and a.html")

    assert result.failure_summaries == []
    assert [item["name"] for item in result.artifacts] == ["a.html"]
    assert len(ctx.published_artifacts) == 2
