"""Reconcile the reviewed, unpublished v0.5.8 Draft; never promote update channels.

This is a one-release maintenance exception, not an overwrite option for the
normal release workflows. Run only under their shared OSS writer lock. Durable
private backups contain original bytes, HEAD metadata and a rollback manifest
before any canonical object receives a delete marker. Version IDs are recorded,
but no historical version is fetched or deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPOSITORY = "TokenRhythm/opensquilla"
RELEASE_ID = 435303238
TAG = "v0.5.8"
SOURCE_SHA = "e41d20356bf41053f5c3f65aa95ca537ccc2d6dc"
BUCKET = "opensquilla-releases"
PREFIX = "releases/v0.5.8/"
BACKUP_ROOT = "releases/_maintenance/v0.5.8/"
CHANNEL_KEYS = ("releases/channels/stable.json", "releases/channels/latest.json")


@dataclass(frozen=True)
class Asset:
    name: str
    asset_id: int
    size: int
    sha256: str
    old_size: int
    old_sha256: str


# Public GitHub asset fingerprints. Neither a different release nor a rebuilt
# binary with the same filename is eligible for this maintenance exception.
ASSETS = (
    Asset(
        "OpenSquilla-0.5.8-mac-arm64.dmg",
        714434900,
        461975794,
        "3911b779f864009fc6fb9860b0f6c3cf24e1963e2e16194a7f9993defb6152df",
        461974845,
        "826ab794a4129d1efc0d77ae69aa58bbf98dfded4d76eaf7745185078cb7e08a",
    ),
    Asset(
        "OpenSquilla-0.5.8-mac-arm64.zip",
        714434903,
        461741801,
        "c20999334618b807e2372552180bfa899b5197a7eab75d719af1861f570aacca",
        461741832,
        "13d5678560f61360893e4c53b68a837ac30e0364bf1a547a7ed432c7345ee3ca",
    ),
    Asset(
        "OpenSquilla-0.5.8-mac-arm64.dmg.blockmap",
        714434907,
        482769,
        "1d791cb1f236de20a81144703479fc6d6c5673a5fff625038e6e5d4c3ba03bad",
        483021,
        "2bbf7c3b27e4db4290ecefd91c8fbfe9985eb06fd132704cfeb217782a76eeaf",
    ),
    Asset(
        "OpenSquilla-0.5.8-mac-arm64.zip.blockmap",
        714434906,
        482089,
        "cff6b003c3465a97a5ad21ca17376d60aa3967b700e299aba87a2ec38569d768",
        482114,
        "c1035928ae7f9d7c1f1891ead7c548e98b88f519ed77130d0d2f453bb0517afd",
    ),
    Asset(
        "latest-mac.yml",
        714435005,
        521,
        "8b28b8f911f97f36055efd2e6b11dfa8fa5eec4f16bf4996eb0a083e427642d9",
        521,
        "02dc7f54e1b94fb1682298e029b050dade2edf43a0753c40630b257d19e446b7",
    ),
    Asset(
        "OpenSquilla-0.5.8-win-x64.exe",
        714434905,
        423194528,
        "3d894c726a9fe3e57d2fb844b9a709449bca35e7aabc761bd8562feceae97fe0",
        423195648,
        "033fa87eca4573ddc15348594ae1873f5e01bdf5689e858eaabcd76fb2b43ea4",
    ),
    Asset(
        "OpenSquilla-0.5.8-win-x64.exe.blockmap",
        714434941,
        441784,
        "7fdefbdb5de876cc2a251df467f0fb43092e98238271521daaab5aaf3f7c9832",
        441856,
        "3b81cacddeb7137505d133b0ab436de7b75db514b983b5d8e8849be3d4b74308",
    ),
    Asset(
        "latest.yml",
        714435018,
        355,
        "fa4788515c8533e06e1356955490e258f1fc179aa197a438f402f2c91e691c46",
        355,
        "3332ffce6916d58c15d4b702111d835a95fde399d4026b29b3c7e3db97b154d7",
    ),
    Asset(
        "opensquilla-0.5.8-py3-none-any.whl",
        714435051,
        176379071,
        "db292f4b604422fb6828acb4d9bb976fb69fcfcff4eaf77109aca0a4aed7151a",
        176378748,
        "d16900234d8f6672f792dd9e5eaaf0b5d4340170438bc3a99853b5955f0fd5d3",
    ),
    Asset(
        "SHA256SUMS",
        714434947,
        870,
        "3d25d02681f7d61e1ce80746709a9f761769f69f1c8de53798ea1239b3acc9ff",
        870,
        "268285d2a1362d33828e0def3ea277c7535f78a4a6c56fd495856c22de5ed431",
    ),
)


class ReconciliationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReconciliationError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_output(raw: str) -> dict[str, Any]:
    """ossutil 2.3.0 appends an elapsed-time trailer after its JSON document."""
    value, end = json.JSONDecoder().raw_decode(raw.lstrip())
    trailer = raw.lstrip()[end:].strip()
    require(isinstance(value, dict), "Expected an OSS JSON object")
    require(
        not trailer or bool(re.fullmatch(r"[0-9.]+\(s\) elapsed", trailer)),
        "Unexpected data after OSS JSON response",
    )
    return value


def headers_from_head(value: dict[str, Any]) -> dict[str, str]:
    raw = value.get("Header")
    require(isinstance(raw, dict), "Missing OSS HEAD response headers")
    headers: dict[str, str] = {}
    for key, values in raw.items():
        require(
            isinstance(key, str)
            and isinstance(values, list)
            and len(values) == 1
            and isinstance(values[0], str),
            "Malformed OSS HEAD response header",
        )
        headers[key.lower()] = values[0]
    version = headers.get("x-oss-version-id", "")
    require(bool(version) and version != "null", "Expected a non-null OSS object version ID")
    require(headers.get("x-oss-delete-marker", "false") == "false", "Object is a delete marker")
    require(headers.get("x-oss-object-type") == "Normal", "Only normal PUT objects are supported")
    require(
        headers.get("x-oss-storage-class", "Standard") == "Standard",
        "Only Standard objects are supported",
    )
    require(
        headers.get("x-oss-tagging-count", "0") == "0", "Tagged objects require separate review"
    )
    require(
        not any(
            key.startswith(("x-oss-server-side-", "x-oss-persistent-", "x-oss-object-worm"))
            for key in headers
        ),
        "Unsupported encryption or protected object metadata",
    )
    require("content-language" not in headers, "Unsupported Content-Language metadata")
    return headers


def writable_metadata(headers: dict[str, str]) -> dict[str, str]:
    ordinary = {
        "content-type",
        "cache-control",
        "content-encoding",
        "content-disposition",
        "expires",
        "x-oss-storage-class",
    }
    return {
        key: value
        for key, value in headers.items()
        if key in ordinary or key.startswith("x-oss-meta-")
    }


def assert_release(release: dict[str, Any], source_sha: str, assets: tuple[Asset, ...]) -> None:
    require(
        release.get("id") == RELEASE_ID and release.get("tag_name") == TAG,
        "Release identity changed",
    )
    require(
        release.get("draft") is True
        and release.get("prerelease") is False
        and release.get("published_at") is None,
        "Release must remain an unpublished final Draft",
    )
    require(source_sha == SOURCE_SHA, "Release tag source changed")
    actual = release.get("assets", [])
    expected = {
        (asset.name, asset.asset_id, asset.size, "sha256:" + asset.sha256) for asset in assets
    }
    observed = {
        (item.get("name"), item.get("id"), item.get("size"), item.get("digest")) for item in actual
    }
    require(
        len(actual) == len(assets) and observed == expected,
        "GitHub Draft asset IDs, sizes or digests changed",
    )
    require(
        all(item.get("state") == "uploaded" for item in actual),
        "GitHub Draft has an incomplete upload",
    )


def check_file(path: Path, size: int, sha256: str) -> None:
    require(path.is_file() and not path.is_symlink(), f"Missing regular file: {path.name}")
    require(
        path.stat().st_size == size and digest(path) == sha256,
        f"Byte verification failed: {path.name}",
    )


def check_checksums(folder: Path, assets: tuple[Asset, ...]) -> None:
    lines = (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected = {f"{asset.sha256}  {asset.name}" for asset in assets if asset.name != "SHA256SUMS"}
    require(len(lines) == len(expected) and set(lines) == expected, "SHA256SUMS contract changed")


class Backend:
    """Only fixed production keys and attempt-scoped private backups are writable."""

    def __init__(self, attempt: str) -> None:
        require(bool(re.fullmatch(r"[0-9]+-[0-9]+", attempt)), "Invalid workflow attempt")
        self.backup_prefix = BACKUP_ROOT + attempt + "/"
        self.oss = [
            "ossutil",
            "--config-file",
            os.devnull,
            "--region",
            "cn-beijing",
            "--endpoint",
            "https://oss-cn-beijing.aliyuncs.com",
            "--addressing-style",
            "virtual",
            "--retry-times",
            "2",
        ]

    def run(
        self,
        command: list[str],
        destination: Path | None = None,
        *,
        environment: dict[str, str] | None = None,
        stream: bool = False,
    ) -> str:
        # The administrative workflow uses Ubuntu. Fail closed on Windows rather
        # than leaving a CLI descendant writing while rollback starts.
        require(os.name == "posix", "OSS maintenance execution requires a POSIX runner")
        output = destination.open("wb") if destination is not None else None
        try:
            process = subprocess.Popen(
                command,
                stdout=output if output else (None if stream else subprocess.PIPE),
                stderr=subprocess.PIPE,
                text=destination is None,
                env=environment,
                start_new_session=True,
            )
            try:
                stdout, _ = process.communicate()
            except BaseException:
                # Include bash's ossutil children, not only the shell process.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                raise
        finally:
            if output:
                output.close()
        # Do not echo CLI configuration, signed URLs or environment values on errors.
        require(
            process.returncode == 0,
            f"External command failed ({command[0]}, exit {process.returncode})",
        )
        return stdout if isinstance(stdout, str) else ""

    def api(self, operation: str, *arguments: str) -> dict[str, Any]:
        raw = self.run(
            [*self.oss, "api", operation, "--bucket", BUCKET, *arguments, "--output-format", "json"]
        )
        return json_output(raw)

    def github(self, path: str) -> dict[str, Any]:
        return json.loads(self.run(["gh", "api", f"repos/{REPOSITORY}/{path}"]))

    def inspect_release(self) -> tuple[dict[str, Any], str]:
        release = self.github(f"releases/{RELEASE_ID}")
        ref = self.github(f"git/ref/tags/{TAG}")["object"]
        for _ in range(3):
            if ref["type"] == "commit":
                return release, ref["sha"]
            require(ref["type"] == "tag", "Unexpected release tag object type")
            ref = self.github(f"git/tags/{ref['sha']}")["object"]
        raise ReconciliationError("Release tag nesting is unsupported")

    def versioning(self) -> str:
        return self.api("get-bucket-versioning").get("Status", "")

    def download_asset(self, asset: Asset, path: Path) -> None:
        self.run(
            [
                "gh",
                "api",
                f"repos/{REPOSITORY}/releases/assets/{asset.asset_id}",
                "-H",
                "Accept: application/octet-stream",
            ],
            path,
        )

    def download(self, key: str, path: Path) -> None:
        self.run([*self.oss, "cp", "--force", f"oss://{BUCKET}/{key}", str(path)])

    def head(self, key: str) -> dict[str, str]:
        return headers_from_head(self.api("head-object", "--key", key))

    def acl(self, key: str) -> str:
        acl = self.api("get-object-acl", "--key", key).get("AccessControlList", {}).get("Grant")
        require(acl in {"default", "private", "public-read"}, "Unexpected object ACL")
        return acl

    def backup(self, key: str, path: Path) -> None:
        require(key.startswith(self.backup_prefix), "Backup destination escaped this attempt")
        listing = self.run([*self.oss, "ls", "--short-format", f"oss://{BUCKET}/{key}"])
        require(f"oss://{BUCKET}/{key}" not in listing.splitlines(), "Backup already exists")
        self.api(
            "put-object",
            "--key",
            key,
            "--body",
            "file://" + str(path.resolve()),
            "--forbid-overwrite",
            "true",
            "--object-acl",
            "private",
            "--cache-control",
            "no-store",
        )

    def delete_marker(self, key: str) -> None:
        require(key in {PREFIX + asset.name for asset in ASSETS}, "Deletion is not allow-listed")
        # Deliberately no --version-id: Enabled versioning retains every old version.
        value = self.api("delete-object", "--key", key)
        headers = {
            key.lower(): values[0]
            for key, values in value.get("Header", {}).items()
            if isinstance(values, list) and len(values) == 1
        }
        require(
            headers.get("x-oss-delete-marker") == "true"
            and headers.get("x-oss-version-id") not in {None, "", "null"},
            "DeleteObject did not confirm a versioned delete marker",
        )

    def restore(self, key: str, path: Path, metadata: dict[str, str], acl: str) -> None:
        require(key in {PREFIX + asset.name for asset in ASSETS}, "Restore is not allow-listed")
        require(acl in {"default", "private", "public-read"}, "Unexpected rollback object ACL")
        options = ["--key", key, "--body", "file://" + str(path.resolve()), "--object-acl", acl]
        for name, value in writable_metadata(metadata).items():
            if name.startswith("x-oss-meta-"):
                options.extend(["--metadata", name.removeprefix("x-oss-meta-") + "=" + value])
            else:
                options.extend(["--" + name.removeprefix("x-oss-"), value])
        # Exact original bytes/metadata only, solely for rollback of this fixed set.
        self.api("put-object", *options)

    def prestage(self, folder: Path) -> None:
        environment = dict(os.environ)
        environment.update(
            ALIYUN_OSS_BUCKET=BUCKET,
            ALIYUN_OSS_PREFIX_NORMALIZED="releases",
            OSS_ENDPOINT="https://oss-cn-beijing.aliyuncs.com",
            OSS_ADDRESSING_STYLE_NORMALIZED="virtual",
            OPENSQUILLA_ALLOW_IMMUTABLE_REPAIR="1",
        )
        helper = Path(__file__).with_name("prestage-release-to-oss.sh")
        self.run(["bash", str(helper), str(folder), TAG], environment=environment, stream=True)


def inspect_channels(backend: Backend, folder: Path) -> dict[str, str]:
    result = {}
    for key in CHANNEL_KEYS:
        path = folder / (key.rsplit("/", 1)[-1] + ".channel")
        backend.download(key, path)
        require(
            json.loads(path.read_text())["tag"] == "v0.5.7",
            "Public update channel is no longer v0.5.7",
        )
        result[key] = digest(path)
    return result


def snapshot(backend: Backend, asset: Asset, path: Path) -> dict[str, Any]:
    key = PREFIX + asset.name
    before = backend.head(key)
    acl = backend.acl(key)
    backend.download(key, path)
    after_acl = backend.acl(key)
    after = backend.head(key)
    require(
        before["x-oss-version-id"] == after["x-oss-version-id"]
        and writable_metadata(before) == writable_metadata(after)
        and acl == after_acl,
        f"Object changed during snapshot: {asset.name}",
    )
    size, sha256 = path.stat().st_size, digest(path)
    require(
        (size, sha256) in {(asset.old_size, asset.old_sha256), (asset.size, asset.sha256)},
        f"Unreviewed OSS object bytes: {asset.name}",
    )
    require(str(size) == before.get("content-length"), f"HEAD size mismatch: {asset.name}")
    return {
        "key": key,
        "size": size,
        "sha256": sha256,
        "headers": before,
        "acl": acl,
        "version_id": before["x-oss-version-id"],
    }


def reconcile(
    backend: Backend, folder: Path, *, apply: bool = False, assets: tuple[Asset, ...] = ASSETS
) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=False)
    new, old, verify = (folder / name for name in ("new", "old", "verify"))
    for directory in (new, old, verify):
        directory.mkdir()
    require(backend.versioning() == "Enabled", "OSS versioning must be Enabled")
    assert_release(*backend.inspect_release(), assets)
    channels = inspect_channels(backend, verify)
    for asset in assets:
        backend.download_asset(asset, new / asset.name)
        check_file(new / asset.name, asset.size, asset.sha256)
    check_checksums(new, assets)
    originals = {asset.name: snapshot(backend, asset, old / asset.name) for asset in assets}
    changed = [asset for asset in assets if originals[asset.name]["sha256"] != asset.sha256]
    report = {
        "release_id": RELEASE_ID,
        "tag": TAG,
        "source_sha": SOURCE_SHA,
        "mode": "apply" if apply else "dry-run",
        "changed": [a.name for a in changed],
        "backup_prefix": backend.backup_prefix,
        "channels": channels,
        "originals": originals,
        "expected_assets": [asdict(asset) for asset in assets],
    }
    (folder / "inspection.json").write_text(json.dumps(report, indent=2) + "\n")
    assert_release(*backend.inspect_release(), assets)
    require(
        inspect_channels(backend, verify) == channels, "Public channel changed during inspection"
    )
    if not apply or not changed:
        return {"status": "dry-run" if not apply else "already-matching", **report}

    # Back up all ten objects, including their full HEAD metadata, before touching
    # any canonical key. Private immutable manifests support process-death recovery.
    for asset in assets:
        original = originals[asset.name]
        key = backend.backup_prefix + "objects/" + asset.name
        backend.backup(key, old / asset.name)
        backend.download(key, verify / asset.name)
        check_file(verify / asset.name, original["size"], original["sha256"])
        original["backup_key"] = key
        original["backup_version_id"] = backend.head(key)["x-oss-version-id"]
    report["recovery"] = (
        "Under the same OSS writer lock, restore every changed key from its private backup "
        "bytes using the recorded writable HEAD metadata and recorded object ACL. Verify every "
        "original SHA256. Never delete historical version IDs or write channel/latest keys."
    )
    manifest = folder / "rollback-manifest.json"
    manifest.write_text(json.dumps(report, indent=2) + "\n")
    manifest_key = backend.backup_prefix + manifest.name
    backend.backup(manifest_key, manifest)
    backend.download(manifest_key, verify / manifest.name)
    check_file(verify / manifest.name, manifest.stat().st_size, digest(manifest))
    print(f"Durable rollback manifest: oss://{BUCKET}/{manifest_key}", flush=True)

    # Re-read bytes as well as HEAD identities after persisting all backup data.
    for asset in assets:
        current = snapshot(backend, asset, verify / asset.name)
        original = originals[asset.name]
        require(
            current["version_id"] == original["version_id"]
            and current["sha256"] == original["sha256"]
            and current["acl"] == original["acl"]
            and writable_metadata(current["headers"]) == writable_metadata(original["headers"]),
            f"Object changed before mutation: {asset.name}",
        )
    assert_release(*backend.inspect_release(), assets)
    require(backend.versioning() == "Enabled", "OSS versioning changed before mutation")
    require(inspect_channels(backend, verify) == channels, "Public channel changed before mutation")
    attempted: list[Asset] = []
    try:
        for asset in changed:
            current = backend.head(PREFIX + asset.name)
            require(
                current["x-oss-version-id"] == originals[asset.name]["version_id"],
                f"Object changed immediately before deletion: {asset.name}",
            )
            # Set before the request: a lost response may conceal a successful delete.
            attempted.append(asset)
            backend.delete_marker(PREFIX + asset.name)
        backend.prestage(new)
        for asset in assets:
            current = snapshot(backend, asset, verify / asset.name)
            require(current["sha256"] == asset.sha256, f"Reconciled hash mismatch: {asset.name}")
        assert_release(*backend.inspect_release(), assets)
        require(
            inspect_channels(backend, verify) == channels, "Public channel changed after prestage"
        )
    except BaseException as failure:
        if not attempted:
            raise
        rollback_errors = []
        # Untouched keys must not be overwritten if their final HEAD check detected
        # an external writer. Once deletion completes, attempted is the full changed set.
        for asset in attempted:
            original = originals[asset.name]
            try:
                check_file(old / asset.name, original["size"], original["sha256"])
                backend.restore(
                    PREFIX + asset.name, old / asset.name, original["headers"], original["acl"]
                )
                restored = snapshot(backend, asset, verify / asset.name)
                require(
                    restored["sha256"] == original["sha256"]
                    and writable_metadata(restored["headers"])
                    == writable_metadata(original["headers"])
                    and restored["acl"] == original["acl"],
                    f"Rollback verification failed: {asset.name}",
                )
            except BaseException:
                rollback_errors.append(asset.name)
        result = {
            "status": "rollback-incomplete" if rollback_errors else "rolled-back",
            "rollback_errors": rollback_errors,
            "manifest_key": manifest_key,
        }
        (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        if rollback_errors:
            raise ReconciliationError(
                f"Rollback incomplete for {rollback_errors}; use durable manifest {manifest_key}"
            ) from failure
        raise ReconciliationError(
            "Reconciliation failed; original changed objects restored"
        ) from failure
    return {"status": "reconciled", "manifest_key": manifest_key, **report}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry-run", "apply"), default="dry-run")
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    require(
        os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
        and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and os.environ.get("GITHUB_REF") == "refs/heads/main",
        "Run this workflow from main",
    )
    require(
        os.environ.get("ALIYUN_OSS_BUCKET") == BUCKET
        and os.environ.get("OSS_REGION") == "cn-beijing"
        and os.environ.get("ALIYUN_OSS_PREFIX", "releases").strip("/") == "releases",
        "Unexpected OSS bucket, region or prefix",
    )
    require(
        all(
            os.environ.get(name)
            for name in ("GH_TOKEN", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET")
        ),
        "Missing protected-environment credentials",
    )
    attempt = os.environ.get("GITHUB_RUN_ID", "") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "")

    def interrupted(signum: int, frame: Any) -> None:
        raise ReconciliationError(f"Interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    result = reconcile(Backend(attempt), args.work_dir, apply=args.mode == "apply")
    (args.work_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "changed": result["changed"]}))


if __name__ == "__main__":
    try:
        main()
    except (ReconciliationError, OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error)) from None
