# Contributing

Thanks for improving OpenSquilla. Keep pull requests small, focused, and covered by tests that outside contributors can run without private access.

## Target Branch

Open pull requests against `main` by default. OpenSquilla now uses `main` as the active integration branch for feature work, bug fixes, tests, documentation, and contributor changes.

Use `release/*`, `hotfix/*`, `staging/*`, `integration/*`, `sandbox-*`, or a
maintainer-approved staging/collaboration label only when maintainers request a
temporary collaboration branch. When in doubt, target `main`.

## Linked Issues

Declare issue relationships in pull request descriptions with GitHub keywords:

- Use `Fixes #123`, `Closes #123`, or `Resolves #123` when the pull request is intended to fix the issue.
- Use `Refs #123` when the pull request is related but should not move the issue toward closure.
- Use `None` when no public issue is linked.

OpenSquilla keeps issue closure tied to the default branch. Merging a fixing
pull request into `main` removes the linked-pull-request marker so the issue
can follow GitHub's normal closing flow. Maintainers may use `has-linked-pr`
while work is still under review. If a linked pull request is closed without
merging, the automation removes `has-linked-pr`.

## Reporting Bugs

Use the bug-report issue template. Attach a diagnostics bundle
(`opensquilla bundle`, or the Web UI / desktop download button — see
`docs/troubleshooting.md`); it is redacted by default and safe to share. Quote
any `(ref: …)` code from the error message you saw. For suspected
vulnerabilities use SECURITY.md instead.

## Attribution On Squash Or Replay

When maintainer cleanup, replay, or squash merging collapses contributor
commits, keep the final non-empty commit attributable with `Co-authored-by:`
trailers for every human contributor whose work is included. Preserve pull
request author attribution and commit author attribution separately when they
differ.

If an older squash, replay, or follow-up pull request dropped contributor
attribution, do not rewrite protected branch history only to repair it. Open a
focused attribution repair pull request instead: update `CONTRIBUTORS.md` or the
release notes with affected pull requests and evidence, and include
`Co-authored-by:` trailers on the repair commit for missing human contributors
when GitHub can associate those emails. Do not treat "already appears in GitHub
contributors" as complete repair; that confirms global account representation,
not attribution for the specific squashed or replayed work.

## Default Checks

Full source development and the public quality gate require Node.js 22.12+ with
npm for the Vue control console; backend-only editable work can still use
`uv sync` without building it. Official wheels and Desktop installers are
already built and do not require Node.js. Install the locked frontend and
Python development dependencies:

```powershell
cd opensquilla-webui
npm ci
npm run build
cd ..
uv sync --extra dev --extra recommended
```

Run the public quality gate before opening a pull request:

```powershell
uv run ruff check src tests
uv run pytest -q
npm --prefix opensquilla-webui run build
uv build --wheel
```

The Web UI build writes an ignored package artifact that `uv build --wheel`
validates and embeds. Rebuild it after frontend changes; do not commit the
generated `src/opensquilla/gateway/static/dist/` tree.

Default tests must be offline, deterministic, credential-free, and safe for forks. Do not add network, provider, browser, or channel requirements to the default pull request path.

## Test Expectations

Add or update public regression tests for behavior changes and bug fixes. Prefer focused unit or integration tests unless the behavior crosses the gateway, browser UI, provider, or channel boundary.

Live checks are maintainer-only gates. The `Live Release E2E` workflow covers real provider, browser, and optional channel smoke tests with GitHub secrets and explicit opt-in inputs.

## Private Materials

Private test suites, release red-team prompts, real provider transcripts, real channel identifiers, local paths, credentials, and AI session artifacts must not be committed.

Local maintainer-only files may live under `tests/_private/` or `.omx/private-golden/`; both are excluded from the public tree and default pytest collection.

## Third-Party Origins

Declare any third-party origin in the pull request. If no third-party material is
involved, say `none`. If there is any uncertainty, use the more conservative
category and let maintainers narrow it during review.

- `inspired-by`: only the idea influenced the change; no code, rules, fixtures,
  structure, or copied text is reused.
- `adapted/ported`: OpenSquilla re-expresses upstream behavior, rules, or
  structure in OpenSquilla code.
- `vendored`: upstream source is copied into the repository with minimal or no
  changes.
- `direct dependency`: OpenSquilla depends on an external package through
  `pyproject.toml` or another package manager.
- `modified upstream`: vendored upstream source is patched or otherwise changed
  in the OpenSquilla tree.

For `adapted/ported`, `vendored`, and `modified upstream` material, include the
upstream URL, license, copyright notice, and any required changes to
`THIRD_PARTY_NOTICES.md` or a local provenance file in the same pull request.
For direct dependencies, note the package name and license so maintainers can
audit redistribution and release-bundle obligations.

Permissive licenses such as Apache-2.0, MIT, MIT-0, BSD, ISC, and compatible
public-domain-equivalent grants are usually acceptable. GPL, AGPL, LGPL, SSPL,
source-available, custom commercial, or unclear licenses require explicit
maintainer approval before code, rules, fixtures, or adapted implementations are
merged.

## Security Reports

Do not include vulnerability details, exploit steps, credentials, or provider tokens in public issues. Use the process in `SECURITY.md` for suspected vulnerabilities.

## Community Standards

Keep discussion technical, specific, and respectful. The expected conduct for issues, pull requests, and maintainer decisions is documented in `CODE_OF_CONDUCT.md`.
