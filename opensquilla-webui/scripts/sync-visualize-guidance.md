# Maintaining the Visualize generation guidance

The bundled Python skill reads committed text resources. Node is used only by
maintainers and frontend checks to generate or verify those resources; the
Gateway does not invoke Node or fetch upstream instructions at runtime.

`sync-visualize-guidance.mjs` calls the public `generative-frame/prompts`
`buildWidgetGuidance` API from the exact npm version 0.0.5. It verifies the source,
the complete imported JavaScript graph, and LICENSE bytes before importing that
API. The npm integrity from the lockfile is recorded in the generated manifest.
The eight generated references include the common guidance and all seven upstream
modules. The source remains upstream; local differences are explicit adaptations.

The host configuration disables external origins, eval and direct model/tool
calls. Configuration alone does not remove upstream instructions for tool names,
localStorage, the streaming lifecycle, or follow-up buttons inside individual
modules. Those differences are narrow, exact-match replacements with one-match
assertions. Every adaptation records before/after hashes in the manifest. Changes
to the upstream headings or module framing fail instead of silently producing a
partially compatible skill. The upstream desktop and mobile outputs are combined
into one responsive-layout section: layout follows the actual container, while
mobile reflow and compact-chart rules apply only at narrow widths. The 360px
target is an acceptance viewport, not an assertion about the current frame or a
default phone-device layout. Explicit phone-screen mockups retain the upstream
360px device-frame option, bounded by the actual container. Primary text and
controls use at least 14px, inputs use 16px, and 12px is reserved for captions.
These local adaptations are recorded separately in the generated manifest.

Run in `opensquilla-webui`:

```text
npm run visualize:guidance:write
npm run visualize:guidance:check
npm run test:visualize-guidance
```

`--check` never writes. `--write` updates only the listed generated files. Review
the diff, including `upstream-guidance.json`, before committing. Both modes use
LF-normalized output comparison so a Windows CRLF checkout remains verifiable.
The input hashes are hashes of the original npm bytes, without normalization.

For an upstream upgrade, update the dependency and lockfile deliberately, review
changes to upstream guidance/theme code, update the version and input hashes,
review each host adaptation, regenerate and run the checks. Do not blindly update
hashes to accept an unexamined package. Keep the host API description synchronized
with the actual bridge, state limits, CSP and composer behavior. Runtime screenshot
patches have a separate compatibility review; this script never patches npm files.

`LICENSE.generative-frame.txt` accompanies the derived text in the Python wheel.
The manifest distinguishes upstream MIT guidance from the original OpenSquilla
publication/state/context contract and three optional examples. Root notices must
continue to describe this mixed provenance accurately. `.txt` is intentional:
the current wheel configuration excludes generic `references/*.md` and skill
`LICENSE.md` files. The real-wheel resource test verifies the delivered license,
manifest, references, hashes and `skill_view` access.

Acceptance must use natural requests beyond the original six trials. Cover plain
diagrams, dataset charts, custom data views, simulations, product mockups, form
collection and illustrations, including combinations and unseen subject matter.
Separate local-only tasks (no invented chat action) from tasks that explicitly
need later model work (optional context with accurate values and honest feedback).
The presence of a button or an API call alone is not evidence of useful behavior.
Check calculation units, state restoration, theme, keyboard, inline and expanded
desktop layout, and 360px layout as appropriate to each visual. Include an
unspecified-device prototype that uses available width and an explicitly requested
phone prototype that retains its device frame. Expansion changes the available
layout space; it does not magnify a fixed-width design or its text.
Do not force every example to have reset, submit or
continue-discussion controls, or train the acceptance prompts around exact labels.
