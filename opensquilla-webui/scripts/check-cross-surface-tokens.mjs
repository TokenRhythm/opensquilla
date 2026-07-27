import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { hexToRgb, stripAllComments } from './lib/css-utils.mjs'

// Cross-surface brand-accent guard.
//
// The product ships several independently-authored surfaces (the Vue console,
// the Electron splash, the Electron onboarding window). Issue #403 found the
// brand accent had fragmented into six different oranges across them. The Vue
// console is already token-guarded by check-webui-colors.mjs; this guard keeps
// the DESKTOP launch sequence (splash + onboarding) locked to the one canonical
// "strike" accent so it can't drift off again.
//
// Scope: only a SATURATED ORANGE (a brand-accent candidate) is checked — a hue
// in the orange band with real saturation. Danger-reds, greens, warm neutrals
// and paper whites are ignored, so the guard is about brand identity, not every
// colour. Backgrounds are intentionally out of scope (too many valid neutral
// shades to allowlist without false positives).
//
// The gateway serves the generated Vue bundle from static/dist; its source
// tokens are covered by check-webui-colors.mjs and therefore are not rescanned.
const repoRoot = fileURLToPath(new URL('../../', import.meta.url))

// The canonical "strike" family — the Instrument accent and its documented
// hover / deep / secondary / light-theme siblings. Stored as normalized
// "r,g,b" so hex and rgb()/rgba() forms compare equal.
const CANONICAL = new Set([
  '242,106,27', // #F26A1B  accent (dark)
  '255,122,46', // #FF7A2E  accent-hover (dark)
  '217,90,17', //  #D95A11  accent-deep (dark) / onboarding hover
  '255,138,76', // #FF8A4C  accent-secondary
  '186,77,15', //  #BA4D0F  accent (light)
  '165,68,12', //  #A5440C  accent-hover (light)
  '142,58,10', //  #8E3A0A  accent-deep (light)
  '182,80,28', //  #B6501C  accent-secondary (light)
])

// The onboarding provider promotion intentionally uses a quieter orange-brown
// family so it does not compete with the primary setup action. Keep this list
// exact: it preserves that established treatment while still rejecting new,
// unreviewed orange accents across the desktop launch sequence.
const ONBOARDING_PROMOTION = new Set([
  '169,87,40', // #A95728
  '145,72,31', // #91481F
  '123,75,44', // #7B4B2C
  '130,80,55', // #825037
  '111,63,35', // #6F3F23
  '142,75,37', // #8E4B25
  '84,47,26', //  #542F1A
])

const ALLOWED_DESKTOP_ACCENTS = new Set([...CANONICAL, ...ONBOARDING_PROMOTION])

// Files that make up the desktop launch sequence.
const targets = [
  'desktop/electron/src/boot.html',
  'desktop/electron/src/main.ts',
]

// Is this rgb a saturated orange — i.e. a brand-accent candidate?
function isBrandOrange([r, g, b]) {
  const rn = r / 255, gn = g / 255, bn = b / 255
  const max = Math.max(rn, gn, bn), min = Math.min(rn, gn, bn)
  const l = (max + min) / 2
  const d = max - min
  if (d === 0) return false
  const s = d / (1 - Math.abs(2 * l - 1))
  let hue
  if (max === rn) hue = ((gn - bn) / d) % 6
  else if (max === gn) hue = (bn - rn) / d + 2
  else hue = (rn - gn) / d + 4
  hue = ((hue * 60) + 360) % 360
  // Orange band, well saturated, mid lightness — excludes red danger (<16),
  // yellow (>46), and low-saturation warm taupes/papers.
  return hue >= 16 && hue <= 46 && s >= 0.4 && l >= 0.18 && l <= 0.72
}

const hexRe = /#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b/g
const rgbRe = /rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})/g

const failures = []
for (const rel of targets) {
  let text
  try {
    text = readFileSync(repoRoot + rel, 'utf8')
  } catch {
    console.warn(`[cross-surface] skipped (not found): ${rel}`)
    continue
  }
  // Comments are stripped so hexes named in them don't trip the guard.
  const lines = stripAllComments(text).split('\n')
  lines.forEach((line, i) => {
    const found = []
    for (const m of line.matchAll(hexRe)) found.push({ raw: m[0], rgb: hexToRgb(m[0]) })
    for (const m of line.matchAll(rgbRe)) found.push({ raw: m[0], rgb: [+m[1], +m[2], +m[3]] })
    for (const { raw, rgb } of found) {
      if (!isBrandOrange(rgb)) continue
      if (ALLOWED_DESKTOP_ACCENTS.has(rgb.join(','))) continue
      failures.push(
        `${rel}:${i + 1}: unapproved brand orange ${raw} (rgb ${rgb.join(',')}); use an approved desktop accent family.`,
      )
    }
  })
}

if (failures.length > 0) {
  console.error(
    `Cross-surface accent guard: ${failures.length} unapproved brand orange(s) in the desktop launch sequence:\n` +
      failures.join('\n'),
  )
  process.exit(1)
}

console.log('Cross-surface accent guard passed.')
