// Backend wrapper for the pet's shared i18n (pet/shared/i18n.js).
//
// The renderer loads i18n.js as a <script> (browser sets window.OctoI18n). The
// main process needs the same translator for backend-authored labels, but the
// desktop/electron package is "type":"module", so a raw require() of the UMD
// file would run it as ESM and get no exports. Instead we read the file and
// evaluate it with a CommonJS `module`/`exports` in scope — the UMD factory
// then populates module.exports exactly as it would in Node CJS.

import { app } from 'electron'
import fs from 'node:fs'
import path from 'node:path'

interface I18nModule {
  t: (key: string, vars?: Record<string, unknown>) => string
  setLang: (lang: string) => void
  getLang: () => string
  LANGS: string[]
}

let cached: I18nModule | null = null

export function loadPetI18n(): I18nModule {
  if (cached) return cached
  const base = app.getAppPath()
  const fp = path.join(base, 'pet', 'shared', 'i18n.js')
  const code = fs.readFileSync(fp, 'utf8')
  const mod = { exports: {} as Record<string, unknown> }
  // Evaluate with module/exports in scope (UMD checks typeof module !== 'undefined').
  const fn = new Function('module', 'exports', 'window', code + '\n;return module.exports;')
  const api = fn(mod, mod.exports, undefined) as I18nModule
  cached = api
  return api
}

export function petT(key: string, vars?: Record<string, unknown>): string {
  return loadPetI18n().t(key, vars)
}
