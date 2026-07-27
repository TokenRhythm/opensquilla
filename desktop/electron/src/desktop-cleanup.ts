import { randomUUID } from 'node:crypto'
import { isAbsolute, relative, resolve, sep } from 'node:path'

export const DESKTOP_CLEANUP_MODES = [
  'reset-current-settings',
  'delete-current-profile',
  'delete-all-user-data',
] as const

export type DesktopCleanupMode = typeof DESKTOP_CLEANUP_MODES[number]
export type DesktopCleanupOutcome = 'ready' | 'blocked' | 'complete' | 'partial'

export interface DesktopCleanupItem {
  kind: string
  path: string
  exists: boolean
  identity: string | null
}

export interface DesktopCleanupReport {
  schema_version: 1
  outcome: DesktopCleanupOutcome
  stable_code: string
  mode: DesktopCleanupMode
  items: DesktopCleanupItem[]
  transaction_id: string
  revision: number
  scope_fingerprint: string
}

export interface DesktopCleanupSelection {
  mode: DesktopCleanupMode
  profileKey: string
}

export interface TrustedDesktopCleanupPreview {
  id: string
  report: DesktopCleanupReport
  selection: DesktopCleanupSelection
  createdAt: number
}

const CLEANUP_MODE_SET = new Set<string>(DESKTOP_CLEANUP_MODES)
const CLEANUP_OUTCOME_SET = new Set<string>(['ready', 'blocked', 'complete', 'partial'])

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

export function parseDesktopCleanupMode(value: unknown): DesktopCleanupMode | null {
  return typeof value === 'string' && CLEANUP_MODE_SET.has(value)
    ? value as DesktopCleanupMode
    : null
}

export function parseDesktopCleanupProtocol(value: unknown): DesktopCleanupReport {
  const payload = record(value)
  if (!payload || payload.schema_version !== 1) {
    throw new Error('Cleanup command returned an unsupported protocol schema.')
  }
  const outcome = typeof payload.outcome === 'string' && CLEANUP_OUTCOME_SET.has(payload.outcome)
    ? payload.outcome as DesktopCleanupOutcome
    : null
  const mode = parseDesktopCleanupMode(payload.mode)
  if (!outcome || !mode) throw new Error('Cleanup command returned an invalid outcome or mode.')
  if (typeof payload.stable_code !== 'string' || !payload.stable_code) {
    throw new Error('Cleanup command omitted its stable code.')
  }
  if (typeof payload.transaction_id !== 'string' || !payload.transaction_id) {
    throw new Error('Cleanup command omitted its transaction id.')
  }
  if (!Number.isSafeInteger(payload.revision) || Number(payload.revision) < 0) {
    throw new Error('Cleanup command returned an invalid revision.')
  }
  if (
    typeof payload.scope_fingerprint !== 'string'
    || !/^[0-9a-f]{64}$/.test(payload.scope_fingerprint)
  ) {
    throw new Error('Cleanup command returned an invalid scope fingerprint.')
  }
  if (!Array.isArray(payload.items)) {
    throw new Error('Cleanup command returned an invalid inventory.')
  }
  const items = payload.items.map((value): DesktopCleanupItem => {
    const item = record(value)
    if (
      !item
      || typeof item.kind !== 'string'
      || !item.kind
      || typeof item.path !== 'string'
      || !item.path
      || typeof item.exists !== 'boolean'
      || (item.identity !== null && typeof item.identity !== 'string')
    ) {
      throw new Error('Cleanup command returned an invalid inventory item.')
    }
    return {
      kind: item.kind,
      path: item.path,
      exists: item.exists,
      identity: item.identity as string | null,
    }
  })
  return {
    schema_version: 1,
    outcome,
    stable_code: payload.stable_code,
    mode,
    items,
    transaction_id: payload.transaction_id,
    revision: Number(payload.revision),
    scope_fingerprint: payload.scope_fingerprint,
  }
}

export function cleanupSelectorArgs(
  selection: DesktopCleanupSelection,
): string[] {
  return [
    '--mode', selection.mode,
    '--profile-kind', 'primary',
  ]
}

export function desktopCleanupScopeIsContained(
  report: DesktopCleanupReport,
  userData: string,
): boolean {
  const root = resolve(userData)
  return report.items.every((item) => {
    const fromRoot = relative(root, resolve(item.path))
    return fromRoot === '' || (
      fromRoot !== '..'
      && !fromRoot.startsWith(`..${sep}`)
      && !isAbsolute(fromRoot)
    )
  })
}

export function sameDesktopCleanupScope(
  displayed: DesktopCleanupReport,
  refreshed: DesktopCleanupReport,
  userData: string,
): boolean {
  const signature = (report: DesktopCleanupReport) => report.items
    .map((item) => `${item.kind}\u0000${resolve(item.path)}`)
    .sort()
  return displayed.mode === refreshed.mode
    && desktopCleanupScopeIsContained(displayed, userData)
    && desktopCleanupScopeIsContained(refreshed, userData)
    && JSON.stringify(signature(displayed)) === JSON.stringify(signature(refreshed))
}

/**
 * Holds the one main-process-authoritative cleanup inventory that a renderer
 * may confirm. The renderer receives only its opaque id; mode/profile/path
 * selectors are never accepted back across IPC.
 */
export class DesktopCleanupPreviewStore {
  private preview: TrustedDesktopCleanupPreview | null = null

  constructor(private readonly ttlMs = 10 * 60 * 1000) {}

  create(
    report: DesktopCleanupReport,
    selection: DesktopCleanupSelection,
    createdAt = Date.now(),
  ): TrustedDesktopCleanupPreview {
    if (report.outcome !== 'ready' || report.mode !== selection.mode) {
      throw new Error('Only a ready, selector-bound cleanup inventory can be approved.')
    }
    const preview = {
      id: randomUUID(),
      report,
      selection: { ...selection },
      createdAt,
    }
    this.preview = preview
    return preview
  }

  consume(
    previewId: unknown,
    activeProfileKey: string,
    now = Date.now(),
  ): TrustedDesktopCleanupPreview | null {
    const preview = this.preview
    this.preview = null
    if (
      !preview
      || typeof previewId !== 'string'
      || preview.id !== previewId
      || now - preview.createdAt > this.ttlMs
      || now < preview.createdAt
      || preview.selection.profileKey !== activeProfileKey
    ) return null
    return preview
  }

  discard(
    previewId: unknown,
    activeProfileKey: string,
  ): boolean {
    const preview = this.preview
    if (
      !preview
      || typeof previewId !== 'string'
      || preview.id !== previewId
      || preview.selection.profileKey !== activeProfileKey
    ) return false
    this.preview = null
    return true
  }

  clear(): void {
    this.preview = null
  }
}
