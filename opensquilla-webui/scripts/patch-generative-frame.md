# generative-frame 0.0.5 screenshot compatibility patch

Upstream: https://github.com/assistant-ui/assistant-ui/tree/main/packages/generative-frame

The dependency remains pinned to 0.0.5. The public screenshot API and its private
MessageChannel continue to run inside the existing opaque sandbox. This patch
does not grant same-origin access or rerun the generated HTML.

The upstream `src/runtime/screenshot.ts` is embedded in `dist/bootstrap.js`.
`patch-generative-frame.mjs` validates the package version and the complete
original file SHA-256 before changing three exact fragments. Repeated application
is byte-for-byte idempotent; unexpected upstream bytes fail closed. The script
runs before the normal npm development, artifact build and unit-test commands.
After changing the patch, restart an existing Vite server with `--force` to
invalidate its dependency prebundle.

The original bootstrap SHA-256 is:
`099be85a7d5581ada49860453ba2944465edc0690cc98cca5894cdfacc99fad8`.

Changes are confined to the screenshot clone:

1. Skip author `<style>` elements after computed styles have been copied. A rule
   such as `svg { margin: 24px 0 }` otherwise also affects the outer screenshot
   SVG, introducing layout shifts. The browser trial confirmed a 24px shift
   disappeared when this rule stopped being reapplied.
2. Lay out the clone at the source `devicePixelRatio` and compensate by scaling
   the SVG viewBox, preserving the original output dimensions. SVG image
   documents do not inherit browser zoom. At 80% browser zoom, freezing an
   auto-sized button's used CSS width and laying out its text at 100% caused the
   final character to wrap and be clipped. The original upstream code only used
   DPR in the final canvas rasterization step. The browser comparison confirmed
   that matching the layout scale restores the complete single-line label.

The upstream sparse computed-style copy remains intact. Screenshots retain
the upstream limits: URL-loaded fonts, cross-origin images and pseudo-elements
are best effort. Unit tests establish version/hash/idempotency and host bounds;
actual opaque-frame PNGs must also be opened and compared with live content.

Maintenance: review these two fixes against each proposed upstream update,
remove the patch if upstream covers them, and rerun the real PNG comparison
(including a narrow viewport and changed input values). Do not update the hash
alone to bypass an unsupported package change.

License: MIT, Copyright (c) 2026 AgentbaseAI Inc. The full upstream license is
included in `public-assets/assets/visualize-licenses/generative-frame.txt`.
