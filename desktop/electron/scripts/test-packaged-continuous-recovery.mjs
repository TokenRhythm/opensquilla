import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import { createServer as createTcpServer } from 'node:net'
import { readFile, readdir, mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { DatabaseSync } from 'node:sqlite'
import { setTimeout as delay } from 'node:timers/promises'
import { launchWithProtocolRegistrationGuard } from './packaged-recovery-inspector.mjs'
import {
  DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS, environmentWithoutProviderSecrets,
  requiredOption, waitFor, writeSyntheticCredential,
} from './packaged-smoke-helpers.mjs'
import {
  captureElectronProcessIdentity, cleanupPackagedFirstSend, electronProcessSnapshot,
} from './packaged-first-send-cleanup.mjs'

// Actual packaged Electron, renderer assets, frozen Gateway, SQLite and provider
// adapter. Only the loopback Ollama service and delayed client receipts are
// synthetic. No dirty notice, snapshot, resume proof or stream event is invented.
const executablePath = resolve(requiredOption('--executable'))
const userDataDir = resolve(requiredOption('--user-data-dir'))
const outputDir = resolve(requiredOption('--output-dir'))
const model = 'opensquilla-packaged-continuous-recovery'
const prefix = 'PACKAGED_RECOVERY_START'
const methodAck = 'transport.sessionFlow.update.v2'
const isolationPath = fileURLToPath(new URL('./fixtures/packaged-recovery-isolation.cjs', import.meta.url))
let app, page, provider, processIdentity, failure, result
const phases = []
const rendererDiagnostics = []
const phase = (name, detail = {}) => {
  const entry = { phase: name, at: new Date().toISOString(), ...detail }
  phases.push(entry)
  console.error(JSON.stringify(entry))
}

async function startProvider() {
  const sockets = new Set()
  let active, calls = 0, finished = false, providerError
  const pieces = []
  const server = createServer(async (request, response) => {
    const path = new URL(request.url || '/', 'http://127.0.0.1').pathname
    if (request.method === 'GET' && path === '/api/tags') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ models: [{
        name: model, model, size: 1, digest: 'synthetic', modified_at: '2026-01-01T00:00:00Z',
        details: { family: 'llama', parameter_size: '1B', quantization_level: 'Q4_0' },
        model_info: { 'llama.context_length': 131072 },
      }] }))
      return
    }
    if (request.method === 'GET' && path === '/api/version') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ version: '0.9.0' }))
      return
    }
    if (request.method !== 'POST' || path !== '/api/chat') {
      response.writeHead(404); response.end(); return
    }
    try {
      let body = ''
      for await (const chunk of request) {
        body += chunk
        assert.ok(body.length < 4 * 1024 * 1024, 'bounded synthetic request')
      }
      const value = JSON.parse(body)
      assert.equal(value.model, model)
      assert.equal(++calls, 1, 'one explicit send must invoke one provider request')
      assert.equal(value.stream, true)
      response.writeHead(200, { 'content-type': 'application/x-ndjson' })
      response.flushHeaders()
      active = response
    } catch (error) {
      providerError = error
      response.writeHead(500); response.end()
    }
  })
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)) })
  await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen))
  const chunk = (content, done = false) => JSON.stringify({
    model, created_at: new Date().toISOString(), message: { role: 'assistant', content }, done,
    ...(done ? { done_reason: 'stop', prompt_eval_count: 8, eval_count: pieces.length } : {}),
  }) + '\n'
  return {
    baseUrl: `http://127.0.0.1:${server.address().port}`,
    ready: () => Boolean(active),
    emit(text) {
      assert.ok(active && !finished && !active.destroyed, 'provider must remain streaming')
      pieces.push(text); active.write(chunk(text))
    },
    finish() { assert.ok(active && !finished); finished = true; active.end(chunk('', true)) },
    expected: () => pieces.join('').trim(),
    expectedRaw: () => pieces.join(''),
    status: () => ({ calls, finished, pieces: pieces.length }),
    assertHealthy: () => { if (providerError) throw providerError },
    async close() {
      for (const socket of sockets) socket.destroy()
      await new Promise(resolveClose => server.close(resolveClose))
    },
  }
}

async function unusedLoopbackPort() {
  const server = createTcpServer()
  await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen))
  const { port } = server.address()
  await new Promise(resolveClose => server.close(resolveClose))
  return port
}

function installWireProbe() {
  const NativeWebSocket = globalThis.WebSocket
  const probe = {
    requests: [], responses: [], events: [], hello: null, socketCount: 0,
    target: null, heldAcks: [], heldSnapshot: null, holdAcks: false,
    holdNextSnapshot: false, holdReplayAcks: false, dirtyCount: 0,
    errors: [], raf: {
      running: false, samples: 0, missing: 0, hiddenByStyle: 0,
      regressions: 0, nonPrefixChanges: 0, duplicates: 0, maxLength: 0, maxGapMs: 0,
    },
  }
  globalThis.__packagedRecoveryProbe = probe
  const requests = new Map()
  globalThis.WebSocket = class extends NativeWebSocket {
    constructor(...args) {
      super(...args)
      probe.socketCount++
      this.addEventListener('message', event => {
        if (probe.redispatch) return
        let frame
        try { frame = JSON.parse(String(event.data)) } catch { return }
        if (frame.type === 'hello-ok') probe.hello = {
          protocol: frame.protocol, policy: frame.policy, methods: frame.features?.methods,
        }
        if (frame.type === 'event' && frame.event === 'transport.flow.dirty'
          && frame.payload?.dirty_keys?.includes(probe.target)) {
          probe.dirtyCount++
          if (probe.dirtyCount === 1) probe.holdNextSnapshot = true
        }
        if (frame.type === 'event' && (
          frame.payload?.session_key === probe.target || frame.payload?.key === probe.target
        )) probe.events.push(frame)
        if (frame.type === 'res') {
          const request = requests.get(frame.id)
          if (!request) return
          const record = { ...frame, method: request.method }
          if (request.method === 'sessions.messages.snapshot.read'
            && request.params?.key === probe.target && probe.holdNextSnapshot) {
            probe.holdNextSnapshot = false
            probe.heldSnapshot = { socket: this, data: event.data }
            record.held = true
            event.stopImmediatePropagation()
          }
          probe.responses.push(record)
        }
      })
    }
    send(data) {
      let frame
      try { frame = JSON.parse(String(data)) } catch { /* binary stays untouched */ }
      if (frame?.type === 'req' && frame.method !== 'connect') {
        // The isolated profile contains only synthetic test data. Handshake
        // authentication and arbitrary unknown requests are never retained.
        if (frame.method === 'chat.send') probe.target = frame.params.sessionKey
        if (frame.method.startsWith('sessions.messages.') || frame.method.startsWith('sessions.read.')
          || frame.method.startsWith('sessions.history.') || frame.method === 'chat.history' || frame.method === 'chat.send'
          || frame.method === 'transport.sessionFlow.update.v2') {
          requests.set(frame.id, frame)
          probe.requests.push(frame)
        }
        if (frame.method === 'sessions.messages.resume' && frame.params?.key === probe.target
          && probe.holdReplayAcks) probe.holdAcks = true
        if (frame.method === 'transport.sessionFlow.update.v2'
          && frame.params?.consumed?.length && probe.holdAcks) {
          probe.heldAcks.push({ socket: this, data }); return
        }
      }
      return super.send(data)
    }
  }
  probe.releaseAcks = (keepHolding = false) => {
    probe.holdAcks = keepHolding
    const held = probe.heldAcks.splice(0)
    for (const item of held) NativeWebSocket.prototype.send.call(item.socket, item.data)
    return held.length
  }
  probe.releaseSnapshot = () => {
    const held = probe.heldSnapshot
    if (!held) throw new Error('No real snapshot response is held')
    probe.heldSnapshot = null
    probe.redispatch = true
    try { held.socket.dispatchEvent(new MessageEvent('message', { data: held.data })) }
    finally { probe.redispatch = false }
  }
  probe.startRaf = (expectedPrefix) => {
    const state = probe.raf
    let previousText = ''
    let previousAt = 0
    state.running = true
    const sample = () => {
      if (!state.running) return
      const candidates = [...document.querySelectorAll('.msg-ai-text')]
        .filter(node => node.textContent.includes(expectedPrefix))
      const matches = candidates.filter(node => {
        const rect = node.getBoundingClientRect()
        if (rect.width <= 0 || rect.height <= 0) return false
        for (let owner = node; owner; owner = owner.parentElement) {
          const style = getComputedStyle(owner)
          if (style.display === 'none' || style.visibility === 'hidden'
            || style.visibility === 'collapse' || Number(style.opacity) === 0) return false
        }
        return true
      })
      const text = matches[0]?.textContent.trim() || ''
      const now = performance.now()
      if (previousAt) state.maxGapMs = Math.max(state.maxGapMs, now - previousAt)
      previousAt = now
      state.samples++
      if (matches.length === 0) state.missing++
      if (candidates.length > 0 && matches.length === 0) state.hiddenByStyle++
      if (matches.length > 1) state.duplicates++
      if (text.length < state.maxLength) state.regressions++
      if (previousText && !text.startsWith(previousText)) state.nonPrefixChanges++
      state.maxLength = Math.max(state.maxLength, text.length)
      previousText = text
      requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  }
}

const wire = () => page.evaluate(() => {
  const p = globalThis.__packagedRecoveryProbe
  return {
    requests: p.requests, responses: p.responses, events: p.events, hello: p.hello,
    target: p.target, dirtyCount: p.dirtyCount, socketCount: p.socketCount,
    heldAcks: p.heldAcks.length, heldSnapshot: Boolean(p.heldSnapshot), raf: p.raf,
  }
})
const answerText = () => page.locator('.msg-ai-text').filter({ hasText: prefix.trim() }).innerText({ timeout: 1_000 })
const desktopRecords = async () => (await readFile(resolve(userDataDir, 'logs/desktop.log'), 'utf8'))
  .split(/\r?\n/).flatMap(line => { try { return [JSON.parse(line)] } catch { return [] } })
const gatewayLog = () => readFile(resolve(userDataDir, 'logs/gateway.log'), 'utf8')
const gatewayRecoveryLines = async () => (await gatewayLog()).split(/\r?\n/)
  .filter(line => line.includes('gateway.session_recovery'))

function readCommittedAnswer(sessionKey, committedEvent, expectedText) {
  const path = resolve(userDataDir, 'opensquilla/state/sessions.db')
  // A separate read-only connection observes committed WAL records. Do not use
  // immutable=1, which can hide still-live WAL data, or access app memory caches.
  const database = new DatabaseSync(path, { readOnly: true })
  try {
    database.exec('PRAGMA query_only=ON')
    const session = database.prepare('SELECT session_id, epoch FROM sessions WHERE session_key=?').get(sessionKey)
    assert.ok(session?.session_id, 'actual committed session must exist in SQLite')
    const rows = database.prepare(
      "SELECT id,message_id,content,content_byte_length,turn_context,created_at FROM transcript_entries WHERE session_id=? AND session_key=? AND role='assistant' ORDER BY created_at,id",
    ).all(session.session_id, sessionKey)
    assert.equal(rows.length, 1, 'one explicit synthetic turn must commit exactly one canonical assistant row')
    const row = rows[0]
    assert.equal(row.content, expectedText, 'SQLite canonical content must preserve every provider byte exactly')
    const turnContext = JSON.parse(row.turn_context)
    const committedTurn = committedEvent.payload.turn_id || committedEvent.payload.task_id
    assert.ok(committedTurn, 'actual turn_committed event must name its turn')
    assert.equal(turnContext.turn_id, committedTurn, 'the canonical row must belong to the completed actual task')
    assert.equal(row.content_byte_length, Buffer.byteLength(expectedText, 'utf8'))
    return {
      path, access: 'separate read-only connection including live WAL',
      sessionId: session.session_id, sessionEpoch: session.epoch, turnId: committedTurn,
      rowId: row.id, messageId: row.message_id, assistantRows: rows.length,
      contentBytes: Buffer.byteLength(row.content, 'utf8'),
      contentSha256: createHash('sha256').update(row.content).digest('hex'),
      providerSha256: createHash('sha256').update(expectedText).digest('hex'),
    }
  } finally { database.close() }
}

try {
  try { assert.deepEqual(await readdir(userDataDir), [], 'use a new, empty disposable profile') }
  catch (error) { if (error.code !== 'ENOENT') throw error }
  await mkdir(outputDir, { recursive: true })
  provider = await startProvider()
  await writeSyntheticCredential(userDataDir, {
    baseUrl: provider.baseUrl, model, disableNetworkObservability: true,
  })
  const gatewayPort = await unusedLoopbackPort()
  const inheritedEnvironment = environmentWithoutProviderSecrets(process.env)
  // Caller-specific profile/runtime overrides must never escape this isolated
  // fixture. Main pins its own child profile; explicitly set all gate controls.
  for (const name of Object.keys(inheritedEnvironment)) {
    if (name.startsWith('OPENSQUILLA_') || name === 'ELECTRON_RUN_AS_NODE') delete inheritedEnvironment[name]
  }
  phase('launching', { executablePath, userDataDir })
  const guardedLaunch = await launchWithProtocolRegistrationGuard({
    executablePath,
    args: ['--opensquilla-isolated-recovery-gate',
      '--use-mock-keychain', `--user-data-dir=${userDataDir}`],
    env: {
      ...inheritedEnvironment,
      OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
      OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
      OPENSQUILLA_DESKTOP_GATEWAY_PORT: String(gatewayPort),
      OPENSQUILLA_LLM_CONTEXT_WINDOW_TOKENS: '131072', OPENSQUILLA_LLM_MAX_TOKENS: '4096',
      OPENSQUILLA_NAMING_ENABLED: 'false', OPENSQUILLA_TESTING: '0', GITHUB_ACTIONS: '0',
      NO_PROXY: '127.0.0.1,localhost,::1', no_proxy: '127.0.0.1,localhost,::1',
    },
    timeout: 180_000,
  }, isolationPath)
  app = guardedLaunch.app
  processIdentity = await captureElectronProcessIdentity(app)
  const isolation = await app.evaluate(({ app }) => ({
    isPackaged: app.isPackaged, isolation: globalThis.__packagedRecoveryIsolation,
    version: app.getVersion(), appPath: app.getAppPath(), resourcesPath: process.resourcesPath,
  }))
  assert.equal(isolation.isPackaged, true, 'gate must run the real packaged executable')
  assert.ok(isolation.isolation, 'the test-only OS protocol guard must be installed')
  Object.assign(isolation, {
    preloadPath: isolationPath,
    preloadSha256: createHash('sha256').update(await readFile(isolationPath)).digest('hex'),
    patchedApi: 'electron.app.setAsDefaultProtocolClient',
    startupGuard: guardedLaunch.startupGuard,
  })
  page = await app.firstWindow({ timeout: 60_000 })
  page.on('console', message => {
    if (['warning', 'error'].includes(message.type())) rendererDiagnostics.push({
      at: new Date().toISOString(), type: message.type(), text: message.text(),
    })
  })
  page.on('pageerror', error => rendererDiagnostics.push({
    at: new Date().toISOString(), type: 'pageerror', text: error.stack || String(error),
  }))
  await waitFor(() => page.url().startsWith('opensquilla-app://desktop/chat'), 'packaged chat renderer')
  await waitFor(() => page.evaluate(async () => {
    const desktop = window.opensquillaDesktop
    const [gateway, connection] = await Promise.all([
      desktop.getGatewayStatus(), desktop.getGatewayConnection(),
    ])
    return gateway.owned === true && gateway.status === 'ready' && connection.status === 'ready'
  }), 'owned frozen Gateway readiness', DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + 45_000)
  const readyIsolation = await app.evaluate(({ app }) => ({
    userDataDir: app.getPath('userData'),
    protocolRegistrationAttempts: globalThis.__packagedRecoveryIsolation.protocolRegistrationAttempts,
  }))
  assert.equal(resolve(readyIsolation.userDataDir), userDataDir, 'the actual app must own only the disposable profile')
  assert.ok(readyIsolation.protocolRegistrationAttempts >= 1, 'the OS registration call must be intercepted')
  assert.ok((await desktopRecords()).some(record =>
    record.event === 'deep_link_protocol_registered' && record.registered === false))
  Object.assign(isolation, readyIsolation)
  // No Playwright WS proxy: use the actual renderer WebSocket and delay only
  // its real receipt bytes. Reload only after Electron's first loadURL settled.
  await page.addInitScript(installWireProbe)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.setViewportSize({ width: 1300, height: 850 })
  await page.bringToFront()
  const composer = page.locator('.chat-textarea')
  await composer.waitFor({ state: 'visible', timeout: 45_000 })
  await composer.fill('Synthetic packaged continuous-output recovery acceptance.')
  const send = page.locator('.chat-send-btn.btn--primary')
  await waitFor(async () => !await send.isDisabled(), 'enabled packaged send')
  await send.click()
  await waitFor(provider.ready, 'real Gateway request to the synthetic provider', 45_000)
  provider.emit(prefix)
  await waitFor(async () => (await answerText()).trim() === prefix.trim(), 'first actual streamed text')
  const initial = await wire()
  assert.equal(initial.requests.filter(request => request.method === 'chat.send').length, 1)
  assert.ok(initial.target)
  await waitFor(async () => {
    const current = await wire()
    return current.requests.filter(request => request.method === methodAck)
      .every(request => current.responses.some(response => response.id === request.id && response.ok))
  }, 'initial receipt settling', 5_000)
  await delay(250)
  await page.evaluate((text) => {
    const p = globalThis.__packagedRecoveryProbe
    p.startRaf(text)
    p.holdAcks = true
  }, prefix.trim())
  const baseline = await wire()
  const gatewayRecoveryBaseline = (await gatewayRecoveryLines()).length
  phase('withholding-real-lane-acks', { session: initial.target })
  let sequence = 0
  const emit = async () => {
    provider.emit(` word${String(++sequence).padStart(3, '0')}`)
    await delay(180)
  }
  for (let i = 0; i < 20 && (await wire()).dirtyCount === 0; i++) await emit()
  await waitFor(async () => (await wire()).dirtyCount === 1, 'real lane credit exhaustion')
  await waitFor(async () => (await wire()).heldSnapshot, 'real recovery snapshot response')
  const atDirty = await wire()
  const newLaneEvents = atDirty.events.slice(baseline.events.length)
    .filter(frame => frame.meta?.session_flow_v2)
  assert.equal(newLaneEvents.length, 8, 'actual lane admits precisely 8 unacknowledged frames')
  const snapshot = atDirty.responses.find(response => response.held)
  assert.equal(snapshot.ok, true)
  assert.equal(snapshot.payload.segment_count, 1, 'bounded synthetic snapshot')
  const snapshotValue = JSON.parse(Buffer.from(snapshot.payload.data, 'base64').toString('utf8'))
  assert.ok(snapshotValue.task_id, 'snapshot must retain the active task identity')
  phase('real-dirty-snapshot-held', {
    admittedFrames: newLaneEvents.length, snapshotId: snapshot.payload.snapshot_id,
    snapshotSeq: snapshot.payload.current_stream_seq, sampledFrames: atDirty.raf.samples,
  })
  // Make the original replay span more than one physical 8-frame ACK window.
  for (let i = 0; i < 12; i++) await emit()
  await page.evaluate(() => {
    const p = globalThis.__packagedRecoveryProbe
    p.releaseAcks()
    p.holdReplayAcks = true
    p.releaseSnapshot()
  })
  let replayAckWindows = 0
  for (; replayAckWindows < 12; replayAckWindows++) {
    await waitFor(async () => (await wire()).heldAcks > 0, 'real replay consumption ACK', 10_000)
    // Advance provider output before *every* original replay ACK, the former
    // moving-target recovery loop's trigger. The provider is still unfinished.
    await emit()
    await emit()
    await page.evaluate(() => globalThis.__packagedRecoveryProbe.releaseAcks(true))
    await delay(250)
    if ((await gatewayRecoveryLines()).slice(gatewayRecoveryBaseline)
      .some(line => line.includes('tail_handoff'))) {
      replayAckWindows++
      break
    }
  }
  assert.ok(replayAckWindows >= 2 && replayAckWindows < 12, 'one replay finishes while output advances')
  await page.evaluate(() => {
    const p = globalThis.__packagedRecoveryProbe
    p.holdReplayAcks = false
    p.releaseAcks()
  })
  await waitFor(async () => (await answerText()).trim() === provider.expected(),
    'exact no-loss active recovered answer', 30_000)
  const active = await wire()
  assert.ok(active.raf.samples - atDirty.raf.samples >= 3,
    'animation-frame samples must cover the actual recovery interval')
  assert.equal(provider.status().finished, false)
  assert.equal(active.dirtyCount, 1, 'no repeated dirty recovery')
  const recoveryReads = active.requests.slice(baseline.requests.length)
    .filter(request => request.method === 'sessions.messages.snapshot.read')
  const recoveryResumes = active.requests.slice(baseline.requests.length)
    .filter(request => request.method === 'sessions.messages.resume')
  assert.equal(recoveryReads.length, 1, 'one original recovery snapshot')
  assert.equal(recoveryResumes.length, 1, 'one original recovery resume')
  const proof = active.responses.find(response => response.id === recoveryResumes[0].id)?.payload
  assert.equal(proof.snapshot_id, snapshot.payload.snapshot_id)
  assert.ok(proof.replay_to_seq > snapshot.payload.current_stream_seq + 8)
  assert.ok(!active.events.some(frame => [
    'session.event.done', 'session.event.error', 'task.failed', 'task.cancelled',
  ].includes(frame.event)),
    'recovery must complete before the actual task terminates')
  assert.equal(await page.locator('.chat-stop-btn').isVisible(), true,
    'recovered active task must retain its actual Stop control')
  const replay = active.events.filter(frame => frame.event === 'session.event.text_delta'
    && frame.payload.stream_seq > snapshot.payload.current_stream_seq)
  const streamSequences = replay.map(frame => frame.payload.stream_seq)
  assert.equal(new Set(streamSequences).size, streamSequences.length, 'no duplicate replay sequence')
  assert.deepEqual(streamSequences, [...streamSequences].sort((a, b) => a - b), 'replay stays in sequence order')
  assert.ok(streamSequences.at(-1) > proof.replay_to_seq,
    'real output must cross the original frozen proof before task completion')
  const recoveredPrefix = snapshotValue.events.filter(frame => frame.event === 'session.event.text_delta')
    .map(frame => frame.payload.text).join('')
  assert.equal(recoveredPrefix + replay.map(frame => frame.payload.text).join(''), provider.expectedRaw(),
    'snapshot plus real replay yields exactly all provider bytes')
  phase('recovered-while-provider-active', { replayAckWindows, snapshotId: proof.snapshot_id,
    proofToSeq: proof.replay_to_seq, finalReplaySeq: streamSequences.at(-1),
    sampledFrames: active.raf.samples, recoverySampledFrames: active.raf.samples - atDirty.raf.samples })
  for (let i = 0; i < 8; i++) await emit()
  await waitFor(async () => (await answerText()).trim() === provider.expected(), 'post-recovery live output')
  provider.finish()
  await waitFor(async () => (await wire()).events.some(frame => frame.event === 'session.event.done'),
    'actual task completion', 45_000)
  await waitFor(async () => (await wire()).events.some(frame => frame.event === 'session.event.turn_committed'),
    'actual durable turn commit', 45_000)
  await waitFor(async () => await page.locator('.chat-stop-btn').count() === 0
    && await send.count() === 1, 'terminal composer controls', 45_000)
  await delay(1_000)
  assert.equal((await answerText()).trim(), provider.expected(), 'terminal live/history reconciliation loses no text')
  await page.evaluate(() => { globalThis.__packagedRecoveryProbe.raf.running = false })
  const finalWire = await wire()
  assert.equal(finalWire.raf.missing, 0, 'no animation frame hides readable output')
  assert.equal(finalWire.raf.regressions, 0, 'no animation frame rolls back visible output')
  assert.equal(finalWire.raf.nonPrefixChanges, 0, 'every animation frame preserves the complete prior text')
  assert.equal(finalWire.raf.duplicates, 0, 'no animation frame duplicates the answer')
  assert.ok(finalWire.raf.samples > 50)
  assert.equal(finalWire.dirtyCount, 1)
  assert.equal(finalWire.socketCount, baseline.socketCount, 'recovery keeps the actual transport')
  assert.equal(provider.status().calls, 1)
  provider.assertHealthy()
  const committedEvent = finalWire.events.find(frame => frame.event === 'session.event.turn_committed')
  const committedAnswer = readCommittedAnswer(initial.target, committedEvent, provider.expectedRaw())
  await page.screenshot({ path: resolve(outputDir, 'recovered-terminal.png') })
  // Reload reads the SQLite-backed history through the packaged application.
  await page.reload({ waitUntil: 'domcontentloaded' })
  await waitFor(async () => (await answerText()).trim() === provider.expected(),
    'durable exact answer after full packaged renderer reload', 45_000)
  const logs = await desktopRecords()
  const gatewaySpawn = logs.find(record => record.event === 'gateway_spawned')
  assert.equal(gatewaySpawn?.runtimeMode, 'bundled', 'the actual child must use the bundled frozen runtime')
  assert.equal(resolve(gatewaySpawn.runtimeCommand),
    resolve(dirname(executablePath), 'resources/runtime/gateway/opensquilla-gateway/opensquilla-gateway.exe'))
  const recoveryLogs = logs.filter(record => record.event === 'session_recovery')
  assert.ok(recoveryLogs.some(record => record.reason === 'server_dirty'))
  assert.ok(recoveryLogs.some(record => record.reason === 'recovery_completed'))
  const gatewayLines = (await gatewayRecoveryLines()).slice(gatewayRecoveryBaseline)
  assert.ok(gatewayLines.some(line => line.includes('lane_frames_exhausted')))
  assert.ok(gatewayLines.some(line => line.includes('tail_handoff')))
  const artifacts = [
    executablePath, resolve(dirname(executablePath), 'resources/app.asar'),
    resolve(dirname(executablePath), 'resources/runtime/gateway/opensquilla-gateway/opensquilla-gateway.exe'),
  ]
  const hashes = []
  for (const path of artifacts) hashes.push({
    path, sha256: createHash('sha256').update(await readFile(path)).digest('hex'),
  })
  result = {
    schemaVersion: 1, reportType: 'packaged-continuous-recovery', isolation,
    evidenceBoundary: 'Real packaged Electron/UI/frozen Gateway/SQLite; synthetic loopback Ollama and delayed real client ACK/snapshot receipt.',
    hashes, gatewayPort, gatewaySpawn, session: initial.target, provider: provider.status(), admittedFrames: newLaneEvents.length,
    recoverySnapshots: recoveryReads.length, recoveryResumes: recoveryResumes.length,
    dirtyCount: finalWire.dirtyCount, replayAckWindows, snapshot: snapshot.payload,
    proof, replayStreamSequences: streamSequences, noLossText: provider.expected(),
    animationFrames: {
      ...finalWire.raf,
      phaseSamples: {
        beforeFault: baseline.raf.samples, dirtySnapshotHeld: atDirty.raf.samples,
        recoveredActive: active.raf.samples, terminal: finalWire.raf.samples,
        duringRecovery: active.raf.samples - atDirty.raf.samples,
      },
    },
    durableReloadVerified: true,
    committedAnswer,
    recoveryLogs, gatewayRecoveryLogs: gatewayLines,
  }
  await writeFile(resolve(outputDir, 'wire-evidence.json'), JSON.stringify(finalWire, null, 2))
} catch (error) {
  failure = error
  phase('failed', { error: error.stack || String(error) })
  if (page) {
    await writeFile(resolve(outputDir, 'renderer-diagnostics.json'), JSON.stringify(rendererDiagnostics, null, 2))
    await writeFile(resolve(outputDir, 'failure-wire.json'), JSON.stringify(await wire().catch(() => null), null, 2))
    const presentation = await page.evaluate(() => {
      const components = []
      const seen = new Set()
      for (const element of document.querySelectorAll('*')) {
        for (let instance = element.__vueParentComponent; instance && !seen.has(instance); instance = instance.parent) {
          seen.add(instance)
          const state = instance.setupState || {}
          const name = instance.type?.__name || instance.type?.name
          if (name !== 'ChatView') continue
          const fields = {}
          for (const key of ['sessionKey', 'historySessionKey', 'isStreaming', 'streamBubble', 'answerRevealOpen',
            'presentationMetadataReady', 'livePhase', 'activeStreamTaskId', 'lastStreamSeq',
            'sessionViewCache', 'presentedLive']) {
            try { fields[key] = JSON.parse(JSON.stringify(state[key])) } catch {}
          }
          components.push({ name, keys: Object.keys(state), fields })
        }
      }
      return { url: location.href, hidden: document.hidden, text: document.body.innerText,
        assistantCount: document.querySelectorAll('.msg-ai').length, components }
    }).catch(error => ({ error: String(error) }))
    await writeFile(resolve(outputDir, 'failure-presentation.json'), JSON.stringify(presentation, null, 2))
    await page.screenshot({ path: resolve(outputDir, 'failure.png') }).catch(() => {})
  }
} finally {
  // Finish an active synthetic provider so normal quit does not show an
  // active-task confirmation dialog. Cleanup failures fail the gate too.
  if (provider?.ready() && !provider.status().finished) {
    try { provider.finish(); await delay(1_000) } catch {}
  }
  try {
    // A failed streaming fixture can leave the normal unknown/active-work quit
    // confirmation open. Exercise that actual dialog only in this owned app.
    app?.on('window', async window => {
      try {
        await window.waitForURL(url => url.href.startsWith('data:text/html'), { timeout: 5_000 })
        if (!decodeURIComponent(window.url()).includes('window.opensquillaDesktop.quitDialogRespond')) return
        await window.locator('#confirm').click({ timeout: 5_000 })
        phase('owned-quit-dialog-confirmed')
      } catch { /* Normal bounded cleanup retains its failure backstop. */ }
    })
    await cleanupPackagedFirstSend({
      app, provider, processIdentity,
      diagnostics: () => ({ processes: electronProcessSnapshot(processIdentity) }),
      onPhase: phase,
    })
  } catch (error) { failure ??= error }
}
const report = { ...result, ok: !failure, phases,
  rendererDiagnostics,
  processesAfterCleanup: electronProcessSnapshot(processIdentity),
  ...(failure ? { error: failure.stack || String(failure) } : {}),
}
await mkdir(outputDir, { recursive: true })
await writeFile(resolve(outputDir, 'report.json'), JSON.stringify(report, null, 2))
console.log(JSON.stringify(report, null, 2))
if (failure) process.exitCode = 1
