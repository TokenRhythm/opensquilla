import { createHash } from 'node:crypto'
import { readFile, readdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import { expectedShutdownCancellationIndices } from './packaged-first-send-cleanup.mjs'

export const FIRST_SEND_REPORT_VERSION = 2
export const MAIN_CONSOLE_JOURNAL = 'first-send-main-console.jsonl'
export const SHUTDOWN_CANCELLATION = '[useSessions] session directory error: Connection closed'
export const FORBIDDEN_RENDERER_ERROR = /(?:emitsOptions|\bexposed\b|nextSibling|getNextHostNode|Teleport\.process|\[ErrorBoundary\])/i
export const PLAYWRIGHT_ELECTRON_SANDBOX_ERRORS = new Set([
  'Electron sandboxed_renderer.bundle.js script failed to run',
  "TypeError: Cannot destructure property 'preloadScripts' of 'binding.startupData' as it is null.",
])

export function consoleSource(value) {
  try {
    const url = new URL(value)
    url.username = ''
    url.password = ''
    url.search = ''
    url.hash = ''
    return url.toString()
  } catch {
    return ''
  }
}

export function parseEvidenceLog(source) {
  const records = []
  let malformedRecords = 0
  for (const line of source.split(/\r?\n/)) {
    if (!line.trim()) continue
    try {
      const record = JSON.parse(line)
      if (!record || Array.isArray(record) || typeof record !== 'object'
        || typeof record.event !== 'string' || !record.event
        || !Number.isFinite(Date.parse(record.at))) throw new Error('Invalid log record')
      records.push(record)
    } catch {
      malformedRecords += 1
      records.push(null)
    }
  }
  return {
    bytes: Buffer.byteLength(source, 'utf8'),
    sha256: createHash('sha256').update(source, 'utf8').digest('hex'),
    complete: source.length > 0 && source.endsWith('\n') && malformedRecords === 0
      && records.every(record => record?.detail_omitted !== true),
    malformedRecords,
    records,
  }
}

export async function readEvidenceLog(path) {
  try {
    const bytes = await readFile(path)
    return parseEvidenceLog(new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes))
  } catch (error) {
    return { ...parseEvidenceLog(''), diagnosticError: error?.code || String(error) }
  }
}

export function summarizeDesktopLog(log) {
  const eventCounts = {}
  const rendererErrors = []
  const quitSteps = []
  let forbiddenErrorCount = 0
  let playwrightSandboxErrorCount = 0
  const expected = expectedShutdownCancellationIndices(log.records)
  const unexpectedRendererIndices = []
  for (const [index, record] of log.records.entries()) {
    if (!record) continue
    eventCounts[record.event] = (eventCounts[record.event] || 0) + 1
    if (FORBIDDEN_RENDERER_ERROR.test(JSON.stringify(record))) forbiddenErrorCount += 1
    if (record.event === 'quit_commit_step') quitSteps.push(record)
    if (record.event !== 'renderer_console') continue
    rendererErrors.push({ index, ...record })
    if (PLAYWRIGHT_ELECTRON_SANDBOX_ERRORS.has(record.message)) playwrightSandboxErrorCount += 1
    else if (!expected.has(index)) unexpectedRendererIndices.push(index)
  }
  return {
    ...log, eventCounts, rendererErrors, quitSteps, forbiddenErrorCount,
    playwrightSandboxErrorCount,
    unexpectedRendererErrorCount: unexpectedRendererIndices.length,
    expectedShutdownCancellationCount: expected.size,
  }
}

export async function readDesktopLogEvidence(userDataDir) {
  const log = await readEvidenceLog(resolve(userDataDir, 'logs', 'desktop.log'))
  try {
    log.rotated = (await readdir(resolve(userDataDir, 'logs')))
      .some(name => /^desktop\.log\.\d+$/.test(name))
    if (log.rotated) log.complete = false
  } catch (error) {
    log.complete = false
    log.diagnosticError ??= error?.code || String(error)
  }
  return summarizeDesktopLog(log)
}

// A console message does not expose a reliable frame identity in Playwright.
// Observe Electron's native event in the isolated candidate, without changing
// application code, and persist each identity before the process exits.
export async function installMainConsoleObservation(app, userDataDir) {
  await app.evaluate(({ app: electronApp, webContents }, journalPath) => {
    const { appendFileSync, writeFileSync } = process.getBuiltinModule('fs')
    writeFileSync(journalPath, '', { flag: 'wx', mode: 0o600 })
    let index = 0
    let cleanup = false
    let writeErrors = 0
    const emit = detail => {
      try {
        appendFileSync(journalPath,
          JSON.stringify({ index: index++, at: new Date().toISOString(), ...detail }) + '\n', 'utf8')
      } catch (error) {
        writeErrors += 1
        throw error
      }
    }
    const observed = new WeakSet()
    const attach = contents => {
      if (observed.has(contents)) return
      observed.add(contents)
      contents.on('console-message', details => {
        if (details.level !== 'error') return
        try {
          let mainFrame = false
          try { mainFrame = details.frame != null && details.frame === contents.mainFrame } catch {}
          let source = ''
          try {
            const url = new URL(details.sourceId)
            url.username = ''; url.password = ''; url.search = ''; url.hash = ''
            source = url.toString()
          } catch {}
          emit({ event: 'console', webContentsId: contents.id, mainFrame,
            phase: cleanup ? 'electron-cleanup' : 'running', level: details.level,
            message: details.message, source, line: details.lineNumber })
        } catch (error) {
          // Observation must not alter the lifecycle. Missing records cannot
          // match and a persisted diagnostic makes the final evidence fail.
          try { emit({ event: 'observation-error', error: String(error) }) } catch {}
        }
      })
    }
    electronApp.on('web-contents-created', (_event, contents) => attach(contents))
    for (const contents of webContents.getAllWebContents()) attach(contents)
    emit({ event: 'observation-start' })
    process.once('exit', code => {
      try { emit({ event: 'observation-exit', code, writeErrors }) } catch {}
    })
    globalThis.__opensquillaFirstSendObservation = {
      markCleanup() {
        if (cleanup) throw new Error('First-send observation cleanup already started')
        cleanup = true
        emit({ event: 'cleanup-start' })
      },
    }
  }, resolve(userDataDir, MAIN_CONSOLE_JOURNAL))
}

export async function markMainConsoleCleanup(app) {
  await app.evaluate(() => {
    if (!globalThis.__opensquillaFirstSendObservation) throw new Error('First-send observation missing')
    globalThis.__opensquillaFirstSendObservation.markCleanup()
  })
}

export function observeRendererPages(context, getPhase) {
  const pages = new Map()
  const subframePageIds = new Set()
  const pageErrorDetails = []
  const consoleErrorDetails = []
  const attach = page => {
    if (pages.has(page)) return
    const pageId = pages.size + 1
    pages.set(page, pageId)
    // Electron 42 can attribute a same-origin subframe console event to its
    // main frame. Once a child frame has existed, never waive that page's
    // console errors, even if it detaches before the error is delivered.
    const markSubframe = frame => {
      if (frame !== page.mainFrame()) subframePageIds.add(pageId)
    }
    page.on('frameattached', markSubframe)
    page.on('framedetached', markSubframe)
    for (const frame of page.frames()) markSubframe(frame)
    page.on('pageerror', error => pageErrorDetails.push({
      pageId, phase: getPhase(), observedAt: new Date().toISOString(),
      message: String(error?.message || error),
    }))
    page.on('console', message => {
      if (message.type() !== 'error') return
      const location = message.location()
      consoleErrorDetails.push({ index: consoleErrorDetails.length, pageId,
        phase: getPhase(), observedAt: new Date().toISOString(), message: message.text(),
        frameIsolationProven: !subframePageIds.has(pageId),
        source: consoleSource(location.url),
        line: Number.isSafeInteger(location.lineNumber) && location.lineNumber >= 0
          ? location.lineNumber + 1 : null,
      })
    })
  }
  context.on('page', attach)
  for (const page of context.pages()) attach(page)
  return { pages, subframePageIds, pageErrorDetails, consoleErrorDetails, attach }
}

function positions(records, event) {
  return records.flatMap((record, index) => record?.event === event ? [index] : [])
}

function exactCancellation(record) {
  return record?.message === SHUTDOWN_CANCELLATION
    && typeof record.source === 'string'
    && record.source.startsWith('opensquilla-app://desktop/')
    && consoleSource(record.source) === record.source
    && Number.isSafeInteger(record.line) && record.line > 0
}

function sameConsole(left, right) {
  return left.message === right.message && left.source === right.source && left.line === right.line
}

export function evaluateFirstSendEvidence({ renderer, desktopLog, observation,
  cleanupSucceeded, externalRendererRequests }) {
  const failures = []
  const fail = (condition, message) => { if (!condition) failures.push(message) }
  const records = desktopLog.records
  const journal = observation.journal
  const consoleRecords = renderer.consoleErrorDetails
  const mainRecords = observation.mainConsoleRecords
  fail(cleanupSucceeded === true, 'cleanup did not complete naturally')
  fail(renderer.pageErrors === 0 && renderer.pageErrorDetails.length === 0, 'renderer page errors')
  fail(renderer.consoleErrors === consoleRecords.length, 'console report count mismatch')
  fail(externalRendererRequests === 0, 'external renderer requests')
  fail(desktopLog.complete === true && desktopLog.malformedRecords === 0, 'incomplete desktop log')
  fail(desktopLog.forbiddenErrorCount === 0, 'forbidden renderer error')
  fail(journal.complete === true && journal.malformedRecords === 0, 'incomplete main console journal')
  fail(observation.completed === true && observation.errors.length === 0, 'incomplete renderer observation')
  fail(Array.isArray(observation.subframePageIds)
    && observation.subframePageIds.every(id => Number.isSafeInteger(id) && id > 0)
    && new Set(observation.subframePageIds).size === observation.subframePageIds.length,
  'invalid subframe observation')
  fail(Number.isSafeInteger(observation.targetPageId) && observation.targetPageId > 0
    && Number.isSafeInteger(observation.targetWebContentsId) && observation.targetWebContentsId > 0,
  'missing target renderer identity')
  fail(journal.records.every((record, index) => record?.index === index), 'journal index mismatch')
  fail(JSON.stringify(mainRecords) === JSON.stringify(journal.records.filter(record => record?.event === 'console')),
    'main console journal mismatch')
  fail(journal.records.every(record => record && ['observation-start', 'cleanup-start', 'observation-exit', 'console'].includes(record.event)),
    'unexpected main observation event')
  const starts = positions(journal.records, 'observation-start')
  const cleanupStarts = positions(journal.records, 'cleanup-start')
  const observationExits = positions(journal.records, 'observation-exit')
  fail(starts.length === 1 && starts[0] === 0 && cleanupStarts.length === 1
    && cleanupStarts[0] > starts[0] && observationExits.length === 1
    && observationExits[0] === journal.records.length - 1
    && observationExits[0] > cleanupStarts[0]
    && journal.records[observationExits[0]].code === 0
    && journal.records[observationExits[0]].writeErrors === 0,
  'invalid observation lifecycle')
  const before = positions(records, 'before_quit')
  const accepted = positions(records, 'quit_gateway_shutdown_requested')
  const exits = positions(records, 'quit_gateway_exit')
  const committed = records.flatMap((record, index) => record?.event === 'desktop_exit_phase'
    && record.to === 'committed' ? [index] : [])
  const naturalQuit = before.length === 1 && accepted.length === 1 && exits.length === 1
    && committed.length === 1 && before[0] < accepted[0] && accepted[0] < exits[0]
    && exits[0] < committed[0] && records[accepted[0]].accepted === true
    && records[accepted[0]].alreadyStopping === false && records[exits[0]].exited === true
    && records[exits[0]].hardTerminated === false
    && records[committed[0]].reason === 'all lifecycle-owned Gateways exited'
  fail(naturalQuit, 'invalid natural Quit evidence')
  fail(!records.some(record => ['renderer_unresponsive', 'renderer_process_gone',
    'renderer_console_suppressed', 'quit_gateway_drain_failed', 'quit_gateway_still_running'].includes(record?.event)
    || (record?.event === 'desktop_exit_phase' && record.to === 'running')),
  'renderer or shutdown failure in desktop log')

  const expected = expectedShutdownCancellationIndices(records)
  const desktopCandidates = records.flatMap((record, index) => record?.event === 'renderer_console'
    && !PLAYWRIGHT_ELECTRON_SANDBOX_ERRORS.has(record.message) ? [index] : [])
  const consoleMatches = []
  // Zip ordered records rather than subtracting a count. Every observation in
  // every stream must identify the same trusted main-frame emission; a child
  // frame, extra window, unknown source, or missing event cannot be cancelled.
  for (let index = 0; index < Math.max(consoleRecords.length, mainRecords.length, desktopCandidates.length); index += 1) {
    const consoleRecord = consoleRecords[index]
    const mainRecord = mainRecords[index]
    const desktopIndex = desktopCandidates[index]
    const desktopRecord = records[desktopIndex]
    if (!naturalQuit || !consoleRecord || !mainRecord || !desktopRecord
      || !expected.has(desktopIndex) || !exactCancellation(consoleRecord)
      || !exactCancellation(mainRecord) || !exactCancellation(desktopRecord)
      || consoleRecord.index !== index || consoleRecord.pageId !== observation.targetPageId
      || consoleRecord.frameIsolationProven !== true || observation.subframePageIds?.length !== 0
      || consoleRecord.phase !== 'electron-cleanup-start'
      || mainRecord.webContentsId !== observation.targetWebContentsId || mainRecord.mainFrame !== true
      || mainRecord.phase !== 'electron-cleanup' || mainRecord.index <= cleanupStarts[0]
      || mainRecord.level !== 'error' || desktopRecord.level !== 'error'
      || !sameConsole(consoleRecord, mainRecord) || !sameConsole(mainRecord, desktopRecord)
      || !Number.isFinite(Date.parse(consoleRecord.observedAt))
      // Both native logs are written synchronously in the same process. The
      // Playwright receipt may be delayed by IPC/runner scheduling, so its wall
      // clock is diagnostic only; identity and ordered coverage do the match.
      || Date.parse(mainRecord.at) < Date.parse(records[accepted[0]].at)
      || Date.parse(mainRecord.at) > Date.parse(records[exits[0]].at)) continue
    consoleMatches.push({ consoleIndex: index, mainRecordIndex: mainRecord.index, desktopRecordIndex: desktopIndex })
  }
  const unexpectedConsoleIndices = consoleRecords.flatMap((_, index) =>
    consoleMatches.some(match => match.consoleIndex === index) ? [] : [index])
  const unexpectedMainRecordIndices = mainRecords.flatMap(record =>
    consoleMatches.some(match => match.mainRecordIndex === record.index) ? [] : [record.index])
  const unexpectedDesktopRecordIndices = desktopCandidates.filter(index =>
    !consoleMatches.some(match => match.desktopRecordIndex === index))
  fail(unexpectedConsoleIndices.length === 0, 'unmatched renderer console errors')
  fail(unexpectedMainRecordIndices.length === 0, 'unmatched main console errors')
  fail(unexpectedDesktopRecordIndices.length === 0, 'unmatched desktop console errors')
  return { version: 1, cleanupSucceeded, consoleMatches, unexpectedConsoleIndices,
    unexpectedMainRecordIndices, unexpectedDesktopRecordIndices, failures }
}
