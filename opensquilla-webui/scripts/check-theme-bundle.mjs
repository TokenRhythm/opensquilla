import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

// Post-build invariant: an expressive skin must be fully lazy — a console user
// who never opens a skinned route downloads ZERO of its bytes. We prove it by
// asserting each skin's scope rules never land in an eagerly-loaded chunk (the
// entry JS/CSS referenced by index.html), while still being emitted as their own
// on-demand chunks. Run after `vite build`.
const root = fileURLToPath(new URL('..', import.meta.url))
// Vite owns the source artifact; packaging stages it explicitly afterwards.
const dist = join(root, 'dist')
const themesDir = join(root, 'src', 'themes')
const indexHtml = join(dist, 'index.html')

if (!existsSync(indexHtml)) {
  console.error('check-theme-bundle: no built index.html — run `vite build` first.')
  process.exit(1)
}

// Expressive skin ids = theme folders that ship a skin.css.
const skinIds = existsSync(themesDir)
  ? readdirSync(themesDir, { withFileTypes: true })
      .filter((d) => d.isDirectory() && existsSync(join(themesDir, d.name, 'skin.css')))
      .map((d) => d.name)
  : []

if (skinIds.length === 0) {
  console.log('Theme bundle guard: no expressive skins to check.')
  process.exit(0)
}

const html = readFileSync(indexHtml, 'utf8')
// Assets the browser fetches on first paint (module scripts, modulepreloads,
// stylesheets in <head>).
const eager = new Set([...html.matchAll(/(?:src|href)="\.?\/?(assets\/[^"]+)"/g)].map((m) => m[1]))
const read = (relPath) => {
  const p = join(dist, relPath)
  return existsSync(p) ? readFileSync(p, 'utf8') : ''
}
const eagerText = [...eager].map(read).join('\n')

// Minifiers drop quotes in attribute selectors, so match both forms.
const hasScope = (text, id) =>
  text.includes(`[data-skin="${id}"]`) || text.includes(`[data-skin=${id}]`)

const failures = []
const report = {}
for (const id of skinIds) {
  // 1. the skin's scoped rules must NOT be in any eager chunk
  if (hasScope(eagerText, id)) {
    failures.push(`skin "${id}" leaked into an eagerly-loaded chunk (found in entry).`)
  }
  // 2. it MUST exist as its own lazy chunk somewhere in dist/assets
  const assets = readdirSync(join(dist, 'assets'))
  const lazyCss = assets.filter(
    (f) => f.endsWith('.css') && !eager.has(`assets/${f}`) && hasScope(read(`assets/${f}`), id),
  )
  if (lazyCss.length === 0) {
    failures.push(`skin "${id}" has no lazy stylesheet chunk (expected an on-demand CSS asset scoped to it).`)
  }
  report[id] = { lazyCssChunks: lazyCss.length }
}

// Worlds: a value theme's global "world" layer (world.css) must also stay lazy.
// Its DESCENDANT rules ("[data-theme=<id>] <sel>") live in world.css; the eager
// tokens.css only emits the palette root block ("[data-theme=<id>]{...}"), so a
// trailing space after the attribute distinguishes world rules from the palette.
const worldIds = existsSync(themesDir)
  ? readdirSync(themesDir, { withFileTypes: true })
      .filter((d) => d.isDirectory() && existsSync(join(themesDir, d.name, 'world.css')))
      .map((d) => d.name)
  : []
const hasWorld = (text, id) =>
  text.includes(`[data-theme="${id}"] `) || text.includes(`[data-theme=${id}] `)
for (const id of worldIds) {
  if (hasWorld(eagerText, id)) {
    failures.push(`world "${id}" leaked structural rules into an eager chunk (world.css must be lazy).`)
  }
  const assets = readdirSync(join(dist, 'assets'))
  const lazy = assets.filter(
    (f) => f.endsWith('.css') && !eager.has(`assets/${f}`) && hasWorld(read(`assets/${f}`), id),
  )
  if (lazy.length === 0) failures.push(`world "${id}" has no lazy stylesheet chunk.`)
}

// No font woff2 may be inlined into the eager entry (they are the skins' weight).
for (const rel of eager) {
  if (/\.js$/.test(rel) && read(rel).includes('data:font/woff2')) {
    failures.push(`eager chunk ${rel} inlines a woff2 font — skin fonts must stay separate.`)
  }
}

if (failures.length) {
  console.error('Theme bundle guard failed:\n' + failures.map((f) => '  ' + f).join('\n'))
  process.exit(1)
}
console.log(
  `Theme bundle guard passed — ${skinIds.length} skin(s) + ${worldIds.length} world(s) fully lazy (0 bytes in the entry). ` +
    `skins: ${skinIds.join(', ') || 'none'}; worlds: ${worldIds.join(', ') || 'none'}.`,
)
