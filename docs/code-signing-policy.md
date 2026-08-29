# OpenSquilla Code Signing Policy

This policy documents the current code signing status for OpenSquilla release
artifacts and the rules for the signing workflow.

## Current Status

Windows release builds are signed through DigiCert KeyLocker with the Beijing
TokenRhythm Technologies Co., Ltd. public code-signing certificate. The
protected `windows-code-signing` GitHub environment gates access to CI
credentials. Manually dispatched test artifacts from `main` and `v*` release
tags fail closed when credentials are unavailable, signing fails, or the
resulting Authenticode identity and timestamp do not match policy.

macOS release packaging is handled separately through the Apple signing and
notarization path configured by maintainers for macOS artifacts. This document's
Windows signing policy applies to open-source community release artifacts.

## User Verification

Users should download OpenSquilla release artifacts from the official GitHub
Releases page and compare file hashes against the published `SHA256SUMS` file
for the same release. On Windows, users can also inspect the installer digital
signature and verify that the publisher is Beijing TokenRhythm Technologies
Co., Ltd.

## Windows Signer Disclosure

The Windows publisher identity is pinned in
`.github/signing/windows-signing-policy.json`. Changing the expected
certificate, publisher, or timestamp endpoint requires a reviewed repository
change and a successful signed test build before a release tag is created.

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
service. Commercial or proprietary release artifacts must use credentials and
certificate identities that are separately approved for their distribution
scope.

## Release Build Requirements

The Windows signing workflow must run before updater metadata, blockmaps,
and `SHA256SUMS` are finalized. Signing an `.exe` after `latest.yml`,
`.blockmap`, or `SHA256SUMS` has been generated changes the installer bytes and
invalidates those release metadata files.

Maintainers must verify:

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

Repository: <https://github.com/opensquilla/opensquilla>

Initial committers and reviewers:

- [@Open-Squilla](https://github.com/Open-Squilla)

Initial Windows signing environment approvers:

- [@Open-Squilla](https://github.com/Open-Squilla)

OpenSquilla maintainers are responsible for release approval, release notes,
and final publication. Windows signing environment approvers approve signing
jobs only for the open-source community artifacts covered by this policy.
Additional committers, reviewers, or signing approvers must be listed in this
policy before they approve release signing requests. All committers, reviewers,
and approvers must use multi-factor authentication for GitHub and DigiCert ONE
access.

## Revocation Or Incident Response

If a signed artifact is found to be incorrect, compromised, or outside the
approved signing scope, maintainers will stop distributing the affected asset,
publish a corrected release or advisory, and request revocation through the
signing provider when appropriate. Unsigned artifacts remain covered by the
project's normal release correction and checksum replacement process.
