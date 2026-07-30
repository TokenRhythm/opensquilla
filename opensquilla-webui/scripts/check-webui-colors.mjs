import { readdirSync, readFileSync, existsSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { walkFiles } from './lib/fs-walk.mjs'
import { hexToRgb, parseTokenDefinitions } from './lib/css-utils.mjs'

// Every control-UI surface must use the semantic color tokens from
// src/assets/base.css so both themes render correctly. Raw hex / rgb() / hsl()
// color literals anywhere under src/ are violations — use a token, or
// color-mix(in srgb, var(--token) …).
//
// base.css is the token source, so it is scanned under a narrower rule: raw
// color literals are allowed only inside custom-property definitions
// (`--token: …`), comments, and neutral black/white rgba (the primitive the
// shadow/border tokens are themselves built from). An off-palette literal in a
// real base.css rule is still a violation — that is how theme-parity drift
// (e.g. a hardcoded near-black border that vanishes on dark) sneaks in.
const root = fileURLToPath(new URL('..', import.meta.url))
const srcDir = join(root, 'src')

// Token-definition sources. Raw color literals are allowed only inside
// custom-property definitions in these files: the base.css component file plus
// the layered token layer (foundation L0 + each theme's L1 tokens.css). Anywhere
// else a literal is a theme-parity violation.
function isTokenSource(f) {
  const r = f.replace(/\\/g, '/')
  return (
    r.endsWith('/src/assets/base.css') ||
    r.endsWith('/src/assets/foundation.css') ||
    /\/src\/themes\/[^/]+\/tokens\.css$/.test(r) ||
    // a skin's scoped stylesheet defines its own token pack; structural rules in
    // it still may not use raw literals (only custom-prop lines are exempt).
    /\/src\/themes\/[^/]+\/skin\.css$/.test(r)
  )
}

// Negative lookbehind keeps HTML entities like &#8593; out of the hex match.
const colorLiteral = /(?<!&)#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/
// SVG fragment references such as url(#cg2) are ids, not colors.
const urlRef = /url\(#[\w-]+\)/g
// Neutral black/white — the primitive shadows/borders are built from; allowed
// in base.css rules. Matches rgba(0,0,0,…), rgba(255,255,255,…), #000(0), #fff(f).
const neutralColor =
  /\brgba?\(\s*(?:0\s*,\s*0\s*,\s*0|255\s*,\s*255\s*,\s*255)\b[^)]*\)|#(?:000000|ffffff|000|fff)\b/gi
const customProp = /^\s*--[\w-]+\s*:/

const files = walkFiles(srcDir, /\.(vue|css)$/)

// ---------------------------------------------------------------------------
// Pass 1 — raw color literals
// ---------------------------------------------------------------------------
const failures = []
for (const file of files) {
  const rel = relative(root, file).replace(/\\/g, '/')
  const isBase = isTokenSource(file)
  const lines = readFileSync(file, 'utf8').split('\n')
  let inBlockComment = false
  lines.forEach((line, index) => {
    let code = line
    // Strip block comments (base.css is scanned, so its banner hex must not
    // trip the guard); track multi-line state.
    if (inBlockComment) {
      const end = code.indexOf('*/')
      if (end === -1) return
      code = code.slice(end + 2)
      inBlockComment = false
    }
    code = code.replace(/\/\*.*?\*\//g, '')
    const open = code.indexOf('/*')
    if (open !== -1) {
      inBlockComment = true
      code = code.slice(0, open)
    }
    code = code.replace(urlRef, '')
    if (isBase) {
      if (customProp.test(code)) return // a token definition — allowed
      code = code.replace(neutralColor, '') // neutral shadow/border primitive
    }
    if (colorLiteral.test(code)) {
      failures.push(
        `${rel}:${index + 1}: raw color literal; use a base.css token or color-mix(in srgb, var(--token) …). ${line.trim()}`,
      )
    }
  })
}

// ---------------------------------------------------------------------------
// Pass 2 — references to undefined tokens
// ---------------------------------------------------------------------------
// A bare `var(--token)` with no fallback that is never defined anywhere renders
// to nothing (e.g. a colorless status message). Fallback forms `var(--x, …)`
// are intentional and exempt — that is how runtime/host-set vars are consumed.
// Scoped to the semantic color/surface namespace: layout vars like
// `--router-left`/`--i`/`--composer-h` are legitimately set at runtime via
// :style bindings and are out of scope for a color guard.
const semanticToken = /^--(bg|surface|text|color|accent|ok|warn|danger|info|success|border|hairline|card|shadow|scrim|sidebar|syntax)/
const defined = new Set()
const bareUsages = [] // { token, rel, line }
const defRe = /(?:^|[\s;{])(--[\w-]+)\s*:/g
const bareVarRe = /var\(\s*(--[\w-]+)\s*\)/g
for (const file of files) {
  const rel = relative(root, file).replace(/\\/g, '/')
  const text = readFileSync(file, 'utf8')
  for (const m of text.matchAll(defRe)) defined.add(m[1])
  text.split('\n').forEach((line, index) => {
    for (const m of line.matchAll(bareVarRe)) {
      bareUsages.push({ token: m[1], rel, line: index + 1, text: line.trim() })
    }
  })
}
const undefinedTokens = bareUsages.filter(
  (u) => semanticToken.test(u.token) && !defined.has(u.token),
)

// ---------------------------------------------------------------------------
// Pass 3 — per-theme contrast floors (WCAG 2.x). Each value theme's reading
// pairs must clear a floor so a new theme cannot ship illegible text. Values are
// resolved through var()/color-mix(in srgb, …) within each theme's tokens + the
// foundation layer.
//
// The floors distinguish INK (colour used as `color:` text) from FILL (dots,
// bars, badge bodies — never text):
//   • --text / --text-muted are body reading text → AA normal 4.5:1.
//   • The colour-carrying INK — --accent, --accent-secondary and the status
//     spectrum (ok/warn/danger/info/queued) — is used as `color:` text in 100+
//     sites (links, nav, inline status, error copy). It MUST clear AA 4.5:1 on
//     the two grounds it actually sits on: the base (--bg) and the card/surface
//     (--bg-surface). On --bg-elevated (modals/popovers — a minority ground for
//     coloured text) it is held to the 3:1 UI-component floor. This is the fix
//     for the old bug where these were floored at 3.0 against --bg only (or, for
//     accent, not checked at all), which green-lit sub-AA coloured text.
//   • The matching --*-fill tokens are marks, never text → 3:1 on --bg.
const foundationFile = join(srcDir, 'assets', 'foundation.css')
const themesRoot = join(srcDir, 'themes')
const valueThemeFiles = existsSync(themesRoot)
  ? readdirSync(themesRoot, { withFileTypes: true })
      .filter((d) => d.isDirectory() && existsSync(join(themesRoot, d.name, 'tokens.css')))
      .map((d) => [d.name, join(themesRoot, d.name, 'tokens.css')])
  : []

function tokenMapFor(themeFile) {
  const map = new Map()
  for (const p of [foundationFile, themeFile]) {
    if (!existsSync(p)) continue
    parseTokenDefinitions(readFileSync(p, 'utf8'), map)
  }
  return map
}
function splitTop(s) {
  const out = []
  let depth = 0
  let buf = ''
  for (const ch of s) {
    if (ch === '(') { depth++; buf += ch }
    else if (ch === ')') { depth--; buf += ch }
    else if (ch === ',' && depth === 0) { out.push(buf); buf = '' }
    else buf += ch
  }
  if (buf.trim()) out.push(buf)
  return out
}
function toRgb(value, map, seen = new Set()) {
  if (value == null) return null
  const v = value.trim()
  const varM = v.match(/^var\(\s*(--[\w-]+)\s*(?:,\s*([\s\S]+))?\)$/)
  if (varM) {
    const name = varM[1].slice(2)
    if (seen.has(name)) return null
    const next = new Set(seen).add(name)
    if (map.has(name)) return toRgb(map.get(name), map, next)
    return varM[2] ? toRgb(varM[2], map, next) : null
  }
  let m = v.match(/^#([0-9a-fA-F]{3,8})$/)
  if (m) return hexToRgb(v)
  m = v.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/)
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3])]
  m = v.match(/^color-mix\(in srgb,\s*([\s\S]+)\)$/)
  if (m) {
    const parts = splitTop(m[1]).map((p) => {
      const pm = p.trim().match(/^([\s\S]*?)\s+([\d.]+)%$/)
      return pm
        ? { rgb: toRgb(pm[1], map, new Set(seen)), pct: Number(pm[2]) }
        : { rgb: toRgb(p, map, new Set(seen)), pct: null }
    })
    if (parts.length < 2 || !parts[0].rgb || !parts[1].rgb) return null
    let [a, b] = parts
    if (a.pct == null && b.pct == null) { a.pct = 50; b.pct = 50 }
    else if (a.pct == null) a.pct = 100 - b.pct
    else if (b.pct == null) b.pct = 100 - a.pct
    const t = a.pct / (a.pct + b.pct)
    return [0, 1, 2].map((i) => Math.round(a.rgb[i] * t + b.rgb[i] * (1 - t)))
  }
  return null
}
const chanLin = (c) => { const s = c / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4 }
const relLum = (rgb) => 0.2126 * chanLin(rgb[0]) + 0.7152 * chanLin(rgb[1]) + 0.0722 * chanLin(rgb[2])
function contrast(a, b) {
  const la = relLum(a)
  const lb = relLum(b)
  const [hi, lo] = la > lb ? [la, lb] : [lb, la]
  return (hi + 0.05) / (lo + 0.05)
}
// Colour-carrying ink used as text (see the note above). AA on --bg + --bg-surface;
// 3:1 UI floor on --bg-elevated. --accent-hover is included because it paints
// link/CTA hover text (a:hover, OverviewView). --accent-deep is intentionally
// absent — it is only ever a fill/border/gradient stop, never `color:` text.
const INK_TEXT = ['accent', 'accent-hover', 'accent-secondary', 'ok', 'warn', 'danger', 'info', 'queued']
// Fill tokens: marks/dots/badge bodies, never text → 3:1 on --bg.
const FILL_MARKS = ['ok-fill', 'warn-fill', 'danger-fill', 'info-fill', 'queued-fill']
const CONTRAST_CHECKS = [
  { fg: 'text', bg: 'bg', min: 4.5 },
  { fg: 'text', bg: 'bg-surface', min: 4.5 },
  { fg: 'text-muted', bg: 'bg', min: 4.5 },
  { fg: 'text-muted', bg: 'bg-surface', min: 4.5 },
  { fg: 'text-dim', bg: 'bg', min: 3.0 },
  { fg: 'accent-foreground', bg: 'accent', min: 3.0 },
  ...INK_TEXT.flatMap((fg) => [
    { fg, bg: 'bg', min: 4.5 },
    { fg, bg: 'bg-surface', min: 4.5 },
    { fg, bg: 'bg-elevated', min: 3.0 },
  ]),
  ...FILL_MARKS.map((fg) => ({ fg, bg: 'bg', min: 3.0 })),
]
const contrastFailures = []
const unresolvable = []
for (const [theme, file] of valueThemeFiles) {
  const map = tokenMapFor(file)
  const reported = new Set()
  for (const c of CONTRAST_CHECKS) {
    const fg = toRgb(`var(--${c.fg})`, map)
    const bg = toRgb(`var(--${c.bg})`, map)
    if (!fg || !bg) {
      // A required role toRgb() can't parse (hsl()/oklch()/named colour/…)
      // must FAIL, not silently skip — otherwise a theme authored in an
      // unsupported syntax ships with zero contrast coverage while the guard
      // reports it as checked.
      for (const [role, rgb] of [[c.fg, fg], [c.bg, bg]]) {
        const key = `${theme}:${role}`
        if (!rgb && !reported.has(key)) {
          reported.add(key)
          unresolvable.push(
            `  [${theme}] --${role}: ${map.get(role) ?? '(undefined)'} — not parseable as a colour (supported: hex, rgb()/rgba(), var() chains, color-mix(in srgb, …))`,
          )
        }
      }
      continue
    }
    const r = contrast(fg, bg)
    if (r < c.min) {
      contrastFailures.push(`  [${theme}] --${c.fg} on --${c.bg}: ${r.toFixed(2)}:1 (floor ${c.min}:1)`)
    }
  }
}

// ---------------------------------------------------------------------------
// Pass 4 — ink/fill discipline (harvested from the Out-of-Register work). A
// `*-fill` token is chroma-tuned for small marks (dots, bars, badge bodies); it
// is NOT a text colour. Using one as `color:` is a legibility bug — use the
// matching ink token. (background-color / border-color etc. are fine.)
const fillAsColor = /(?<![-\w])color\s*:\s*var\(\s*--[\w-]*-fill\b/
const fillMisuse = []
for (const file of files) {
  const relf = relative(root, file).replace(/\\/g, '/')
  readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
    if (fillAsColor.test(line)) {
      fillMisuse.push(`${relf}:${i + 1}: a *-fill token is for fills, not text — use its ink token. ${line.trim()}`)
    }
  })
}

// ---------------------------------------------------------------------------
let failed = false
if (failures.length > 0) {
  failed = true
  console.error('Raw color literals found — use base.css design tokens:\n' + failures.join('\n'))
}
if (undefinedTokens.length > 0) {
  failed = true
  console.error(
    '\nReferences to undefined CSS tokens (use a defined token or add a fallback `var(--x, …)`):\n' +
      undefinedTokens.map((u) => `${u.rel}:${u.line}: ${u.token} is never defined. ${u.text}`).join('\n'),
  )
}
if (contrastFailures.length > 0) {
  failed = true
  console.error('\nTheme contrast below floor (WCAG):\n' + contrastFailures.join('\n'))
}
if (unresolvable.length > 0) {
  failed = true
  console.error(
    '\nTheme contrast UNVERIFIABLE — required colour roles the checker cannot parse (author them in a supported syntax so the WCAG floors actually apply):\n' +
      unresolvable.join('\n'),
  )
}
if (fillMisuse.length > 0) {
  failed = true
  console.error('\nInk/fill discipline — a fill token used as text colour:\n' + fillMisuse.join('\n'))
}
if (failed) process.exit(1)

console.log(
  `WebUI color guard passed (${valueThemeFiles.length} theme(s) contrast-checked).`,
)
