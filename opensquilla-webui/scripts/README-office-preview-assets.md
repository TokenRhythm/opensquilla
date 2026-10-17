# Offline Office preview assets

`npm run prepare:office-assets` prepares the four pinned Noto SC region-subset
Regular/Bold fonts in `node_modules/.cache/office-preview-fonts`. The first run
needs access to the immutable upstream URLs; subsequent runs verify the cache
without network. `node scripts/prepare-office-preview-assets.mjs --offline`
fails if anything is missing or corrupted. Normal dev/build commands prepare
the assets before Vite. Unit-test scripts only verify the cache with `--offline`;
they never download test dependencies. Run the explicit preparation command as
a dependency setup step after a fresh `npm ci`, before npm or direct `vitest`
invocations. CI follows the same setup/verification split. Direct `vite` runs
also require asset preparation first. Runtime previews never fetch third-party fonts.

The cache is excluded from Git and the WebUI source fingerprint. Its immutable
manifest and preparation script are fingerprinted instead. Vite emits hashed
font assets through the same graph as Silurus JS, Worker and WASM files. Keep
the emitted files together; do not separately copy the npm package into the
desktop archive. The existing shared `control-ui-dist` package is the only
desktop copy. The license files under `public-assets/assets` ship with it.

The initial adapter uses `mode: 'main'`, `useGoogleFonts: false` and
`cjkFallback: 'sc'`. Call `ensureOfficePreviewFonts()` before constructing/loading
the viewer so text measurement and drawing see the same loaded faces. Parsing
still runs in the upstream Worker; layout and painting remain on the UI thread.
Document-embedded or installed authored fonts may produce different layout from
Noto fallback. Do not claim exact Office pagination or substitute an authored
font name with an alias. Moving layout/paint to render Workers requires a
separate verified font-loading implementation in those Workers; registration
in `document.fonts` alone is insufficient.

Release checks (not all established by a local browser smoke):

- Windows x64 and macOS arm64 installed app: offline first preview, actual
  `opensquilla-app://` asset URLs, module Worker/CSP, WASM MIME, four Chinese font
  faces, regular/bold sans/serif text, selection, teardown, and native PDF.
- Linux and any additional CPU architectures: run the equivalent packaged test
  when that installer is produced; browser compatibility is not installer QA.
- WebUI root and `/control` mounts: CSP, HTTP font/WASM MIME, subpath assets,
  unsupported OffscreenCanvas handling, and an offline preview with fonts loaded.
- Compare the generated `dist`, installed resource directory and each actual
  installer before/after. Per-asset gzip and npm unpacked size are separate
  measurements, and neither predicts resident memory.
- Exercise math, TIFF and optional chart inputs explicitly. Missing MathJax
  skips equations; missing TIFF support raises a rendering error. Lazy loading
  must not be described as omitting the runtime asset from the installer.
- The Gateway writer dependencies `python-docx`, `python-pptx` and `openpyxl`
  being declared in `pyproject.toml` does not establish that an installed runtime
  can import them. With a temporary profile and the actual packaged interpreter,
  import each module, write a small document under a test output directory,
  reopen it, and verify a requested targeted edit before declaring writeback ready.
