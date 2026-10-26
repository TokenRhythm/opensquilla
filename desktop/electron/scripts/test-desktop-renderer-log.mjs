import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { EventEmitter } from 'node:events'
import { runInNewContext } from 'node:vm'

import { appendDesktopLogRecord } from '../dist/desktop-log-file.js'
import { createMainWindowConsoleListener } from './renderer-log-test-helpers.mjs'
import {
  buildRendererConsoleLogEntry,
  buildRendererGoneLogEntry,
  buildRendererStateLogEntry,
  isLiveMainFrameConsoleMessage,
  RendererConsoleLogLimiter,
  shouldForwardConsoleLevel,
} from '../dist/desktop-renderer-log.js'

const consoleRecords = []
const mainFrame = {}
let windowDestroyed = false
let contentsDestroyed = false
const contents = {
  isDestroyed: () => contentsDestroyed,
  get mainFrame() {
    assert.equal(contentsDestroyed, false, 'Destroyed WebContents must not be read.')
    return mainFrame
  },
}
const consoleWindow = {
  isDestroyed: () => windowDestroyed,
  get webContents() {
    assert.equal(windowDestroyed, false, 'Destroyed BrowserWindow must not be read.')
    return contents
  },
}
const consoleListener = createMainWindowConsoleListener(consoleWindow, consoleRecords)
const consoleMessage = {
  frame: mainFrame, level: 'error', message: 'synthetic renderer error',
  sourceId: 'https://example.test/main.js', lineNumber: 1,
}
consoleListener(consoleMessage)
assert.equal(consoleRecords.length, 1)
consoleListener({ ...consoleMessage, frame: {} })
assert.equal(consoleRecords.length, 1, 'Child frames must not write lifecycle logs.')
contentsDestroyed = true
assert.doesNotThrow(() => consoleListener(consoleMessage))
windowDestroyed = true
assert.doesNotThrow(() => consoleListener(consoleMessage))
assert.equal(consoleRecords.length, 1, 'Late events must not write after teardown.')
// A late console event must not touch an already destroyed native window.
// The signed Windows package failed here after app.exit: Electron's default
// uncaught-exception handler opened a synchronous error dialog and blocked exit.
const trustedMainFrame = {}
const liveContents = { isDestroyed: () => false, mainFrame: trustedMainFrame }
const liveWindow = { isDestroyed: () => false, webContents: liveContents }
assert.equal(isLiveMainFrameConsoleMessage(liveWindow, { frame: trustedMainFrame }), true)
assert.equal(isLiveMainFrameConsoleMessage(liveWindow, { frame: {} }), false)
for (const missingFrame of [null, undefined]) {
  assert.equal(isLiveMainFrameConsoleMessage(liveWindow, { frame: missingFrame }), false)
  assert.equal(isLiveMainFrameConsoleMessage({
    isDestroyed: () => false,
    webContents: { isDestroyed: () => false, mainFrame: missingFrame },
  }, { frame: missingFrame }), false, 'two missing frames must not establish trusted identity')
}

let lateWindowContentsReads = 0
let lateDetailsFrameReads = 0
const destroyedWindow = {
  isDestroyed: () => true,
  get webContents() {
    lateWindowContentsReads += 1
    throw new TypeError('Object has been destroyed')
  },
}
const lateDetails = {
  get frame() {
    lateDetailsFrameReads += 1
    throw new TypeError('Object has been destroyed')
  },
}
assert.equal(isLiveMainFrameConsoleMessage(destroyedWindow, lateDetails), false)
assert.equal(lateWindowContentsReads, 0, 'a destroyed window must fence its native getter')
assert.equal(lateDetailsFrameReads, 0, 'a destroyed window must also fence the event frame getter')

// Reproduce the exact old expression from signed app.asar/dist/main.js:7904.
// The guarded call must reject the same window without repeating that access.
const previousFrameCheck = (window, details) => details.frame !== window.webContents.mainFrame
assert.throws(
  () => previousFrameCheck(destroyedWindow, { frame: trustedMainFrame }),
  { name: 'TypeError', message: 'Object has been destroyed' },
)
assert.equal(lateWindowContentsReads, 1)
assert.equal(isLiveMainFrameConsoleMessage(destroyedWindow, { frame: trustedMainFrame }), false)
assert.equal(lateWindowContentsReads, 1)

let destroyedContentsFrameReads = 0
const destroyedContentsWindow = {
  isDestroyed: () => false,
  webContents: {
    isDestroyed: () => true,
    get mainFrame() {
      destroyedContentsFrameReads += 1
      throw new TypeError('Object has been destroyed')
    },
  },
}
assert.equal(isLiveMainFrameConsoleMessage(destroyedContentsWindow, lateDetails), false)
assert.equal(destroyedContentsFrameReads, 0, 'destroyed contents must fence its mainFrame getter')
assert.equal(lateDetailsFrameReads, 0, 'destroyed contents must fence the event frame getter')

// Native teardown can invalidate a getter after the alive checks pass.
assert.equal(isLiveMainFrameConsoleMessage(liveWindow, lateDetails), false)
assert.equal(lateDetailsFrameReads, 1)
let invalidatedMainFrameReads = 0
assert.equal(isLiveMainFrameConsoleMessage({
  isDestroyed: () => false,
  webContents: {
    isDestroyed: () => false,
    get mainFrame() {
      invalidatedMainFrameReads += 1
      throw new TypeError('Object has been destroyed')
    },
  },
}, { frame: trustedMainFrame }), false)
assert.equal(invalidatedMainFrameReads, 1)
let invalidatedContentsReads = 0
assert.equal(isLiveMainFrameConsoleMessage({
  isDestroyed: () => false,
  get webContents() {
    invalidatedContentsReads += 1
    throw new TypeError('Object has been destroyed')
  },
}, { frame: trustedMainFrame }), false)
assert.equal(invalidatedContentsReads, 1)

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

const recoveryMessage = {
  level: 'warning',
  message: '[opensquilla-recovery] ' + JSON.stringify({
    phase: 'failed', reason: 'live_subscription_failed',
    session_key_sha256: '8ed3f6ad685b959e', connection_generation: 7,
    delivery_epoch: 'epoch-1', stream_seq: 91, suppressed_count: 12,
    error_code: 'SNAPSHOT_STALE', error_kind: 'busy', retryable: true,
    // The disk boundary must whitelist fields even if the renderer is buggy.
    message: 'private output', payload: 'private payload', session_key: 'raw-private-session',
  }),
  sourceId: 'opensquilla-app://desktop/chat', lineNumber: 10,
}
assert.deepEqual(buildRendererConsoleLogEntry(recoveryMessage), {
  event: 'session_recovery',
  detail: {
    phase: 'failed', reason: 'live_subscription_failed',
    session_key_sha256: '8ed3f6ad685b959e', delivery_epoch: 'epoch-1',
    connection_generation: 7, stream_seq: 91, suppressed_count: 12,
    retryable: true, error_code: 'SNAPSHOT_STALE', error_kind: 'busy',
  },
})
for (const message of [
  '[opensquilla-recovery] not-json',
  '[opensquilla-recovery] null',
  '[opensquilla-recovery] []',
  '[opensquilla-recovery] {"phase":"failed","reason":"private output"}',
  '[opensquilla-recovery] ' + 'x'.repeat(3000),
]) assert.equal(buildRendererConsoleLogEntry({ ...recoveryMessage, message }), null)
assert.equal(buildRendererConsoleLogEntry({ ...recoveryMessage, level: 'info' }), null)
const recoveryRecords = []
const recoveryListener = createMainWindowConsoleListener(liveWindow, recoveryRecords)
recoveryListener({ ...recoveryMessage, frame: trustedMainFrame })
recoveryListener({ ...recoveryMessage, frame: {} })
assert.equal(recoveryRecords.length, 1, 'Only the trusted main frame may persist recovery breadcrumbs.')
assert.equal(recoveryRecords[0].event, 'session_recovery')

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
assert.deepEqual(nextWindow[0].detail, { count: 2, window_ms: 1000, channel: 'renderer_console' })
assert.equal(nextWindow[1], errorEntry)
assert.deepEqual(limiter.flush(), [])

const recoveryEntry = (scope, generation = 0) => ({
  event: 'session_recovery',
  detail: {
    phase: 'failed', reason: 'metadata_hydration_failed',
    session_key_sha256: scope, connection_generation: generation,
  },
})
const scopeHash = index => index.toString(16).padStart(16, '0')

// Different chats do not spend each other's allowance or the renderer-error
// allowance. This is one long-lived main limiter, not a fresh one per document.
const chatLimiter = new RendererConsoleLogLimiter()
for (let chat = 0; chat < 20; chat++) {
  const entry = recoveryEntry(scopeHash(chat))
  assert.deepEqual(chatLimiter.accept(entry, chat), [entry])
}
for (let error = 0; error < 10; error++) {
  assert.deepEqual(chatLimiter.accept(errorEntry, error), [errorEntry])
}
assert.deepEqual(chatLimiter.flush(), [])

// Reloading the same chat cannot reset its main-process budget. Conversely,
// saturating recovery diagnostics must not hide the next real renderer error.
const reloadLimiter = new RendererConsoleLogLimiter()
for (let document = 0; document < 1000; document++) {
  const entry = recoveryEntry(scopeHash(1), document)
  assert.equal(reloadLimiter.accept(entry, document).length, document < 8 ? 1 : 0)
}
assert.deepEqual(reloadLimiter.accept(errorEntry, 1000), [errorEntry])
assert.deepEqual(reloadLimiter.flush(), [{
  event: 'renderer_console_suppressed',
  detail: { count: 992, window_ms: 60_000, channel: 'session_recovery' },
}])
assert.deepEqual(reloadLimiter.accept(recoveryEntry(scopeHash(1)), 1001), [])

// Error flooding cannot consume recovery slots either. Lost error records keep
// their original failure signal, and neither channel disguises it as success.
const errorFloodLimiter = new RendererConsoleLogLimiter()
for (let error = 0; error < 1000; error++) errorFloodLimiter.accept(errorEntry, error)
const afterErrors = recoveryEntry(scopeHash(2))
assert.deepEqual(errorFloodLimiter.accept(afterErrors, 1000), [afterErrors])
assert.deepEqual(errorFloodLimiter.flush(), [{
  event: 'renderer_console_suppressed',
  detail: { count: 990, window_ms: 60_000, channel: 'renderer_console' },
}])

// Scope churn cannot evict a spent allowance: at most 64 * 8 recovery writes
// fit in one fixed window, even when unbounded distinct identities are offered.
const scopeLimiter = new RendererConsoleLogLimiter()
let recoveryWrites = 0
for (let scope = 0; scope < 1000; scope++) {
  for (let record = 0; record < 8; record++) {
    recoveryWrites += scopeLimiter.accept(recoveryEntry(scopeHash(scope), record), 0).length
  }
}
assert.equal(recoveryWrites, 512)
assert.deepEqual(scopeLimiter.accept(recoveryEntry(scopeHash(0), 9), 1), [])
assert.deepEqual(scopeLimiter.flush(), [{
  event: 'renderer_console_suppressed',
  detail: { count: 7489, window_ms: 60_000, channel: 'session_recovery' },
}])
assert.equal(scopeLimiter.accept(recoveryEntry(scopeHash(999)), 60_000).length, 1)

// Only a validated hash can create a scope; malformed or missing IDs cannot
// bypass the global bucket, even if a caller bypasses the console parser.
const invalidScopeLimiter = new RendererConsoleLogLimiter()
for (let index = 0; index < 20; index++) {
  const hash = index % 2 === 0 ? undefined : `untrusted-${index}`
  assert.equal(invalidScopeLimiter.accept(recoveryEntry(hash), index).length, index < 8 ? 1 : 0)
}
assert.deepEqual(invalidScopeLimiter.flush(), [{
  event: 'renderer_console_suppressed',
  detail: { count: 12, window_ms: 60_000, channel: 'session_recovery' },
}])

// Exercise the production listener with successive document frame identities,
// retaining the same window-owned limiter just as Electron does on navigation.
let documentFrame = {}
const navigationRecords = []
const navigationWindow = {
  isDestroyed: () => false,
  webContents: { isDestroyed: () => false, get mainFrame() { return documentFrame } },
}
const navigationListener = createMainWindowConsoleListener(navigationWindow, navigationRecords)
const recoveryConsoleMessage = scope => ({
  ...recoveryMessage, frame: documentFrame,
  message: '[opensquilla-recovery] ' + JSON.stringify(recoveryEntry(scope).detail),
})
for (let chat = 0; chat < 20; chat++) {
  documentFrame = {}
  navigationListener(recoveryConsoleMessage(scopeHash(chat)))
}
assert.equal(navigationRecords.filter(record => record.event === 'session_recovery').length, 20)
for (let reload = 0; reload < 20; reload++) {
  documentFrame = {}
  navigationListener(recoveryConsoleMessage(scopeHash(0)))
}
assert.equal(navigationRecords.filter(record => record.event === 'session_recovery').length, 27)
for (let error = 0; error < 10; error++) {
  navigationListener({ ...consoleMessage, frame: documentFrame })
}
assert.equal(navigationRecords.filter(record => record.event === 'renderer_console').length, 10)

const largestRecovery = buildRendererConsoleLogEntry({
  ...recoveryMessage,
  message: '[opensquilla-recovery] ' + JSON.stringify({
    phase: 'recovered', reason: 'metadata_hydration_failed',
    session_key_sha256: 'f'.repeat(16), delivery_epoch: 'ep/'.repeat(53) + 'e',
    ...Object.fromEntries(['connection_generation', 'delivery_id', 'stream_seq', 'expected_seq',
      'actual_seq', 'key_count', 'suppressed_count'].map(key => [key, Number.MAX_SAFE_INTEGER])),
    global: false, retryable: false, error_code: 'HISTORY_CURSOR_INVALIDATED',
    error_kind: 'budget-exhausted',
  }),
})
assert.notEqual(largestRecovery, null)
const recoveryFixtureBytes = Buffer.byteLength(JSON.stringify(largestRecovery), 'utf8')
assert.ok(recoveryFixtureBytes <= 1024, 'The maximal schema fixture must remain content-free and bounded.')
console.log(`renderer recovery maximal schema fixture: ${recoveryFixtureBytes} bytes; 512 records/window maximum.`)

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

// Exercise the main-process recovery owner without launching Electron.
const mainSource = readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8')
const recoveryStart = mainSource.indexOf('let rendererRecoveryGeneration = 0')
const recoveryEnd = mainSource.indexOf("window.webContents.on('unresponsive'", recoveryStart)
assert.ok(recoveryStart >= 0 && recoveryEnd > recoveryStart)
const recoverySource = mainSource.slice(recoveryStart, recoveryEnd)

function recoveryHarness() {
  const webContents = new EventEmitter()
  let reloads = 0
  webContents.reload = () => { reloads += 1 }
  webContents.getOSProcessId = () => 123
  const timers = new Map()
  const logs = []
  let nextTimer = 0
  const window = { webContents, isDestroyed: () => false }
  const context = {
    window, isQuitting: false, appExitPhase: 'running', rendererUnresponsiveAt: null,
    flushRendererConsoleSuppression() {}, releaseRendererOwnedArtifactPreviews() {},
    recordRendererCrash() {}, buildRendererGoneLogEntry,
    desktopLog: (event, detail) => logs.push({ event, detail }),
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id },
    clearTimeout(id) { timers.delete(id) },
  }
  runInNewContext(recoverySource, context)
  return {
    context, webContents, timers, logs,
    get reloads() { return reloads },
    crash() { webContents.emit('render-process-gone', {}, { reason: 'crashed', exitCode: 1 }) },
  }
}

{
  const h = recoveryHarness()
  h.crash()
  const staleReady = h.webContents.listeners('did-finish-load')[0]
  h.crash()
  assert.equal(h.reloads, 2, 'a second crash during recovery must reload the new generation')
  assert.equal(h.timers.size, 1, 'the superseded recovery timer must be cleared')
  staleReady()
  assert.equal(h.logs.filter(log => log.event === 'renderer_recovery_ready').length, 0)
  h.webContents.emit('did-finish-load')
  assert.deepEqual(h.logs.filter(log => log.event === 'renderer_recovery_ready').map(log => log.detail.generation), [2])
  assert.equal(h.timers.size, 0)
}

{
  const h = recoveryHarness()
  for (let index = 0; index < 4; index += 1) h.crash()
  assert.equal(h.reloads, 3, 'repeated crashes must retain the existing three-attempt budget')
  assert.equal(h.timers.size, 0)
  assert.equal(h.webContents.listenerCount('did-finish-load'), 0)
  assert.equal(h.logs.at(-1).event, 'renderer_recovery_exhausted')
}

{
  const h = recoveryHarness()
  h.crash()
  h.webContents.emit('did-finish-load')
  for (let index = 0; index < 3; index += 1) h.crash()
  assert.equal(h.reloads, 4, 'successful recovery must reset the failure budget')
}

for (const retire of [
  h => { h.context.isQuitting = true },
  h => { h.context.appExitPhase = 'deferred' },
  h => { h.context.window.isDestroyed = () => true },
]) {
  const h = recoveryHarness()
  h.crash()
  retire(h)
  h.crash()
  assert.equal(h.reloads, 1, 'a superseding crash during teardown must not reload')
  assert.equal(h.timers.size, 0)
  assert.equal(h.webContents.listenerCount('did-finish-load'), 0)
}

console.log('desktop renderer logging and recovery contract: all assertions passed.')
