#!/usr/bin/env node
/**
 * Real Chromium worker for one long-task release-gate browser case.
 *
 * Input and output are temporary JSON files. The result intentionally contains
 * only bounded numeric evidence; page text, prompts, responses, console output,
 * and exception messages are never serialized or printed.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import { chromium } from '@playwright/test'

const COUNT_KEYS = new Set([
  'attachment_inputs',
  'browser_failure_code',
  'cancelled_turns',
  'dispatched_inputs',
  'dom_nodes',
  'incremental_chunks',
  'interruption_notices',
  'mounted_rows',
  'output_bytes',
  'queue_exact_once',
  'queued_inputs',
  'reasoning_pulses',
  'activity_events',
  'stop_phases',
  'subscription_recoveries',
  'transcript_occurrences',
])
let failureCode = 1
const METRIC_KEYS = new Set([
  'anchor_drift_px',
  'activity_latency_ms',
  'bottom_gap_px',
  'first_reasoning_ms',
  'hidden_duration_ms',
  'input_next_paint_max_ms',
  'input_next_paint_p95_ms',
  'max_main_thread_task_ms',
  'max_reasoning_pulse_gap_ms',
  'peak_heap_delta_bytes',
  'post_gc_heap_delta_bytes',
  'subscription_recovery_ms',
])

function argumentsFrom(argv) {
  const result = {}
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index]
    const value = argv[index + 1]
    if (!['--input', '--output'].includes(key) || !value) throw new Error('invalid arguments')
    result[key.slice(2)] = value
  }
  if (!result.input || !result.output) throw new Error('missing arguments')
  return result
}

function safeTemporaryPaths(inputName, outputName) {
  const input = fs.realpathSync(inputName)
  const requestedOutput = path.resolve(outputName)
  const outputParent = fs.realpathSync(path.dirname(requestedOutput))
  const output = path.join(outputParent, path.basename(requestedOutput))
  const temporary = fs.realpathSync(os.tmpdir())
  if (!input.startsWith(`${temporary}${path.sep}`) || !output.startsWith(`${temporary}${path.sep}`)) {
    throw new Error('paths must be temporary')
  }
  if (path.dirname(input) !== path.dirname(output)) throw new Error('paths must share a parent')
  if (!path.basename(path.dirname(input)).startsWith('opensquilla-live-case-')) {
    throw new Error('paths are not owned by live driver')
  }
  const inputStat = fs.lstatSync(input)
  if (!inputStat.isFile() || inputStat.isSymbolicLink() || inputStat.size > 64 * 1024) {
    throw new Error('invalid input file')
  }
  if (fs.existsSync(output)) {
    const outputStat = fs.lstatSync(output)
    if (!outputStat.isFile() || outputStat.isSymbolicLink()) throw new Error('invalid output file')
  }
  return { input, output }
}

function validateInput(raw) {
  const fields = new Set([
    'alternateSessionKey',
    'commandPath',
    'gatewayUrl',
    'marker',
    'prompt',
    'queueMarker',
    'queuePrompt',
    'readyPath',
    'recoveryMarker',
    'recoveryPrompt',
    'scenario',
    'schemaVersion',
    'sessionKey',
    'stopPrompts',
    'timeoutMs',
  ])
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('invalid input')
  if (Object.keys(raw).some(key => !fields.has(key)) || raw.schemaVersion !== 1) {
    throw new Error('unsupported input schema')
  }
  for (const key of [
    'alternateSessionKey', 'commandPath', 'gatewayUrl', 'marker', 'prompt',
    'queueMarker', 'queuePrompt', 'readyPath', 'recoveryMarker', 'recoveryPrompt',
    'scenario', 'sessionKey',
  ]) {
    if (typeof raw[key] !== 'string' || !raw[key]) throw new Error('missing input string')
  }
  if (!Number.isInteger(raw.timeoutMs) || raw.timeoutMs < 1 || raw.timeoutMs > 30 * 60 * 1000) {
    throw new Error('invalid timeout')
  }
  const gateway = new URL(raw.gatewayUrl)
  if (gateway.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(gateway.hostname)) {
    throw new Error('gateway must be loopback HTTP')
  }
  if (!raw.stopPrompts || typeof raw.stopPrompts !== 'object') throw new Error('missing stop prompts')
  return raw
}

function boundedEvidence(status, counts = {}, metrics = {}) {
  const safeCounts = {}
  const safeMetrics = {}
  for (const [key, value] of Object.entries(counts)) {
    if (COUNT_KEYS.has(key) && Number.isInteger(value) && value >= 0) safeCounts[key] = value
  }
  for (const [key, value] of Object.entries(metrics)) {
    if (METRIC_KEYS.has(key) && Number.isFinite(value) && value >= 0) safeMetrics[key] = value
  }
  return { status, counts: safeCounts, metrics: safeMetrics }
}

function writeResult(output, result) {
  const temporary = `${output}.tmp-${process.pid}`
  fs.writeFileSync(temporary, `${JSON.stringify(result)}\n`, { encoding: 'utf8', mode: 0o600, flag: 'wx' })
  fs.chmodSync(temporary, 0o600)
  fs.renameSync(temporary, output)
}

function chatUrl(config, sessionKey) {
  return `${config.gatewayUrl.replace(/\/$/, '')}/control/chat?session=${encodeURIComponent(sessionKey)}`
}

async function installMeasurements(page) {
  await page.addInitScript(() => {
    const state = {
      baselineHeap: 0,
      inputPaint: [],
      longTasks: [],
      maxHeap: 0,
      mutations: 0,
      observerInstalled: false,
      providerActivityEvents: 0,
      reasoningFrameTimes: [],
      firstReasoningFrameAt: 0,
      firstTextFrameAt: 0,
      terminalFrameAt: 0,
    }
    globalThis.__opensquillaLiveMetrics = state
    try {
      const NativeWebSocket = globalThis.WebSocket
      globalThis.WebSocket = class ObservedWebSocket extends NativeWebSocket {
        constructor(...args) {
          super(...args)
          super.addEventListener('message', event => {
            if (typeof event.data !== 'string') return
            let frame
            try { frame = JSON.parse(event.data) } catch { return }
            const observedAt = performance.now()
            if (frame?.event === 'session.event.text_delta' && !state.firstTextFrameAt) {
              state.firstTextFrameAt = observedAt
            }
            if (
              (frame?.event === 'session.event.done' || frame?.event === 'session.event.error')
              && !state.terminalFrameAt
            ) {
              state.terminalFrameAt = observedAt
            }
            if (frame?.event !== 'session.event.provider_activity') return
            state.providerActivityEvents += 1
            if (frame?.payload?.phase !== 'reasoning') return
            state.reasoningFrameTimes.push(observedAt)
            if (!state.firstReasoningFrameAt) state.firstReasoningFrameAt = observedAt
          })
        }
      }
    } catch {}
    try {
      const heap = Number(performance.memory?.usedJSHeapSize || 0)
      state.baselineHeap = heap
      state.maxHeap = heap
      setInterval(() => {
        state.maxHeap = Math.max(state.maxHeap, Number(performance.memory?.usedJSHeapSize || 0))
      }, 250)
    } catch {}
    try {
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) state.longTasks.push(Number(entry.duration || 0))
      }).observe({ entryTypes: ['longtask'] })
    } catch {}
    document.addEventListener('input', event => {
      if (!(event.target instanceof HTMLTextAreaElement)) return
      const started = performance.now()
      requestAnimationFrame(() => requestAnimationFrame(() => {
        state.inputPaint.push(Math.max(0, performance.now() - started))
      }))
    }, true)
    const attach = () => {
      if (state.observerInstalled) return
      const target = document.querySelector('.chat-messages, .chat-message-list, .chat-scroll')
      if (!target) return setTimeout(attach, 100)
      state.observerInstalled = true
      new MutationObserver(records => { state.mutations += records.length }).observe(target, {
        childList: true,
        characterData: true,
        subtree: true,
      })
    }
    document.addEventListener('DOMContentLoaded', attach, { once: true })
  })
}

async function openChat(page, config, sessionKey = config.sessionKey) {
  await page.goto(chatUrl(config, sessionKey), { waitUntil: 'domcontentloaded' })
  await page.locator('.chat-textarea').waitFor({ state: 'visible', timeout: 30_000 })
  await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: 30_000 })
}

async function sendText(page, text) {
  const composer = page.locator('.chat-textarea')
  await composer.fill(text)
  const send = page.locator('.chat-send-btn.btn--primary').first()
  if (await send.isVisible()) {
    await send.click()
  } else {
    // During an active turn the primary send affordance is replaced by Stop;
    // Enter exercises the product's staged follow-up shortcut instead.
    await composer.press('Enter')
  }
}

async function waitForRunning(page, timeout = 30_000) {
  await page.locator('.chat-send-btn.btn--danger, .assistant-activity--live, .msg-ai')
    .first().waitFor({ state: 'visible', timeout })
}

async function waitForText(page, selector, needle, timeout) {
  await page.waitForFunction(
    ({ selector: candidate, needle: expected }) => Array.from(document.querySelectorAll(candidate))
      .some(node => String(node.textContent || '').includes(expected)),
    { selector, needle },
    { timeout },
  )
}

async function waitForTerminal(page, marker, timeout) {
  await waitForText(page, '.msg-ai', marker, timeout)
  failureCode = 571
  await page.waitForFunction(
    () => !document.querySelector('.chat-send-btn.btn--danger'),
    undefined,
    { timeout },
  )
  failureCode = 572
}

async function typeDuringStreaming(page) {
  const composer = page.locator('.chat-textarea')
  for (let index = 0; index < 30; index += 1) {
    await composer.fill(`synthetic composer responsiveness probe ${index}`)
    await page.waitForTimeout(20)
  }
  await composer.fill('')
}

async function resetMeasurements(page) {
  await page.evaluate(() => {
    const state = globalThis.__opensquillaLiveMetrics
    if (!state) throw new Error('measurement state absent')
    try { globalThis.gc?.() } catch {}
    const heap = Number(performance.memory?.usedJSHeapSize || 0)
    state.baselineHeap = heap
    state.maxHeap = heap
    state.inputPaint = []
    state.longTasks = []
    state.mutations = 0
    state.providerActivityEvents = 0
    state.reasoningFrameTimes = []
    state.firstReasoningFrameAt = 0
    state.firstTextFrameAt = 0
    state.terminalFrameAt = 0
  })
}

async function runLongReasoning(page, config) {
  failureCode = 61
  await resetMeasurements(page)
  const sentAt = await page.evaluate(() => performance.now())
  failureCode = 62
  await sendText(page, config.prompt)
  failureCode = 63
  await waitForRunning(page)
  failureCode = 64
  await page.waitForFunction(
    () => Number(globalThis.__opensquillaLiveMetrics?.firstReasoningFrameAt || 0) > 0,
    undefined,
    { timeout: config.timeoutMs },
  )
  const firstReasoningAt = await page.evaluate(
    () => Number(globalThis.__opensquillaLiveMetrics?.firstReasoningFrameAt || 0),
  )
  failureCode = 65
  await page.waitForFunction(() => {
    const expression = /thinking|reasoning|思考|推理/i
    return Array.from(document.querySelectorAll('.assistant-activity--live, .assistant-activity'))
      .some(node => expression.test(String(node.textContent || '')))
  }, undefined, { timeout: 1_000 })
  const visibleAt = await page.evaluate(() => performance.now())
  failureCode = 66
  await waitForTerminal(page, config.marker, config.timeoutMs)
  const activity = await page.evaluate(() => {
    const state = globalThis.__opensquillaLiveMetrics || {}
    const times = Array.isArray(state.reasoningFrameTimes) ? state.reasoningFrameTimes : []
    let maxGap = 0
    for (let index = 1; index < times.length; index += 1) {
      maxGap = Math.max(maxGap, Number(times[index]) - Number(times[index - 1]))
    }
    if (times.length) {
      const reasoningEnd = Number(state.firstTextFrameAt || state.terminalFrameAt || performance.now())
      maxGap = Math.max(maxGap, Math.max(0, reasoningEnd - Number(times[times.length - 1])))
    }
    return {
      activityEvents: Number(state.providerActivityEvents || 0),
      maxGap,
      reasoningPulses: times.length,
    }
  })
  return boundedEvidence('passed', {
    activity_events: activity.activityEvents,
    reasoning_pulses: activity.reasoningPulses,
  }, {
    activity_latency_ms: Math.max(0, visibleAt - firstReasoningAt),
    first_reasoning_ms: Math.max(0, firstReasoningAt - sentAt),
    max_reasoning_pulse_gap_ms: Math.max(0, activity.maxGap),
  })
}

async function beginAnchorProbe(page) {
  await page.waitForFunction(() => {
    const scroller = document.querySelector('.chat-thread')
    return scroller && scroller.scrollHeight > scroller.clientHeight + 256
  }, undefined, { timeout: 60_000 })
  const scroller = page.locator('.chat-thread')
  await scroller.hover()
  await page.mouse.wheel(0, -512)
  await page.waitForFunction(() => {
    const thread = document.querySelector('.chat-thread')
    return thread
      && thread.scrollHeight - thread.clientHeight - thread.scrollTop >= 128
  }, undefined, { timeout: 10_000 })
  return page.evaluate(() => {
    const scroller = document.querySelector('.chat-thread')
    if (!(scroller instanceof HTMLElement)) throw new Error('chat scroller absent')
    return {
      scrollTop: scroller.scrollTop,
      scrollHeight: scroller.scrollHeight,
    }
  })
}

async function finishAnchorProbe(page, probe) {
  const drift = await page.evaluate(({ scrollTop }) => {
    const scroller = document.querySelector('.chat-thread')
    if (!(scroller instanceof HTMLElement)) throw new Error('chat scroller absent')
    return Math.abs(scroller.scrollTop - scrollTop)
  }, probe)
  await page.evaluate(() => {
    const scroller = document.querySelector('.chat-thread')
    if (!(scroller instanceof HTMLElement)) throw new Error('chat scroller absent')
    scroller.scrollTop = scroller.scrollHeight
    scroller.dispatchEvent(new Event('scroll'))
  })
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  return drift
}

async function measurementSnapshot(page) {
  return page.evaluate(() => {
    const state = globalThis.__opensquillaLiveMetrics || {}
    const paints = Array.isArray(state.inputPaint) ? [...state.inputPaint].sort((a, b) => a - b) : []
    const percentile = paints.length ? paints[Math.min(paints.length - 1, Math.ceil(paints.length * 0.95) - 1)] : 0
    const scroller = document.querySelector('.chat-thread')
    const bottomGap = scroller
      ? Math.abs(scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop)
      : 0
    const currentHeap = Number(performance.memory?.usedJSHeapSize || 0)
    try { globalThis.gc?.() } catch {}
    const postGc = Number(performance.memory?.usedJSHeapSize || currentHeap)
    return {
      counts: {
        dom_nodes: document.querySelectorAll('*').length,
        incremental_chunks: Number(state.mutations || 0),
        mounted_rows: document.querySelectorAll('[data-testid="chat-message-row"]').length,
      },
      metrics: {
        bottom_gap_px: Math.max(0, bottomGap),
        input_next_paint_max_ms: paints.length ? Math.max(...paints) : 0,
        input_next_paint_p95_ms: Number(percentile || 0),
        max_main_thread_task_ms: Array.isArray(state.longTasks) && state.longTasks.length
          ? Math.max(...state.longTasks)
          : 0,
        peak_heap_delta_bytes: Math.max(0, Number(state.maxHeap || currentHeap) - Number(state.baselineHeap || 0)),
        post_gc_heap_delta_bytes: Math.max(0, postGc - Number(state.baselineHeap || 0)),
      },
    }
  })
}

async function assistantOutputBytes(page) {
  return page.locator('.msg-ai').last().evaluate(node => (
    new TextEncoder().encode(String(node.textContent || '')).byteLength
  ))
}

async function requestLifecycle(config, action) {
  fs.rmSync(config.readyPath, { force: true })
  fs.writeFileSync(config.commandPath, `${JSON.stringify({ action })}\n`, {
    encoding: 'utf8', mode: 0o600, flag: 'wx',
  })
  const deadline = Date.now() + 60_000
  while (!fs.existsSync(config.readyPath)) {
    if (Date.now() >= deadline) throw new Error('lifecycle timeout')
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  fs.rmSync(config.readyPath, { force: true })
}

async function waitForRecovered(page) {
  const started = performance.now()
  await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: 30_000 })
  return Math.max(0, performance.now() - started)
}

async function runLongAnswer(page, config) {
  failureCode = 51
  await resetMeasurements(page)
  failureCode = 52
  await sendText(page, config.prompt)
  failureCode = 53
  await waitForRunning(page)
  failureCode = 54
  await typeDuringStreaming(page)
  failureCode = 55
  const anchorProbe = await beginAnchorProbe(page)
  failureCode = 56
  await page.waitForFunction((initialHeight) => {
    const scroller = document.querySelector('.chat-thread')
    return scroller && scroller.scrollHeight >= initialHeight + 32
  }, anchorProbe.scrollHeight, { timeout: 60_000 })
  failureCode = 57
  await waitForTerminal(page, config.marker, config.timeoutMs)
  failureCode = 58
  const anchorDrift = await finishAnchorProbe(page, anchorProbe)
  failureCode = 59
  const snapshot = await measurementSnapshot(page)
  snapshot.counts.output_bytes = await assistantOutputBytes(page)
  snapshot.metrics.anchor_drift_px = anchorDrift
  return boundedEvidence('passed', snapshot.counts, snapshot.metrics)
}

async function runRefresh(page, config) {
  await sendText(page, config.prompt)
  await waitForRunning(page)
  await page.waitForTimeout(500)
  const started = performance.now()
  await page.reload({ waitUntil: 'domcontentloaded' })
  await waitForRecovered(page)
  const recovery = Math.max(0, performance.now() - started)
  await waitForTerminal(page, config.marker, config.timeoutMs)
  const snapshot = await measurementSnapshot(page)
  snapshot.counts.subscription_recoveries = 1
  snapshot.metrics.subscription_recovery_ms = recovery
  return boundedEvidence('passed', snapshot.counts, snapshot.metrics)
}

async function runWebSocketInterrupt(page, context, config) {
  await sendText(page, config.prompt)
  await waitForRunning(page)
  await context.setOffline(true)
  await page.waitForTimeout(30_000)
  const started = performance.now()
  await context.setOffline(false)
  const recovery = await waitForRecovered(page)
  // If history reconciliation completed the first turn while offline, a new
  // turn proves the recovered subscription still delivers incremental output.
  await sendText(page, config.recoveryPrompt)
  await waitForTerminal(page, config.recoveryMarker, config.timeoutMs)
  const snapshot = await measurementSnapshot(page)
  snapshot.counts.subscription_recoveries = 1
  snapshot.metrics.subscription_recovery_ms = Math.min(recovery, performance.now() - started)
  return boundedEvidence('passed', snapshot.counts, snapshot.metrics)
}

async function runGatewayRestart(page, config, forced) {
  await sendText(page, config.prompt)
  await waitForRunning(page)
  await requestLifecycle(config, forced ? 'restart_forced' : 'restart_graceful')
  const recovery = await waitForRecovered(page)
  let interruptionNotices = 0
  if (forced) {
    const interruption = page.locator(
      '[data-testid="turn-outcome-interrupted"], .assistant-activity--interrupted',
    ).first()
    await interruption.waitFor({ state: 'visible', timeout: 30_000 })
    interruptionNotices = await interruption.count()
  }
  await sendText(page, config.recoveryPrompt)
  await waitForTerminal(page, config.recoveryMarker, config.timeoutMs)
  const snapshot = await measurementSnapshot(page)
  snapshot.counts.subscription_recoveries = 1
  snapshot.counts.interruption_notices = interruptionNotices
  snapshot.metrics.subscription_recovery_ms = recovery
  return boundedEvidence('passed', snapshot.counts, snapshot.metrics)
}

async function runHidden(page, context, config) {
  await sendText(page, config.prompt)
  await waitForRunning(page)
  const cover = await context.newPage()
  await cover.goto('about:blank')
  await cover.bringToFront()
  const started = performance.now()
  await page.waitForTimeout(11 * 60 * 1000)
  const hiddenDuration = performance.now() - started
  await page.bringToFront()
  await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: 30_000 })
  await waitForTerminal(page, config.marker, config.timeoutMs)
  await cover.close()
  const snapshot = await measurementSnapshot(page)
  snapshot.metrics.hidden_duration_ms = hiddenDuration
  return boundedEvidence('passed', snapshot.counts, snapshot.metrics)
}

async function dropSyntheticAttachment(page) {
  await page.evaluate(() => {
    const transfer = new DataTransfer()
    transfer.items.add(new File(['synthetic queued attachment'], 'queued-synthetic.txt', {
      type: 'text/plain',
    }))
    const target = document.querySelector('.chat')
    if (!target) throw new Error('chat target absent')
    target.dispatchEvent(new DragEvent('drop', {
      bubbles: true,
      cancelable: true,
      dataTransfer: transfer,
    }))
  })
  await page.locator('.attachment-chip').waitFor({ state: 'visible', timeout: 10_000 })
  await page.locator('.attachment-chip--busy').waitFor({ state: 'detached', timeout: 30_000 })
}

async function runQueue(page, context, config) {
  await sendText(page, config.prompt)
  await waitForRunning(page)
  await dropSyntheticAttachment(page)
  await sendText(page, config.queuePrompt)
  const pending = page.locator('.chat-pending-card').filter({ hasText: config.queueMarker })
  await pending.waitFor({ state: 'visible', timeout: 30_000 })
  let secondPage = null
  if (config.scenario === 'queue_refresh') {
    await page.reload({ waitUntil: 'domcontentloaded' })
    await waitForRecovered(page)
    await page.locator('.chat-pending-card').filter({ hasText: config.queueMarker })
      .waitFor({ state: 'visible', timeout: 30_000 })
  } else if (config.scenario === 'queue_switch_session') {
    await page.goto(chatUrl(config, config.alternateSessionKey), { waitUntil: 'domcontentloaded' })
    await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: 30_000 })
    await page.goto(chatUrl(config, config.sessionKey), { waitUntil: 'domcontentloaded' })
    await page.locator('.chat-pending-card').filter({ hasText: config.queueMarker })
      .waitFor({ state: 'visible', timeout: 30_000 })
  } else if (config.scenario === 'queue_dual_tab') {
    secondPage = await context.newPage()
    await installMeasurements(secondPage)
    await openChat(secondPage, config)
    await secondPage.locator('.chat-pending-card').filter({ hasText: config.queueMarker })
      .waitFor({ state: 'visible', timeout: 30_000 })
  } else if (config.scenario === 'queue_gateway_restart') {
    await requestLifecycle(config, 'restart_graceful')
    await waitForRecovered(page)
  }
  await page.locator('.chat-pending-card').filter({ hasText: config.queueMarker })
    .waitFor({ state: 'detached', timeout: config.timeoutMs })
  await waitForText(page, '.msg-user', config.queueMarker, config.timeoutMs)
  await waitForTerminal(page, config.queueMarker, config.timeoutMs)
  if (secondPage) {
    await waitForText(secondPage, '.msg-user', config.queueMarker, config.timeoutMs)
    await secondPage.close()
  }
  const snapshot = await measurementSnapshot(page)
  Object.assign(snapshot.counts, {
    attachment_inputs: 1,
    dispatched_inputs: 1,
    queue_exact_once: 1,
    queued_inputs: 1,
    transcript_occurrences: 1,
  })
  return boundedEvidence('passed', snapshot.counts, snapshot.metrics)
}

async function waitForPhase(page, phase) {
  if (phase === 'tool') {
    await page.locator('.msg-tool, .tool-part, [data-part-kind="tool"]')
      .first().waitFor({ state: 'visible', timeout: 120_000 })
    return
  }
  if (phase === 'output') {
    await page.locator('.msg-ai').last().waitFor({ state: 'visible', timeout: 120_000 })
    return
  }
  const expression = phase === 'retry'
    ? /retry|rate limit|限流|重试/i
    : /thinking|reasoning|思考|推理/i
  await page.waitForFunction(
    source => {
      const regex = new RegExp(source, 'i')
      return Array.from(document.querySelectorAll('.assistant-activity--live, .assistant-activity'))
        .some(node => regex.test(String(node.textContent || '')))
    },
    expression.source,
    { timeout: 120_000 },
  )
}

async function stopCurrentTurn(page) {
  const stop = page.locator('.chat-send-btn.btn--danger')
  await stop.waitFor({ state: 'visible', timeout: 30_000 })
  await stop.click()
  await stop.waitFor({ state: 'detached', timeout: 30_000 })
}

async function runStopEachPhase(page, config) {
  let stopped = 0
  for (const phase of ['reasoning', 'tool', 'output']) {
    await sendText(page, config.stopPrompts[phase])
    await waitForPhase(page, phase)
    await stopCurrentTurn(page)
    stopped += 1
  }
  await requestLifecycle(config, 'reconfigure_retry')
  await waitForRecovered(page)
  await sendText(page, config.stopPrompts.retry)
  await waitForPhase(page, 'retry')
  await stopCurrentTurn(page)
  stopped += 1
  const snapshot = await measurementSnapshot(page)
  snapshot.counts.stop_phases = stopped
  snapshot.counts.cancelled_turns = stopped
  return boundedEvidence('passed', snapshot.counts, snapshot.metrics)
}

async function execute(config) {
  failureCode = 3
  const browser = await chromium.launch({
    headless: true,
    args: ['--enable-precise-memory-info', '--js-flags=--expose-gc'],
  })
  try {
    const context = await browser.newContext()
    const page = await context.newPage()
    await installMeasurements(page)
    failureCode = 4
    await openChat(page, config)
    failureCode = 5
    if (config.scenario === 'long_reasoning') return await runLongReasoning(page, config)
    if (config.scenario === 'long_answer') return await runLongAnswer(page, config)
    if (config.scenario === 'browser_refresh') return await runRefresh(page, config)
    if (config.scenario === 'browser_websocket_interrupt') {
      return await runWebSocketInterrupt(page, context, config)
    }
    if (config.scenario === 'browser_gateway_graceful_restart') {
      return await runGatewayRestart(page, config, false)
    }
    if (config.scenario === 'browser_gateway_forced_restart') {
      return await runGatewayRestart(page, config, true)
    }
    if (config.scenario === 'browser_hidden_11_minutes') {
      return await runHidden(page, context, config)
    }
    if (config.scenario === 'browser_stop_each_phase') {
      return await runStopEachPhase(page, config)
    }
    if (config.scenario.startsWith('queue_')) return await runQueue(page, context, config)
    throw new Error('unsupported browser scenario')
  } finally {
    await browser.close()
  }
}

let output
try {
  const args = argumentsFrom(process.argv.slice(2))
  const paths = safeTemporaryPaths(args.input, args.output)
  output = paths.output
  failureCode = 2
  const config = validateInput(JSON.parse(fs.readFileSync(paths.input, 'utf8')))
  const result = await execute(config)
  writeResult(output, result)
  process.exitCode = result.status === 'passed' ? 0 : 1
} catch {
  if (output) {
    try { writeResult(output, boundedEvidence('failed', { browser_failure_code: failureCode })) } catch {}
  }
  process.exitCode = 1
}
