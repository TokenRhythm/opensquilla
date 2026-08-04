import assert from 'node:assert/strict'

import {
  buildRendererConsoleLogEntry,
  buildRendererGoneLogEntry,
  shouldForwardConsoleLevel,
} from '../dist/desktop-renderer-log.js'

// error/warning are persisted; info/debug are dropped so routine renderer
// chatter never floods desktop.log.
assert.equal(shouldForwardConsoleLevel('error'), true)
assert.equal(shouldForwardConsoleLevel('warning'), true)
assert.equal(shouldForwardConsoleLevel('info'), false)
assert.equal(shouldForwardConsoleLevel('debug'), false)

// An error message becomes a structured desktop.log entry.
const errorEntry = buildRendererConsoleLogEntry({
  level: 'error',
  message: 'TypeError: cannot read properties of undefined',
  sourceId: 'app://control/assets/index.js',
  lineNumber: 1234,
})
assert.notEqual(errorEntry, null)
assert.equal(errorEntry.event, 'renderer_console')
assert.equal(errorEntry.detail.level, 'error')
assert.equal(errorEntry.detail.message, 'TypeError: cannot read properties of undefined')
assert.equal(errorEntry.detail.source, 'app://control/assets/index.js')
assert.equal(errorEntry.detail.line, 1234)

// A warning is also persisted (a stuck UI often only warns).
const warnEntry = buildRendererConsoleLogEntry({
  level: 'warning',
  message: 'stream idle for 630s',
  sourceId: 'app://control/assets/useChatStream.js',
  lineNumber: 42,
})
assert.notEqual(warnEntry, null)
assert.equal(warnEntry.detail.level, 'warning')

// info/debug produce no entry (dropped).
assert.equal(
  buildRendererConsoleLogEntry({ level: 'info', message: 'hi', sourceId: 's', lineNumber: 1 }),
  null,
)
assert.equal(
  buildRendererConsoleLogEntry({ level: 'debug', message: 'hi', sourceId: 's', lineNumber: 1 }),
  null,
)

// A very long message is truncated so one line can't bloat the log file.
const huge = 'x'.repeat(10000)
const truncated = buildRendererConsoleLogEntry({
  level: 'error',
  message: huge,
  sourceId: 's',
  lineNumber: 1,
})
assert.ok(truncated.detail.message.length < huge.length, 'oversized message must be truncated')
assert.ok(
  String(truncated.detail.message).includes('truncated'),
  'truncation must be marked so readers know the message was clipped',
)

// A gone render process produces a first, always-present breadcrumb.
const goneEntry = buildRendererGoneLogEntry({ reason: 'crashed', exitCode: 133 })
assert.equal(goneEntry.event, 'renderer_process_gone')
assert.equal(goneEntry.detail.reason, 'crashed')
assert.equal(goneEntry.detail.exitCode, 133)

console.log('desktop-renderer-log contract: all assertions passed.')
