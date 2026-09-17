import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { test } from 'node:test'
import {
  SHUTDOWN_CANCELLATION,
  evaluateFirstSendEvidence,
  observeRendererPages,
  parseEvidenceLog,
  summarizeDesktopLog,
} from './packaged-first-send-evidence.mjs'

const at = offset => new Date(Date.UTC(2026, 8, 17, 0, 0, 0, offset)).toISOString()
const source = 'opensquilla-app://desktop/assets/useSessions-test.js'
const logText = records => records.map(record => JSON.stringify(record)).join('\n') + '\n'

export function evidenceFixture(cancellations = 1) {
  const desktopRecords = [
    { at: at(0), event: 'startup' },
    { at: at(100), event: 'before_quit' },
    { at: at(101), event: 'quit_gateway_shutdown_requested', accepted: true, alreadyStopping: false },
    ...Array.from({ length: cancellations }, (_, index) => ({ at: at(102 + index),
      event: 'renderer_console', level: 'error', message: SHUTDOWN_CANCELLATION, source, line: 42 })),
    { at: at(200), event: 'quit_gateway_exit', exited: true, hardTerminated: false },
    { at: at(201), event: 'desktop_exit_phase', to: 'committed', reason: 'all lifecycle-owned Gateways exited' },
  ]
  const mainRecords = [
    { index: 0, at: at(1), event: 'observation-start' },
    { index: 1, at: at(99), event: 'cleanup-start' },
    ...Array.from({ length: cancellations }, (_, index) => ({ index: index + 2, at: at(102 + index),
      event: 'console', webContentsId: 10, mainFrame: true, phase: 'electron-cleanup',
      level: 'error', message: SHUTDOWN_CANCELLATION, source, line: 42 })),
    { index: cancellations + 2, at: at(202), event: 'observation-exit', code: 0, writeErrors: 0 },
  ]
  const journal = parseEvidenceLog(logText(mainRecords))
  return {
    reportType: 'packaged-first-send', schemaVersion: 2, ok: true,
    iterations: 20, rpc: { chatSend: 40, uniqueSessions: 20 }, provider: { chatRequestCount: 40 },
    renderer: { pageErrors: 0, pageErrorDetails: [], consoleErrors: cancellations,
      consoleErrorDetails: Array.from({ length: cancellations }, (_, index) => ({ index,
        pageId: 1, phase: 'electron-cleanup-start', observedAt: at(103 + index),
        frameIsolationProven: true,
        message: SHUTDOWN_CANCELLATION, source, line: 42 })),
    },
    desktopLog: summarizeDesktopLog(parseEvidenceLog(logText(desktopRecords))),
    observation: { targetPageId: 1, targetWebContentsId: 10, subframePageIds: [], completed: true, errors: [],
      journal, mainConsoleRecords: journal.records.filter(record => record.event === 'console') },
    cleanupSucceeded: true,
    externalRendererRequests: 0,
  }
}

function refreshLogs(fixture) {
  fixture.desktopLog = summarizeDesktopLog(parseEvidenceLog(logText(fixture.desktopLog.records)))
  fixture.observation.journal = parseEvidenceLog(logText(fixture.observation.journal.records))
  fixture.observation.mainConsoleRecords = fixture.observation.journal.records.filter(record => record?.event === 'console')
}

if (process.argv.includes('--fixture-json')) {
  const result = evidenceFixture(process.argv.includes('--no-cancellation') ? 0 : 1)
  result.acceptance = evaluateFirstSendEvidence(result)
  console.log(JSON.stringify({ result,
    desktopSource: logText(result.desktopLog.records),
    consoleSource: logText(result.observation.journal.records),
  }))
} else {
  test('normal zero-error and exact one-to-one shutdown observations pass', () => {
    for (const count of [0, 1, 2]) {
      const fixture = evidenceFixture(count)
      const result = evaluateFirstSendEvidence(fixture)
      assert.deepEqual(result.failures, [])
      assert.equal(result.consoleMatches.length, count)
      assert.deepEqual(result.consoleMatches, Array.from({ length: count }, (_, index) => ({
        consoleIndex: index, mainRecordIndex: index + 2, desktopRecordIndex: index + 3,
      })))
      assert.equal(fixture.renderer.consoleErrors, count, 'raw error counts are never subtracted')
    }
  })

  const invalidCases = [
    ['runtime same text', fixture => { fixture.renderer.consoleErrorDetails[0].phase = 'iteration-start' }],
    ['before cleanup phase', fixture => { fixture.renderer.consoleErrorDetails[0].phase = 'cleanup-start' }],
    ['after cleanup phase', fixture => { fixture.renderer.consoleErrorDetails[0].phase = 'provider-cleanup-start' }],
    ['child frame same text', fixture => { fixture.observation.journal.records[2].mainFrame = false }],
    ['native misattributed detached child frame', fixture => { fixture.observation.subframePageIds = [1] }],
    ['frame not isolated at console emission', fixture => { fixture.renderer.consoleErrorDetails[0].frameIsolationProven = false }],
    ['other native window same text', fixture => { fixture.observation.journal.records[2].webContentsId = 11 }],
    ['other Playwright page same text', fixture => { fixture.renderer.consoleErrorDetails[0].pageId = 2 }],
    ['unattributed source', fixture => { fixture.renderer.consoleErrorDetails[0].source = '' }],
    ['different source', fixture => { fixture.observation.journal.records[2].source += '.other' }],
    ['line mismatch', fixture => { fixture.renderer.consoleErrorDetails[0].line += 1 }],
    ['unknown message', fixture => { fixture.renderer.consoleErrorDetails[0].message = 'TypeError: synthetic failure' }],
    ['different message in native stream', fixture => { fixture.observation.journal.records[2].message = 'unknown' }],
    ['different message in desktop stream', fixture => { fixture.desktopLog.records[3].message = 'unknown' }],
    ['extra Playwright event', fixture => {
      fixture.renderer.consoleErrorDetails.push({ ...fixture.renderer.consoleErrorDetails[0], index: 1 })
      fixture.renderer.consoleErrors += 1
    }],
    ['missing Playwright event', fixture => { fixture.renderer.consoleErrorDetails = []; fixture.renderer.consoleErrors = 0 }],
    ['extra native event', fixture => { fixture.observation.journal.records.push({ ...fixture.observation.journal.records[2], index: 3 }) }],
    ['missing native event', fixture => { fixture.observation.journal.records.splice(2, 1) }],
    ['missing desktop event', fixture => { fixture.desktopLog.records.splice(3, 1) }],
    ['page exception during cleanup', fixture => {
      fixture.renderer.pageErrors = 1
      fixture.renderer.pageErrorDetails.push({ phase: 'electron-cleanup-start', message: 'late error' })
    }],
    ['outbound request during cleanup', fixture => { fixture.externalRendererRequests = 1 }],
    ['forced or failed cleanup', fixture => { fixture.cleanupSucceeded = false }],
    ['missing accepted shutdown', fixture => { fixture.desktopLog.records.splice(2, 1) }],
    ['hard-terminated Gateway', fixture => { fixture.desktopLog.records[4].hardTerminated = true }],
    ['accepted has wrong type', fixture => { fixture.desktopLog.records[2].accepted = 'true' }],
    ['already stopping Gateway', fixture => { fixture.desktopLog.records[2].alreadyStopping = true }],
    ['missing committed desktop exit', fixture => { fixture.desktopLog.records.pop() }],
    ['duplicate quit attempt', fixture => { fixture.desktopLog.records.push({ ...fixture.desktopLog.records[1] }) }],
    ['out-of-order exit', fixture => { [fixture.desktopLog.records[2], fixture.desktopLog.records[4]] = [fixture.desktopLog.records[4], fixture.desktopLog.records[2]] }],
    ['corrupt record outside exit interval', fixture => { fixture.desktopLog.records[0] = null }],
    ['suppressed console outside exit interval', fixture => { fixture.desktopLog.records[0].event = 'renderer_console_suppressed' }],
    ['renderer crash after Gateway exit', fixture => { fixture.desktopLog.records.push({ at: at(201), event: 'renderer_process_gone' }) }],
    ['missing observation start', fixture => { fixture.observation.journal.records[0].event = 'other' }],
    ['observation write error', fixture => { fixture.observation.journal.records.push({ index: 3, at: at(200), event: 'observation-error', error: 'EIO' }) }],
    ['missing observation exit', fixture => { fixture.observation.journal.records.pop() }],
    ['nonzero observation exit', fixture => { fixture.observation.journal.records.at(-1).code = 1 }],
    ['silently failed journal write', fixture => { fixture.observation.journal.records.at(-1).writeErrors = 1 }],
    ['journal wrong index', fixture => { fixture.observation.journal.records[2].index = 8 }],
    ['invalid Playwright timestamp', fixture => { fixture.renderer.consoleErrorDetails[0].observedAt = 'invalid' }],
    ['native error before shutdown accepted', fixture => { fixture.observation.journal.records[2].at = at(50) }],
    ['native error after Gateway exit', fixture => { fixture.observation.journal.records[2].at = at(300) }],
    ['native console still running', fixture => { fixture.observation.journal.records[2].phase = 'running' }],
  ]
  for (const [name, mutate] of invalidCases) {
    test(`reject ${name}`, () => {
      const fixture = evidenceFixture()
      mutate(fixture)
      refreshLogs(fixture)
      assert.notEqual(evaluateFirstSendEvidence(fixture).failures.length, 0, name)
    })
  }

  test('equal counts cannot use a shutdown event to cancel a runtime or child-frame error', () => {
    const fixture = evidenceFixture(2)
    fixture.renderer.consoleErrorDetails[0].phase = 'iteration-complete'
    fixture.observation.journal.records[2].mainFrame = false
    refreshLogs(fixture)
    const result = evaluateFirstSendEvidence(fixture)
    assert.deepEqual(result.unexpectedConsoleIndices, [0])
    assert.deepEqual(result.unexpectedMainRecordIndices, [2])
    assert.deepEqual(result.unexpectedDesktopRecordIndices, [3])
    assert.equal(result.consoleMatches.length, 1)
    assert.notEqual(result.failures.length, 0)
  })

  test('delayed Playwright delivery preserves exact ordered native-event attribution', () => {
    const fixture = evidenceFixture()
    fixture.renderer.consoleErrorDetails[0].observedAt = at(5000)
    assert.deepEqual(evaluateFirstSendEvidence(fixture).failures, [])
  })

  test('damaged, truncated, missing and omitted log evidence cannot pass', () => {
    for (const text of ['', '{"event":', '{"at":"2026-09-17T00:00:00Z","event":"start"}',
      '{"at":"2026-09-17T00:00:00Z","event":"start","detail_omitted":true}\n']) {
      assert.equal(parseEvidenceLog(text).complete, false)
    }
    const fixture = evidenceFixture()
    fixture.desktopLog.complete = false
    assert.notEqual(evaluateFirstSendEvidence(fixture).failures.length, 0)
  })

  test('listeners attach before readiness to existing and later pages and keep cleanup errors', () => {
    let phase = 'electron-launch-start'
    const first = new EventEmitter()
    const mainFrame = {}
    first.mainFrame = () => mainFrame
    first.frames = () => [mainFrame]
    const context = new EventEmitter()
    context.pages = () => [first]
    const observed = observeRendererPages(context, () => phase)
    const error = () => ({ type: () => 'error', text: () => 'synthetic error',
      location: () => ({ url: source, lineNumber: 41 }) })
    first.emit('pageerror', new Error('startup before connected'))
    first.emit('console', error())
    const second = new EventEmitter()
    second.mainFrame = () => mainFrame
    second.frames = () => [mainFrame]
    context.emit('page', second)
    phase = 'electron-cleanup-start'
    second.emit('console', error())
    first.emit('pageerror', new Error('cleanup failure'))
    assert.deepEqual(observed.consoleErrorDetails.map(record => [record.pageId, record.phase, record.line]),
      [[1, 'electron-launch-start', 42], [2, 'electron-cleanup-start', 42]])
    assert.deepEqual(observed.pageErrorDetails.map(record => record.message), ['startup before connected', 'cleanup failure'])
  })

  test('subframe history survives detach and invalidates earlier or later cancellation attribution', () => {
    for (const emitBeforeDetach of [true, false]) {
      const page = new EventEmitter()
      const mainFrame = {}
      page.mainFrame = () => mainFrame
      page.frames = () => [mainFrame]
      const context = new EventEmitter()
      context.pages = () => [page]
      const observed = observeRendererPages(context, () => 'electron-cleanup-start')
      const frame = {}
      const emit = () => page.emit('console', { type: () => 'error', text: () => SHUTDOWN_CANCELLATION,
        location: () => ({ url: source, lineNumber: 41 }) })
      page.emit('frameattached', frame)
      if (emitBeforeDetach) emit()
      page.emit('framedetached', frame)
      if (!emitBeforeDetach) emit()
      assert.deepEqual([...observed.subframePageIds], [1])
      assert.equal(observed.consoleErrorDetails[0].frameIsolationProven, false)
      const fixture = evidenceFixture()
      fixture.observation.subframePageIds = [...observed.subframePageIds]
      assert.notEqual(evaluateFirstSendEvidence(fixture).failures.length, 0)
    }
    const noErrors = evidenceFixture(0)
    noErrors.observation.subframePageIds = [1]
    assert.deepEqual(evaluateFirstSendEvidence(noErrors).failures, [], 'frames alone are not renderer errors')
  })
}
