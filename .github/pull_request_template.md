## Scope

Scope boundary:

Non-goals:

## Branch

Base branch: main | staging/collaboration

Target exception: N/A | release | hotfix | staging/collaboration | maintainer-approved

## Issue

Linked issue: Fixes #... | Refs #... | None

If None, reason:

## Release Note

Release note: NONE |

## Tests

Ruff:

Pytest:

Build:

Regression tests: added | not needed

Notes:

The default test path remains offline, deterministic, credential-free, and safe for forks.

## Maintainer Live Check

Maintainer live check: no | yes

Surface: N/A | provider | browser | gateway | channel | release

Maintainer-only note: contributors are not expected to provide secrets or run credentialed live checks. Maintainers may manually run `LLM Smoke`, `Live Search E2E`, or `Live Telegram Smoke` for model API connectivity, search providers, or Telegram delivery. These workflows do not provide full Agent or Gateway model E2E coverage.

## Safety

Secrets, local-only artifacts, private prompts/transcripts, channel identifiers, AI session artifacts, non-public fixtures, and tests/_private/ contents must not be committed.

## Third-Party Origin

Third-party origin: none | inspired-by | adapted/ported | vendored | direct dependency | modified upstream

Details if non-none: upstream URL, license, copied/adapted code/rules/fixtures/text, and notice/provenance updates.

## Documentation Changes

- [ ] Links point to existing repository files or stable external pages.
- [ ] Code fences and Markdown tables render correctly on GitHub.
- [ ] Examples avoid real secrets, local private paths, and private transcripts.
