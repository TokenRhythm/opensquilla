import type { ChatRouterTierConfig } from '@/types/chat'

// The last-known router tier shape, persisted so the WebChat router-strip
// reserve twin can size and hold its slot on the first turn — before the async
// `config.get` that normally populates it resolves. `config.get` stays
// authoritative and overwrites these values once it lands; the cache only
// bridges the pre-resolve window. Tier names + model ids only — never secrets.
export interface RouterShape {
  enabled: boolean
  slots: string[]
  models: Record<string, string>
  configs: Record<string, ChatRouterTierConfig>
}

const VERSION = 1

/** Serialize a router shape into a versioned envelope for localStorage. */
export function encodeRouterShape(shape: RouterShape): string {
  return JSON.stringify({
    v: VERSION,
    enabled: shape.enabled === true,
    slots: shape.slots,
    models: shape.models,
    configs: shape.configs,
  })
}

/**
 * Parse a persisted router shape. Returns null for anything that cannot seed a
 * well-formed reserve: bad JSON, wrong version, malformed fields, or an empty
 * `models` map (a tier-less shape would render a <=1-cell reserve, which the
 * reserve gate rejects anyway — so refuse it here and keep that gate honest).
 */
export function decodeRouterShape(raw: string | null | undefined): RouterShape | null {
  if (!raw) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (!isRecord(parsed)) return null
  if (parsed.v !== VERSION) return null

  const models = asStringRecord(parsed.models)
  if (!models || Object.keys(models).length === 0) return null

  const slots = asStringArray(parsed.slots)
  if (!slots) return null

  const configs = asTierConfigRecord(parsed.configs)
  if (!configs) return null

  return { enabled: parsed.enabled === true, slots, models, configs }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function asStringArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null
  return value.every(item => typeof item === 'string') ? (value as string[]) : null
}

function asStringRecord(value: unknown): Record<string, string> | null {
  if (!isRecord(value)) return null
  const out: Record<string, string> = {}
  for (const [key, val] of Object.entries(value)) {
    if (typeof val !== 'string') return null
    out[key] = val
  }
  return out
}

function asTierConfigRecord(value: unknown): Record<string, ChatRouterTierConfig> | null {
  if (!isRecord(value)) return null
  const out: Record<string, ChatRouterTierConfig> = {}
  for (const [key, val] of Object.entries(value)) {
    if (!isRecord(val)) return null
    out[key] = {
      model: typeof val.model === 'string' ? val.model : '',
      supportsImage: val.supportsImage === true,
      imageOnly: val.imageOnly === true,
      ...(typeof val.ensembleEnabled === 'boolean'
        ? { ensembleEnabled: val.ensembleEnabled }
        : {}),
    }
  }
  return out
}
