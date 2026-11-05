from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/reconcile-v058-draft-oss.py"
_SPEC = importlib.util.spec_from_file_location("reconcile_v058_draft_oss", _SCRIPT)
assert _SPEC and _SPEC.loader
repair = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = repair
_SPEC.loader.exec_module(repair)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _headers(data: bytes, version: str = "synthetic-version") -> dict[str, str]:
    return {
        "x-oss-version-id": version,
        "x-oss-object-type": "Normal",
        "x-oss-storage-class": "Standard",
        "content-length": str(len(data)),
        "content-type": "application/octet-stream",
        "cache-control": "public,max-age=31536000,immutable",
        "x-oss-meta-synthetic": "fixture",
    }


class FakeBackend:
    def __init__(self, assets, old, new):
        self.assets, self.old, self.new = assets, old, new
        self.backup_prefix = repair.BACKUP_ROOT + "101-1/"
        self.events = []
        self.objects = {repair.PREFIX + name: (data, _headers(data)) for name, data in old.items()}
        for key in repair.CHANNEL_KEYS:
            data = b'{"tag":"v0.5.7"}'
            self.objects[key] = (data, _headers(data))
        self.release = {
            "id": repair.RELEASE_ID,
            "tag_name": repair.TAG,
            "draft": True,
            "prerelease": False,
            "published_at": None,
            "assets": [
                {
                    "name": asset.name,
                    "id": asset.asset_id,
                    "size": asset.size,
                    "digest": "sha256:" + asset.sha256,
                    "state": "uploaded",
                }
                for asset in assets
            ],
        }
        self.source = repair.SOURCE_SHA
        self.versioning_state = "Enabled"
        self.fail_backup = False
        self.fail_manifest = False
        self.fail_prestage_after = None
        self.fail_delete_after = False
        self.fail_restore_name = None
        self.race_after_manifest = False
        self.race_at_delete_head = False
        self.race_on_second_delete = False
        self.manifest_verified = False
        self.post_manifest_heads = 0
        self.acls = {repair.PREFIX + asset.name: "public-read" for asset in assets}

    def inspect_release(self):
        self.events.append(("release",))
        return copy.deepcopy(self.release), self.source

    def versioning(self):
        return self.versioning_state

    def download_asset(self, asset, path):
        self.events.append(("asset", asset.name))
        path.write_bytes(self.new[asset.name])

    def download(self, key, path):
        self.events.append(("download", key))
        path.write_bytes(self.objects[key][0])
        if key.endswith("rollback-manifest.json"):
            self.manifest_verified = True
            if self.race_after_manifest:
                first = repair.PREFIX + self.assets[0].name
                data, headers = self.objects[first]
                self.objects[first] = (data, {**headers, "x-oss-version-id": "external-version"})

    def head(self, key):
        if self.manifest_verified and key.startswith(repair.PREFIX):
            self.post_manifest_heads += 1
            first_race = (
                self.race_at_delete_head and self.post_manifest_heads == len(self.assets) * 2 + 1
            )
            second_race = (
                self.race_on_second_delete and self.post_manifest_heads == len(self.assets) * 2 + 2
            )
            if first_race or second_race:
                data, headers = self.objects[key]
                self.objects[key] = (data, {**headers, "x-oss-version-id": "external-version"})
        return dict(self.objects[key][1])

    def acl(self, key):
        return self.acls.get(key, "private")

    def backup(self, key, path):
        self.events.append(("backup", key))
        if self.fail_backup or (self.fail_manifest and key.endswith("rollback-manifest.json")):
            raise RuntimeError("synthetic backup failure")
        assert key not in self.objects
        data = path.read_bytes()
        self.objects[key] = (data, _headers(data, "synthetic-backup-version"))

    def delete_marker(self, key):
        self.events.append(("delete", key))
        assert self.manifest_verified
        self.objects.pop(key)
        if self.fail_delete_after:
            self.fail_delete_after = False
            raise RuntimeError("synthetic lost delete response")

    def prestage(self, folder):
        self.events.append(("prestage",))
        for index, asset in enumerate(self.assets):
            data = (folder / asset.name).read_bytes()
            self.objects[repair.PREFIX + asset.name] = (
                data,
                _headers(data, "synthetic-new-version"),
            )
            self.acls[repair.PREFIX + asset.name] = "default"
            if index == self.fail_prestage_after:
                raise RuntimeError("synthetic partial put failure")

    def restore(self, key, path, metadata, acl):
        self.events.append(("restore", key))
        if key.endswith(self.fail_restore_name or "never-matches"):
            raise RuntimeError("synthetic restore permission failure")
        data = path.read_bytes()
        self.objects[key] = (data, {**metadata, "x-oss-version-id": "synthetic-restored-version"})
        self.acls[key] = acl


@pytest.fixture
def backend(monkeypatch):
    names = [asset.name for asset in repair.ASSETS]
    old = {name: ("synthetic old " + name).encode() for name in names if name != "SHA256SUMS"}
    new = {name: ("synthetic new " + name).encode() for name in names if name != "SHA256SUMS"}
    for bodies in (old, new):
        bodies["SHA256SUMS"] = "".join(
            f"{_sha(data)}  {name}\n" for name, data in bodies.items()
        ).encode()
    assets = tuple(
        repair.Asset(
            name, 1000 + index, len(new[name]), _sha(new[name]), len(old[name]), _sha(old[name])
        )
        for index, name in enumerate(names)
    )
    monkeypatch.setattr(repair, "ASSETS", assets)
    return FakeBackend(assets, old, new)


def _run(backend, tmp_path, *, apply=True):
    return repair.reconcile(backend, tmp_path / "work", apply=apply, assets=backend.assets)


def _writes(backend):
    return [
        event for event in backend.events if event[0] in {"backup", "delete", "prestage", "restore"}
    ]


def test_dry_run_downloads_and_verifies_without_remote_writes(backend, tmp_path):
    result = _run(backend, tmp_path, apply=False)
    assert result["status"] == "dry-run"
    assert len(result["changed"]) == 10
    assert len([event for event in backend.events if event[0] == "asset"]) == 10
    assert _writes(backend) == []


def test_all_matching_is_idempotent_without_even_backup_writes(backend, tmp_path):
    for name, data in backend.new.items():
        backend.objects[repair.PREFIX + name] = (data, _headers(data))
    assert _run(backend, tmp_path)["status"] == "already-matching"
    assert _writes(backend) == []


def test_complete_durable_backup_precedes_every_delete_and_channels_stay_untouched(
    backend, tmp_path
):
    before_channels = {key: backend.objects[key] for key in repair.CHANNEL_KEYS}
    result = _run(backend, tmp_path)
    assert result["status"] == "reconciled"
    first_delete = next(i for i, event in enumerate(backend.events) if event[0] == "delete")
    backups = [event for event in backend.events[:first_delete] if event[0] == "backup"]
    assert len(backups) == 11
    manifest_key = backend.backup_prefix + "rollback-manifest.json"
    assert ("download", manifest_key) in backend.events[:first_delete]
    manifest = json.loads(backend.objects[manifest_key][0])
    assert manifest["source_sha"] == repair.SOURCE_SHA
    assert len(manifest["expected_assets"]) == len(manifest["originals"]) == 10
    assert all(
        item["version_id"] and item["backup_version_id"] and item["headers"]["cache-control"]
        for item in manifest["originals"].values()
    )
    assert {event[1] for event in backend.events if event[0] == "delete"} == {
        repair.PREFIX + asset.name for asset in backend.assets
    }
    for name, data in backend.new.items():
        assert backend.objects[repair.PREFIX + name][0] == data
    assert {key: backend.objects[key] for key in repair.CHANNEL_KEYS} == before_channels


@pytest.mark.parametrize(
    "change",
    [
        "published",
        "wrong-release",
        "wrong-tag",
        "wrong-source",
        "asset-id",
        "asset-size",
        "asset-digest",
        "extra-asset",
    ],
)
def test_release_drift_aborts_before_any_backup_or_delete(backend, tmp_path, change):
    if change == "published":
        backend.release["draft"] = False
    elif change == "wrong-release":
        backend.release["id"] += 1
    elif change == "wrong-tag":
        backend.release["tag_name"] = "v9.9.9"
    elif change == "wrong-source":
        backend.source = "a" * 40
    elif change == "extra-asset":
        backend.release["assets"].append(dict(backend.release["assets"][0]))
    else:
        field = {"asset-id": "id", "asset-size": "size", "asset-digest": "digest"}[change]
        backend.release["assets"][0][field] = "unreviewed"
    with pytest.raises(repair.ReconciliationError):
        _run(backend, tmp_path)
    assert _writes(backend) == []


@pytest.mark.parametrize("where", ["github", "oss"])
def test_unreviewed_bytes_abort_before_any_remote_write(backend, tmp_path, where):
    name = backend.assets[0].name
    if where == "github":
        backend.new[name] += b"unreviewed"
    else:
        data = b"unreviewed"
        backend.objects[repair.PREFIX + name] = (data, _headers(data))
    with pytest.raises(repair.ReconciliationError):
        _run(backend, tmp_path)
    assert _writes(backend) == []


@pytest.mark.parametrize("state", ["Suspended", "", "Disabled"])
def test_versioning_must_be_enabled(backend, tmp_path, state):
    backend.versioning_state = state
    with pytest.raises(repair.ReconciliationError, match="Enabled"):
        _run(backend, tmp_path)
    assert _writes(backend) == []


@pytest.mark.parametrize("failure", ["fail_backup", "fail_manifest"])
def test_incomplete_backup_never_changes_canonical_objects(backend, tmp_path, failure):
    setattr(backend, failure, True)
    with pytest.raises(RuntimeError, match="backup"):
        _run(backend, tmp_path)
    assert not any(event[0] in {"delete", "restore", "prestage"} for event in backend.events)


@pytest.mark.parametrize("race", ["race_after_manifest", "race_at_delete_head"])
def test_race_before_first_delete_never_overwrites_an_external_change(backend, tmp_path, race):
    setattr(backend, race, True)
    with pytest.raises(repair.ReconciliationError, match="changed"):
        _run(backend, tmp_path)
    assert not any(event[0] in {"delete", "restore", "prestage"} for event in backend.events)
    assert backend.objects[repair.PREFIX + backend.assets[0].name][1]["x-oss-version-id"] == (
        "external-version"
    )


@pytest.mark.parametrize("failure", ["partial-put", "lost-delete-response"])
def test_partial_failure_restores_whole_changed_set_and_original_metadata(
    backend, tmp_path, failure
):
    if failure == "partial-put":
        backend.fail_prestage_after = 2
    else:
        backend.fail_delete_after = True
    with pytest.raises(repair.ReconciliationError, match="original changed objects restored"):
        _run(backend, tmp_path)
    expected_restores = 10 if failure == "partial-put" else 1
    assert len([event for event in backend.events if event[0] == "restore"]) == expected_restores
    for name, data in backend.old.items():
        body, headers = backend.objects[repair.PREFIX + name]
        assert body == data
        assert repair.writable_metadata(headers) == repair.writable_metadata(_headers(data))
        assert backend.acls[repair.PREFIX + name] == "public-read"
    assert json.loads((tmp_path / "work/result.json").read_text())["status"] == "rolled-back"


def test_late_race_rolls_back_only_attempted_keys_without_overwriting_external_writer(
    backend, tmp_path
):
    backend.race_on_second_delete = True
    with pytest.raises(repair.ReconciliationError, match="restored"):
        _run(backend, tmp_path)
    assert [event[1] for event in backend.events if event[0] == "restore"] == [
        repair.PREFIX + backend.assets[0].name
    ]
    second = repair.PREFIX + backend.assets[1].name
    assert backend.objects[second][1]["x-oss-version-id"] == "external-version"


def test_rollback_failure_keeps_durable_recovery_manifest_and_reports_exact_key(backend, tmp_path):
    backend.fail_prestage_after = 1
    backend.fail_restore_name = backend.assets[0].name
    with pytest.raises(repair.ReconciliationError, match="Rollback incomplete"):
        _run(backend, tmp_path)
    result = json.loads((tmp_path / "work/result.json").read_text())
    assert result["rollback_errors"] == [backend.assets[0].name]
    assert result["manifest_key"] in backend.objects
    manifest = json.loads(backend.objects[result["manifest_key"]][0])
    assert len(manifest["originals"]) == 10


def test_known_cli_json_with_elapsed_trailer_and_header_lists():
    value = {"Header": {key: [value] for key, value in _headers(b"synthetic").items()}}
    decoded = repair.json_output(json.dumps(value) + "\n\n0.002479(s) elapsed\n")
    assert repair.headers_from_head(decoded)["x-oss-version-id"] == "synthetic-version"
    assert repair.json_output('{"Status":"Enabled"}\n0.1(s) elapsed')["Status"] == "Enabled"
    with pytest.raises(repair.ReconciliationError):
        repair.json_output('{"Status":"Enabled"}\n{"unexpected":true}')


@pytest.mark.parametrize(
    "field,value",
    [
        ("x-oss-version-id", "null"),
        ("x-oss-object-type", "Symlink"),
        ("x-oss-tagging-count", "1"),
        ("x-oss-server-side-encryption", "KMS"),
    ],
)
def test_unsupported_head_contract_fails_closed(field, value):
    headers = _headers(b"synthetic")
    headers[field] = value
    with pytest.raises(repair.ReconciliationError):
        repair.headers_from_head({"Header": {key: [value] for key, value in headers.items()}})


def test_delete_backend_has_no_version_delete_or_unscoped_key(monkeypatch):
    backend = repair.Backend("101-1")
    calls = []

    def run(command, destination=None):
        calls.append(command)
        return json.dumps(
            {"Header": {"X-Oss-Delete-Marker": ["true"], "X-Oss-Version-Id": ["synthetic-marker"]}}
        )

    monkeypatch.setattr(backend, "run", run)
    backend.delete_marker(repair.PREFIX + repair.ASSETS[0].name)
    assert "--version-id" not in calls[0]
    assert "--bypass-governance-retention" not in calls[0]
    assert "--recursive" not in calls[0]
    for forbidden in [
        "releases/channels/stable.json",
        "releases/latest/installer.exe",
        "releases/v0.5.7/old.exe",
        repair.PREFIX + "unexpected",
    ]:
        with pytest.raises(repair.ReconciliationError, match="allow-listed"):
            backend.delete_marker(forbidden)
    assert len(calls) == 1
