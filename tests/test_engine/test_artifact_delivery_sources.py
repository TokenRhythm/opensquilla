from __future__ import annotations

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


async def _preview_opener(*args, **kwargs):
    raise AssertionError("The delivery backstop must not open a preview")


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
        workspace_preview_opener=_preview_opener,
    )


@contextmanager
def _active_context(ctx: ToolContext) -> Iterator[None]:
    token = current_tool_context.set(ctx)
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize("path_form", ["relative", "absolute", "workspace_alias"])
@pytest.mark.parametrize("caller_kind", [CallerKind.WEB, CallerKind.CHANNEL])
async def test_custom_name_publication_satisfies_same_source_backstop(
    artifact_context: ToolContext, path_form: str, caller_kind: CallerKind,
) -> None:
    ctx = artifact_context
    ctx.caller_kind = caller_kind
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


async def test_unregistered_html_sources_are_not_auto_published(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
    with _active_context(ctx):
        await write_file("site/index.html", "<h1>Home</h1>")
        await write_file("site/about.html", "<h1>About</h1>")

    result = auto_publish_omitted_workspace_artifacts(
        ctx,
        final_text="Created site/index.html and site/about.html",
    )

    assert result.artifacts == []
    assert result.failure_summaries == []
    assert ctx.published_artifacts == []
    assert (workspace / "site/index.html").is_file()
    assert (workspace / "site/about.html").is_file()


@pytest.mark.parametrize("suffix", [".html", ".htm"])
@pytest.mark.parametrize(
    "surface",
    ["channel", "cli", "web-no-opener", "web-denied", "web-allowlist", "web-catalog"],
)
async def test_html_delivery_backstop_without_preview_capability(
    artifact_context: ToolContext, suffix: str, surface: str,
) -> None:
    ctx = artifact_context
    if surface in {"channel", "cli"}:
        ctx.caller_kind = CallerKind.CHANNEL if surface == "channel" else CallerKind.CLI
    elif surface == "web-no-opener":
        ctx.workspace_preview_opener = None
    elif surface == "web-denied":
        ctx.denied_tools.add("open_workspace_preview")
    elif surface == "web-allowlist":
        ctx.allowed_tools = {"write_file"}
    else:
        ctx.authorized_tool_names = frozenset({"write_file"})
    name = f"site/weather{suffix}"
    content = "<html><title>Weather</title><body>Beijing</body></html>"
    with _active_context(ctx):
        await write_file(name, content)
    # Persisted scopes do not make a preview accessible on this surface.
    ctx.workspace_preview_scopes = [{
        "workspace": ctx.workspace_dir,
        "source_path": name,
        "bundle_mode": "directory",
        "bundle_root": "site",
    }]

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text=f"Created {name}")

    assert result.failure_summaries == []
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact["source"] == "auto_publish_omitted"
    _, path = ArtifactStore(ctx.artifact_media_root).resolve_for_download(
        artifact["id"], session_id=ctx.artifact_session_id,
    )
    assert path.read_bytes() == content.encode()
    repeated = auto_publish_omitted_workspace_artifacts(ctx, final_text=f"Created {name}")
    assert repeated.artifacts == []


@pytest.mark.parametrize("created, final_text", [(False, "Updated weather.html"), (True, "Done")])
async def test_channel_html_backstop_preserves_existing_candidate_checks(
    artifact_context: ToolContext, created: bool, final_text: str,
) -> None:
    ctx = artifact_context
    ctx.caller_kind = CallerKind.CHANNEL
    if not created:
        (Path(ctx.workspace_dir) / "weather.html").write_text("old", encoding="utf-8")
    with _active_context(ctx):
        await write_file("weather.html", "<h1>Weather</h1>")

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text=final_text)

    assert result.artifacts == []
    assert result.failure_summaries == []


async def test_failed_publication_of_rewritten_source_does_not_suppress_new_bytes(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    workspace = Path(ctx.workspace_dir)
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
    assert result.artifacts == []
    assert [item["id"] for item in ctx.published_artifacts] == [first["artifact"]["id"]]
    store = ArtifactStore(ctx.artifact_media_root)
    _, old_path = store.resolve_for_download(
        first["artifact"]["id"], session_id=ctx.artifact_session_id,
    )
    assert old_path.read_bytes() == first_bytes.encode()
    assert (workspace / "index.html").read_bytes() == second_bytes.encode()


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
    assert result.artifacts == []
    assert len(ctx.published_artifacts) == 1


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

    assert result.artifacts == []
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
    assert result.artifacts == []


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
    assert result.artifacts == []
    assert [item["id"] for item in ctx.published_artifacts] == [old_id]
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
    assert result.artifacts == []
    assert len(ctx.published_artifacts) == 1


async def test_channel_backstop_captures_dependencies_without_expanding_candidates(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    ctx.caller_kind = CallerKind.CHANNEL
    workspace = Path(ctx.workspace_dir)
    (workspace / "edited.html").write_text("old", encoding="utf-8")
    (workspace / "untracked.html").write_text("untracked", encoding="utf-8")
    with _active_context(ctx):
        await write_file("site/index.html", '<link rel="stylesheet" href="style.css">')
        await write_file("site/style.css", '@import "theme.css"; body{background:url(logo.svg)}')
        await write_file("site/theme.css", "body{color:blue}")
        await write_file("site/logo.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>')
        await write_file("site/unrelated.json", '{"private":true}')
        await write_file("edited.html", "edited")
        await write_file("unmentioned.html", "unmentioned")
        await write_file("memory/private.html", "private")
    ctx.workspace_file_writes.append(dict(ctx.workspace_file_writes[0]))

    result = auto_publish_omitted_workspace_artifacts(
        ctx,
        final_text=(
            "Created site/index.html; edited.html, untracked.html and memory/private.html exist"
        ),
    )

    assert result.failure_summaries == []
    assert len(result.artifacts) == 1
    assert result.artifacts[0]["name"] == "index.html"
    store = ArtifactStore(ctx.artifact_media_root)
    artifact_id = result.artifacts[0]["id"]
    bundle = store.read_verified_bundle(artifact_id, session_id=ctx.artifact_session_id)
    assert {item.path for item in bundle.files} == {
        "index.html", "style.css", "theme.css", "logo.svg",
    }
    sources = [
        source for source in ctx.artifact_source_paths.values()
        if source.artifact_id == artifact_id
    ]
    assert len(sources) == 1
    assert sources[0].path == str((workspace / "site/index.html").resolve())
    assert sources[0].bundle_mode == "auto"
    (workspace / "site/theme.css").write_text("body{color:red}", encoding="utf-8")
    saved = store.read_verified_bundle(artifact_id, session_id=ctx.artifact_session_id)
    saved_theme = next(item.data for item in saved.files if item.path == "theme.css")
    assert saved_theme == b"body{color:blue}"


@pytest.mark.parametrize("explicit_first", [False, True], ids=["automatic", "explicit"])
async def test_channel_changed_sidecar_creates_new_bundle_with_same_entry_bytes(
    artifact_context: ToolContext, explicit_first: bool,
) -> None:
    ctx = artifact_context
    ctx.caller_kind = CallerKind.CHANNEL
    entry = '<link rel="stylesheet" href="style.css"><h1>Report</h1>'
    with _active_context(ctx):
        await write_file("index.html", entry)
        await write_file("style.css", "body{color:blue}")
        if explicit_first:
            await publish_artifact("index.html", name="Friendly report.html")
    if not explicit_first:
        first = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")
        assert len(first.artifacts) == 1
    old_artifact = ctx.published_artifacts[0]
    unchanged = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")
    assert unchanged.artifacts == []
    with _active_context(ctx):
        await write_file("style.css", "body{color:green}")

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Updated index.html")

    assert result.failure_summaries == []
    assert len(result.artifacts) == 1
    new_artifact = result.artifacts[0]
    assert new_artifact["id"] != old_artifact["id"]
    assert new_artifact["sha256"] == old_artifact["sha256"]
    store = ArtifactStore(ctx.artifact_media_root)
    manifests = [
        store.describe_preview_bundle(item["id"], session_id=ctx.artifact_session_id)
        for item in (old_artifact, new_artifact)
    ]
    assert all(item is not None for item in manifests)
    assert manifests[0].bundle_digest != manifests[1].bundle_digest
    for artifact, expected in (
        (old_artifact, b"body{color:blue}"), (new_artifact, b"body{color:green}"),
    ):
        bundle = store.read_verified_bundle(artifact["id"], session_id=ctx.artifact_session_id)
        assert next(item.data for item in bundle.files if item.path == "style.css") == expected
    repeated = auto_publish_omitted_workspace_artifacts(ctx, final_text="Updated index.html")
    assert repeated.artifacts == []
    assert len(ctx.published_artifacts) == 2


@pytest.mark.parametrize("reference", ["missing.css", "../outside.css", ".env"])
async def test_explicit_and_automatic_html_reject_unavailable_dependencies(
    artifact_context: ToolContext, reference: str,
) -> None:
    ctx = artifact_context
    ctx.caller_kind = CallerKind.CHANNEL
    workspace = Path(ctx.workspace_dir)
    (workspace.parent / "outside.css").write_text("outside", encoding="utf-8")
    (workspace / ".env").write_text("secret", encoding="utf-8")
    with _active_context(ctx):
        await write_file("index.html", f'<link rel="stylesheet" href="{reference}">')
        for mode in ("auto", "none"):
            with pytest.raises(ToolError, match="dependenc"):
                await publish_artifact("index.html", bundle=mode)

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")

    assert result.artifacts == []
    assert len(result.failure_summaries) == 1
    assert "dependenc" in result.failure_summaries[0]
    assert result.resolved_target_keys == []
    assert ctx.published_artifacts == []
    assert ctx.artifact_source_paths == {}
    assert not (Path(ctx.artifact_media_root) / "artifacts").exists()


async def test_single_file_mode_requires_self_contained_html(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    ctx.caller_kind = CallerKind.CHANNEL
    with _active_context(ctx):
        await write_file("style.css", "body{color:blue}")
        await write_file("index.html", '<link rel="stylesheet" href="style.css">')
        with pytest.raises(ToolError, match="missing a local dependency"):
            await publish_artifact("index.html", bundle="none")

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")

    assert result.failure_summaries == []
    assert len(result.artifacts) == 1
    bundle = ArtifactStore(ctx.artifact_media_root).read_verified_bundle(
        result.artifacts[0]["id"], session_id=ctx.artifact_session_id,
    )
    assert {item.path for item in bundle.files} == {"index.html", "style.css"}


async def test_self_contained_single_file_publication_satisfies_channel_backstop(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    ctx.caller_kind = CallerKind.CHANNEL
    content = '<style>body{color:blue}</style><h1>Self-contained</h1>'
    with _active_context(ctx):
        await write_file("index.html", content)
        explicit = json.loads(await publish_artifact("index.html", bundle="none"))

    assert "delivery receipt" in explicit["note"]
    store = ArtifactStore(ctx.artifact_media_root)
    artifact_id = explicit["artifact"]["id"]
    assert store.describe_preview_bundle(artifact_id, session_id=ctx.artifact_session_id) is None
    saved = store.read_verified_bundle(artifact_id, session_id=ctx.artifact_session_id)
    assert saved.files[0].data == content.encode()
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")
    assert result.artifacts == []
    assert result.failure_summaries == []
    assert len(ctx.published_artifacts) == 1


@pytest.mark.parametrize("budget", ["per-file", "disk"])
async def test_html_bundle_retains_explicit_and_automatic_publication_budgets(
    artifact_context: ToolContext, budget: str,
) -> None:
    ctx = artifact_context
    ctx.caller_kind = CallerKind.CHANNEL
    with _active_context(ctx):
        await write_file("index.html", '<link rel="stylesheet" href="style.css">')
        await write_file("style.css", "body{color:blue}")
        if budget == "per-file":
            ctx.artifact_max_bytes = 1
        else:
            ctx.artifact_disk_budget_bytes = 1
        with pytest.raises(ToolError, match=f"{budget} budget"):
            await publish_artifact("index.html")

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created index.html")

    assert result.artifacts == []
    assert len(result.failure_summaries) == 1
    assert f"{budget} budget" in result.failure_summaries[0]
    assert ctx.published_artifacts == []
    assert ctx.artifact_source_paths == {}


async def test_channel_backstop_does_not_publish_an_outside_tracked_path(
    artifact_context: ToolContext,
) -> None:
    ctx = artifact_context
    ctx.caller_kind = CallerKind.CHANNEL
    outside = Path(ctx.workspace_dir).parent / "outside.html"
    outside.write_text("<h1>Outside</h1>", encoding="utf-8")
    ctx.workspace_file_writes.append({
        "path": str(outside), "relative_path": "../outside.html",
        "name": "outside.html", "created": True,
    })
    with _active_context(ctx), pytest.raises(ToolError, match="outside workspace"):
        await publish_artifact(str(outside))

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created outside.html")

    assert result.artifacts == []
    assert result.failure_summaries == []
    assert ctx.published_artifacts == []


async def test_html_single_file_publishes_the_exact_bytes_that_passed_validation(
    artifact_context: ToolContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.tools.builtin.artifacts as artifacts_tool

    ctx = artifact_context
    ctx.caller_kind = CallerKind.CHANNEL
    original = b"<h1>Validated</h1>"
    target = Path(ctx.workspace_dir) / "index.html"
    validate = artifacts_tool.validate_html_archive

    def validate_then_change(bundle) -> None:
        validate(bundle)
        target.write_bytes(b'<img src="missing.png">')

    monkeypatch.setattr(artifacts_tool, "validate_html_archive", validate_then_change)
    with _active_context(ctx):
        await write_file("index.html", original.decode())
        explicit = json.loads(await publish_artifact("index.html", bundle="none"))

    _, material = ArtifactStore(ctx.artifact_media_root).resolve_for_download(
        explicit["artifact"]["id"], session_id=ctx.artifact_session_id,
    )
    assert material.read_bytes() == original
    assert target.read_bytes() == b'<img src="missing.png">'
