# Aliyun OSS release mirror

OpenSquilla mirrors published GitHub Release assets to Aliyun OSS for faster
Mainland China downloads.

The workflow lives at `.github/workflows/mirror-release-to-oss.yml`. It runs
when a GitHub Release is published and can also be run manually with a tag. The
workflow downloads release assets from GitHub, verifies `SHA256SUMS`, then
uploads version-scoped assets, moving installer aliases, and strict JSON
channel manifests used by update clients.

## Repository configuration

Configure these GitHub repository secrets:

- `ALIYUN_OSS_ACCESS_KEY_ID`
- `ALIYUN_OSS_ACCESS_KEY_SECRET`

Configure these GitHub repository variables:

- `ALIYUN_OSS_BUCKET`: OSS bucket name, for example `opensquilla-downloads`.
- `ALIYUN_OSS_REGION`: OSS region ID, for example `cn-hangzhou`.
- `ALIYUN_OSS_PREFIX`: optional object prefix. Defaults to `releases`.
- `ALIYUN_OSS_ENDPOINT`: optional custom endpoint or CNAME endpoint. Use this
  when the bucket or account requires a custom OSS data API endpoint.
- `ALIYUN_OSS_ADDRESSING_STYLE`: optional ossutil addressing style. Supported
  values are `virtual`, `path`, and `cname`. Leave it unset for normal OSS
  endpoints. Set it to `cname` when `ALIYUN_OSS_ENDPOINT` is a bound custom
  upload domain; the workflow also auto-selects `cname` when the endpoint host
  does not end in `aliyuncs.com`.

Use a dedicated RAM user or role scoped to the release mirror bucket/prefix. It
needs `oss:ListObjects`, `oss:GetObject`, `oss:PutObject`, `oss:DeleteObject`,
and the bucket-level `oss:GetBucketVersioning` permission: the workflow verifies
the bucket state, lists aliases, copies existing aliases to a short-lived
backup, uploads versioned assets, aliases, and channel manifests, and removes
backups and legacy `latest.html`. Do not use a full-access account key.

Keep OSS bucket versioning disabled for this mirror. Before uploading, the
workflow queries the versioning state through the standard regional OSS
endpoint and fails closed unless the bucket is unversioned. Version-scoped
uploads then use the OSS `x-oss-forbid-overwrite` condition so a concurrent
writer cannot replace an object between the workflow's existence check and
upload; OSS ignores that condition when bucket versioning is enabled or
suspended. Moving `latest/` and `channels/` objects retain their explicit
backup-and-rollback behavior.

## Destination layout

For tag `v0.5.0rc4` and the default prefix, the workflow writes version-scoped
assets:

```text
oss://<bucket>/releases/v0.5.0rc4/OpenSquilla-0.5.0-rc4-win-x64.exe
oss://<bucket>/releases/v0.5.0rc4/OpenSquilla-0.5.0-rc4-mac-arm64.dmg
oss://<bucket>/releases/v0.5.0rc4/opensquilla-0.5.0rc4-py3-none-any.whl
oss://<bucket>/releases/v0.5.0rc4/SHA256SUMS
```

After those checked assets are uploaded, it also replaces these two moving
installer aliases:

```text
oss://<bucket>/releases/latest/OpenSquilla-win-x64.exe
oss://<bucket>/releases/latest/OpenSquilla-mac-arm64.dmg
```

Use versioned paths when a download must remain pinned to a release tag. Use the
`latest` aliases only for user-facing "download the newest desktop app" links.
The aliases advance only when the mirrored release is the highest eligible
published release; older manual backfills cannot replace them.

Update clients do not use those moving installer aliases or `latest.json`.
Stable clients read `stable.json`; preview clients read their release-line
manifest. The manifest supplies a validated tag and versioned asset filenames
so discovery and download stay on one release:

```text
oss://<bucket>/releases/channels/latest.json
oss://<bucket>/releases/channels/stable.json
oss://<bucket>/releases/channels/preview/0.5.0.json
```

`stable.json` is advanced only by final releases. A preview-line manifest is
advanced by a higher RC or by the final release for the same base version, so
`0.5.0rc4` can move to `0.5.0rc5` or `0.5.0` but never to `0.6.0rc1`.
`latest.json` records the existing fixed-link behavior across release lines and
is used only to commit and roll back those aliases. Channel JSON requires cache
revalidation. Version-scoped OSS objects are write-once and use an immutable
cache policy: a rerun downloads every existing object, verifies that its SHA-256
matches the GitHub Release asset, and skips the upload only when the bytes are
identical. New objects are created with OSS's server-side forbid-overwrite
condition and verified again after upload. A changed asset under an already
mirrored tag is rejected; publish corrected release bytes under a new tag
instead.

Unsigned Windows clients do not execute an OSS object directly. They fetch the
matching GitHub Release's canonical `SHA256SUMS`, stream the exact versioned EXE
from the selected GitHub or OSS source into an application-owned directory,
verify its SHA-256, and only then reveal the file for an explicit manual install.
If GitHub's checksum is unavailable or the digest differs, the client fails
closed and deletes the partial or mismatched download.

With the default public endpoint, use these direct download URLs:

```text
https://<bucket>.oss-<region>.aliyuncs.com/releases/latest/OpenSquilla-win-x64.exe
https://<bucket>.oss-<region>.aliyuncs.com/releases/latest/OpenSquilla-mac-arm64.dmg
```

OSS default domains force browser downloads for these files. That is expected
for installer links and does not require a custom domain. The workflow does not
publish an HTML latest-release landing page because OSS default-domain security
policy forces HTML to download as well.

## Manual backfill

To mirror an already-published release, run the workflow manually and enter the
release tag, for example `v0.5.0rc4`. Manual backfills upload missing
version-scoped objects and verify existing ones byte-for-byte. They never
replace an existing object with different bytes under the same tag.
Before changing any moving object, the workflow compares the candidate with an
authenticated inventory of published GitHub Releases and with any existing OSS
manifest. The candidate must be the highest release for that channel and must
not be older than the OSS state. This also protects the first run after channel
manifests are introduced: backfilling `v0.5.0rc3` cannot replace already-live
rc4 aliases merely because `latest.json` has not been created yet. A backfill
that cannot advance `latest.json` also leaves the aliases and retired
`latest.html` object untouched.

## Failure model

The mirror workflow fails if required OSS configuration is missing, if the
GitHub Release has no downloadable assets, if `SHA256SUMS` is missing, if a
release asset is not listed in `SHA256SUMS`, if checksum verification fails,
if exactly one macOS DMG and Windows EXE installer cannot be found, or if an
existing channel manifest is malformed, or an existing version-scoped object
differs from the verified GitHub asset. Channel manifests and aliases are backed
up and rolled back as one promotion group; a legacy `latest.html` object is
included when it is removed during the same promotion. Each workflow attempt
uses a distinct backup prefix, so rerunning a failed workflow cannot overwrite a
snapshot retained for manual recovery. In those cases, GitHub remains the
source of truth and moving objects are not left partially updated.
After a successful, verified promotion, failure to remove the temporary backup
is reported as a workflow warning instead of falsely marking the committed
release mirror as failed.
