// Condenses the pinned self-migration report wire shape
// into the compact numbers the Data & backups panel renders before an import.
// The report crosses the desktop preload bridge as `unknown`, so every field is
// guarded: a partial, absent, or future-shaped report degrades to zeros and
// empty lists instead of crashing the panel.

export interface MigrationItemCounts {
  migrated: number
  planned: number
  skipped: number
  error: number
}

export interface MigrationReportSummary {
  itemCounts: MigrationItemCounts
  /** Imported scheduler jobs — all arrive paused. */
  pausedJobs: number
  diskRequiredBytes: number | null
  diskFreeBytes: number | null
  /** The dry-run recorded the non-empty-target preflight error: the import
   *  refuses to run until the operator opts into overwrite-with-backups. */
  needsOverwrite: boolean
  /** Human-readable reason for the ordinary replace-or-cancel decision. */
  replacementReason: string | null
  /** `kind: reason` lines for every error item (redaction-guaranteed upstream). */
  errorNotes: string[]
  /** Free-form advisories that are not per-item results. */
  notes: string[]
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

export function summarizeMigrationReport(report: unknown): MigrationReportSummary {
  const root = asRecord(report) ?? {}
  const items = Array.isArray(root.items) ? root.items : []
  const itemCounts: MigrationItemCounts = { migrated: 0, planned: 0, skipped: 0, error: 0 }
  let needsOverwrite = false
  let replacementReason: string | null = null
  const errorNotes: string[] = []
  for (const raw of items) {
    const item = asRecord(raw)
    if (!item) continue
    const status = typeof item.status === 'string' ? item.status : ''
    if (status in itemCounts) itemCounts[status as keyof MigrationItemCounts] += 1
    if (status !== 'error') continue
    const kind = typeof item.kind === 'string' ? item.kind : ''
    const reason = typeof item.reason === 'string' ? item.reason : ''
    if (kind === 'preflight/target') {
      needsOverwrite = true
      replacementReason = reason || null
      continue
    }
    const note = kind && reason ? `${kind}: ${reason}` : kind || reason
    if (note) errorNotes.push(note)
  }
  const preflight = asRecord(root.preflight) ?? {}
  return {
    itemCounts,
    pausedJobs: Array.isArray(root.paused_jobs) ? root.paused_jobs.length : 0,
    diskRequiredBytes:
      typeof preflight.disk_required_bytes === 'number' ? preflight.disk_required_bytes : null,
    diskFreeBytes:
      typeof preflight.disk_free_bytes === 'number' ? preflight.disk_free_bytes : null,
    needsOverwrite,
    replacementReason,
    errorNotes,
    notes: Array.isArray(root.notes)
      ? root.notes.filter((n): n is string => typeof n === 'string')
      : [],
  }
}

/** Human-readable binary size; "—" for unknown/invalid values. */
export function formatByteSize(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  let value = bytes
  let unit = 'B'
  for (const next of ['KiB', 'MiB', 'GiB', 'TiB']) {
    if (value < 1024) break
    value /= 1024
    unit = next
  }
  return `${value >= 10 ? Math.round(value) : Math.round(value * 10) / 10} ${unit}`
}
