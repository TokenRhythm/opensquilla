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
2. Prefer `computedStyleMap()` for width/height, logical inline/block sizes,
   their min/max constraints, flex-basis, margin/padding longhands and insets.
   It preserves computed layout
   inputs such as `auto` and percentages instead of freezing `getComputedStyle`
   resolved pixel sizes. In a real narrow travel planner, three used widths plus
   the declared gaps summed to 299.0316px inside a 299.023px flex row. Feeding
   these rounded outputs back as explicit widths wrapped the last button; the
   copied fixed parent height then caused overlapping text. The actual frame's
   Typed OM values were `auto`, and keeping those inputs restored the complete
   row and footer in the exported PNG. Both physical and logical sizes are
   covered so a later logical declaration cannot undo the fix.
   The same issue affects automatic margins: a budget input's `margin-left:auto`
   became a fixed 143px margin and wrapped its header in the screenshot.
   Only single-value layout longhands use Typed OM; applying `get()` to every
   property could silently discard later values of a multi-layer background.

This patch does not infer layout zoom from DPR: device density and browser zoom
are different inputs. The upstream output scaling is unchanged. Earlier local
patch candidates are reversed before the full original hash check, allowing a
developer's existing dependency directory to migrate to this exact patch.

`computedStyleMap()` is feature-detected. If it or a property's typed value is
unavailable, the clone uses the original resolved-style value. That is a
compatible rendering fallback, **not verified equivalent PNG fidelity** for
flex layouts. The real narrow-width export was verified in Chromium; a desktop
viewport resized to phone width does not establish Safari, an old WebView, or
physical phone acceptance.

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
