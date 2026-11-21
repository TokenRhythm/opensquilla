import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_OPEN_RECOVERY_UNDER_XVFB !== '1') {
  const child = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_OPEN_RECOVERY_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (child.error) throw child.error
  process.exit(child.status ?? 1)
}

const requests = []
const web = createServer((request, response) => {
  requests.push(request.url)
  if (request.url === '/http-redirect' || request.url === '/http-failure') {
    response.writeHead(302, { location: request.url === '/http-failure' ? '/broken-landing' : '/landing' })
    response.end()
    return
  }
  if (request.url === '/broken-landing') {
    response.destroy()
    return
  }
  const destinations = {
    '/script-redirect': '/landing',
    '/script-chain': '/script-chain-next',
    '/script-chain-next': '/landing',
    '/script-failure': '/broken-landing',
    '/script-http-failure': '/http-failure',
    '/script-dialog': '/dialog-landing',
  }
  const loop = /^\/script-loop-(\d+)$/.exec(request.url)
  const destination = destinations[request.url]
    ?? (loop ? Number(loop[1]) < 5 ? `/script-loop-${Number(loop[1]) + 1}` : '/landing' : undefined)
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
  response.end(`<!doctype html><title>Browser recovery fixture</title>
    <style>body{font:17px system-ui;background:#f4f7fb;color:#15324b;padding:32px}
    input{font:inherit;padding:10px;border:1px solid #849eb4;border-radius:6px}</style>
    <h1>${request.url === '/landing' ? 'Recovered page' : 'Browser startup'}</h1>
    <p>The same browser tab remains available after route changes.</p>
    <label>Retained form <input aria-label="Retained form" id="retained"></label>
    <script>window.redirectNow=()=>location.replace('/landing');
    ${destination ? `location.replace(${JSON.stringify(destination)});` : ''}
    ${request.url === '/dialog-landing' ? "window.dialogResult=prompt('Synthetic opening dialog', 'fixture');" : ''}</script>`)
})
await new Promise(resolve => web.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${web.address().port}`
const directory = await mkdtemp(join(tmpdir(), 'opensquilla-open-recovery-'))
let app
const watchdog = setTimeout(() => {
  console.error('Browser open recovery fixture exceeded its deadline')
  app?.process().kill('SIGKILL')
  web.closeAllConnections()
}, 60_000)
watchdog.unref()
try {
  app = await electron.launch({
    args: [`--user-data-dir=${join(directory, 'chromium')}`,
      fileURLToPath(new URL('./fixtures/native-workbench-smoke', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '*', no_proxy: '*' },
  })
  const environment = await app.evaluate(async ({ BrowserWindow }, origin) => {
    const owner = new BrowserWindow({ show: true, width: 1080, height: 760,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
    await owner.loadURL('data:text/html,' + encodeURIComponent(`<!doctype html><title>Browser recovery</title>
      <style>body{background:#15273d;color:#edf5ff;font:18px system-ui;margin:32px}p{width:240px;line-height:1.7}</style>
      <h2>Browser recovery</h2><p>One retained tab.<br>Route changes handled.<br>Sidebar state preserved.</p>`))
    const events = []
    const manager = new globalThis.__opensquillaNativeWorkbenchSurfaceManager({
      getWindow: () => owner, emit(event) { events.push(event) },
    })
    const fixture = { owner, manager, events, trace: [], scenario: 'plain', attempts: 0, injected: 0, origin }
    globalThis.openRecoveryFixture = fixture
    const createSurface = manager.createSurface.bind(manager)
    manager.createSurface = async (...args) => {
      const result = await createSurface(...args)
      if (fixture.scenario === 'competing-after-create' && !fixture.injected) {
        fixture.injected += 1
        const record = manager.surfaces.get(args[0].surfaceId)
        await record.contents.loadURL(origin + '/landing')
      }
      return result
    }
    const configure = manager.configureWebContents.bind(manager)
    manager.configureWebContents = record => {
      const result = configure(record)
      for (const name of ['did-start-navigation', 'will-frame-navigate', 'will-navigate', 'did-finish-load',
        'did-fail-load', 'did-fail-provisional-load', 'did-stop-loading']) {
        record.contents.on(name, (event, ...args) => fixture.trace.push({ name,
          ...(name.startsWith('did-fail') ? { code: args[0], failedUrl: args[2], mainFrame: args[3] }
            : { url: event?.url ?? args[0] }),
          documentUrl: record.documentUrl, ready: record.browserDocumentReady,
          owner: record.browserNavigationOwner, opening: { ...record.browserOpeningNavigation },
          error: record.browserNavigationError,
          navigationGeneration: record.browserNavigationGeneration, documentGeneration: record.annotationDocumentGeneration }))
      }
      return result
    }
    const changeRoute = async record => {
      const completed = new Promise(resolve => record.contents.once('did-navigate-in-page', resolve))
      await record.contents.executeJavaScript(`history.replaceState({},'', '#startup-${++fixture.injected}')`)
      await completed
    }
    const cdp = manager.cdpCommand.bind(manager)
    manager.cdpCommand = async (record, method, ...args) => {
      const result = await cdp(record, method, ...args)
      if (method !== 'Emulation.setDeviceMetricsOverride' || !record.browserOpenedHidden) return result
      fixture.attempts += 1
      if (fixture.scenario === 'route-churn' || fixture.scenario === 'viewport-route' && !fixture.injected) {
        await changeRoute(record)
      } else if (fixture.scenario === 'late-script-redirect' && !fixture.injected) {
        fixture.injected += 1
        const completed = new Promise(resolve => record.contents.once('did-finish-load', resolve))
        await record.contents.executeJavaScript('window.redirectNow()')
        await completed
      } else if (fixture.scenario === 'competing-navigation' && !fixture.injected) {
        fixture.injected += 1
        await record.contents.loadURL(origin + '/landing')
      }
      return result
    }
    const pointer = manager.touchBrowserPointer.bind(manager)
    manager.touchBrowserPointer = async record => {
      const result = await pointer(record)
      if (fixture.scenario === 'pointer-route' && !fixture.injected) await changeRoute(record)
      return result
    }
    const server = new globalThis.__opensquillaDesktopBrowserServer(
      (request, signal) => manager.executeBrowser(request, signal), undefined,
      (request, signal) => manager.executeBrowserMcp(request, signal))
    fixture.server = server
    return await server.start()
  }, origin)
  const call = async (operation, arguments_ = {}) => {
    const response = await fetch(environment.OPENSQUILLA_DESKTOP_BROWSER_URL, {
      method: 'POST', signal: AbortSignal.timeout(15_000), headers: { 'Content-Type': 'application/json',
        Authorization: `Bearer ${environment.OPENSQUILLA_DESKTOP_BROWSER_TOKEN}` },
      body: JSON.stringify({ sessionKey: 'synthetic-open-recovery', operation, ...arguments_ }),
    })
    return { status: response.status, body: await response.json() }
  }
  const success = response => {
    assert.equal(response.status, 200, JSON.stringify(response.body))
    return response.body
  }
  const failure = (response, code, recovery = 'inspect') => {
    assert.equal(response.status, 409, JSON.stringify(response.body))
    assert.equal(response.body.code, code, JSON.stringify(response.body))
    assert.equal(response.body.retryable, false)
    assert.equal(response.body.recovery, recovery)
    assert.equal(typeof response.body.targetRef, 'string')
    return response.body
  }
  const prepare = async scenario => await app.evaluate(async ({}, scenario) => {
    const fixture = globalThis.openRecoveryFixture
    for (const record of [...fixture.manager.surfaces.values()]) await fixture.manager.destroySurface(record.id)
    fixture.scenario = scenario
    fixture.attempts = 0
    fixture.injected = 0
    fixture.events.length = 0
    fixture.trace.length = 0
  }, scenario)
  const stats = () => app.evaluate(() => {
    const fixture = globalThis.openRecoveryFixture
    return { attempts: fixture.attempts, injected: fixture.injected,
      targets: fixture.manager.surfaces.size,
      opened: fixture.events.filter(event => event.type === 'browser-opened').length }
  })
  const inspect = async targetRef => {
    const snapshot = success(await call('snapshot', { targetRef }))
    assert.equal(snapshot.targetRef, targetRef)
    return snapshot
  }
  const inspectRetained = async (targetRef, expectedUrl) => {
    const deadline = Date.now() + 4000
    while (true) {
      const targets = success(await call('list')).targets
      assert.equal(targets.length, 1, 'a failed open must retain exactly one tab')
      assert.equal(targets[0].targetRef, targetRef)
      if (targets[0].pageState === 'ready' && (!expectedUrl || targets[0].url === expectedUrl)) {
        await inspect(targetRef)
        return targets[0]
      }
      assert.ok(Date.now() < deadline, 'The retained page did not become ready')
      await new Promise(resolve => setTimeout(resolve, 20))
    }
  }
  const recover = async targetRef => {
    const targets = success(await call('list')).targets
    assert.equal(targets.length, 1, 'a failed open must retain exactly one tab')
    assert.equal(targets[0].targetRef, targetRef)
    const navigated = success(await call('open', { targetRef, url: origin + '/landing' }))
    assert.equal(navigated.targetRef, targetRef)
    await inspect(targetRef)
    assert.equal(success(await call('list')).targets.length, 1)
  }

  await prepare('plain')
  const malformed = await call('open', { url: `[${origin}/landing](${origin}/landing)` })
  assert.equal(malformed.status, 400)
  assert.equal(malformed.body.code, 'INVALID_REQUEST')
  assert.equal((await stats()).targets, 0, 'invalid arguments must not create a tab')
  for (const url of ['about:blank', origin + '/plain', origin + '/http-redirect']) {
    await prepare('plain')
    const target = success(await call('open', { url }))
    if (process.env.OPENSQUILLA_OPEN_RECOVERY_TRACE) {
      console.log(url, target, await app.evaluate(() => globalThis.openRecoveryFixture.trace))
    }
    assert.equal(target.url, url.endsWith('/http-redirect') ? origin + '/landing' : url)
    assert.equal(target.pageState, 'ready')
    await inspect(target.targetRef)
    assert.equal((await stats()).targets, 1)
  }

  for (const [scenario, path] of [['plain', '/script-redirect'], ['plain', '/script-chain'],
    ['late-script-redirect', '/route']]) {
    await prepare(scenario)
    const landingRequests = requests.filter(url => url === '/landing').length
    const target = success(await call('open', { url: origin + path }))
    assert.equal(target.url, origin + '/landing', 'a benign startup redirect must finish successfully')
    assert.equal((await inspectRetained(target.targetRef, origin + '/landing')).url, origin + '/landing')
    const state = await stats()
    assert.equal(state.targets, 1)
    assert.equal(state.opened, 1)
    assert.equal(requests.filter(url => url === '/landing').length, landingRequests + 1,
      'startup recovery must not repeat the completed redirect')
  }

  await prepare('plain')
  const dialogTarget = success(await call('open', { url: origin + '/script-dialog' }))
  assert.equal(dialogTarget.url, origin + '/dialog-landing')
  assert.equal((await stats()).targets, 1)
  await app.evaluate(async ({}, targetRef) => {
    const { manager } = globalThis.openRecoveryFixture
    const record = [...manager.surfaces.values()].find(record => record.targetRef === targetRef)
    const pending = record.prompt.state()
    if (!pending) throw new Error('Opening redirected page did not retain its dialog')
    await manager.executeBrowserMcp({ sessionKey: 'synthetic-open-recovery', operation: 'dialog',
      targetRef, dialogId: pending.id, accept: true, promptText: 'Recovered dialog' }, new AbortController().signal)
    if (await record.contents.executeJavaScript('window.dialogResult') !== 'Recovered dialog') {
      throw new Error('Opening dialog response was lost')
    }
  }, dialogTarget.targetRef)
  await inspectRetained(dialogTarget.targetRef, origin + '/dialog-landing')

  for (const path of ['/script-failure', '/script-http-failure']) {
    await prepare('plain')
    const response = await call('open', { url: origin + path })
    if (process.env.OPENSQUILLA_OPEN_RECOVERY_TRACE) {
      console.log(path, response, await app.evaluate(() => globalThis.openRecoveryFixture.trace))
    }
    const failed = failure(response,
      'NAVIGATION_FAILED', 'change_url_or_network')
    assert.equal(failed.pageState, 'navigation_failed')
    const failedTargets = success(await call('list')).targets
    assert.equal(failedTargets.length, 1)
    assert.equal(failedTargets[0].targetRef, failed.targetRef)
    assert.equal(failedTargets[0].pageState, 'navigation_failed')
    await recover(failed.targetRef)
  }

  await prepare('plain')
  const excessive = failure(await call('open', { url: origin + '/script-loop-0' }), 'PAGE_CHANGED')
  await inspectRetained(excessive.targetRef, origin + '/landing')
  assert.equal((await stats()).opened, 1)
  for (let hop = 0; hop <= 5; hop += 1) {
    assert.equal(requests.filter(url => url === `/script-loop-${hop}`).length, 1,
      'redirect recovery must not replay an excessive redirect chain')
  }

  for (const scenario of ['viewport-route', 'pointer-route']) {
    await prepare(scenario)
    const requestCount = requests.filter(url => url === '/route').length
    const target = success(await call('open', { url: origin + '/route' }))
    assert.match(target.url, /#startup-1$/)
    assert.deepEqual(await stats(), { attempts: 2, injected: 1, targets: 1, opened: 1 })
    assert.equal(requests.filter(url => url === '/route').length, requestCount + 1,
      'startup recovery must not repeat page navigation')
    await inspect(target.targetRef)
  }

  for (const scenario of ['route-churn', 'competing-navigation', 'competing-after-create']) {
    await prepare(scenario)
    const retained = failure(await call('open', { url: origin + '/route' }), 'PAGE_CHANGED')
    const state = await stats()
    assert.equal(state.targets, 1)
    assert.equal(state.opened, 1)
    if (scenario === 'route-churn') assert.equal(state.attempts, 3, 'route recovery must be bounded')
    else assert.equal(state.injected, 1)
    await app.evaluate(() => { globalThis.openRecoveryFixture.scenario = 'plain' })
    await inspectRetained(retained.targetRef)
    await recover(retained.targetRef)
  }

  const target = success(await call('list')).targets[0]
  const retainedState = await app.evaluate(async ({}, targetRef) => {
    const { manager } = globalThis.openRecoveryFixture
    const record = [...manager.surfaces.values()].find(record => record.targetRef === targetRef)
    await record.contents.executeJavaScript("document.getElementById('retained').value='Retained after reopening';window.fixtureState='retained'")
    const rect = { version: 4, surfaceId: record.id, x: 320, y: 24, width: 728, height: 680 }
    for (const visible of [true, false, true, false, true]) {
      const result = manager.setSurfaceRect({ ...rect, visible })
      if (!result.ok) throw new Error('Sidebar visibility change failed')
    }
    return { targetRef: record.targetRef, visible: record.view.getVisible(),
      form: await record.contents.executeJavaScript("document.getElementById('retained').value"),
      state: await record.contents.executeJavaScript('window.fixtureState') }
  }, target.targetRef)
  assert.deepEqual(retainedState, { targetRef: target.targetRef, visible: true,
    form: 'Retained after reopening', state: 'retained' })
  await inspect(target.targetRef)
  if (process.env.OPENSQUILLA_OPEN_RECOVERY_SCREENSHOT) {
    const png = await app.evaluate(async () => {
      const { manager } = globalThis.openRecoveryFixture
      const record = [...manager.surfaces.values()][0]
      await record.contents.executeJavaScript('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
      return (await record.contents.capturePage()).toPNG().toString('base64')
    })
    await writeFile(process.env.OPENSQUILLA_OPEN_RECOVERY_SCREENSHOT, Buffer.from(png, 'base64'))
  }
  console.log('Browser open recovery passed: blank/plain, HTTP and immediate/late/multiple script redirects, redirect failures/dialogs/limits, viewport/pointer routes, competing navigation, bounded churn, retained-target recovery, invalid URL and sidebar state retention.')
} finally {
  await app?.evaluate(async () => {
    const fixture = globalThis.openRecoveryFixture
    if (!fixture) return
    await fixture.server.close()
    await fixture.manager.destroyAll()
    fixture.owner.destroy()
  }).catch(() => {})
  await app?.close().catch(() => {})
  web.closeAllConnections()
  await new Promise(resolve => web.close(resolve))
  clearTimeout(watchdog)
  await rm(directory, { recursive: true, force: true })
}
