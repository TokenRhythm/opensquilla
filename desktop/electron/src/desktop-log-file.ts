import {
  appendFileSync,
  existsSync,
  mkdirSync,
  renameSync,
  statSync,
  unlinkSync,
} from 'node:fs'
import { Buffer } from 'node:buffer'
import { dirname } from 'node:path'

export const DESKTOP_LOG_MAX_BYTES = 5 * 1024 * 1024
export const DESKTOP_LOG_BACKUP_COUNT = 2
const DESKTOP_LOG_MAX_RECORD_BYTES = 64 * 1024

export interface DesktopLogFileOptions {
  maxBytes?: number
  backupCount?: number
  now?: Date
}

function rotateDesktopLog(logPath: string, backupCount: number): void {
  if (backupCount <= 0) {
    if (existsSync(logPath)) unlinkSync(logPath)
    return
  }

  const oldest = `${logPath}.${backupCount}`
  if (existsSync(oldest)) unlinkSync(oldest)
  for (let index = backupCount - 1; index >= 1; index -= 1) {
    const source = `${logPath}.${index}`
    if (existsSync(source)) renameSync(source, `${logPath}.${index + 1}`)
  }
  if (existsSync(logPath)) renameSync(logPath, `${logPath}.1`)
}

function serializedDesktopLogRecord(
  event: string,
  detail: Record<string, unknown> | undefined,
  now: Date,
): string {
  const record = JSON.stringify({ at: now.toISOString(), event, ...detail }) + '\n'
  const recordBytes = Buffer.byteLength(record, 'utf8')
  if (recordBytes <= DESKTOP_LOG_MAX_RECORD_BYTES) return record
  return JSON.stringify({
    at: now.toISOString(),
    event,
    detail_omitted: true,
    original_bytes: recordBytes,
  }) + '\n'
}

/**
 * Append one bounded JSONL record, rotating the file before it would cross
 * the configured limit. All operations are synchronous because lifecycle
 * breadcrumbs may immediately precede app.exit(); callers keep this fail-open.
 */
export function appendDesktopLogRecord(
  logPath: string,
  event: string,
  detail?: Record<string, unknown>,
  options: DesktopLogFileOptions = {},
): void {
  const maxBytes = Math.max(1, options.maxBytes ?? DESKTOP_LOG_MAX_BYTES)
  const backupCount = Math.max(0, options.backupCount ?? DESKTOP_LOG_BACKUP_COUNT)
  const record = serializedDesktopLogRecord(event, detail, options.now ?? new Date())
  const recordBytes = Buffer.byteLength(record, 'utf8')

  mkdirSync(dirname(logPath), { recursive: true })
  const currentBytes = existsSync(logPath) ? statSync(logPath).size : 0
  if (currentBytes > 0 && currentBytes + recordBytes > maxBytes) {
    // Rotation is best-effort. A transient rename race must not discard the
    // lifecycle record; append to the current file if rotation cannot finish.
    try {
      rotateDesktopLog(logPath, backupCount)
    } catch {
      // The outer desktop logger still prevents any append failure affecting UI.
    }
  }
  appendFileSync(logPath, record, 'utf8')
}
