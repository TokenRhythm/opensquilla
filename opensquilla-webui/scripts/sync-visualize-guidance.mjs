import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const webuiRoot = fileURLToPath(new URL('../', import.meta.url))
const repoRoot = path.dirname(webuiRoot)
const packageRoot = path.join(webuiRoot, 'node_modules/generative-frame')
export const skillRoot = path.join(repoRoot, 'src/opensquilla/skills/bundled/visualize')
export const UPSTREAM_VERSION = '0.0.5'
export const MODULES = ['diagram', 'chart', 'data_viz', 'interactive', 'mockup', 'elicitation', 'art']
// Both the reviewable source and the complete imported JavaScript graph are pinned.
// An upgrade must review these inputs and the generated diff together.
export const UPSTREAM_HASHES = {
  'src/prompts/guidance.ts': '5b941af8e64379d842d8f1f9b0b4461ae307e7d8a093ffe900e8865caa9ce290',
  'src/theme.ts': '018ab8eb676ce0af7f5dd3dbffaac3e3467618c3754c447488dcfa8ebd87674f',
  'dist/prompts.js': '9567813822a646914b32d21abf2cf550a37345720629ada51de2797acf187ae8',
  'dist/prompts/guidance.js': '964dd0e5102de968dadea939878156cbd729e8eb42a920b008abf83bdafa7cd1',
  'dist/theme.js': '0c5f8e6ca9fe0310fb51a3c5673eb611b19dd5c4d33310242421e9e4ec775129',
  'dist/csp.js': '3fb0926ce209eab446d5057af1bc1412ed8060ff9a1e11498309d3cb636a26f9',
  LICENSE: 'eafb05991a572efa34097474b31fef45b14a8879ed1d1a8fd77de62e6fa7d275',
}
export const GUIDANCE_OPTIONS = {
  platform: 'desktop', width: 680,
  cdnOrigins: [], fontStyleOrigins: [], fontOrigins: [], connectOrigins: [],
  allowEval: false,
  hostApi: { prompt: false, callTool: false, modelContext: false },
}
export const sha256 = value => createHash('sha256').update(value).digest('hex')
export const normalizeText = text => text.replace(/\r\n/g, '\n')

export function replaceExactlyOnce(text, before, after, id, records = []) {
  const count = text.split(before).length - 1
  assert.equal(count, 1, `Upstream guidance drift: ${id} expected one match, found ${count}`)
  records.push({ id, matches: count, beforeSha256: sha256(before), afterSha256: sha256(after) })
  return text.replace(before, after)
}

function replaceSection(text, title, nextTitle, replacement, records) {
  const start = `## ${title}\n\n`
  const end = `\n\n## ${nextTitle}`
  assert.equal(text.split(start).length - 1, 1, `Missing or duplicate section: ${title}`)
  assert.equal(text.split(end).length - 1, 1, `Missing or duplicate section: ${nextTitle}`)
  const from = text.indexOf(start)
  const to = text.indexOf(end, from)
  assert.ok(to > from, `Out-of-order section: ${title}`)
  return replaceExactlyOnce(text, text.slice(from, to), `## ${title}\n\n${replacement}`, `base:${title}`, records)
}

export function adaptBase(raw, mobile, records = []) {
  let text = raw
  const edit = (before, after, id) => { text = replaceExactlyOnce(text, before, after, id, records) }
  edit('A widget is a visual you place in the conversation by calling `show_widget`. Its `widget_code` streams into a sandboxed frame and renders element by element while you write it.',
    'A widget is a visual published inside the conversation using the OpenSquilla publication contract in SKILL.md. The host displays the registered HTML revision in an isolated frame.', 'base:publication')
  edit('The frame is about 680px wide and grows to the height of your content.',
    'Use the available container width and let content determine its height. Reflow when the frame is resized or expanded. A 360px width is a narrow-screen acceptance check, not the default or fixed widget width.', 'base:responsive-width')
  edit('## Streaming order', '## Document order', 'base:document-order')
  edit('Scripts are held until the whole widget has arrived, then run once in document order.',
    'The host loads a complete immutable revision; scripts run once in document order.', 'base:complete-revision')
  edit('They repaint on every streamed chunk and look unstable;', 'They add visual noise and rendering cost;', 'base:render-cost')
  edit('Pin exact versions in library URLs, for example `(no script CDN is allowed: chart.js@4.4.1/dist/chart.umd.min.js is unavailable)`. Prefer UMD builds loaded with `<script src>`; use `<script type="module">` with ES module URLs only when no UMD build exists.',
    'No external library URL is available. Use native SVG/Canvas/DOM, or a permitted library whose exact code is already embedded in the self-contained file; never invent a CDN or assume a global library exists. The complete file must stay under 1 MiB.', 'base:offline-libraries')
  edit('`localStorage` can keep small UI state (the open tab, a toggle) between visits when the host gives the widget a stable id; otherwise it starts empty. Read it defensively, and expect the browser to evict it. Do not use cookies: browsers block them in third-party frames. State that must last goes through `genframe.setState`.',
    'Do not use localStorage, sessionStorage, cookies, local file paths or external frames. Restore and save UI state with `window.opensquillaVisualize.getState()` and `.setState(value)` as described in SKILL.md.', 'base:state-storage')
  text = replaceSection(text, 'Host API', 'Accessibility', [
    '- Use only `window.opensquillaVisualize`: `ready`, `getState()`, `setState(value)`, and optional `setContext(value)`; see SKILL.md for the exact contract.',
    '- Local controls calculate, filter, select, navigate or animate locally. Do not generate a generic continue-discussion button or call legacy `sendPrompt` in new visuals.',
    '- Only when the task calls for subsequent model work, publish a useful selection with `setContext({ summary, data?, suggestedPrompt? })`. This prepares context, not a message. The host owns the action that adds it to an editable chat draft; the user sends that draft.',
    '- `setContext(null)` clears the prepared selection. Await its acknowledgement before reporting that context is available; never claim a message was sent.',
    '- The window fires `themechange` when the host theme changes; resolve CSS tokens again when repainting Canvas.',
    '- Do not call tools, navigate to external links, request credentials, or create your own expand/reload/export controls. The host supplies canvas controls.',
  ].join('\n'), records)
  edit('Keep text at 12px or larger and at readable contrast.',
    'Keep readable contrast. Use the host\'s normal text size for main content and controls, at least 14px; reserve 12px for secondary captions. Reflow content instead of shrinking important text to fit.', 'base:readable-primary-text')
  edit('To change a widget you already showed, call `edit_widget` with exact `old_string` → `new_string` replacements copied from its latest code. Each `old_string` must occur exactly once; include neighboring text to make it unique. Re-send the whole widget with `show_widget` under the same title only for broad rewrites.',
    'To change a direct in-chat visual, call `edit_widget` with exact `old_string` → `new_string` replacements copied from its latest code. Each `old_string` must occur exactly once; include neighboring text to make it unique. Re-send the whole widget with `show_widget` under the same title only for broad rewrites. For a file-backed visual, edit its source file and call `open_workspace_preview(path=..., presentation="inline")` again. Registration alone does not verify rendering or interaction.', 'base:immutable-edits')
  const moduleStart = '\n\n## Modules\n\n'
  assert.equal(text.split(moduleStart).length - 1, 1, 'Missing or duplicate module index')
  const index = text.slice(text.indexOf(moduleStart))
  edit(index, '\n\n## Modules\n\nRead the relevant generated reference through `skill_view(name="visualize", file_path="references/guidance-MODULE.txt")`. Choose and combine modules from the index in SKILL.md; these are design capabilities, not fixed templates.', 'base:skill-module-routing')
  const mobileStart = '## Platform: mobile\n\n'
  const mobileEnd = '\n\n## Before sending'
  assert.equal(mobile.split(mobileStart).length - 1, 1)
  const from = mobile.indexOf(mobileStart)
  const to = mobile.indexOf(mobileEnd, from)
  assert.ok(to > from)
  let mobileRules = replaceExactlyOnce(mobile.slice(from, to), mobileStart, '', 'base:mobile-rules-heading', records)
  mobileRules = replaceExactlyOnce(mobileRules,
    'The frame is about 360px wide. Use a single column and stack anything that would sit side by side.',
    'At narrow frame widths, including 360px, use a single column and stack anything that would sit side by side.', 'base:conditional-mobile-width', records)
  mobileRules = replaceExactlyOnce(mobileRules,
    'Keep charts at most 240px tall and limit them to the few series that matter.',
    'At narrow frame widths, keep charts at most 240px tall and limit them to the few series that matter.', 'base:conditional-mobile-chart', records)
  edit('## Platform: desktop', '## Responsive layout', 'base:responsive-heading')
  edit('The frame is about 680px wide. Up to three columns fit; collapse to one column below 480px with a media query.',
    'Use fluid widths, flexbox or grid to make use of the actual frame. On wider frames, up to three columns can fit; collapse to one column below 480px with a media query.', 'base:responsive-columns')
  edit(mobileEnd, `\n\n${mobileRules}\n- For dense diagrams or long labels, reflow or show a focused view at phone widths; do not shrink a desktop SVG until text becomes unreadable.\n- Expansion provides more layout space; it does not magnify fixed-width content or increase its font size. Only an explicitly requested device mockup should retain a phone-sized frame.\n\n## Before sending`, 'base:include-conditional-mobile')
  return `${text}\n`
}

export function adaptModule(name, raw, records = []) {
  let text = raw
  const edit = (before, after, id) => { text = replaceExactlyOnce(text, before, after, `${name}:${id}`, records) }
  if (name === 'diagram') {
    edit('Draw in this order so the stream builds up sensibly and lines tuck under boxes:', 'Draw in this order so lines tuck under boxes:', 'layer-order')
  } else if (name === 'chart') {
    edit('they render while streaming and need no library.', 'they need no external library.', 'native-svg')
    edit('For anything richer use Chart.js 4: `<script src="(no script CDN is allowed: chart.js@4.4.1/dist/chart.umd.min.js is unavailable)"></script>`.',
      'For richer charts use responsive SVG/Canvas. Chart.js-specific advice below applies only if its permitted code is already embedded in the file; the host does not preload Chart.js and external scripts are unavailable.', 'offline-chart')
    edit('so they show while streaming.', 'so labels are available before a chart script runs.', 'visible-labels')
  } else if (name === 'data_viz') {
    edit('For custom visualizations use D3 v7 (`(no script CDN is allowed: d3@7.9.0/dist/d3.min.js is unavailable)`).',
      'For custom visualizations use native SVG/Canvas, or D3 only if its permitted code is already embedded in the file; the host does not preload D3 and external scripts are unavailable.', 'offline-data-viz')
  } else if (name === 'interactive') {
    edit('Offer follow-up questions through buttons that call `sendPrompt` with the current state described in words.',
      'Keep parameter changes and results local. Do not add a generic continue-discussion or submit-to-chat button. If the requested workflow includes subsequent model work, use optional `setContext` from SKILL.md; the host presents the chat action.', 'optional-model-context')
  } else if (name === 'mockup') {
    edit('Frame phone screens as a 360px column with `--radius-lg` corners and a 1px border, centered; desktop screens can get a thin window bar.',
      'Only when the user explicitly requests a phone-screen mockup, use a centered 360px device frame with `--radius-lg` corners, a 1px border and `max-width:100%`. Otherwise make the interface responsive to the actual container; narrow-screen support does not imply a phone mockup. Desktop screens can get a thin window bar.', 'explicit-phone-frame')
    const beforeBoundary = text
    text += '\n\nMockup actions demonstrate the depicted product locally. Do not turn a simulated product submit button into a real model request.\n'
    records.push({ id: 'mockup:simulation-boundary', matches: 1, beforeSha256: sha256(beforeBoundary), afterSha256: sha256(text) })
  } else if (name === 'elicitation') {
    edit('On submit: `preventDefault()`, validate and show messages next to the fields, then call `sendPrompt` once with a single readable line, for example `Trip preferences — Dates: 12–19 May · Budget: mid-range · Pace: relaxed`.',
      'Validate inputs and show messages next to the fields. If a local form submit is useful for validation, call `preventDefault()`. Publish valid task conditions with `await visual.setContext({ summary, data, suggestedPrompt })`; use a concise summary of the actual values and a task-specific suggestedPrompt. The host supplies the action that adds these conditions to the conversation. Do not duplicate it with a generic continue-discussion button.', 'collect-context')
    edit('After sending, disable the form and show a short confirmation in place of the submit button.',
      'Keep fields editable after preparing context. Update or clear context when fields change, become invalid, or reset. A successful setContext acknowledgement means conditions are available, not that a message was sent or a model has answered.', 'honest-acknowledgement')
  }
  return `${text.trimEnd()}\n`
}

export async function verifyUpstream(root = packageRoot) {
  const pkg = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'))
  assert.equal(pkg.version, UPSTREAM_VERSION, 'Unsupported generative-frame version; review guidance before upgrading')
  assert.equal(pkg.license, 'MIT', 'Unexpected upstream license')
  for (const [file, digest] of Object.entries(UPSTREAM_HASHES)) {
    assert.equal(sha256(await readFile(path.join(root, file))), digest, `Unknown upstream guidance input: ${file}`)
  }
  return pkg
}

export async function buildGeneratedFiles() {
  await verifyUpstream()
  const { buildWidgetGuidance, WIDGET_MODULES } = await import('generative-frame/prompts')
  assert.deepEqual(WIDGET_MODULES, MODULES, 'Review new upstream modules before generating')
  const lock = JSON.parse(await readFile(path.join(webuiRoot, 'package-lock.json'), 'utf8'))
  const locked = lock.packages['node_modules/generative-frame']
  assert.equal(locked.version, UPSTREAM_VERSION)
  assert.ok(locked.integrity, 'Missing npm package integrity')
  const base = buildWidgetGuidance({ ...GUIDANCE_OPTIONS, modules: [] })
  const mobile = buildWidgetGuidance({ ...GUIDANCE_OPTIONS, platform: 'mobile', width: 360, modules: [] })
  const records = []
  const banner = 'Generated from generative-frame 0.0.5 (MIT), adapted for OpenSquilla.\nCopyright (c) 2026 AgentbaseAI Inc. See LICENSE.generative-frame.txt and upstream-guidance.json.\nDo not edit this generated file; run npm run visualize:guidance:write in opensquilla-webui.\n\n'
  const files = new Map([['references/guidance-base.txt', banner + adaptBase(base, mobile, records)]])
  for (const name of MODULES) {
    const full = buildWidgetGuidance({ ...GUIDANCE_OPTIONS, modules: [name] })
    assert.ok(full.startsWith(`${base}\n\n`), `Upstream module framing changed: ${name}`)
    files.set(`references/guidance-${name}.txt`, banner + adaptModule(name, full.slice(base.length + 2), records))
  }
  files.set('LICENSE.generative-frame.txt', normalizeText(await readFile(path.join(packageRoot, 'LICENSE'), 'utf8')))
  const outputs = [...files].map(([file, text]) => ({ file, sha256TextLF: sha256(text) }))
  files.set('upstream-guidance.json', `${JSON.stringify({
    schemaVersion: 1,
    generatedBy: 'opensquilla-webui/scripts/sync-visualize-guidance.mjs',
    upstream: {
      package: 'generative-frame', version: UPSTREAM_VERSION, license: 'MIT',
      repository: 'https://github.com/assistant-ui/assistant-ui/tree/main/packages/generative-frame',
      npmIntegrity: locked.integrity, inputs: UPSTREAM_HASHES,
    },
    options: GUIDANCE_OPTIONS,
    mobileOptions: { platform: 'mobile', width: 360 },
    modules: MODULES,
    adaptations: records,
    outputs,
  }, null, 2)}\n`)
  return files
}

export async function syncGuidance({ write = false, root = skillRoot, files } = {}) {
  const expected = files ?? await buildGeneratedFiles()
  const drift = []
  for (const [file, content] of expected) {
    const target = path.join(root, file)
    let current
    try { current = normalizeText(await readFile(target, 'utf8')) } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }
    if (current === content) continue
    if (!write) { drift.push(file); continue }
    await mkdir(path.dirname(target), { recursive: true })
    await writeFile(target, content, 'utf8')
  }
  assert.equal(drift.length, 0, `Generated Visualize guidance is missing or stale: ${drift.join(', ')}. Run npm run visualize:guidance:write and review its diff.`)
  return expected.size
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const args = process.argv.slice(2)
  assert.ok(args.length <= 1 && (!args.length || ['--check', '--write'].includes(args[0])), 'Use --check or --write')
  const write = args[0] === '--write'
  const count = await syncGuidance({ write })
  console.log(`Visualize guidance ${write ? 'generated' : 'verified'}: ${count} files, generative-frame ${UPSTREAM_VERSION}`)
}
