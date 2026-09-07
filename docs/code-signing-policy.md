# OpenSquilla Code Signing Policy

This policy documents the current code signing status for OpenSquilla release
artifacts and the rules for any future signing workflow.

## Current Status

Windows release builds are currently unsigned. The Windows desktop installer,
updater metadata, and checksums are built and published without a Windows
code-signing certificate. Download pages and release notes must not claim Windows code signing
until a signing workflow has been approved, enabled, and verified for the
specific release artifact.

macOS release packaging is handled separately through the Apple signing and
notarization path configured by maintainers for macOS artifacts. This document's
planned SignPath section applies to Windows code signing for open-source
community release artifacts.

## User Verification

Users should download OpenSquilla release artifacts from the official GitHub
Releases page and compare file hashes against the published `SHA256SUMS` file
for the same release. A matching checksum verifies that the downloaded bytes
match the bytes published by the project; it does not imply Windows Authenticode
code signing while Windows builds remain unsigned.

## Future SignPath Foundation Plan

OpenSquilla is preparing to apply for free open-source Windows code signing
through SignPath Foundation. This is not enabled yet.

If the project is approved and signing is enabled, the affected open-source
community release artifacts may show `SignPath Foundation` as the Windows
publisher. Because approval is still pending, the following attribution is a
planned signing disclosure and does not claim that current Windows artifacts are
signed:

Free code signing provided by SignPath.io, certificate by SignPath Foundation.

The SignPath Foundation path will apply only to OpenSquilla open-source
community artifacts that are released under the project's OSI-approved license
and that do not include proprietary or commercial-only components.

## Privacy Policy

OpenSquilla's privacy policy is published at [`PRIVACY.md`](../PRIVACY.md). It
describes local data, provider requests, network observability, logs, release
downloads, and deletion. Non-user-initiated network observability can be
disabled before startup with:

```sh
OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true
```

or with:

```toml
[privacy]
disable_network_observability = true
```

Legacy compatibility environment variables remain honored:

```sh
OPENSQUILLA_TELEMETRY_DISABLED=true
OPENSQUILLA_UPDATE_CHECK_DISABLED=true
```

## Commercial Builds

This policy does not restrict future commercial editions, enterprise builds,
hosted services, support offerings, or proprietary add-ons from using a
separate commercial code-signing certificate or a separate commercial signing
service. Commercial or proprietary release artifacts must not be signed with
the SignPath Foundation certificate path unless they independently satisfy the
foundation program requirements.

## Release Build Requirements

Any future Windows signing workflow must run before updater metadata, blockmaps,
and `SHA256SUMS` are finalized. Signing an `.exe` after `latest.yml`,
`.blockmap`, or `SHA256SUMS` has been generated changes the installer bytes and
invalidates those release metadata files.

Before enabling Windows signing, maintainers must verify:

- the signing provider and certificate are approved for the exact artifact type
- the build runs from the trusted release workflow
- release signing requires maintainer approval
- team members with release or signing access use multi-factor authentication
- if network observability or any other non-user-specified network transfer
  remains enabled by default, the installer displays the privacy policy and
  exposes the unified network observability disable switch before startup
- signed artifacts, updater metadata, blockmaps, and checksums are generated
  from the same final bytes
- release notes and download pages accurately describe the signing status

## Roles And Approval

Repository: <https://github.com/TokenRhythm/opensquilla>

Initial committers and reviewers:

- [@Open-Squilla](https://github.com/Open-Squilla)

Initial SignPath approvers:

- [@Open-Squilla](https://github.com/Open-Squilla)

OpenSquilla maintainers are responsible for release approval, release notes, and
final publication. If SignPath signing is enabled later, SignPath approvers will
approve signing requests only for the open-source community artifacts covered by
this policy. Additional committers, reviewers, or SignPath approvers must be
listed in this policy before they approve release signing requests. All
committers, reviewers, and approvers must use multi-factor authentication for
GitHub and SignPath access.

## Revocation Or Incident Response

If a signed artifact is found to be incorrect, compromised, or outside the
approved signing scope, maintainers will stop distributing the affected asset,
publish a corrected release or advisory, and request revocation through the
signing provider when appropriate. Unsigned artifacts remain covered by the
project's normal release correction and checksum replacement process.
