import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile, readdir } from 'node:fs/promises'
import { basename, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import {
  environmentWithoutProviderSecrets,
  launchPackagedCandidate,
  requiredOption,
  waitFor,
} from './packaged-smoke-helpers.mjs'

const DEFAULT_ITERATIONS = 20
const SEND_TIMEOUT_MS = 45_000
const FORBIDDEN_RENDERER_ERROR = /(?:emitsOptions|\bexposed\b|nextSibling|getNextHostNode|Teleport\.process|\[ErrorBoundary\])/i
const WIDE_VIEWPORT = { width: 1440, height: 900 }
const TIGHT_VIEWPORT = { width: 900, height: 780 }

function optionalIntegerOption(name, fallback) {
  const index = process.argv.indexOf(name)
  if (index < 0) return fallback
  const value = Number(process.argv[index + 1])
  if (!Number.isSafeInteger(value) || value < 1 || value > 100) {
    throw new Error(`${name} must be an integer between 1 and 100`)
  }
  return value
}

function assertSecretScrubbingBoundary() {
  const source = { SAFE_METADATA: 'retained' }
  Object.defineProperty(source, 'SYNTHETIC_API_KEY', {
    enumerable: true,
    get() {
      throw new Error('provider secret value was read')
    },
  })
  assert.deepEqual(
    environmentWithoutProviderSecrets(source),
    { SAFE_METADATA: 'retained' },
    'provider secret names must be discarded before their values are read',
  )
}

function isLoopbackUrl(value) {
  try {
    const url = new URL(value)
    return url.hostname === '127.0.0.1' || url.hostname === 'localhost' || url.hostname === '::1'
  } catch {
    return false
  }
}

async function startSyntheticOllama() {
  let requestCount = 0
  let chatRequestCount = 0
  const server = createServer((request, response) => {
    requestCount += 1
    const requestUrl = new URL(request.url || '/', 'http://127.0.0.1')

    if (request.method === 'GET' && requestUrl.pathname === '/api/tags') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({
        models: [{
          name: 'opensquilla-packaged-first-send-gate',
          model: 'opensquilla-packaged-first-send-gate',
          modified_at: '2026-01-01T00:00:00Z',
          size: 1,
          digest: 'synthetic-offline-model',
          details: {},
        }],
      }))
      return
    }
    if (request.method === 'GET' && requestUrl.pathname === '/api/version') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ version: '0.0.0-opensquilla-offline-gate' }))
      return
    }
    if (request.method !== 'POST' || requestUrl.pathname !== '/api/chat') {
      response.writeHead(404, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ error: 'unsupported synthetic endpoint' }))
      return
    }

    chatRequestCount += 1
    // Drain without parsing or retaining prompts. The gate only needs protocol
    // conformance and must not persist candidate conversation content.
    request.resume()
    request.once('end', async () => {
      response.writeHead(200, {
        'content-type': 'application/x-ndjson',
        'cache-control': 'no-store',
      })
      response.write(JSON.stringify({
        model: 'opensquilla-packaged-first-send-gate',
        created_at: '2026-01-01T00:00:00Z',
        message: { role: 'assistant', content: 'Synthetic packaged response.' },
        done: false,
      }) + '\n')
      await delay(5)
      response.end(JSON.stringify({
        model: 'opensquilla-packaged-first-send-gate',
        created_at: '2026-01-01T00:00:00Z',
        message: { role: 'assistant', content: '' },
        done: true,
        done_reason: 'stop',
        prompt_eval_count: 8,
        eval_count: 3,
      }) + '\n')
    })
  })

  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  const address = server.address()
  assert.ok(address && typeof address === 'object', 'synthetic provider did not bind a port')
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    counts: () => ({ requestCount, chatRequestCount }),
    close: () => new Promise((resolveClose, rejectClose) => {
      server.closeIdleConnections?.()
      server.close((error) => error ? rejectClose(error) : resolveClose())
    }),
  }
}

async function assertIsolatedUserData(userDataDir) {
  try {
    const entries = await readdir(userDataDir)
    assert.equal(
      entries.length,
      0,
      '--user-data-dir must identify a new or empty isolated directory',
    )
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error
  }
}

async function readDesktopLogSummary(userDataDir) {
  const path = resolve(userDataDir, 'logs', 'desktop.log')
  let source = ''
  try {
    source = await readFile(path, 'utf8')
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error
  }
  const eventCounts = {}
  const rendererErrors = []
  let malformedRecords = 0
  let forbiddenErrorCount = 0
  for (const line of source.split(/\r?\n/)) {
    if (!line.trim()) continue
    if (FORBIDDEN_RENDERER_ERROR.test(line)) forbiddenErrorCount += 1
    try {
      const record = JSON.parse(line)
      const event = typeof record?.event === 'string' ? record.event : 'unknown'
      eventCounts[event] = (eventCounts[event] || 0) + 1
      if (event === 'renderer_console' && rendererErrors.length < 10) {
        rendererErrors.push({
          level: record?.level,
          message: record?.message,
          source: record?.source,
          line: record?.line,
        })
      }
    } catch {
      malformedRecords += 1
    }
  }
  return {
    bytes: Buffer.byteLength(source, 'utf8'),
    eventCounts,
    rendererErrors,
    forbiddenErrorCount,
    malformedRecords,
  }
}

assertSecretScrubbingBoundary()

const executablePath = resolve(requiredOption('--executable'))
const userDataDir = resolve(requiredOption('--user-data-dir'))
const iterations = optionalIntegerOption('--iterations', DEFAULT_ITERATIONS)

let app
let provider
let runError
const pageErrors = []
const consoleErrors = []
const outboundNetwork = []
const rpcSendCounts = new Map()
const rpcSessions = new Map()
let desktopLogSummary

async function browserRpcSnapshot(page) {
  return await page.evaluate(() => {
    const probe = globalThis.__opensquillaP15RpcProbe
    return {
      methods: probe?.methods || {},
      sends: probe?.sends || [],
    }
  })
}

async function observedChatSendCount(page, message) {
  const snapshot = await browserRpcSnapshot(page)
  return snapshot.sends.filter((entry) => entry.message === message).length
}

async function syncObservedChatSends(page) {
  const snapshot = await browserRpcSnapshot(page)
  for (const entry of snapshot.sends) {
    const countInDocument = snapshot.sends.filter(
      candidate => candidate.message === entry.message,
    ).length
    rpcSendCounts.set(entry.message, countInDocument)
    if (entry.sessionKey) rpcSessions.set(entry.message, entry.sessionKey)
  }
  return snapshot
}

async function assertSettledMessageReceipt(page) {
  const assistant = page.locator('.msg-ai').last()
  await waitFor(
    async () => await assistant.count() === 1
      && await assistant.locator('.msg-ai-text').count() === 1,
    'settled canonical assistant answer',
    SEND_TIMEOUT_MS,
  )
  assert.equal(
    await assistant.locator('.msg-ai-text').count(),
    1,
    'the canonical answer must render exactly once',
  )
  const usageTrigger = assistant.locator('.msg-meta__more-btn')
  await usageTrigger.waitFor({ state: 'visible', timeout: SEND_TIMEOUT_MS })
  const order = await assistant.evaluate((element) => {
    const activity = element.querySelector('.assistant-activity')
    const answer = element.querySelector('.assistant-answer, .msg-ai-text')
    const footer = element.querySelector('.msg-ai-footer')
    return {
      activityBeforeAnswer: !activity || !answer
        ? true
        : Boolean(activity.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING),
      answerBeforeFooter: !answer || !footer
        ? false
        : Boolean(answer.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING),
      activityExpanded: activity?.getAttribute('data-share-expanded') || null,
    }
  })
  assert.equal(order.activityBeforeAnswer, true, 'settled activity must remain before the answer')
  assert.equal(order.answerBeforeFooter, true, 'the compact receipt must remain after the answer')
  if (order.activityExpanded !== null) {
    assert.equal(order.activityExpanded, 'false', 'settled activity must collapse automatically')
  }

  assert.equal(await usageTrigger.count(), 1, 'a settled turn must expose one compact usage entry')
  await usageTrigger.click()
  const usagePopover = assistant.locator('.msg-meta-popover')
  await usagePopover.waitFor({ state: 'visible', timeout: SEND_TIMEOUT_MS })
  const usageText = await usagePopover.innerText()
  assert.match(usageText, /opensquilla-packaged-first-send-gate/i)
  assert.match(usageText, /8/)
  assert.match(usageText, /3/)
  assert.equal(
    await assistant.locator('.turn-usage-details, [data-turn-usage-details]').count(),
    0,
    'usage details must not be embedded in the activity disclosure',
  )
  // Keep the pointer outside the hover target so Escape exercises the pinned
  // popover state consistently across Electron's Windows and macOS builds.
  await page.mouse.move(1, 1)
  await page.keyboard.press('Escape')
  await usagePopover.waitFor({ state: 'hidden', timeout: SEND_TIMEOUT_MS })
}

try {
  await assertIsolatedUserData(userDataDir)
  provider = await startSyntheticOllama()
  app = await launchPackagedCandidate({
    executablePath,
    userDataDir,
    baseUrl: provider.baseUrl,
    disableNetworkObservability: true,
    model: 'opensquilla-packaged-first-send-gate',
    scrubProviderSecrets: true,
    env: {
      GITHUB_ACTIONS: '0',
      OPENSQUILLA_LLM_CONTEXT_WINDOW_TOKENS: '131072',
      OPENSQUILLA_TESTING: '0',
      NO_PROXY: '127.0.0.1,localhost,::1',
      no_proxy: '127.0.0.1,localhost,::1',
    },
  })

  await app.context().route((url) => {
    return (url.protocol === 'http:' || url.protocol === 'https:') && !isLoopbackUrl(url.toString())
  }, async (route) => {
    outboundNetwork.push(new URL(route.request().url()).origin)
    await route.abort('blockedbyclient')
  })
  const page = await app.firstWindow({ timeout: 60_000 })
  page.on('pageerror', (error) => pageErrors.push(String(error?.message || error)))
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  await waitFor(() => page.url().includes('/control/chat'), 'candidate Control UI')
  // Observe the renderer's own WebSocket without proxying it. Playwright's
  // routeWebSocket transparent proxy changes the ASGI accept sequence in a
  // packaged Electron app, so the release gate instruments send() in the page
  // before a clean reload and leaves the real Gateway connection untouched.
  await page.addInitScript(() => {
    const probe = { methods: {}, sends: [] }
    Object.defineProperty(globalThis, '__opensquillaP15RpcProbe', {
      configurable: false,
      enumerable: false,
      value: probe,
      writable: false,
    })
    const originalSend = WebSocket.prototype.send
    WebSocket.prototype.send = function opensquillaP15ObservedSend(data) {
      try {
        if (typeof data === 'string') {
          const frame = JSON.parse(data)
          if (frame?.type === 'req' && typeof frame.method === 'string') {
            probe.methods[frame.method] = (probe.methods[frame.method] || 0) + 1
            if (frame.method === 'chat.send') {
              probe.sends.push({
                message: typeof frame.params?.message === 'string' ? frame.params.message : '',
                sessionKey: typeof frame.params?.sessionKey === 'string'
                  ? frame.params.sessionKey
                  : '',
              })
            }
          }
        }
      } catch {
        // The probe is diagnostic only; malformed/non-JSON frames stay intact.
      }
      return originalSend.call(this, data)
    }
  })
  await page.reload({ waitUntil: 'domcontentloaded' })

  for (let iteration = 1; iteration <= iterations; iteration += 1) {
    await page.setViewportSize(iteration % 2 === 1 ? WIDE_VIEWPORT : TIGHT_VIEWPORT)
    const draftUrl = new URL(page.url())
    const alreadyOnEmptyDraft = draftUrl.pathname === '/control/chat/new'
      && draftUrl.search === ''
      && draftUrl.hash === ''
    draftUrl.pathname = '/control/chat/new'
    draftUrl.search = ''
    draftUrl.hash = ''
    // Packaged macOS Electron can report ERR_ABORTED for a redundant
    // same-document navigation immediately after launch. The first iteration
    // already starts on the empty draft; later iterations still exercise the
    // real transition back from a materialized session.
    if (!alreadyOnEmptyDraft) {
      await page.goto(draftUrl.toString(), { waitUntil: 'domcontentloaded' })
    }

    const composer = page.locator('.chat-textarea')
    const header = page.locator('#app-route-header [data-testid="chat-header-actions"]')
    await page.locator('.conn-pill.connected').waitFor({
      state: 'visible',
      timeout: SEND_TIMEOUT_MS,
    })
    await composer.waitFor({ state: 'visible', timeout: SEND_TIMEOUT_MS })
    // The packaged app can expose its initial draft URL before Vue has mounted
    // the permanent route header. Establish the landing-route baseline before
    // asserting that session materialization preserves the same DOM node.
    await header.waitFor({ state: 'attached', timeout: SEND_TIMEOUT_MS })
    assert.equal(
      await header.count(),
      1,
      'Chat routes must synchronously own one permanent route header',
    )
    const landingHeaderNode = await header.elementHandle()
    assert.ok(landingHeaderNode, 'landing route header node must remain mounted')
    assert.equal(await header.isHidden(), true, 'landing route header must be hidden with its node mounted')

    const firstMessage = `Synthetic first send ${String(iteration).padStart(2, '0')}`
    await composer.fill(firstMessage)
    const sendButton = page.locator('.chat-send-btn.btn--primary')
    await waitFor(async () => await sendButton.count() === 1 && !await sendButton.isDisabled(), 'enabled first send')
    await sendButton.click()

    await waitFor(
      () => observedChatSendCount(page, firstMessage).then(count => count === 1),
      `first chat.send ${iteration}`,
    )
    await syncObservedChatSends(page)
    await waitFor(() => /\/control\/chat\?session=/.test(page.url()), `session materialization ${iteration}`, SEND_TIMEOUT_MS)
    assert.equal(await page.locator('#app-route-header').count(), 1)
    assert.equal(await page.locator('.chat').count(), 1)
    assert.equal(await page.locator('.chat-textarea').count(), 1)
    assert.equal(await page.locator('.error-boundary').count(), 0)
    assert.equal(await header.count(), 1)
    assert.equal(await header.isVisible(), true)
    const materializedHeaderNode = await header.elementHandle()
    assert.ok(materializedHeaderNode, 'materialized route header node must exist')
    assert.equal(
      await landingHeaderNode.evaluate((node, candidate) => node === candidate, materializedHeaderNode),
      true,
      'route materialization must preserve the header DOM identity',
    )
    await waitFor(
      async () => await page.locator('.chat-send-btn.btn--primary').count() === 1
        && !await page.locator('.chat-send-btn.btn--primary').isDisabled(),
      `first turn terminal state ${iteration}`,
      SEND_TIMEOUT_MS,
    )
    await assertSettledMessageReceipt(page)

    const followupMessage = `Synthetic follow-up ${String(iteration).padStart(2, '0')}`
    await composer.fill(followupMessage)
    await page.locator('.chat-send-btn.btn--primary').click()
    await waitFor(
      () => observedChatSendCount(page, followupMessage).then(count => count === 1),
      `follow-up chat.send ${iteration}`,
    )
    await syncObservedChatSends(page)
    assert.equal(
      rpcSessions.get(followupMessage),
      rpcSessions.get(firstMessage),
      'follow-up must retain the materialized session identity',
    )
    await waitFor(
      async () => await page.locator('.chat-send-btn.btn--primary').count() === 1
        && !await page.locator('.chat-send-btn.btn--primary').isDisabled(),
      `follow-up terminal state ${iteration}`,
      SEND_TIMEOUT_MS,
    )
    await assertSettledMessageReceipt(page)
    assert.equal(rpcSendCounts.get(firstMessage), 1)
    assert.equal(rpcSendCounts.get(followupMessage), 1)
    assert.equal(await page.locator('.error-boundary').count(), 0)
    assert.equal(await page.locator('#app-route-header').count(), 1)
    assert.equal(await page.locator('.chat').count(), 1)
    assert.equal(await page.locator('.chat-textarea').count(), 1)
  }

  assert.equal(pageErrors.length, 0, `renderer page errors: ${pageErrors.length}`)
  assert.equal(consoleErrors.length, 0, `renderer console errors: ${consoleErrors.length}`)
  assert.equal(outboundNetwork.length, 0, `unexpected external renderer requests: ${outboundNetwork.length}`)
  await syncObservedChatSends(page)
  for (const [message, count] of rpcSendCounts) {
    assert.equal(count, 1, `duplicate chat.send for ${message.slice(0, 18)}`)
  }
  assert.equal(rpcSendCounts.size, iterations * 2)
  assert.equal(
    new Set(rpcSessions.values()).size,
    iterations,
    'each new-task iteration must materialize one distinct session',
  )
} catch (error) {
  runError = error
} finally {
  await app?.close().catch(() => {})
  await provider?.close().catch(() => {})
  desktopLogSummary = await readDesktopLogSummary(userDataDir)
}

if (runError) {
  console.error(JSON.stringify({
    ok: false,
    iterations,
    completedChatSends: rpcSendCounts.size,
    provider: provider?.counts(),
    renderer: {
      pageErrors: pageErrors.length,
      consoleErrors: consoleErrors.length,
      consoleErrorMessages: consoleErrors.slice(0, 10),
    },
    externalRendererRequests: outboundNetwork.length,
    desktopLog: desktopLogSummary,
  }, null, 2))
  throw runError
}

assert.equal(desktopLogSummary.forbiddenErrorCount, 0, 'desktop.log contains a forbidden renderer failure')
assert.equal(desktopLogSummary.eventCounts.renderer_console || 0, 0, 'desktop.log contains renderer console errors')
assert.equal(desktopLogSummary.eventCounts.renderer_unresponsive || 0, 0, 'renderer became unresponsive')
assert.equal(
  provider?.counts().chatRequestCount,
  iterations * 2,
  'each accepted chat.send must complete exactly one synthetic provider request',
)

console.log(JSON.stringify({
  ok: true,
  executable: basename(executablePath),
  iterations,
  viewports: { wide: Math.ceil(iterations / 2), tight: Math.floor(iterations / 2) },
  rpc: { chatSend: rpcSendCounts.size, uniqueSessions: new Set(rpcSessions.values()).size },
  provider: provider?.counts(),
  renderer: { pageErrors: pageErrors.length, consoleErrors: consoleErrors.length },
  externalRendererRequests: outboundNetwork.length,
  desktopLog: desktopLogSummary,
}, null, 2))
