import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { appendDesktopLogRecord } from '../dist/desktop-log-file.js'
import {
  buildRendererConsoleLogEntry,
  buildRendererGoneLogEntry,
  buildRendererStateLogEntry,
  RendererConsoleLogLimiter,
  shouldForwardConsoleLevel,
} from '../dist/desktop-renderer-log.js'

// Only errors are forwarded by default. Renderer warning/info/debug chatter is
// dropped; a real process hang has its own structured lifecycle event.
assert.equal(shouldForwardConsoleLevel('error'), true)
assert.equal(shouldForwardConsoleLevel('warning'), false)
assert.equal(shouldForwardConsoleLevel('info'), false)
assert.equal(shouldForwardConsoleLevel('debug'), false)

const errorEntry = buildRendererConsoleLogEntry({
  level: 'error',
  message: [
    'TypeError: failed',
    'OPENAI_API_KEY=sk-synthetic-secret-value',
    'Authorization: Bearer synthetic-bearer-value',
    'unknown credential abcdefghijklmnopqrstuvwxyz0123456789',
    'at /synthetic-profile/private/index.js',
  ].join(' '),
  sourceId: 'file:///synthetic-profile/app/index.js?token=source-secret#fragment',
  lineNumber: 1234.9,
}, { homeDir: '/synthetic-profile' })
assert.notEqual(errorEntry, null)
assert.equal(errorEntry.event, 'renderer_console')
assert.equal(errorEntry.detail.level, 'error')
assert.match(String(errorEntry.detail.message), /\[redacted\]/)
assert.doesNotMatch(String(errorEntry.detail.message), /synthetic-secret|synthetic-bearer/)
assert.doesNotMatch(String(errorEntry.detail.message), /abcdefghijklmnopqrstuvwxyz0123456789/)
assert.doesNotMatch(String(errorEntry.detail.message), /\/synthetic-profile/)
assert.equal(errorEntry.detail.source, '~/app/index.js')
assert.doesNotMatch(String(errorEntry.detail.source), /token=|fragment/)
assert.equal(errorEntry.detail.line, 1234)

for (const level of ['warning', 'info', 'debug']) {
  assert.equal(
    buildRendererConsoleLogEntry({ level, message: 'ignored', sourceId: 's', lineNumber: 1 }),
    null,
  )
}

const invalidLine = buildRendererConsoleLogEntry({
  level: 'error', message: 'boom', sourceId: 'data:text/javascript,secret', lineNumber: NaN,
})
assert.equal(invalidLine.detail.source, '[inline]')
assert.equal(invalidLine.detail.line, 0)

// Byte-bound truncation must stay valid for multi-byte characters.
const huge = buildRendererConsoleLogEntry({
  level: 'error', message: '鲸'.repeat(2000), sourceId: 's', lineNumber: 1,
})
assert.ok(Buffer.byteLength(String(huge.detail.message), 'utf8') <= 2048)
assert.match(String(huge.detail.message), /truncated/)

// A hot console loop produces at most the configured writes plus one summary.
const limiter = new RendererConsoleLogLimiter({ maxEntries: 2, windowMs: 1000 })
assert.deepEqual(limiter.accept(errorEntry, 0), [errorEntry])
assert.deepEqual(limiter.accept(errorEntry, 1), [errorEntry])
assert.deepEqual(limiter.accept(errorEntry, 2), [])
assert.deepEqual(limiter.accept(errorEntry, 3), [])
const nextWindow = limiter.accept(errorEntry, 1000)
assert.equal(nextWindow.length, 2)
assert.equal(nextWindow[0].event, 'renderer_console_suppressed')
assert.deepEqual(nextWindow[0].detail, { count: 2, window_ms: 1000 })
assert.equal(nextWindow[1], errorEntry)
assert.deepEqual(limiter.flush(), [])

const goneEntry = buildRendererGoneLogEntry({ reason: 'crashed', exitCode: 133 })
assert.equal(goneEntry.event, 'renderer_process_gone')
assert.equal(goneEntry.detail.reason, 'crashed')
assert.equal(goneEntry.detail.exitCode, 133)
assert.deepEqual(buildRendererStateLogEntry('unresponsive'), {
  event: 'renderer_unresponsive', detail: {},
})
assert.deepEqual(buildRendererStateLogEntry('responsive', 123.9), {
  event: 'renderer_responsive', detail: { duration_ms: 123 },
})

// The lifecycle log is bounded with same-directory backups and valid JSONL.
const tempDir = mkdtempSync(join(tmpdir(), 'opensquilla-desktop-log-'))
try {
  const logPath = join(tempDir, 'logs', 'desktop.log')
  const options = { maxBytes: 180, backupCount: 2, now: new Date('2026-08-06T00:00:00Z') }
  appendDesktopLogRecord(logPath, 'first', { message: 'a'.repeat(80) }, options)
  appendDesktopLogRecord(logPath, 'second', { message: 'b'.repeat(80) }, options)
  appendDesktopLogRecord(logPath, 'third', { message: 'c'.repeat(80) }, options)

  const current = JSON.parse(readFileSync(logPath, 'utf8'))
  const firstBackup = JSON.parse(readFileSync(`${logPath}.1`, 'utf8'))
  const secondBackup = JSON.parse(readFileSync(`${logPath}.2`, 'utf8'))
  assert.equal(current.event, 'third')
  assert.equal(firstBackup.event, 'second')
  assert.equal(secondBackup.event, 'first')

  appendDesktopLogRecord(logPath, 'oversized', { message: 'x'.repeat(70_000) }, {
    maxBytes: 5 * 1024 * 1024,
    backupCount: 2,
    now: new Date('2026-08-06T00:00:01Z'),
  })
  const records = readFileSync(logPath, 'utf8').trim().split('\n').map(line => JSON.parse(line))
  assert.equal(records.at(-1).event, 'oversized')
  assert.equal(records.at(-1).detail_omitted, true)
  assert.ok(records.at(-1).original_bytes > 64 * 1024)
} finally {
  rmSync(tempDir, { recursive: true, force: true })
}

console.log('desktop renderer logging contract: all assertions passed.')
