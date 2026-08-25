from __future__ import annotations

import json
from pathlib import Path

import pytest

import opensquilla.artifacts as artifacts_module
from opensquilla.artifacts import (
    ARTIFACT_BUNDLE_BLOBS_DIR,
    ARTIFACT_BUNDLE_MANIFEST_NAME,
    ArtifactBudgetError,
    ArtifactBundle,
    ArtifactBundleManifest,
    ArtifactBundleSourceFile,
    ArtifactBundleUnsupportedError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactPathError,
    ArtifactStore,
    artifact_bundle_manifest,
    artifact_payload,
    collect_artifact_bundle,
)
from opensquilla.tools.builtin.artifacts import publish_artifact
from opensquilla.tools.types import ToolContext, ToolError, current_tool_context


def _site(workspace: Path) -> Path:
    site = workspace / "site"
    (site / "css").mkdir(parents=True)
    (site / "js").mkdir()
    (site / "assets").mkdir()
    (site / "data").mkdir()
    (site / "index.html").write_text(
        """
        <link rel="stylesheet" href="./css/app.css">
        <script type="importmap">
          {"imports":{"mapped":"./js/mapped.js"}}
        </script>
        <script type="module" src="./js/app.js"></script>
        <img src="./assets/logo.svg"
             srcset="./assets/logo.svg 1x, ./assets/logo-2.svg 2x">
        """,
        encoding="utf-8",
    )
    (site / "css" / "app.css").write_text(
        '@import "./theme.css"; @font-face{src:url("../assets/font.woff2")}',
        encoding="utf-8",
    )
    (site / "css" / "theme.css").write_text("body{color:teal}", encoding="utf-8")
    (site / "js" / "app.js").write_text(
        """
        import "./dep.js";
        import("./lazy.js");
        fetch("../data/config.json");
        new Worker("./worker.js");
        new URL("../assets/module.wasm", import.meta.url);
        """,
        encoding="utf-8",
    )
    for name in ("dep.js", "lazy.js", "mapped.js", "worker.js"):
        (site / "js" / name).write_text(f"export const name={name!r};", encoding="utf-8")
    for name in ("logo.svg", "logo-2.svg", "font.woff2", "module.wasm"):
        (site / "assets" / name).write_bytes(name.encode())
    (site / "data" / "config.json").write_text('{"ready":true}', encoding="utf-8")
    return site


def _store_bundle(tmp_path: Path, bundle: ArtifactBundle):
    store = ArtifactStore(tmp_path / "media")
    ref = store.publish_bundle(
        bundle,
        session_id="session-1",
        session_key="agent:main:webchat:session-1",
        name="site.html",
        mime="text/html",
        source="publish_artifact",
    )
    return store, ref


def test_auto_bundle_collects_literal_html_css_js_and_importmap_dependencies(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    site = _site(workspace)

    bundle = collect_artifact_bundle(
        site / "index.html",
        workspace_root=workspace,
        mode="auto",
    )

    assert bundle is not None
    assert bundle.entrypoint == "index.html"
    assert bundle.collection_status == "complete"
    assert bundle.warning_codes == ()
    assert {item.path for item in bundle.files} == {
        "assets/font.woff2",
        "assets/logo-2.svg",
        "assets/logo.svg",
        "assets/module.wasm",
        "css/app.css",
        "css/theme.css",
        "data/config.json",
        "index.html",
        "js/app.js",
        "js/dep.js",
        "js/lazy.js",
        "js/mapped.js",
        "js/worker.js",
    }


def test_auto_bundle_reports_partial_for_missing_dynamic_and_unsafe_dependencies(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    site = workspace / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        """
        <script type="module" src="./app.js"></script>
        <img src="./missing.png">
        <img src="%252e%252e/secret.txt">
        <script src="https://cdn.example.test/library.js"></script>
        """,
        encoding="utf-8",
    )
    (site / "app.js").write_text("fetch(runtimePath)", encoding="utf-8")

    bundle = collect_artifact_bundle(
        site / "index.html",
        workspace_root=workspace,
        mode="auto",
    )

    assert bundle is not None
    assert bundle.collection_status == "partial"
    assert set(bundle.warning_codes) == {
        "dynamic_dependency",
        "missing_dependency",
        "outside_or_unsafe_dependency",
    }
    assert {item.path for item in bundle.files} == {"app.js", "index.html"}


def test_auto_bundle_keeps_remote_css_url_import_single_file_complete(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    html = workspace / "index.html"
    html.write_text(
        """
        <style>
          @import url('https://fonts.googleapis.com/css2?family=Inter');
        </style>
        <h1>Remote font stays an external preview resource</h1>
        """,
        encoding="utf-8",
    )

    bundle = collect_artifact_bundle(
        html,
        workspace_root=workspace,
        mode="auto",
    )

    assert bundle is not None
    assert bundle.collection_status == "complete"
    assert bundle.warning_codes == ()
    assert {item.path for item in bundle.files} == {"index.html"}


def test_auto_bundle_collects_literal_service_worker_and_marks_dynamic_url_partial(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    site = workspace / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        '<script type="module" src="./app.js"></script>',
        encoding="utf-8",
    )
    (site / "app.js").write_text(
        """
        navigator.serviceWorker.register("./sw.js");
        const assetUrl = new URL(assetPath, import.meta.url);
        """,
        encoding="utf-8",
    )
    (site / "sw.js").write_text("self.skipWaiting()", encoding="utf-8")

    bundle = collect_artifact_bundle(
        site / "index.html",
        workspace_root=workspace,
        mode="auto",
    )

    assert bundle is not None
    assert bundle.collection_status == "partial"
    assert bundle.warning_codes == ("dynamic_dependency",)
    assert {item.path for item in bundle.files} == {"app.js", "index.html", "sw.js"}


def test_auto_bundle_collects_optional_and_bracket_service_worker_forms(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    site = workspace / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        '<script type="module" src="./app.js"></script>',
        encoding="utf-8",
    )
    (site / "app.js").write_text(
        """
        navigator.serviceWorker?.register("./sw-optional.js");
        navigator["serviceWorker"]["register"]("./sw-bracket.js");
        navigator?.["serviceWorker"]?.["register"]("./sw-deep.js");
        const assetUrl = new URL(
            "./asset.json",
            import.meta.url
        );
        """,
        encoding="utf-8",
    )
    for name in ("sw-optional.js", "sw-bracket.js", "sw-deep.js"):
        (site / name).write_text("self.skipWaiting()", encoding="utf-8")
    (site / "asset.json").write_text("{}", encoding="utf-8")

    bundle = collect_artifact_bundle(
        site / "index.html",
        workspace_root=workspace,
        mode="auto",
    )

    assert bundle is not None
    assert bundle.collection_status == "complete"
    assert bundle.warning_codes == ()
    assert {item.path for item in bundle.files} == {
        "app.js",
        "asset.json",
        "index.html",
        "sw-bracket.js",
        "sw-deep.js",
        "sw-optional.js",
    }


def test_auto_bundle_marks_multiline_nonliteral_import_meta_url_partial(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    site = workspace / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        '<script type="module" src="./app.js"></script>',
        encoding="utf-8",
    )
    (site / "app.js").write_text(
        """
        const dynamicAsset = new URL(
            "./locale-" + locale + ".json",
            import.meta.url
        );
        """,
        encoding="utf-8",
    )

    bundle = collect_artifact_bundle(
        site / "index.html",
        workspace_root=workspace,
        mode="auto",
    )

    assert bundle is not None
    assert bundle.collection_status == "partial"
    assert bundle.warning_codes == ("dynamic_dependency",)
    assert {item.path for item in bundle.files} == {"app.js", "index.html"}


def test_auto_bundle_is_not_created_for_non_html_or_none_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    text = workspace / "report.txt"
    text.write_text("report", encoding="utf-8")
    html = workspace / "index.html"
    html.write_text("<p>hello</p>", encoding="utf-8")

    assert (
        collect_artifact_bundle(text, workspace_root=workspace, mode="auto") is None
    )
    assert (
        collect_artifact_bundle(html, workspace_root=workspace, mode="none") is None
    )

    extensionless = workspace / "preview"
    extensionless.write_text('<script src="./app.js"></script>', encoding="utf-8")
    (workspace / "app.js").write_text("window.ready=true", encoding="utf-8")
    inferred = collect_artifact_bundle(
        extensionless,
        workspace_root=workspace,
        mode="auto",
        entry_mime="text/html",
    )
    assert inferred is not None
    entry = next(item for item in inferred.files if item.path == "preview")
    assert entry.mime == "text/html"
    assert {item.path for item in inferred.files} == {"app.js", "preview"}


def test_directory_bundle_snapshots_dedicated_root_deterministically(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    site = _site(workspace)
    (site / "unreferenced.json").write_text("{}", encoding="utf-8")

    bundle = collect_artifact_bundle(
        site / "index.html",
        workspace_root=workspace,
        mode="directory",
        bundle_root=site,
    )

    assert bundle is not None
    assert bundle.collection_status == "complete"
    assert "unreferenced.json" in {item.path for item in bundle.files}
    assert [item.path for item in bundle.files] == sorted(
        item.path for item in bundle.files
    )


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".env.local",
        ".aws",
        ".git/config",
        "client-secret.json",
        "client_secret.json",
        "client_secret_local.json",
        "client_secret.production.json",
        "credentials.json",
        "my-credentials.json",
        "my-secrets.yml",
        "oauth_client_secret.json",
        "private.pem",
    ],
)
def test_directory_bundle_rejects_sensitive_material(
    tmp_path: Path,
    relative: str,
) -> None:
    workspace = tmp_path / "workspace"
    site = workspace / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<p>safe</p>", encoding="utf-8")
    secret = site / relative
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("synthetic-secret", encoding="utf-8")

    with pytest.raises(ArtifactPathError, match="sensitive"):
        collect_artifact_bundle(
            site / "index.html",
            workspace_root=workspace,
            mode="directory",
            bundle_root=site,
        )


def test_directory_bundle_sensitive_filter_avoids_unrelated_names(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    site = workspace / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<p>safe</p>", encoding="utf-8")
    (site / "client_secretary.json").write_text("{}", encoding="utf-8")
    (site / "my-credentials-guide.md").write_text("# Public guide", encoding="utf-8")
    (site / "secret-garden.html").write_text("<p>novel</p>", encoding="utf-8")

    bundle = collect_artifact_bundle(
        site / "index.html",
        workspace_root=workspace,
        mode="directory",
        bundle_root=site,
    )

    assert bundle is not None
    assert {item.path for item in bundle.files} == {
        "client_secretary.json",
        "index.html",
        "my-credentials-guide.md",
        "secret-garden.html",
    }


@pytest.mark.parametrize("root_name", [".git", ".ssh"])
def test_bundle_rejects_sensitive_root_itself(
    tmp_path: Path,
    root_name: str,
) -> None:
    workspace = tmp_path / "workspace"
    sensitive_root = workspace / root_name
    sensitive_root.mkdir(parents=True)
    entry = sensitive_root / "index.html"
    entry.write_text("<p>must not publish</p>", encoding="utf-8")

    with pytest.raises(ArtifactPathError, match="root is sensitive"):
        collect_artifact_bundle(
            entry,
            workspace_root=workspace,
            mode="directory",
            bundle_root=sensitive_root,
        )


def test_directory_bundle_rejects_workspace_root_case_collision_and_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    site = workspace / "site"
    site.mkdir(parents=True)
    entry = site / "index.html"
    entry.write_text("<p>safe</p>", encoding="utf-8")

    with pytest.raises(ArtifactPathError, match="dedicated"):
        collect_artifact_bundle(
            entry,
            workspace_root=workspace,
            mode="directory",
            bundle_root=workspace,
        )

    (site / "A.js").write_text("a", encoding="utf-8")
    (site / "a.js").write_text("b", encoding="utf-8")
    if len({path.name for path in site.iterdir() if path.suffix == ".js"}) == 2:
        with pytest.raises(ArtifactPathError, match="collision"):
            collect_artifact_bundle(
                entry,
                workspace_root=workspace,
                mode="directory",
                bundle_root=site,
            )
    (site / "A.js").unlink(missing_ok=True)
    (site / "a.js").unlink(missing_ok=True)

    composed = site / "\u00e9.js"
    decomposed = site / "e\u0301.js"
    composed.write_text("a", encoding="utf-8")
    decomposed.write_text("b", encoding="utf-8")
    if len({path.name for path in site.iterdir() if path.suffix == ".js"}) >= 2:
        with pytest.raises(ArtifactPathError, match="collision"):
            collect_artifact_bundle(
                entry,
                workspace_root=workspace,
                mode="directory",
                bundle_root=site,
            )
    composed.unlink(missing_ok=True)
    decomposed.unlink(missing_ok=True)

    outside = workspace / "outside.js"
    outside.write_text("outside", encoding="utf-8")
    link = site / "linked.js"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(ArtifactPathError, match="links or reparse"):
        collect_artifact_bundle(
            entry,
            workspace_root=workspace,
            mode="directory",
            bundle_root=site,
        )


def test_bundle_limits_apply_to_logical_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    site = workspace / "site"
    site.mkdir(parents=True)
    entry = site / "index.html"
    entry.write_text('<script src="./app.js"></script>', encoding="utf-8")
    (site / "app.js").write_text("const ready = true", encoding="utf-8")

    with pytest.raises(ArtifactBudgetError, match="file-count"):
        collect_artifact_bundle(
            entry,
            workspace_root=workspace,
            mode="auto",
            max_files=1,
        )
    with pytest.raises(ArtifactBudgetError, match="total"):
        collect_artifact_bundle(
            entry,
            workspace_root=workspace,
            mode="auto",
            max_bytes=entry.stat().st_size,
        )


@pytest.mark.parametrize(
    "invalid_path",
    [
        "/absolute.js",
        "../escape.js",
        r"\\server\share.js",
        "C:/drive.js",
        "assets/app.js:secret",
        "assets/%2e%2e/secret.js",
        "assets/control\u0001.js",
        "e\u0301.js",
        "/".join(["deep"] * 33),
    ],
)
def test_store_rejects_noncanonical_or_platform_ambiguous_bundle_paths(
    tmp_path: Path,
    invalid_path: str,
) -> None:
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile("index.html", "text/html", b"<p>entry</p>"),
            ArtifactBundleSourceFile(invalid_path, "text/javascript", b""),
        ),
    )

    with pytest.raises(ArtifactPathError):
        _store_bundle(tmp_path, bundle)


def test_store_bundle_keeps_public_entry_contract_and_resolves_resources(
    tmp_path: Path,
) -> None:
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile("index.html", "text/html", b"<script src='app.js'>"),
            ArtifactBundleSourceFile("app.js", "text/javascript", b"window.ready=true"),
        ),
    )
    store, ref = _store_bundle(tmp_path, bundle)

    payload = artifact_payload(ref)
    assert payload["sha256"] == ref.sha256
    assert payload["size"] == len(b"<script src='app.js'>")
    assert payload["download_url"] == f"/api/v1/artifacts/{ref.id}"
    assert "bundle_digest" not in payload
    _, entry_download = store.resolve_for_download(ref.id, session_id="session-1")
    assert entry_download.read_bytes() == b"<script src='app.js'>"

    manifest = store.describe_preview_bundle(ref.id, session_id="session-1")
    assert manifest is not None
    assert manifest.entrypoint == "index.html"
    assert manifest.file_count == 2
    assert (entry_download.parent / ARTIFACT_BUNDLE_MANIFEST_NAME).is_file()
    assert (
        entry_download.parent
        / ARTIFACT_BUNDLE_BLOBS_DIR
        / next(item.sha256 for item in manifest.files if item.path == "app.js")
    ).is_file()

    root_resource = store.resolve_preview_resource(
        ref.id,
        session_id="session-1",
    )
    assert root_resource.logical_path == "index.html"
    assert root_resource.path.read_bytes() == b"<script src='app.js'>"
    app_resource = store.resolve_preview_resource(
        ref.id,
        session_id="session-1",
        logical_path="/app.js",
    )
    assert app_resource.mime == "text/javascript"
    assert app_resource.path.read_bytes() == b"window.ready=true"
    with pytest.raises(ArtifactNotFoundError):
        store.resolve_preview_resource(
            ref.id,
            session_id="session-1",
            logical_path="../data",
        )


def test_store_bundle_detects_manifest_and_blob_tampering(tmp_path: Path) -> None:
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile("index.html", "text/html", b"<p>entry</p>"),
            ArtifactBundleSourceFile("app.js", "text/javascript", b"const x=1"),
        ),
    )
    store, ref = _store_bundle(tmp_path, bundle)
    material = store.path_for(ref)
    manifest_path = material.parent / ARTIFACT_BUNDLE_MANIFEST_NAME
    original = manifest_path.read_text(encoding="utf-8")
    manifest_payload = json.loads(original)
    manifest_payload["collection_status"] = "partial"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="digest"):
        store.describe_preview_bundle(ref.id, session_id="session-1")

    manifest_path.write_text(original, encoding="utf-8")
    manifest = store.describe_preview_bundle(ref.id, session_id="session-1")
    assert manifest is not None
    app = next(item for item in manifest.files if item.path == "app.js")
    (material.parent / ARTIFACT_BUNDLE_BLOBS_DIR / app.sha256).write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="hash"):
        store.resolve_preview_resource(
            ref.id,
            session_id="session-1",
            logical_path="app.js",
        )

    (material.parent / ARTIFACT_BUNDLE_BLOBS_DIR / app.sha256).unlink()
    with pytest.raises(ArtifactIntegrityError, match="unavailable"):
        store.resolve_preview_resource(
            ref.id,
            session_id="session-1",
            logical_path="app.js",
        )

    manifest_path.write_text(original, encoding="utf-8")
    manifest_payload = json.loads(original)
    manifest_payload["version"] = 2
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    with pytest.raises(ArtifactBundleUnsupportedError):
        store.describe_preview_bundle(ref.id, session_id="session-1")


def test_bundle_manifest_rejects_boolean_version_and_sizes() -> None:
    manifest = artifact_bundle_manifest(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile("index.html", "text/html", b"<p>entry</p>"),
            ),
        )
    ).to_dict()

    boolean_version = {**manifest, "version": True}
    with pytest.raises(ArtifactBundleUnsupportedError):
        ArtifactBundleManifest.from_dict(boolean_version)

    boolean_size = json.loads(json.dumps(manifest))
    boolean_size["files"][0]["size"] = True
    with pytest.raises(ValueError, match="size"):
        ArtifactBundleManifest.from_dict(boolean_size)


def test_store_bundle_publication_is_atomic_and_counts_bundle_disk_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile("index.html", "text/html", b"<p>entry</p>"),
            ArtifactBundleSourceFile("app.js", "text/javascript", b"const x=1"),
        ),
    )
    store = ArtifactStore(tmp_path / "media")
    with pytest.raises(ArtifactBudgetError, match="disk budget"):
        store.publish_bundle(
            bundle,
            session_id="session-1",
            session_key="agent:main:webchat:session-1",
            name="site.html",
            mime="text/html",
            source="publish_artifact",
            disk_budget_bytes=len(b"<p>entry</p>"),
        )
    assert not list((tmp_path / "media").rglob("meta.json"))

    real_write = artifacts_module._atomic_write_bytes

    def fail_manifest(path: Path, data: bytes) -> None:
        if path.name == ARTIFACT_BUNDLE_MANIFEST_NAME:
            raise OSError("synthetic manifest write failure")
        real_write(path, data)

    monkeypatch.setattr(artifacts_module, "_atomic_write_bytes", fail_manifest)
    with pytest.raises(OSError, match="synthetic"):
        store.publish_bundle(
            bundle,
            session_id="session-1",
            session_key="agent:main:webchat:session-1",
            name="site.html",
            mime="text/html",
            source="publish_artifact",
        )
    artifact_root = tmp_path / "media" / "artifacts"
    assert not list(artifact_root.rglob("meta.json"))
    assert not [path for path in artifact_root.rglob("*") if path.name.startswith(".")]


def test_entry_limit_does_not_reject_larger_bundle_dependencies(tmp_path: Path) -> None:
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile("index.html", "text/html", b"x"),
            ArtifactBundleSourceFile(
                "assets/animation.bin",
                "application/octet-stream",
                b"12345",
            ),
        ),
    )
    store = ArtifactStore(tmp_path / "media")

    ref = store.publish_bundle(
        bundle,
        session_id="session-1",
        session_key="agent:main:webchat:session-1",
        name="site.html",
        mime="text/html",
        source="publish_artifact",
        max_bytes=1,
        bundle_max_bytes=100,
    )

    resource = store.resolve_preview_resource(
        ref.id,
        session_id="session-1",
        logical_path="assets/animation.bin",
    )
    assert resource.path.read_bytes() == b"12345"


def test_bundle_digest_controls_dedupe_and_fork_copies_sidecars(tmp_path: Path) -> None:
    first = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile("index.html", "text/html", b"<p>same entry</p>"),
            ArtifactBundleSourceFile("app.js", "text/javascript", b"const version=1"),
        ),
    )
    second = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile("index.html", "text/html", b"<p>same entry</p>"),
            ArtifactBundleSourceFile("app.js", "text/javascript", b"const version=2"),
        ),
    )
    store, ref = _store_bundle(tmp_path, first)
    first_manifest = artifact_bundle_manifest(first)
    second_manifest = artifact_bundle_manifest(second)
    assert first_manifest.bundle_digest != second_manifest.bundle_digest
    assert (
        store.find_existing_ref(
            session_id="session-1",
            session_key="agent:main:webchat:session-1",
            sha256=ref.sha256,
            name="site.html",
            mime="text/html",
            bundle_digest=first_manifest.bundle_digest,
        )
        == ref
    )
    assert (
        store.find_existing_ref(
            session_id="session-1",
            session_key="agent:main:webchat:session-1",
            sha256=ref.sha256,
            name="site.html",
            mime="text/html",
            bundle_digest=second_manifest.bundle_digest,
        )
        is None
    )
    assert (
        store.find_existing_ref(
            session_id="session-1",
            session_key="agent:main:webchat:session-1",
            sha256=ref.sha256,
            name="site.html",
            mime="text/html",
            require_single_file=True,
        )
        is None
    )

    assert (
        store.copy_session_artifacts(
            source_session_id="session-1",
            target_session_id="child-1",
            target_session_key="agent:main:webchat:child-1",
        )
        == 1
    )
    child_manifest = store.describe_preview_bundle(ref.id, session_id="child-1")
    assert child_manifest == first_manifest
    child_app = store.resolve_preview_resource(
        ref.id,
        session_id="child-1",
        logical_path="app.js",
    )
    assert child_app.path.read_bytes() == b"const version=1"


@pytest.mark.asyncio
async def test_publish_artifact_defaults_to_auto_bundle_and_reports_partial(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text(
        '<script src="./app.js"></script><img src="./missing.png">',
        encoding="utf-8",
    )
    (workspace / "app.js").write_text("window.ready=true", encoding="utf-8")
    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:webchat:session-1",
    )

    token = current_tool_context.set(ctx)
    try:
        result = json.loads(await publish_artifact(path="index.html"))
    finally:
        current_tool_context.reset(token)

    assert result["status"] == "published"
    assert result["bundle"]["collection_status"] == "partial"
    assert result["bundle"]["warning_codes"] == ["missing_dependency"]
    assert "bundle" not in ctx.published_artifacts[0]
    store = ArtifactStore(tmp_path / "media")
    manifest = store.describe_preview_bundle(
        result["artifact"]["id"],
        session_id="session-1",
    )
    assert manifest is not None
    assert {item.path for item in manifest.files} == {"app.js", "index.html"}


@pytest.mark.parametrize(
    ("bundle", "bundle_root"),
    [
        ("none", ""),
        ("none", " \t\r\n"),
        ("auto", ""),
        ("auto", " \t\r\n"),
    ],
)
@pytest.mark.asyncio
async def test_publish_artifact_treats_blank_optional_bundle_root_as_omitted(
    tmp_path: Path,
    bundle: str,
    bundle_root: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text("<p>entry</p>", encoding="utf-8")
    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:webchat:session-1",
    )

    token = current_tool_context.set(ctx)
    try:
        result = json.loads(
            await publish_artifact(
                path="index.html",
                bundle=bundle,
                bundle_root=bundle_root,
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result["status"] == "published"
    assert ("bundle" in result) is (bundle == "auto")


@pytest.mark.parametrize("bundle", ["none", "auto"])
@pytest.mark.asyncio
async def test_publish_artifact_rejects_nonempty_bundle_root_outside_directory_mode(
    tmp_path: Path,
    bundle: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text("<p>entry</p>", encoding="utf-8")
    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:webchat:session-1",
    )

    token = current_tool_context.set(ctx)
    try:
        with pytest.raises(ToolError, match="only valid with directory mode"):
            await publish_artifact(
                path="index.html",
                bundle=bundle,
                bundle_root=".",
            )
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_publish_artifact_bundle_dedupe_includes_dependency_digest(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text(
        '<script src="./app.js"></script>',
        encoding="utf-8",
    )
    app = workspace / "app.js"
    app.write_text("window.version=1", encoding="utf-8")
    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:webchat:session-1",
    )

    token = current_tool_context.set(ctx)
    try:
        first = json.loads(await publish_artifact(path="index.html"))
        app.write_text("window.version=2", encoding="utf-8")
        second = json.loads(await publish_artifact(path="index.html"))
        third = json.loads(await publish_artifact(path="index.html"))
    finally:
        current_tool_context.reset(token)

    assert first["status"] == second["status"] == "published"
    assert first["artifact"]["sha256"] == second["artifact"]["sha256"]
    assert first["artifact"]["id"] != second["artifact"]["id"]
    assert third["status"] == "already_published"
    assert third["artifact"]["id"] == second["artifact"]["id"]


@pytest.mark.asyncio
async def test_publish_artifact_directory_mode_requires_safe_dedicated_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text("<p>entry</p>", encoding="utf-8")
    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:webchat:session-1",
    )

    token = current_tool_context.set(ctx)
    try:
        with pytest.raises(ToolError, match="dedicated"):
            await publish_artifact(
                path="index.html",
                bundle="directory",
                bundle_root=".",
            )
    finally:
        current_tool_context.reset(token)
