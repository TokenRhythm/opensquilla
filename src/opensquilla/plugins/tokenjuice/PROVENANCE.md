# tokenjuice Backend Provenance

This package contains OpenSquilla's built-in tokenjuice tool-result projection
backend.

## Upstream

- Project: https://github.com/vincentkoc/tokenjuice
- License: MIT
- Copyright notice: Copyright (c) 2026 Vincent Koc

## Adaptation Notes

OpenSquilla does not depend on the upstream tokenjuice npm package at runtime.
The Python reducer in this package is maintained by OpenSquilla and adapts the
rule-driven reduction approach for OpenSquilla's tool-result projection path.

The bundled JSON reduction rules are derived from upstream tokenjuice rules and
are redistributed under the upstream MIT license. The license text is included
in `LICENSE.tokenjuice` and recorded in the repository root
`THIRD_PARTY_NOTICES.md`.

## Update Procedure

When updating this backend or its bundled rules:

1. Review the upstream tokenjuice license and attribution text.
2. Keep `LICENSE.tokenjuice` and `THIRD_PARTY_NOTICES.md` in sync with any
   upstream license or copyright change.
3. Use synthetic fixtures for OpenSquilla tests; do not copy upstream fixtures
   unless their license/provenance is recorded explicitly.
4. Run the tokenjuice projection tests and packaging checks before release.
