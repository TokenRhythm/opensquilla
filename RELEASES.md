# OpenSquilla Releases

| Version | Tag | Date | Notes |
|---|---|---|---|
| 0.5.0 | v0.5.0 | 2026-07-23 | Stable: Model Ensemble and multi-provider routing, safer upgrades and profile protection, signed macOS desktop updates, usage reporting, and the OSS download mirror |
| 0.5.0rc4 | v0.5.0rc4 | 2026-07-13 | Preview: safe profile recovery, explicit Windows Portable transfer, Desktop data retention, update reliability, and OSS downloads |
| 0.5.0rc3 | v0.5.0rc3 | 2026-07-10 | Preview: legacy-home migration, provider and routing expansion, desktop/Web UI improvements, runtime hardening, and container images |
| 0.5.0rc2 | v0.5.0rc2 | 2026-07-06 | Preview: provider/router recovery, Web UI upload refresh, desktop/session fixes, and CI contract repair |
| 0.5.0rc1 | v0.5.0rc1 | 2026-07-04 | Preview: Model Ensemble routing, Control UI, managed execution, OpenTUI, and portable retirement |
| 0.4.1 | v0.4.1 | 2026-06-30 | Desktop reliability, six-language client support, telemetry accuracy, router packaging, and mainline governance |
| 0.4.0 | v0.4.0 | 2026-06-27 | Control UI refresh, manual MetaSkills, coding mode, search expansion, and runtime hardening |
| 0.3.0 | v0.3.0 | 2026-05-31 | MetaSkills, Health Doctor, tool compression, and docs release |
| 0.2.1 | v0.2.1 | 2026-05-21 | 0.2 maintenance release |
| 0.2.0 | v0.2.0 | 2026-05-20 | 0.2 release |
| 0.2.0rc1 | v0.2.0rc1 | 2026-05-19 | Second public preview |
| 0.1.0rc1 | v0.1.0rc1 | 2026-05-12 | First public preview |

0.5.x releases (previews and the stable) publish Electron desktop installers,
updater metadata, the versioned Python wheel, and `SHA256SUMS`:

- `OpenSquilla-<version>-mac-arm64.dmg`
- `OpenSquilla-<version>-mac-arm64.zip`
- `OpenSquilla-<version>-win-x64.exe`
- `latest-mac.yml`
- `latest.yml`
- `*.blockmap`
- `opensquilla-<version>-py3-none-any.whl`
- `SHA256SUMS`

0.5.x preview releases are GitHub pre-releases and must not be marked as Latest;
the 0.5.0 stable is a normal release and may be marked Latest once verified.
They do not publish Windows portable zips, Windows portable latest aliases,
public wheelhouse zips, or separately branded macOS or Linux portable bundles.
The listed macOS `.zip` is the Electron desktop and updater artifact, not a
portable distribution.
Existing 0.4.x release pages keep their legacy Windows portable downloads for
historical compatibility, while new 0.5.x releases publish only the listed
Electron desktop artifacts, updater metadata, versioned wheel, and checksums.

For Windows Desktop upgrades from RC3 to RC4 or later, users must install the
new version directly over the existing installation and must not uninstall RC3
first. The RC3 uninstaller may remove `%APPDATA%\OpenSquilla`; release guidance
must tell users to back up that directory. RC4 and later NSIS packages set
`deleteAppDataOnUninstall=false`.

Container tags follow a separate policy: each release publishes
`ghcr.io/opensquilla/opensquilla:<git-tag>`, and Docker `:latest`
tracks the most recently pushed release tag, including previews and backports.
If a backport moves `:latest`, rerun the container workflow from the newest tag
to restore the intended ordering. The fixed release tag is the rollback and
reproducibility contract.

The Windows desktop installer is currently unsigned; release notes and download
sections must link to `docs/code-signing-policy.md` until a signing workflow is
approved and enabled. Windows browser downloads may carry Mark-of-the-Web, and
SmartScreen, Smart App Control, enterprise policy, and unsigned binary
reputation must be checked on a real Windows machine before broad promotion.

GitHub source archives remain available for code review and developer
reference; source installs should use `git clone` plus Git LFS. Python wheel
filenames must remain versioned because installers validate the version segment
inside the wheel filename.

Release wheels, Electron Desktop installers, and container images include the
CI-built Vue control console; installing those artifacts does not require
Node.js or npm. Git checkouts do not track the generated console. Source
installers and contributors producing Web UI or wheel artifacts require Node.js
22.12+ with npm, run `npm ci` plus `npm run build`, and therefore pay the
frontend dependency download, build-time, and cache-space cost. Backend-only
editable installs remain available without that build. Release notes should
call this out whenever the source build contract changes.

Release docs must describe the unified non-user-initiated network observability
switch. `OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true` or:

```toml
[privacy]
disable_network_observability = true
```

disables automatic install telemetry, passive update checks, and automatic
desktop update checks at startup and, while the app remains open, at most once
per day. The legacy compatibility environment variables
`OPENSQUILLA_TELEMETRY_DISABLED=true` and
`OPENSQUILLA_UPDATE_CHECK_DISABLED=true` remain honored. Manual user-initiated
update-availability checks do not bypass these controls. Opening a release page
or downloading an asset is a separate user-initiated action and may still
contact GitHub.

Update discovery follows the installed release channel. Stable builds only
offer newer stable releases. Preview builds offer the highest published release
on the same version base — a later RC or the final stable release — and never
jump to a preview on a different base. Supported macOS desktop builds check
after startup and at most once per day while the app remains open; surfaces
without native update support refresh the passive Control UI notice through the
local gateway. These long-running checks are included starting with RC4, so an
already-installed Windows RC3 still requires a manual, in-place RC4 upgrade.

README install commands must use tag-pinned URLs such as:

- `https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-mac-arm64.dmg`
- `https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-win-x64.exe`
- `https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl`

## Release SOP

1. Verify `git status` is clean before starting release prep.
2. Confirm the latest `origin/main` SHA is the intended release baseline and
   that its required CI run completed successfully.
3. Prepare a release PR from `origin/main`: update version metadata,
   `CHANGELOG.md`, `RELEASES.md`, `CONTRIBUTORS.md`, release notes, README
   download sections, install scripts, workflow asset contracts, and release
   tests.
4. Confirm release notes and README download sections link to `PRIVACY.md`,
   `THIRD_PARTY_NOTICES.md`, and `docs/code-signing-policy.md`; do not claim
   Windows code signing before it is enabled. Confirm privacy wording documents
   the unified network observability switch and legacy opt-out environment
   variables.
5. Bump `pyproject.toml`, `uv.lock`, `desktop/electron/package.json`,
   `desktop/electron/package-lock.json`, `install.sh`, and `install.ps1` to the
   release version.
6. Run the focused release contract tests locally, then open and merge the
   release PR only after review and CI pass.
7. Fetch `origin main --tags`, verify the merged `origin/main` SHA and CI one
   more time, then create the annotated tag on that exact SHA:

   ```sh
   git tag -a v0.5.0 <verified-sha> -m "OpenSquilla 0.5.0"
   git push origin v0.5.0
   ```

8. Wait for both `.github/workflows/wheelhouse-release.yml` and
   `.github/workflows/docker-image.yml`. Review the draft GitHub Release. For
   the `v0.5.0` stable, confirm it is not marked Pre-release, leave Latest
   unset until the maintainer explicitly confirms it at publish time, and
   confirm it contains only the Electron installers, updater metadata,
   versioned wheel, `SHA256SUMS`, plus GitHub's generated source archives. It
   must not contain `OpenSquilla-*-portable.zip` or
   `OpenSquilla-windows-x64-portable.zip`.
9. Verify GHCR before publishing broadly. For the first container release, make
   the newly created `ghcr.io/opensquilla/opensquilla` package public, then
   confirm both `v0.5.0` and `latest` resolve to an amd64/arm64 manifest and
   pass a gateway health smoke test.
10. Publish the GitHub Release only after maintainer confirmation, then run the
   post-publish tag URL checks:

   ```sh
   curl --fail --head --location https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-mac-arm64.dmg
   curl --fail --head --location https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-win-x64.exe
   curl --fail --head --location https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl
   curl --fail --head --location https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/SHA256SUMS
   ```

11. If a release tag is wrong before publication, stop and report its peeled
    SHA, the intended SHA, CI result, tag message, and protected-tag ruleset.
    Move it only through the protected-tag repair procedure, restore protection,
    and verify both workflows and the remote peeled tag before continuing.
12. For subsequent previews: bump the package version, docs, workflow
    contracts, and tag to the next preview version, for example `0.5.0rc5` /
    `v0.5.0rc5`. Preview GitHub Releases must remain pre-releases and use
    tag-pinned README URLs until a later stable release is intentionally
    promoted.

## GitHub-only release checks

These checks cannot be fully proven by local artifact generation:

- The tag exists on GitHub and matches `pyproject.toml`.
- The release workflow can fetch hydrated Git LFS router assets.
- The draft GitHub Release title is `OpenSquilla 0.5.0`.
- Preview drafts are marked Pre-release and never Latest; the `v0.5.0`
  stable draft is not marked Pre-release, and Latest is applied only at
  publish after explicit maintainer confirmation.
- Preview GitHub Releases contain the Electron installers, updater metadata,
  versioned wheel, and `SHA256SUMS` after `gh release upload --clobber`.
- Preview GitHub Releases do not contain Windows portable zips or portable
  latest aliases.
- The GHCR package is public, and `v0.5.0` plus `latest` expose both amd64
  and arm64 images that pass the gateway health smoke test.
- After a preview GitHub Release is published, the tag-pinned release asset URLs
  resolve.
- Windows browser downloads may carry Mark-of-the-Web; SmartScreen,
  Smart App Control, enterprise policy, and unsigned binary reputation must be
  checked on a real Windows machine.

## Why preview package versions use rc

Release assets are distributed as built artifacts, so the package filename,
installer name, wheel name, and tag should describe the same preview build.
PEP 440 accepts `0.5.0rc4`, while the public GitHub Release title can use the
friendlier name "OpenSquilla 0.5.0 Preview 4".
