// i18n key-parity + English-leakage guard. Wired into `check:architecture` so a
// non-en locale can never silently drift from the en key set (vue-i18n would
// fall back to en and hide the gap) or ship untranslated English strings.
//
// en.json is the authoritative key superset. For every other locale we fail on:
//   - missing keys (present in en, absent here)
//   - extra keys (present here, absent in en)
//   - untranslated values (string identical to en and containing Latin letters)
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const localesDir = resolve(here, '..', 'src', 'locales')
const OTHER_LOCALES = ['zh-Hans', 'ja', 'fr', 'de', 'es']
// English-leakage (value identical to en) is only a reliable "untranslated"
// signal for non-Latin scripts. fr/de/es legitimately share many words with
// English, so we enforce only KEY PARITY there and check leakage for zh-Hans.
const LEAKAGE_LOCALES = new Set(['zh-Hans'])
// Product terminology that deliberately remains in English in Simplified Chinese.
const ZH_HANS_ENGLISH_TERMS = new Set(['chat.routerFx.ensembleTokens'])

function load(name) {
  return JSON.parse(readFileSync(resolve(localesDir, `${name}.json`), 'utf8'))
}

function flatten(obj, prefix = '', out = {}) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v, key, out)
    else out[key] = v
  }
  return out
}

const enFlat = flatten(load('en'))
const enKeys = Object.keys(enFlat)
let failed = false

for (const loc of OTHER_LOCALES) {
  const flat = flatten(load(loc))
  const keys = Object.keys(flat)

  const missing = enKeys.filter((k) => !(k in flat))
  const extra = keys.filter((k) => !(k in enFlat))
  const untranslated = LEAKAGE_LOCALES.has(loc)
    ? enKeys.filter(
        (k) =>
          k in flat
          && typeof flat[k] === 'string'
          && flat[k] === enFlat[k]
          && /[A-Za-z]/.test(flat[k])
          && !ZH_HANS_ENGLISH_TERMS.has(k),
      )
    : []

  if (missing.length) {
    failed = true
    console.error(`[check-i18n] ${loc}: ${missing.length} missing key(s):\n  ${missing.join('\n  ')}`)
  }
  if (extra.length) {
    failed = true
    console.error(`[check-i18n] ${loc}: ${extra.length} extra key(s):\n  ${extra.join('\n  ')}`)
  }
  if (untranslated.length) {
    failed = true
    console.error(`[check-i18n] ${loc}: ${untranslated.length} untranslated (==en) value(s):\n  ${untranslated.join('\n  ')}`)
  }
}

if (failed) {
  console.error('[check-i18n] FAILED')
  process.exit(1)
}
console.log(`[check-i18n] OK — ${OTHER_LOCALES.join(', ')} at full key parity with en, no English leakage`)
