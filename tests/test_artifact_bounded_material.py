from __future__ import annotations

import hashlib
import os

import pytest

from opensquilla import artifacts
from opensquilla.artifacts import (
    ArtifactBudgetError,
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactPathError,
    ArtifactStore,
    _read_regular_bundle_file,
)


def test_oversized_file_is_rejected_before_open(tmp_path, monkeypatch):
    path = tmp_path / "large.docx"
    path.write_bytes(b"12345")

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("oversized file must not be opened")

    monkeypatch.setattr(artifacts.os, "open", unexpected_open)
    with pytest.raises(ArtifactBudgetError):
        _read_regular_bundle_file(path, max_bytes=4)


def test_exact_limit_and_unbounded_legacy_reader(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"1234")
    assert _read_regular_bundle_file(path, max_bytes=4) == b"1234"
    assert _read_regular_bundle_file(path) == b"1234"
    with pytest.raises(ArtifactBudgetError):
        _read_regular_bundle_file(path, max_bytes=3)


def test_growth_after_stat_is_read_with_a_hard_bound(tmp_path, monkeypatch):
    path = tmp_path / "growing.docx"
    path.write_bytes(b"1234")
    original = os.fdopen
    reads = []

    class GrowingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            reads.append(size)
            with path.open("ab") as writer:
                writer.write(b"x" * 100)
            return self.stream.read(size)

    monkeypatch.setattr(artifacts.os, "fdopen", lambda *a, **k: GrowingReader(original(*a, **k)))
    with pytest.raises(ArtifactBudgetError):
        _read_regular_bundle_file(path, max_bytes=8)
    assert reads == [5]  # Original size + 1, never reserve the full policy budget.


def test_replaced_file_with_same_size_and_mtime_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "document"
    replacement = tmp_path / "replacement"
    path.write_bytes(b"old")
    replacement.write_bytes(b"new")
    info = path.stat()
    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
    original = os.open

    def replace_on_open(name, *args, **kwargs):
        os.replace(replacement, path)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(artifacts.os, "open", replace_on_open)
    with pytest.raises(ArtifactPathError, match="changed"):
        _read_regular_bundle_file(path, max_bytes=3)


def test_reader_still_rejects_nonregular_files(tmp_path):
    with pytest.raises(ArtifactPathError, match="regular"):
        _read_regular_bundle_file(tmp_path, max_bytes=100)


def test_verified_material_bounds_before_read_and_is_session_scoped(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "media")
    ref = store.publish_bytes(
        b"sample", session_id="s", session_key="k", name="a.txt", mime="text/plain",
        source="test",
    )
    original = artifacts._read_regular_bundle_file
    calls = []

    def tracked(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(artifacts, "_read_regular_bundle_file", tracked)
    with pytest.raises(ArtifactBudgetError):
        store.read_verified_material(ref.id, session_id="s", max_bytes=5)
    assert not calls
    actual, data, manifest = store.read_verified_material(ref.id, session_id="s", max_bytes=6)
    assert (actual, data, manifest) == (ref, b"sample", None)
    assert len(calls) == 1
    with pytest.raises(ArtifactNotFoundError):
        store.read_verified_material(ref.id, session_id="other")


def test_public_lookup_excludes_internal_and_missing_material(tmp_path):
    store = ArtifactStore(tmp_path / "media")
    ref = store.publish_bytes(
        b"sample", session_id="s", session_key="k", name="a.txt", mime="text/plain",
        visibility="internal", source="test",
    )
    assert store.get_ref(session_id="s", artifact_id=ref.id) == ref
    with pytest.raises(ArtifactNotFoundError):
        store.get_public_ref(session_id="s", artifact_id=ref.id)
    store.promote_internal_ref(session_id="s", artifact_id=ref.id, expected_sha256=ref.sha256)
    assert store.get_public_ref(session_id="s", artifact_id=ref.id) == ref
    store.path_for(ref).unlink()
    with pytest.raises(ArtifactNotFoundError):
        store.get_public_ref(session_id="s", artifact_id=ref.id)


def test_bundle_reuse_keeps_member_integrity_checks(tmp_path):
    store = ArtifactStore(tmp_path / "media")
    bundle = ArtifactBundle(entrypoint="index.html", files=(
        ArtifactBundleSourceFile(path="index.html", mime="text/html", data=b"<p>hello</p>"),
        ArtifactBundleSourceFile(path="a.css", mime="text/css", data=b"p{color:red}"),
    ))
    ref = store.publish_bundle(
        bundle, session_id="s", session_key="k", name="index.html", mime="text/html",
        source="test",
    )
    actual, data, manifest = store.read_verified_material(
        ref.id, session_id="s", validate_bundle=True,
    )
    assert actual == ref and data == b"<p>hello</p>" and manifest is not None
    member = next(item for item in manifest.files if item.path == "a.css")
    blob = store.path_for(ref).parent / artifacts.ARTIFACT_BUNDLE_BLOBS_DIR / member.sha256
    blob.write_bytes(b"larger corrupt member" * 100)
    with pytest.raises(ArtifactIntegrityError):
        store.read_verified_material(ref.id, session_id="s", validate_bundle=True)


def test_export_bundle_reads_material_and_members_only_once(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "media")
    bundle = ArtifactBundle(entrypoint="index.html", files=(
        ArtifactBundleSourceFile(path="index.html", mime="text/html", data=b"<p>v1</p>"),
        ArtifactBundleSourceFile(path="a.css", mime="text/css", data=b"p{color:red}"),
        ArtifactBundleSourceFile(path="nested/b.js", mime="text/javascript", data=b"void 0"),
    ))
    ref = store.publish_bundle(
        bundle, session_id="s", session_key="k", name="index.html", mime="text/html",
        source="test",
    )
    original = artifacts._read_regular_bundle_file
    calls = []

    def tracked(path, **kwargs):
        assert kwargs.get("max_bytes") is not None
        calls.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(artifacts, "_read_regular_bundle_file", tracked)
    actual = store.read_verified_bundle(ref.id, session_id="s")
    assert {item.path: item.data for item in actual.files} == {
        item.path: item.data for item in bundle.files
    }
    assert len(calls) == len(set(calls)) == 4  # entry, manifest, and two dependencies
    with pytest.raises(ArtifactNotFoundError):
        store.read_verified_bundle(ref.id, session_id="other")
    with pytest.raises(ArtifactBudgetError):
        store.read_verified_bundle(ref.id, session_id="s", max_bytes=len(bundle.files[0].data))
    (store.path_for(ref).parent / artifacts.ARTIFACT_BUNDLE_BLOBS_DIR /
     hashlib.sha256(bundle.files[1].data).hexdigest()).write_bytes(b"corrupt")
    with pytest.raises(ArtifactIntegrityError):
        store.read_verified_bundle(ref.id, session_id="s")


def test_export_legacy_single_file(tmp_path):
    store = ArtifactStore(tmp_path / "media")
    ref = store.publish_bytes(
        b"<p>old</p>", session_id="s", session_key="k", name="index.html", mime="text/html",
        source="test",
    )
    bundle = store.read_verified_bundle(ref.id, session_id="s")
    assert bundle.entrypoint == "index.html"
    assert bundle.files[0].data == b"<p>old</p>"


@pytest.mark.parametrize("limit", ["count", "total", "entry"])
def test_collector_rejects_over_budget_before_open(tmp_path, monkeypatch, limit):
    entry = tmp_path / "index.html"
    entry.write_text('<script src="app.js"></script>', encoding="utf-8")
    dependency = tmp_path / "app.js"
    dependency.write_bytes(b"x" * 1024)
    original = os.open
    opened = []

    def tracked(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(artifacts.os, "open", tracked)
    options = {}
    if limit == "entry":
        monkeypatch.setattr(artifacts, "DEFAULT_ARTIFACT_MAX_BYTES", 1)
    elif limit == "count":
        options["max_files"] = 1
    else:
        options["max_bytes"] = entry.stat().st_size + 1
    with pytest.raises(ArtifactBudgetError):
        artifacts.collect_artifact_bundle(entry, workspace_root=tmp_path, **options)
    assert all(not path.endswith("app.js") for path in opened)
    if limit == "entry":
        assert not opened
