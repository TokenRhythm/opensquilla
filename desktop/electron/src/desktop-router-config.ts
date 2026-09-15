import { normalizeRouterTiers, type RouterTier } from './router-tier-normalization.js'

export type RouterPresetBinding = 'follow_primary' | 'custom'
export type DesktopRouterWriteIntent = 'preserve' | 'toggle' | 'replace'

export interface DesktopRouterConfig {
  routerMode: string
  routerDefaultTier: 'c0' | 'c1' | 'c2' | 'c3'
  routerTiers: Record<string, RouterTier>
  routerPresetBinding?: RouterPresetBinding
}

export function normalizeRouterPresetBinding(value: unknown): RouterPresetBinding | undefined {
  return value === 'follow_primary' || value === 'custom' ? value : undefined
}

function sortedValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortedValue)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => [key, sortedValue(item)]))
  }
  return value
}

function semanticTiers(tiers: Record<string, RouterTier>): string {
  return JSON.stringify(sortedValue(Object.fromEntries(Object.entries(tiers).map(([key, tier]) => {
    const value = { ...tier }
    delete value.description
    // Missing flags and their runtime defaults mean the same thing.
    if (!value.imageOnly) delete value.imageOnly
    if (!value.thinkingLevel) delete value.thinkingLevel
    return [key, value]
  }))))
}

/** The renderer supplies reset intent, never preset ownership. Defaults here
 * must come from the main process catalog, not from the IPC payload. */
export function resolveDesktopRouterUpdate(options: {
  payload: { routerResetToRecommended?: unknown; routerTiers?: unknown; routerDefaultTier?: unknown }
  existing: DesktopRouterConfig | null
  routerMode: string
  routerDefaultTier: DesktopRouterConfig['routerDefaultTier']
  defaultTiers: Record<string, RouterTier>
  freshConfig: boolean
  providerChangedWithoutConfig?: boolean
}): DesktopRouterConfig & { writeIntent: DesktopRouterWriteIntent } {
  const { payload, existing, routerMode, defaultTiers } = options
  if (options.freshConfig || payload.routerResetToRecommended === true) {
    return {
      routerMode,
      routerDefaultTier: 'c1',
      routerTiers: normalizeRouterTiers(undefined, defaultTiers),
      routerPresetBinding: 'follow_primary',
      writeIntent: 'replace',
    }
  }
  // Disable/re-enable and provider/key edits must retain the saved ladder,
  // including its missing ownership field. Never infer ownership by equality.
  const routerTiers = normalizeRouterTiers(
    payload.routerTiers ?? existing?.routerTiers,
    payload.routerTiers !== undefined ? defaultTiers : existing?.routerTiers ?? defaultTiers,
  )
  const tiersChanged = payload.routerTiers !== undefined
    && semanticTiers(routerTiers) !== semanticTiers(existing?.routerTiers ?? defaultTiers)
  const defaultChanged = payload.routerDefaultTier !== undefined
    && options.routerDefaultTier !== (existing?.routerDefaultTier ?? 'c1')
  const edited = tiersChanged || defaultChanged
  const binding = edited ? 'custom' : normalizeRouterPresetBinding(existing?.routerPresetBinding)
  // During config recovery the saved credential is the only available owner.
  // Ordinary saves reconcile against config.toml in the primary-change module.
  if (options.providerChangedWithoutConfig && binding === 'follow_primary') {
    return {
      routerMode,
      routerDefaultTier: options.routerDefaultTier,
      routerTiers: normalizeRouterTiers(undefined, defaultTiers),
      routerPresetBinding: binding,
      writeIntent: 'replace',
    }
  }
  return {
    routerMode,
    routerDefaultTier: options.routerDefaultTier,
    routerTiers,
    ...(binding ? { routerPresetBinding: binding } : {}),
    writeIntent: edited ? 'replace'
      : existing && (routerMode === 'disabled') !== (existing.routerMode === 'disabled')
        ? 'toggle' : 'preserve',
  }
}

function tomlValue(value: unknown): string {
  if (typeof value === 'string') return JSON.stringify(value)
  if (typeof value === 'boolean') return String(value)
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (Array.isArray(value)) return `[${value.map(tomlValue).join(', ')}]`
  if (value && typeof value === 'object') {
    return `{ ${Object.entries(value).map(([key, item]) => `${JSON.stringify(key)} = ${tomlValue(item)}`).join(', ')} }`
  }
  throw new Error('Router tier contains a value that cannot be represented in TOML.')
}

/** Production serializer: all ladders are inline, including TokenRhythm.
 * tier_profile is intentionally absent; this avoids gateway profile drift. */
export function routerConfigTomlLines(credential: DesktopRouterConfig): string[] {
  const binding = normalizeRouterPresetBinding(credential.routerPresetBinding)
  const lines = [
    '[squilla_router]',
    `enabled = ${credential.routerMode !== 'disabled'}`,
    'rollout_phase = "full"',
    `default_tier = ${tomlValue(credential.routerDefaultTier)}`,
    ...(binding ? [`preset_binding = ${tomlValue(binding)}`] : []),
  ]
  for (const [name, tier] of Object.entries(credential.routerTiers)) {
    if (!/^[A-Za-z0-9_-]+$/.test(name)) throw new Error('Router tier name is invalid.')
    if (!tier.provider || !tier.model) continue
    const { provider, model, description, imageOnly, thinkingLevel, ensembleEnabled,
      ensembleSelectionMode, supportsImage: _supportsImage, ...extra } = tier
    lines.push('', `[squilla_router.tiers.${name}]`,
      `provider = ${tomlValue(provider)}`, `model = ${tomlValue(model)}`)
    if (description) lines.push(`description = ${tomlValue(description)}`)
    if (imageOnly !== undefined) lines.push(`image_only = ${tomlValue(imageOnly)}`)
    if (thinkingLevel) lines.push(`thinking_level = ${tomlValue(thinkingLevel)}`)
    if (ensembleEnabled !== undefined) lines.push(`ensemble_enabled = ${tomlValue(ensembleEnabled)}`)
    if (ensembleSelectionMode) lines.push(`ensemble_selection_mode = ${tomlValue(ensembleSelectionMode)}`)
    for (const [key, value] of Object.entries(extra)) {
      if (value !== undefined) lines.push(`${JSON.stringify(key)} = ${tomlValue(value)}`)
    }
  }
  return lines
}

// Header recognition handles quoted keys/comments. The scanner tracks TOML
// strings so header-looking text inside a multiline value remains untouched.
function desktopTomlStructure(raw: string): { section: string | null; startsInValue: boolean }[] {
  let multiline: string | null = null
  let depth = 0
  return raw.split(/\r?\n/).map((line) => {
    const startsInValue = multiline !== null || depth > 0
    const header = !startsInValue && line.match(/^\s*\[\[?\s*(.*?)\s*\]\]?\s*(?:#.*)?$/)
    let quote: string | null = multiline
    for (let i = 0; i < line.length; i += 1) {
      if (quote) {
        if (quote.startsWith('"') && line[i] === '\\') { i += 1; continue }
        if (line.startsWith(quote, i)) { i += quote.length - 1; quote = null }
      } else {
        if (line[i] === '#') break
        if (line.startsWith('"""', i) || line.startsWith("'''", i)) {
          quote = line.slice(i, i + 3); i += 2
        } else if (line[i] === '"' || line[i] === "'") quote = line[i]!
        else if (!header && (line[i] === '[' || line[i] === '{')) depth += 1
        else if (!header && (line[i] === ']' || line[i] === '}')) depth -= 1
      }
    }
    multiline = quote && quote.length === 3 ? quote : null
    if (!header) return { section: null, startsInValue }
    // Only simple path components are needed for Desktop-owned sections.
    // Preserve dots inside quoted components instead of mistaking them for paths.
    const parts = header[1]!.match(/"(?:\\.|[^"\\])*"|'[^']*'|[^.\s]+/g) ?? []
    return { section: parts.map((part) => part.startsWith('"') ? JSON.parse(part) as string
      : part.startsWith("'") ? part.slice(1, -1) : part).join('\u0000'), startsInValue }
  })
}

export function desktopTomlSectionNames(raw: string): (string | null)[] {
  return desktopTomlStructure(raw).map((line) => line.section)
}

const ROUTER_ROOT_KEY = /^\s*(?:squilla_router|"squilla_router"|'squilla_router')\s*(\.|=)/

function toggleInlineRouter(line: string, enabled: boolean): string {
  const start = line.indexOf('{', line.indexOf('='))
  if (start < 0) throw new Error('Router inline configuration must be a TOML table.')
  let quote = ''
  let depth = 1
  let memberStart = true
  for (let i = start + 1; i < line.length; i += 1) {
    if (quote) {
      if (quote === '"' && line[i] === '\\') { i += 1; continue }
      if (line[i] === quote) quote = ''
      continue
    }
    if (depth === 1 && memberStart) {
      if (/\s/.test(line[i]!)) continue
      const match = line.slice(i).match(/^(?:enabled|"enabled"|'enabled')\s*=\s*(true|false)/)
      if (match) {
        const offset = i + match[0].length - match[1]!.length
        return line.slice(0, offset) + String(enabled) + line.slice(offset + match[1]!.length)
      }
      memberStart = false
    }
    if (line[i] === '"' || line[i] === "'") quote = line[i]!
    else if (line[i] === '{' || line[i] === '[') depth += 1
    else if (line[i] === '}' || line[i] === ']') depth -= 1
    else if (depth === 1 && line[i] === ',') memberStart = true
  }
  const comma = line.slice(start + 1).trimStart().startsWith('}') ? '' : ','
  return line.slice(0, start + 1) + ` enabled = ${enabled}${comma} ` + line.slice(start + 1)
}

/** Root dotted assignments and inline tables must stay before any [section]. */
export function desktopRouterConfigPreambleLines(
  credential: DesktopRouterConfig,
  existingRaw: string | null,
  writeIntent: DesktopRouterWriteIntent = 'preserve',
): string[] {
  if (existingRaw === null || writeIntent === 'replace') return []
  const structure = desktopTomlStructure(existingRaw)
  const lines = existingRaw.split(/\r?\n/)
  const out: string[] = []
  let keeping = false
  let enabledWritten = false
  for (let i = 0; i < lines.length; i += 1) {
    if (structure[i]!.section !== null) break
    let line = lines[i]!
    if (!structure[i]!.startsInValue) {
      const key = line.match(ROUTER_ROOT_KEY)
      keeping = Boolean(key)
      if (key && writeIntent === 'toggle') {
        if (key[1] === '=') {
          line = toggleInlineRouter(line, credential.routerMode !== 'disabled')
          enabledWritten = true
        } else {
          const rest = line.slice(key[0].length)
          if (/^\s*(?:enabled|"enabled"|'enabled')\s*=/.test(rest)) {
            line = line.slice(0, key[0].length) + rest.replace(/=\s*(?:true|false)/, `= ${credential.routerMode !== 'disabled'}`)
            enabledWritten = true
          }
        }
      }
    }
    if (keeping) out.push(line)
  }
  if (out.length && writeIntent === 'toggle' && !enabledWritten
    && !structure.some((line) => line.section === 'squilla_router')) {
    out.push(`squilla_router.enabled = ${credential.routerMode !== 'disabled'}`)
  }
  return out
}

export function desktopRouterPreambleLineIndexes(raw: string): Set<number> {
  const structure = desktopTomlStructure(raw)
  const indexes = new Set<number>()
  let keeping = false
  const lines = raw.split(/\r?\n/)
  for (let i = 0; i < lines.length; i += 1) {
    if (structure[i]!.section !== null) break
    if (!structure[i]!.startsInValue) keeping = ROUTER_ROOT_KEY.test(lines[i]!)
    if (keeping) indexes.add(i)
  }
  return indexes
}

export function desktopRouterConfigTomlLines(
  credential: DesktopRouterConfig,
  existingRaw: string | null,
  writeIntent: DesktopRouterWriteIntent = 'preserve',
): string[] {
  if (existingRaw === null || writeIntent === 'replace') return routerConfigTomlLines(credential)
  const source = existingRaw.split(/\r?\n/)
  const structure = desktopTomlStructure(existingRaw)
  const out: string[] = []
  let section = ''
  let enabledWritten = false
  let rootFound = false
  for (let i = 0; i < source.length; i += 1) {
    if (structure[i]!.section !== null) {
      section = structure[i]!.section!
      if (section === 'squilla_router') {
        rootFound = true
        out.push(source[i]!)
        if (writeIntent === 'toggle') {
          out.push(`enabled = ${credential.routerMode !== 'disabled'}`)
          enabledWritten = true
        }
        continue
      }
    }
    if (section !== 'squilla_router' && !section.startsWith('squilla_router\u0000')) continue
    if (writeIntent === 'toggle' && section === 'squilla_router' && !structure[i]!.startsInValue
      && /^\s*(?:enabled|"enabled"|'enabled')\s*=/.test(source[i]!)) continue
    out.push(source[i]!)
  }
  if (writeIntent === 'toggle' && !enabledWritten && desktopRouterPreambleLineIndexes(existingRaw).size === 0) {
    // A valid TOML document can declare only child Router tables.
    out.unshift('[squilla_router]', `enabled = ${credential.routerMode !== 'disabled'}`, '')
  }
  if (writeIntent === 'preserve' && !rootFound && out.length === 0) return []
  while (out.length && out[out.length - 1]!.trim() === '') out.pop()
  return out
}
