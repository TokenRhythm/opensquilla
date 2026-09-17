import { expect, test, type Page, type TestInfo, type WebSocketRoute } from '@playwright/test'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './support/gateway-fixture'

const TOKEN = 'synthetic-browser-gateway-token'

async function realAuthGateway(testInfo: TestInfo, origin: string, mode: 'none' | 'token') {
  const root = fileURLToPath(new URL('../..', import.meta.url))
  const output = testInfo.outputPath('auth-gateway')
  await mkdir(output, { recursive: true })
  for (const directory of ['profile', 'appdata', 'local-appdata', 'logs', 'tmp']) {
    await mkdir(join(output, directory), { recursive: true })
  }
  const python = process.env.OPENSQUILLA_WEBUI_E2E_PYTHON || join(
    root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
  )
  const child = spawn(python, [
    '-u', fileURLToPath(new URL('./auth-connection-gateway.py', import.meta.url)),
    origin, output, mode,
  ], {
    cwd: output,
    env: {
      ...Object.fromEntries(['PATH', 'LANG', 'SYSTEMROOT', 'WINDIR'].flatMap(
        key => process.env[key] === undefined ? [] : [[key, process.env[key]]],
      )),
      PYTHONPATH: join(root, 'src'),
      PYTHONNOUSERSITE: '1',
      OPENSQUILLA_STATE_DIR: output,
      OPENSQUILLA_HOME: output,
      OPENSQUILLA_LOG_DIR: join(output, 'logs'),
      OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0',
      OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY: 'true',
      HOME: join(output, 'profile'),
      USERPROFILE: join(output, 'profile'),
      APPDATA: join(output, 'appdata'),
      LOCALAPPDATA: join(output, 'local-appdata'),
      XDG_CONFIG_HOME: join(output, 'profile', 'config'),
      XDG_CACHE_HOME: join(output, 'profile', 'cache'),
      XDG_DATA_HOME: join(output, 'profile', 'data'),
      TMPDIR: join(output, 'tmp'),
      TEMP: join(output, 'tmp'),
      TMP: join(output, 'tmp'),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let log = ''
  child.stdout.on('data', chunk => { log += String(chunk) })
  child.stderr.on('data', chunk => { log += String(chunk) })
  let spawnError: Error | undefined
  child.on('error', error => { spawnError = error })
  const stop = async () => {
    if (child.exitCode === null && child.signalCode === null && child.pid) {
      const exited = once(child, 'exit')
      async function waitForExit() {
        let timer: ReturnType<typeof setTimeout> | undefined
        try {
          return await Promise.race([
            exited.then(() => true),
            new Promise<boolean>(resolve => { timer = setTimeout(() => resolve(false), 5_000) }),
          ])
        } finally { clearTimeout(timer) }
      }
      child.kill('SIGTERM')
      if (!await waitForExit()) {
        child.kill('SIGKILL')
        if (!await waitForExit()) throw new Error('Auth Gateway did not exit after SIGKILL')
      }
    }
    await writeFile(join(output, 'gateway.log'), log)
  }
  try {
    await expect.poll(() => {
      if (spawnError) throw spawnError
      if (child.exitCode !== null) throw new Error(`Auth Gateway stopped: ${log}`)
      return /"authFixturePort":\s*(\d+)/.exec(log)?.[1]
    }, { timeout: 15_000 }).toBeTruthy()
    const port = /"authFixturePort":\s*(\d+)/.exec(log)![1]
    return { wsUrl: `ws://127.0.0.1:${port}/ws`, httpUrl: `http://127.0.0.1:${port}`, stop }
  } catch (error) {
    await stop()
    throw error
  }
}

async function prepare(page: Page, wsUrl: string, token = '') {
  await page.addInitScript(({ endpoint, credential }) => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla.wsUrl', endpoint)
    if (credential) sessionStorage.setItem('opensquilla.wsToken', credential)
  }, { endpoint: wsUrl, credential: token })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
}

function observe(page: Page) {
  const hellos: Array<{
    principal: { authenticated: boolean; authState: string; guestOwnerId?: string }
    guestSessionKey?: string
  }> = []
  const denied: Array<{ method: string; sessionKey: unknown; message: string }> = []
  const sent: Array<{ method: string; sessionKey: unknown }> = []
  let connections = 0
  page.on('websocket', socket => {
    if (new URL(socket.url()).pathname !== '/ws') return
    connections += 1
    const requests = new Map<string, { method: string; sessionKey: unknown }>()
    socket.on('framesent', ({ payload }) => {
      const frame = JSON.parse(String(payload))
      if (frame.type === 'req' && frame.method !== 'connect') {
        requests.set(frame.id, {
          method: frame.method,
          sessionKey: frame.params?.key ?? frame.params?.sessionKey ?? null,
        })
        sent.push(requests.get(frame.id)!)
      }
    })
    socket.on('framereceived', ({ payload }) => {
      const frame = JSON.parse(String(payload))
      if (frame.type === 'hello-ok') hellos.push(frame.auth)
      if (frame.type === 'res' && frame.error?.code === 'UNAUTHORIZED') {
        const request = requests.get(frame.id)
        if (request) denied.push({ ...request, message: frame.error.message })
      }
    })
  })
  return { hellos, denied, sent, get connections() { return connections } }
}

for (const lateHello of [false, true]) {
  test(`subscribes a fresh guest draft after ${lateHello ? 'a delayed' : 'the first'} handshake`, async ({
    page, baseURL, request,
  }, testInfo) => {
    const gateway = await realAuthGateway(testInfo, new URL(baseURL!).origin, 'token')
    try {
      await prepare(page, gateway.wsUrl + (lateHello ? '?holdHandshake=1' : ''))
      const wire = observe(page)
      // The preview serves the relative-base bundle; enter through its existing
      // new-chat URL and let the router canonicalize the draft route.
      await page.goto('/control/chat?newChat=1')
      const input = page.locator('.chat-textarea')
      const send = page.locator('.chat-send-btn[aria-label="Send"]')
      await input.fill('Synthetic guest draft retained through its first handshake.')
      if (lateHello) {
        expect(wire.hellos).toHaveLength(0)
        expect((await request.post(`${gateway.httpUrl}/release-handshake`)).ok()).toBe(true)
      }
      await expect.poll(() => wire.hellos.length).toBe(1)
      await expect(send).toBeEnabled()
      await expect(input).toHaveValue('Synthetic guest draft retained through its first handshake.')
      const owner = wire.hellos[0].principal.guestOwnerId
      expect(owner).toMatch(/^[0-9a-f]{64}$/)
      const subscriptions = () => wire.sent.filter(item => item.method === 'sessions.messages.subscribe')
      await expect.poll(() => subscriptions().length).toBeGreaterThan(0)
      const key = subscriptions().at(-1)!.sessionKey
      expect(key).toMatch(new RegExp(`^agent:main:webchat:guest:${owner}:[a-z0-9]+$`))
      expect(wire.denied.filter(item => item.method === 'sessions.messages.subscribe')).toEqual([])
      expect(wire.sent.filter(item => item.method === 'chat.send')).toEqual([])

      expect((await request.post(`${gateway.httpUrl}/reconnect`)).ok()).toBe(true)
      await expect.poll(() => wire.hellos.length).toBe(2)
      await expect(send).toBeEnabled()
      await expect(input).toHaveValue('Synthetic guest draft retained through its first handshake.')
      expect(subscriptions().at(-1)!.sessionKey).toBe(key)
      expect(wire.hellos[1].principal.guestOwnerId).toBe(owner)
      expect(wire.connections).toBe(2)
      expect(wire.denied.filter(item => item.method === 'sessions.messages.subscribe')).toEqual([])
    } finally { await gateway.stop() }
  })
}

test('keeps an explicit foreign session denied without rewriting it or reconnecting', async ({ page, baseURL }, testInfo) => {
  const gateway = await realAuthGateway(testInfo, new URL(baseURL!).origin, 'token')
  try {
    await prepare(page, gateway.wsUrl)
    const wire = observe(page)
    const foreignSession = 'agent:main:webchat:synthetic-owner-history'
    await page.goto(`/control/chat?session=${encodeURIComponent(foreignSession)}`)
    await expect.poll(() => wire.denied.some(item => (
      item.method === 'sessions.messages.subscribe' && item.sessionKey === foreignSession
    ))).toBe(true)
    await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeDisabled()
    await page.locator('.chat-textarea').fill('Synthetic draft stays in this denied session.')
    await page.clock.install()
    await page.clock.fastForward(120_000)
    expect(new URL(page.url()).searchParams.get('session')).toBe(foreignSession)
    expect(wire.connections).toBe(1)
    expect(wire.sent.filter(item => item.method === 'chat.send')).toEqual([])
  } finally { await gateway.stop() }
})

test('recovers a rejected browser token through the existing connection panel', async ({ page, baseURL }, testInfo) => {
  const gateway = await realAuthGateway(testInfo, new URL(baseURL!).origin, 'token')
  try {
    await page.clock.install()
    await prepare(page, gateway.wsUrl, 'synthetic-rejected-token')
    const wire = observe(page)
    await page.goto('/control/sessions')
    const connection = page.getByRole('button', { name: /^(Manage gateway connection|Connection:)/ })
    await expect(connection).toHaveText('Token required')
    await connection.click()
    await expect(page).toHaveURL(/\/settings\/gateway#connection$/)
    const token = page.locator('#conn-ws-token')
    await expect(token).toBeFocused()
    await expect(page.locator('.conn-status__reason')).toContainText('Automatic retries are paused')
    await token.fill('synthetic-partial-token')
    await page.clock.fastForward(120_000)
    await page.evaluate(() => {
      window.dispatchEvent(new Event('online'))
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await page.clock.runFor(1_000)
    expect(wire.connections).toBe(1)
    await expect(token).toHaveValue('synthetic-partial-token')
    await expect(token).toBeFocused()
    await page.screenshot({ path: testInfo.outputPath('token-recovery.png'), animations: 'disabled' })

    await token.fill(TOKEN)
    await token.press('Enter')
    await expect(page.locator('.conn-status__pill')).toHaveText('Connected')
    await expect.poll(() => wire.hellos.at(-1)?.principal.authenticated).toBe(true)
    expect(wire.connections).toBe(2)
    await expect(page.locator('.conn-status__reason')).not.toContainText('Automatic retries are paused')
  } finally { await gateway.stop() }
})

test('keeps an empty-token tab connected as a legal guest after enabling token auth', async ({ page, baseURL, request }, testInfo) => {
  const gateway = await realAuthGateway(testInfo, new URL(baseURL!).origin, 'none')
  try {
    await prepare(page, gateway.wsUrl)
    const wire = observe(page)
    await page.goto('/control/sessions')
    const connection = page.getByRole('button', { name: /^(Manage gateway connection|Connection:)/ })
    await expect(connection).toHaveText('Connected')
    await expect.poll(() => wire.hellos.length).toBe(1)
    expect(wire.hellos[0].principal.authState).toBe('authenticated')
    expect(await page.evaluate(() => sessionStorage.getItem('opensquilla.wsToken'))).toBeNull()

    expect((await request.post(`${gateway.httpUrl}/enable-token`)).ok()).toBe(true)
    await expect.poll(() => wire.hellos.length).toBe(2)
    expect(wire.hellos[1].principal).toMatchObject({ authenticated: false, authState: 'guest' })
    await expect(connection).toHaveText('Connected')
    await connection.click()
    const token = page.locator('#conn-ws-token')
    await expect(page.locator('.conn-status__pill')).toHaveText('Connected')
    await expect(page.locator('.conn-status__reason')).not.toContainText('Authentication failed')
    await expect(page.locator('.conn-optional')).toHaveText('optional')
    const permissionLog = testInfo.outputPath('guest-permission-responses.json')
    await writeFile(permissionLog, JSON.stringify(wire.denied, null, 2))
    await testInfo.attach('guest-permission-responses', {
      path: permissionLog, contentType: 'application/json',
    })
    await token.fill(TOKEN)
    await token.press('Enter')
    await expect.poll(() => wire.hellos.at(-1)?.principal.authenticated).toBe(true)
    expect(wire.connections).toBe(3)
  } finally { await gateway.stop() }
})

test('does not describe an origin-policy close as a missing token', async ({ page }) => {
  await page.clock.install()
  await prepare(page, 'ws://synthetic-policy.invalid/ws')
  await page.routeWebSocket('ws://synthetic-policy.invalid/ws', socket => {
    socket.close({ code: 1008, reason: 'origin_policy_rejected' })
  })
  await page.goto('/control/sessions')
  await page.getByRole('button', { name: /^(Manage gateway connection|Connection:)/ }).click()
  const token = page.locator('#conn-ws-token')
  await expect(token).toBeVisible()
  await expect(page.locator('.conn-status__pill')).not.toHaveText('Token required')
  await expect(page.locator('.conn-status__reason')).not.toContainText('Authentication failed')
  await expect(token).not.toBeFocused()
})

test('keeps connection settings navigation when a policy close settles the initial draft', async ({ page }) => {
  await page.clock.install()
  await prepare(page, 'ws://synthetic-policy.invalid/ws')
  let socket: WebSocketRoute
  let subscribed = false
  await page.routeWebSocket('ws://synthetic-policy.invalid/ws', ws => {
    socket = ws
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: { methods: [
          'sessions.messages.subscribe', 'sessions.messages.hydrate', 'sessions.messages.snapshot',
        ] } }))
      } else if (['sessions.messages.subscribe', 'sessions.messages.hydrate', 'sessions.messages.snapshot'].includes(frame.method)) {
        // The first live bootstrap stays pending until the operator leaves Chat.
        subscribed = true
      } else if (frame.type === 'req') {
        ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: {} }))
      }
    })
  })
  const connection = page.getByRole('button', { name: /^(Manage gateway connection|Connection:)/ })
  await page.route(/\/(?:assets\/ChatView-[^/]+\.js|src\/views\/ChatView\.vue)(?:\?.*)?$/, async route => {
    // Give the draft a connected bootstrap rather than an immediate offline result.
    await expect(connection).toHaveText('Connected')
    await route.continue()
  })
  await page.route(/\/(?:assets\/SettingsView-[^/]+\.js|src\/views\/web\/SettingsView\.vue)(?:\?.*)?$/, async route => {
    // Reproduce a slow first load of Settings while the initial draft completes.
    socket.close({ code: 1008, reason: 'origin_policy_rejected' })
    await expect(connection).toHaveText('Disconnected')
    await route.continue()
  })
  await page.goto('/control/sessions')
  await expect.poll(() => subscribed).toBe(true)
  await expect(page).toHaveURL(/\/control\/chat$/)
  await connection.click()
  await expect(page).toHaveURL(/\/settings\/gateway#connection$/)
  const token = page.locator('#conn-ws-token')
  await expect(token).toBeVisible()
  await expect(page.locator('.conn-status__pill')).not.toHaveText('Token required')
  await expect(page.locator('.conn-status__reason')).not.toContainText('Authentication failed')
  await expect(token).not.toBeFocused()
})
