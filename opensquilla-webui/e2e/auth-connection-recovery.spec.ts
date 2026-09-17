import { expect, test, type Page, type TestInfo } from '@playwright/test'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

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
  const hellos: Array<{ principal: { authenticated: boolean; authState: string }; guestSessionKey?: string }> = []
  const denied: Array<{ method: string; sessionKey: unknown; message: string }> = []
  let connections = 0
  page.on('websocket', socket => {
    connections += 1
    const requests = new Map<string, { method: string; sessionKey: unknown }>()
    socket.on('framesent', ({ payload }) => {
      const frame = JSON.parse(String(payload))
      if (frame.type === 'req' && frame.method !== 'connect') {
        requests.set(frame.id, {
          method: frame.method,
          sessionKey: frame.params?.key ?? frame.params?.sessionKey ?? null,
        })
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
  return { hellos, denied, get connections() { return connections } }
}

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
