// PetConfig — persist pet preferences under the Electron userData directory.
//
// Mirrors OpenSquilla pet's config.js (atomic write via temp+rename). The OpenSquilla
// desktop shell keeps its own settings; the pet stores its own small file so it
// never collides with the shell's preferences schema.

import { app } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import { PetPrefs } from './types.js'

const DEFAULTS: PetPrefs = {
  petPosition: null,
  skin: 'mascot',
  muted: false,
  mode: 'pet',
  lang: 'zh',
  pinnedSessions: [],
  archivedSessions: [],
  petEnabled: true,
  anticsEnabled: false,
  lootCapturedSessions: [],
  travelLedger: [],
}

let cache: PetPrefs | null = null

function configPath(): string {
  return path.join(app.getPath('userData'), 'pet-config.json')
}

function loadRaw(): PetPrefs {
  try {
    const raw = JSON.parse(fs.readFileSync(configPath(), 'utf8'))
    return { ...DEFAULTS, ...(raw && typeof raw === 'object' ? raw : {}) }
  } catch {
    return { ...DEFAULTS }
  }
}

function writeAtomic(data: PetPrefs): void {
  try {
    const fp = configPath()
    fs.mkdirSync(path.dirname(fp), { recursive: true })
    const tmp = path.join(path.dirname(fp), `.pet-config.${process.pid}.${Date.now()}.tmp`)
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8')
    fs.renameSync(tmp, fp)
  } catch {}
}

export function getPetConfig(): PetPrefs {
  if (!cache) cache = loadRaw()
  return cache
}

export function savePetConfig(patch: Partial<PetPrefs>): PetPrefs {
  const next = { ...getPetConfig(), ...patch }
  cache = next
  writeAtomic(next)
  return next
}
