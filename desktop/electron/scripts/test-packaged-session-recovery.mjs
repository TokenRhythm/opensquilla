import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { basename, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import {
  launchPackagedCandidate,
  requiredOption,
  waitFor,
} from './packaged-smoke-helpers.mjs'
import { assertConcurrentRecoveryTransport } from './session-recovery-transport-contract.mjs'
import { createSessionRecoveryEvidence } from './session-recovery-rpc-evidence.mjs'
import {
  captureElectronProcessIdentity,
  captureFirstSendDiagnostic,
  cleanupPackagedFirstSend,
  electronProcessSnapshot,
} from './packaged-first-send-cleanup.mjs'

const LONG_SESSION_MESSAGE_COUNT = 320
const TERMINAL_RECOVERY_TIMEOUT_MS = 35_000
const SESSION_RECOVERY_TIMEOUT_MS = 30_000

const executablePath = resolve(requiredOption('--executable'))
const userDataDir = resolve(requiredOption('--user-data-dir'))
const sessionKey = requiredOption('--session-key')
const switchSessionKey = requiredOption('--switch-session-key')
const label = requiredOption('--label')
const verifyRecoveredSend = process.argv.includes('--verify-recovered-send')

if (!/^[A-Za-z0-9._-]{1,80}$/.test(label)) {
  throw new Error('Label must contain only ASCII letters, digits, dot, underscore, or dash')
}

const expectedLastMessage =
  `Synthetic retained history message ${String(LONG_SESSION_MESSAGE_COUNT).padStart(4, '0')} (${label})`
const preservedDraft = 'Synthetic draft preserved through packaged session recovery.'
const recoveredReply = `Synthetic live reply after automatic recovery (${label}).`
const recoveryModel = 'opensquilla-release-session-recovery-smoke'

let app
let page
let processIdentity = {}
let runError
let recoveryResult
let injectHang = false
let socketCount = 0
let nextSocketIndex = 0
let healthyCloseCount = 0
let physicalCloseCount = 0
const socketPolicies = new Map()
const healthyNavigationSocketIds = new Set()
const healthySubscribeKeys = []
let heldHistoryRequests = 0
let heldSubscribeRequests = 0
let serverTickCount = 0
const rpcEvidence = createSessionRecoveryEvidence(sessionKey)
let faultReleased = 0
let provider

async function startRecoveryProvider() {
  const sockets = new Set()
  let chatRequests = 0
  let finishResponse
  const errors = []
  const server = createServer(async (request, response) => {
    let stage = 'endpoint'
    try {
      if (request.method === 'GET' && request.url === '/api/tags') {
        response.writeHead(200, { 'content-type': 'application/json' })
        response.end(JSON.stringify({ models: [{ name: recoveryModel, model: recoveryModel,
          modified_at: '2026-01-01T00:00:00Z', size: 1, digest: 'synthetic-recovery',
          details: { context_length: 131_072 } }] }))
        return
      }
      if (request.method === 'GET' && request.url === '/api/version') {
        response.writeHead(200, { 'content-type': 'application/json' })
        response.end(JSON.stringify({ version: '0.0.0-recovery-audit' }))
        return
      }
      assert.ok(request.method === 'POST' && request.url === '/api/chat', 'Unexpected recovery provider endpoint')
      stage = 'request-count'
      assert.equal(++chatRequests, 1, 'Recovery must not submit or retry an additional provider request')
      stage = 'request-body'
      let body = ''
      for await (const chunk of request) {
        body += chunk
        assert.ok(Buffer.byteLength(body) <= 4 * 1024 * 1024, 'Recovery provider request exceeds its limit')
      }
      const payload = JSON.parse(body)
      stage = 'model'
      assert.equal(payload.model, recoveryModel, 'The recovered send must use the loopback model')
      stage = 'explicit-draft'
      const currentUser = payload.messages?.findLast(message => message.role === 'user')?.content
      assert.ok(typeof currentUser === 'string' && currentUser.includes(preservedDraft),
        'The loopback provider must receive the explicitly submitted draft')
      response.writeHead(200, { 'content-type': 'application/x-ndjson', 'cache-control': 'no-store' })
      response.write(JSON.stringify({ model: recoveryModel,
        message: { role: 'assistant', content: recoveredReply }, done: false }) + '\n')
      // The driver must observe a real streamed event and visible answer before
      // allowing completion. A terminal history reload cannot satisfy this proof.
      finishResponse = () => response.end(JSON.stringify({ model: recoveryModel,
        message: { role: 'assistant', content: '' }, done: true, done_reason: 'stop',
        prompt_eval_count: 8, eval_count: 3 }) + '\n')
    } catch {
      // Parser/assertion exceptions can quote request bodies. Retain only a
      // fixed validation stage, never provider messages or prompt fragments.
      errors.push(stage)
      if (!response.headersSent) response.writeHead(422, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ error: 'Synthetic recovery provider rejected the request' }))
    }
  })
  server.on('connection', socket => {
    sockets.add(socket)
    socket.once('close', () => sockets.delete(socket))
  })
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  return {
    baseUrl: `http://127.0.0.1:${server.address().port}`,
    count: () => chatRequests,
    finish() { assert.ok(finishResponse, 'The provider stream must start before completion'); finishResponse() },
    assertHealthy() { assert.deepEqual(errors, [], 'The loopback provider must accept only the explicit turn') },
    async close() {
      await new Promise(resolveClose => { server.close(resolveClose); for (const socket of sockets) socket.destroy() })
    },
  }
}

async function configureSyntheticRecoveryProvider(baseUrl) {
  const configPath = resolve(userDataDir, 'opensquilla', 'config.toml')
  const raw = await readFile(configPath, 'utf8')
  assert.equal(raw.split(/\r?\n/, 1)[0], `# Synthetic ${label} release-preservation profile`,
    'recovered-send verification requires its explicitly seeded synthetic profile')
  const headings = [...raw.matchAll(/^\[llm\]\r?$/gm)]
  assert.equal(headings.length, 1, 'the synthetic profile must have one LLM section')
  const start = headings[0].index
  const next = raw.indexOf('\n[', start + 1)
  const end = next < 0 ? raw.length : next
  const section = raw.slice(start, end)
  assert.match(section, /^provider = "ollama"\r?$/m)
  assert.ok(section.includes(`model = "${recoveryModel}"`), 'the profile must use the synthetic model')
  const updated = section.replace(/^base_url = "http:\/\/127\.0\.0\.1:11434"\r?$/m,
    `base_url = ${JSON.stringify(baseUrl)}`)
  assert.notEqual(updated, section, 'the synthetic baseline endpoint must be present')
  // Existing profile config is authoritative over Desktop's credential cache.
  // Only this disposable send probe redirects its synthetic provider endpoint.
  await writeFile(configPath, raw.slice(0, start) + updated + raw.slice(end), 'utf8')
}

async function captureRecoveryFailure() {
  const directory = resolve(userDataDir, 'logs', 'packaged-session-recovery')
  await mkdir(directory, { recursive: true })
  const ui = page ? await captureFirstSendDiagnostic(() => page.evaluate(() => {
    const composer = document.querySelector('.chat-textarea')
    const send = document.querySelector('.chat-send-btn.btn--primary')
    const notices = [...document.querySelectorAll('[data-testid="chat-session-recovery-status"]')]
    return {
      pathname: location.pathname,
      recoveryStates: notices.map(node => node.getAttribute('data-recovery-state')),
      liveFailureCount: notices.filter(node => node.getAttribute('data-recovery-state') === 'live-degraded').length,
      sendDisabled: send instanceof HTMLButtonElement ? send.disabled : null,
      sendTitle: send?.getAttribute('title'),
      sendAriaLabel: send?.getAttribute('aria-label'),
      composerEditable: composer instanceof HTMLTextAreaElement && !composer.disabled && !composer.readOnly,
      composerFocused: composer === document.activeElement,
      draftLength: composer instanceof HTMLTextAreaElement ? composer.value.length : null,
    }
  })) : { pageUnavailable: true }
  const screenshot = page ? await captureFirstSendDiagnostic(() => page.screenshot({
    path: resolve(directory, 'failure.png'), timeout: 2_500,
  })) : null
  const evidence = {
    label, heldHistoryRequests, heldSubscribeRequests, socketCount, nextSocketIndex,
    physicalCloseCount, serverTickCount, faultReleased,
    ui, rpc: rpcEvidence.snapshot(),
    screenshot: screenshot?.diagnosticError ? screenshot : { captured: Boolean(screenshot) },
  }
  await writeFile(resolve(directory, 'failure.json'), JSON.stringify(evidence, null, 2))
  return { directory, ...evidence }
}

try {
  if (verifyRecoveredSend) {
    provider = await startRecoveryProvider()
    await configureSyntheticRecoveryProvider(provider.baseUrl)
  }
  app = await launchPackagedCandidate({
    executablePath,
    userDataDir,
    model: recoveryModel,
    ...(provider ? { baseUrl: provider.baseUrl, disableNetworkObservability: true, scrubProviderSecrets: true } : {}),
    env: {
      // A release preflight must exercise production deadlines, not the app's
      // ordinary testing shortcuts or mocked timer policy.
      GITHUB_ACTIONS: '0',
      OPENSQUILLA_TESTING: '0',
      // Match the existing first-send gate's declared synthetic model capacity.
      // The retained 320-message fixture must reach this loopback provider;
      // an unknown model's conservative 8K fallback would reject it pre-send.
      ...(provider ? { OPENSQUILLA_LLM_CONTEXT_WINDOW_TOKENS: '131072',
        OPENSQUILLA_LLM_MAX_TOKENS: '4096' } : {}),
    },
  })
  processIdentity = await captureElectronProcessIdentity(app)
  console.error(JSON.stringify({
    event: 'packaged_session_recovery_launched',
    processes: electronProcessSnapshot(processIdentity),
  }))
  await app.context().routeWebSocket(/\/ws$/, (client) => {
    const socketIndex = nextSocketIndex++
    let targetSocketCounted = false
    const countTargetSocket = () => {
      if (targetSocketCounted) return
      targetSocketCounted = true
      socketCount += 1
    }
    const server = client.connectToServer()

    client.onClose(() => {
      physicalCloseCount += 1
      if (!injectHang) healthyCloseCount += 1
    })

    client.onMessage((message) => {
      try {
        const frame = JSON.parse(String(message))
        const held = injectHang && (
          (frame.method === 'chat.history' && frame.params?.sessionKey === sessionKey)
          || (frame.method === 'sessions.messages.subscribe' && frame.params?.key === sessionKey)
        )
        rpcEvidence.request(socketIndex, frame, held)
        if (
          frame?.type === 'req'
          && frame.method === 'sessions.messages.subscribe'
          && !injectHang
          && [sessionKey, switchSessionKey].includes(frame.params?.key)
        ) {
          healthyNavigationSocketIds.add(socketIndex)
          healthySubscribeKeys.push(frame.params.key)
        }
        if (frame?.type === 'req' && injectHang) {
          if (
            frame.method === 'chat.history'
            && frame.params?.sessionKey === sessionKey
          ) {
            countTargetSocket()
            heldHistoryRequests += 1
            return
          }
          if (
            frame.method === 'sessions.messages.subscribe'
            && frame.params?.key === sessionKey
          ) {
            countTargetSocket()
            heldSubscribeRequests += 1
            return
          }
        }
      } catch {
        // Non-JSON protocol frames must remain byte-transparent.
      }
      try {
        server.send(message)
      } catch {
        // Setup navigation or application cleanup can close the peer between
        // the message callback and this forwarding attempt.
      }
    })

    server.onMessage((message) => {
      try {
        const frame = JSON.parse(String(message))
        rpcEvidence.response(socketIndex, frame)
        if (typeof frame?.protocol === 'number') {
          socketPolicies.set(socketIndex, frame.policy)
        }
        if (frame?.type === 'event' && frame.event === 'tick') {
          serverTickCount += 1
        }
      } catch {
        // Non-JSON protocol frames must remain byte-transparent.
      }
      try {
        client.send(message)
      } catch {
        // The client can close while the real Gateway emits a final tick.
      }
    })
  })

  page = await app.firstWindow({ timeout: 60_000 })
  await waitFor(
    () => page.url().startsWith('opensquilla-app://desktop/chat'),
    'candidate Desktop renderer',
  )
  await waitFor(
    async () => (await page.evaluate(
      () => window.opensquillaDesktop?.getGatewayConnection?.(),
    ))?.status === 'ready',
    'candidate Desktop Gateway readiness',
  )
  // The preceding release-upgrade launch can persist this exact chat URL. In
  // that case page.goto() below may not create a new socket, so explicitly
  // reload after installing the context-wide route.
  await page.reload({ waitUntil: 'domcontentloaded' })

  const sessionUrl = new URL(page.url())
  sessionUrl.pathname = '/chat'
  sessionUrl.search = new URLSearchParams({ session: sessionKey }).toString()
  sessionUrl.hash = ''
  await page.goto(sessionUrl.toString(), { waitUntil: 'domcontentloaded' })

  const thread = page.locator('.chat-thread')
  const composer = page.locator('.chat-textarea')
  const sendButton = page.locator('.chat-send-btn.btn--primary')
  const recoveredMessage = page.getByText(expectedLastMessage, { exact: true }).first()
  const sessionRow = key => page.locator(`[data-session-key="${key}"]`)

  await waitFor(
    async () => await recoveredMessage.isVisible() && !await sendButton.isDisabled(),
    'the retained session to become live before healthy navigation',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  // The reload and initial route adoption above are setup, not part of the
  // navigation proof. Start the transport and subscription baselines only
  // after A is live so the assertions below cover exactly A -> B -> A2.
  const healthySocketCountBaseline = nextSocketIndex
  const healthyCloseCountBaseline = healthyCloseCount
  healthyNavigationSocketIds.clear()
  healthySubscribeKeys.length = 0
  await sessionRow(switchSessionKey).locator('.sidebar-history-item').click()
  await waitFor(
    async () => (
      new URL(page.url()).searchParams.get('session') === switchSessionKey
      && healthySubscribeKeys.includes(switchSessionKey)
      && !await sendButton.isDisabled()
    ),
    'the packaged client to switch to the synthetic peer session',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  await sessionRow(sessionKey).locator('.sidebar-history-item').click()
  await waitFor(
    async () => (
      new URL(page.url()).searchParams.get('session') === sessionKey
      && healthySubscribeKeys.filter(key => key === sessionKey).length === 1
      && await recoveredMessage.isVisible()
      && !await sendButton.isDisabled()
    ),
    'the packaged client to return on the original transport',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  const healthyNavigationSample = {
    socketCount: healthyNavigationSocketIds.size,
    newSocketCount: nextSocketIndex - healthySocketCountBaseline,
    closeCount: healthyCloseCount - healthyCloseCountBaseline,
    subscribeKeys: [...healthySubscribeKeys],
  }
  assert.equal(
    healthyNavigationSample.socketCount,
    1,
    'healthy packaged session navigation must keep exactly one WebSocket',
  )
  assert.equal(
    healthyNavigationSample.newSocketCount,
    0,
    'healthy packaged session navigation must not create a replacement WebSocket',
  )
  assert.equal(
    healthyNavigationSample.closeCount,
    0,
    'healthy packaged session navigation must not close the active WebSocket',
  )
  assert.deepEqual(
    healthyNavigationSample.subscribeKeys,
    [switchSessionKey, sessionKey],
    'healthy packaged session navigation must subscribe B and then acquire a fresh A2 lease',
  )
  assert.equal(socketCount, 0, 'healthy navigation must not enter recovery')

  const [recoverySocketIndex] = healthyNavigationSocketIds
  const concurrentHistoryReads = socketPolicies.get(recoverySocketIndex)?.concurrent_history_reads
  assert.equal(
    concurrentHistoryReads,
    true,
    'candidate Gateway hello must advertise concurrent history reads',
  )
  const recoverySocketCountBaseline = nextSocketIndex
  const recoveryCloseCountBaseline = physicalCloseCount
  const recoveryTransportSample = () => assertConcurrentRecoveryTransport({
    concurrentHistoryReads,
    socketCount,
    newSocketCount: nextSocketIndex - recoverySocketCountBaseline,
    closeCount: physicalCloseCount - recoveryCloseCountBaseline,
  })
  injectHang = true
  rpcEvidence.mark('fault-injected')
  await sessionRow(switchSessionKey).locator('.sidebar-history-item').click()
  await waitFor(
    async () => (
      new URL(page.url()).searchParams.get('session') === switchSessionKey
      && !await sendButton.isDisabled()
    ),
    'the peer session before fault injection',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  await sessionRow(sessionKey).locator('.sidebar-history-item').click()
  await waitFor(
    async () => heldHistoryRequests > 0 && heldSubscribeRequests > 0,
    'packaged history and live requests to enter the injected hang',
  )
  await composer.waitFor({ state: 'visible', timeout: 30_000 })

  assert.equal(
    await page.locator('[data-testid="chat-session-load-state"]').count(),
    0,
    'packaged session recovery must never restore the removed blocking load page',
  )
  assert.equal(
    await page.locator(
      '[data-testid="chat-session-recovery-status"][data-recovery-state="history-loading"]',
    ).count(),
    0,
    'routine packaged history loading must not render a recovery notice',
  )
  assert.equal(
    await thread.getAttribute('aria-busy'),
    'false',
    'history recovery must not mark the complete conversation surface busy',
  )
  assert.equal(await composer.isEditable(), true, 'composer must stay editable during recovery')
  await composer.fill(preservedDraft)
  assert.equal(await composer.inputValue(), preservedDraft)
  const recoveryUrl = page.url()
  const retainedComposer = await composer.elementHandle()
  assert.ok(retainedComposer, 'the existing composer must be mounted before recovery')
  assert.equal(await composer.evaluate(node => document.activeElement === node), true,
    'the editable draft owns focus before recovery')
  assert.equal(
    await page.getByText(expectedLastMessage, { exact: true }).count(),
    0,
    'retained history must remain unavailable while its RPC is held',
  )

  const historyFailure = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="history-error"]',
  )
  const liveFailure = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="live-degraded"]',
  )
  const terminalStartedAt = Date.now()
  await waitFor(
    async () => await historyFailure.isVisible() || await liveFailure.isVisible(),
    'packaged session bootstrap to terminate',
    TERMINAL_RECOVERY_TIMEOUT_MS,
  )
  const terminalElapsedMs = Date.now() - terminalStartedAt

  assert.ok(
    terminalElapsedMs <= TERMINAL_RECOVERY_TIMEOUT_MS,
    `packaged recovery exceeded its terminal budget: ${terminalElapsedMs}ms`,
  )
  // Concurrent-read timeouts reject only the held RPC. Recycling this shared
  // socket would interrupt unrelated work and violate the advertised policy.
  const terminalTransport = recoveryTransportSample()
  assert.ok(heldHistoryRequests > 0, 'history hang was not exercised')
  assert.ok(heldSubscribeRequests > 0, 'live subscription hang was not exercised')
  assert.equal(await composer.isEditable(), true)
  assert.equal(await composer.inputValue(), preservedDraft)
  assert.equal(await sendButton.isDisabled(), true, 'live degraded state must fail closed')
  assert.equal(await page.locator('[data-testid="chat-session-recovery-status"]').count(), 1,
    'concurrent domain failures must share one non-blocking recovery notice')

  injectHang = false
  faultReleased = rpcEvidence.mark('fault-released')
  // No click, reload, route change or focus movement may be needed to recover.
  await waitFor(
    () => recoveredMessage.isVisible(),
    'the retained long-session history to recover from the packaged Gateway',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  assert.equal(await historyFailure.count(), 0)

  let recoveredRpc
  await waitFor(
    async () => {
      if (await liveFailure.count() !== 0 || await sendButton.isDisabled()
        || !rpcEvidence.metadataRecovered(faultReleased)) return false
      recoveredRpc = rpcEvidence.assertRecovered(faultReleased)
      return true
    },
    'packaged live subscription and metadata to recover',
    SESSION_RECOVERY_TIMEOUT_MS,
  )

  assert.equal(await composer.inputValue(), preservedDraft)
  assert.equal(await recoveredMessage.isVisible(), true)
  assert.equal(page.url(), recoveryUrl, 'automatic recovery must not navigate the page')
  assert.equal(await composer.evaluate((node, original) => node === original, retainedComposer), true,
    'automatic recovery must preserve the original composer instance')
  assert.equal(await composer.evaluate(node => document.activeElement === node), true,
    'automatic recovery must not move focus away from the draft')
  assert.equal(await thread.getAttribute('aria-busy'), 'false')
  const recoveredTransport = recoveryTransportSample()
  const recoveredViewportSample = await recoveredMessage.evaluate((message) => {
    const threadElement = message.closest('.chat-thread')
    if (!(threadElement instanceof HTMLElement)) return null
    const threadRect = threadElement.getBoundingClientRect()
    const messageRect = message.getBoundingClientRect()
    return {
      scrollTop: Math.round(threadElement.scrollTop),
      scrollHeight: threadElement.scrollHeight,
      clientHeight: threadElement.clientHeight,
      distanceFromBottom: Math.round(
        threadElement.scrollHeight - threadElement.clientHeight - threadElement.scrollTop,
      ),
      messageTop: Math.round(messageRect.top - threadRect.top),
      messageBottom: Math.round(messageRect.bottom - threadRect.top),
      intersectsViewport: (
        messageRect.bottom > threadRect.top
        && messageRect.top < threadRect.bottom
      ),
    }
  })
  assert.equal(
    recoveredViewportSample?.intersectsViewport,
    true,
    'the retained message 0320 must remain inside the recovered conversation viewport',
  )

  let recoveredUserTurn
  if (verifyRecoveredSend) {
    assert.equal(rpcEvidence.sendCount(), 0, 'automatic recovery must never send or resend a user message')
    assert.equal(provider.count(), 0, 'automatic recovery must not invoke the model')
    assert.equal(await page.getByText(expectedLastMessage, { exact: true }).count(), 1,
      'automatic recovery must not duplicate retained history')
    const userAction = rpcEvidence.mark('explicit-user-send')
    await composer.press('Enter')
    const reply = page.locator('.msg-ai-text').filter({ hasText: recoveredReply })
    await waitFor(async () => {
      if (await reply.count() !== 1 || !await reply.isVisible()) return false
      rpcEvidence.assertUserTurn(userAction, { complete: false })
      return true
    }, 'the recovered session to consume a new live streamed reply', SESSION_RECOVERY_TIMEOUT_MS)
    assert.equal((await reply.innerText()).trim(), recoveredReply,
      'the streamed answer must contain the provider text exactly once')
    assert.equal(await composer.inputValue(), '', 'only the explicit send may consume the draft')
    provider.finish()
    await waitFor(async () => {
      if (await sendButton.isDisabled() || await reply.count() !== 1) return false
      recoveredUserTurn = rpcEvidence.assertUserTurn(userAction)
      return true
    }, 'the explicitly sent recovery turn to finish', SESSION_RECOVERY_TIMEOUT_MS)
    // Permit the real turn-committed/history reconciliation to settle, then
    // verify it has not repeated the user send or the visible assistant reply.
    await delay(2_000)
    recoveredUserTurn = rpcEvidence.assertUserTurn(userAction)
    assert.equal(rpcEvidence.sendCount(), 1)
    assert.equal(provider.count(), 1)
    provider.assertHealthy()
    assert.equal(await reply.count(), 1, 'live completion and persisted history must render one answer')
    assert.equal((await reply.innerText()).trim(), recoveredReply,
      'history reconciliation must not append the streamed text a second time')
    assert.equal(await page.getByText(preservedDraft, { exact: true }).count(), 1,
      'the explicit user message must render exactly once')
    assert.equal(page.url(), recoveryUrl)
    assert.equal(await composer.evaluate((node, original) => node === original, retainedComposer), true)
    assert.equal(await page.locator('[data-testid="chat-session-recovery-status"]').count(), 0)
    recoveryTransportSample()
  }

  recoveryResult = {
    ok: true,
    executable: basename(executablePath),
    sessionKey,
    switchSessionKey,
    expectedLastMessage,
    healthyNavigationSocketCount: healthyNavigationSample.socketCount,
    healthyNavigationNewSocketCount: healthyNavigationSample.newSocketCount,
    healthyNavigationCloseCount: healthyNavigationSample.closeCount,
    healthyNavigationSubscribeKeys: healthyNavigationSample.subscribeKeys,
    heldHistoryRequests,
    heldSubscribeRequests,
    socketCount,
    serverTickCount,
    terminalElapsedMs,
    terminalTransport,
    recoveredTransport,
    recoveredViewportSample,
    recoveredRpc,
    ...(recoveredUserTurn ? { recoveredUserTurn, providerChatRequests: provider.count() } : {}),
    rpcEvidence: rpcEvidence.snapshot(),
  }
} catch (error) {
  runError = error
  console.error(JSON.stringify({
    event: 'packaged_session_recovery_failed_before_cleanup',
    error: error?.stack || error?.message || String(error),
  }))
  // Capture before natural process cleanup. Diagnostic failure cannot replace
  // the original contract failure or extend any recovery deadline above.
  console.error(JSON.stringify({
    event: 'packaged_session_recovery_failure_evidence',
    evidence: await captureFirstSendDiagnostic(captureRecoveryFailure, 8_000),
  }))
} finally {
  try {
    await cleanupPackagedFirstSend({
      app,
      processIdentity,
      diagnostics: () => ({ processes: electronProcessSnapshot(processIdentity) }),
      onPhase: (phase, detail = {}) => console.error(JSON.stringify({
        event: 'packaged_session_recovery_cleanup', phase, ...detail,
      })),
    })
  } catch (error) {
    console.error(error)
    // Keep the recovery failure primary when cleanup also fails.
    runError ??= error
  }
  await provider?.close()
}

if (runError) throw runError
console.log(JSON.stringify({
  ...recoveryResult,
  processesAfterCleanup: electronProcessSnapshot(processIdentity),
}, null, 2))
