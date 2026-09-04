# Contributing to the Documentation

OpenSquilla welcomes documentation improvements from users and contributors.
Good documentation changes help people install the product, choose the right
feature, and recover from common problems without needing maintainer context.

## What to Improve

Useful documentation pull requests include:

- clearer install or setup steps;
- runnable command examples;
- screenshots or wording that make the Web UI easier to understand;
- missing provider, channel, memory, skill, or tool workflows;
- troubleshooting notes for common failures;
- corrections when a command, option, or behavior has changed.

Keep feature guides focused on user value and usage. Avoid adding deep runtime
internals unless the detail is needed to help users operate the product.

## How to Edit

1. If the problem is small, open the affected Markdown page on GitHub and use
the pencil edit flow to propose a change. Contributors without repository
write access will submit this through a fork and pull request.
2. If you are not sure of the fix, open a
   [documentation issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
   with the affected page and expected outcome.
3. Open documentation pull requests against `main`.
4. Keep docs changes small and topic-focused.
5. Use relative links for repository pages.
6. Prefer concrete commands and examples over abstract descriptions.
7. If a page describes a CLI command, verify the command name against the local
   CLI or an existing reference page.

For general contribution rules, see [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## Docs-Only Checks

For documentation-only changes, at minimum check:

- links point to existing repository files;
- Markdown code fences are balanced;
- examples do not include private paths, secrets, or real provider keys;
- screenshots or UI wording match the current product surface;
- product claims stay user-facing and do not expose unnecessary implementation
  details.

If a documentation change also changes code, tests, packaging, provider
behavior, gateway behavior, channels, or browser UI behavior, follow the full
project checklist in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## Page Structure

Most user-facing pages should answer:

1. What is this feature for?
2. When should I use it?
3. How do I configure or run it?
4. What should I check when it does not work?
5. Where should I go next?

Independent features should stay on independent pages. For example, memory,
skills, meta-skills, SquillaRouter, tool compression, compaction, channels, and
artifacts should not be merged into one broad mechanism page.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml) · [Contributing](../CONTRIBUTING.md)
