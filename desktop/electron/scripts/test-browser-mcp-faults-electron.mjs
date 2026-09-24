import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_MCP_FAULT_UNDER_XVFB !== '1') {
  const child = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_MCP_FAULT_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (child.error) throw child.error
  process.exit(child.status ?? 1)
}

const heldNavigations = []
let holdNavigationResponses = true
const web = createServer((request, response) => {
  if (request.url === '/disconnect') {
    request.socket.destroy()
    return
  }
  if (request.url === '/held-navigation' && holdNavigationResponses) {
    const pending = { response, closed: false }
    heldNavigations.push(pending)
    response.on('close', () => { pending.closed = true })
    return
  }
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
  response.end(`<!doctype html><title>Concurrent browser fixture</title><h1>Concurrent page</h1>
    <button id="increment" onclick="localStorage.setItem('clicks',String(Number(localStorage.getItem('clicks')||0)+1))">Increment</button>
    <button id="waiting" disabled onclick="localStorage.setItem('clicks',String(Number(localStorage.getItem('clicks')||0)+1))">Waiting button</button>
    <button id="dialog-waiting" disabled onclick="confirm('Continue queued fixture?');window.dialogCompleted=true">Dialog waiting</button>`)
})
await new Promise(resolve => web.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${web.address().port}`
const root = await mkdtemp(join(tmpdir(), 'opensquilla-browser-faults-'))
let app
try {
  app = await electron.launch({
    args: [`--user-data-dir=${join(root, 'chromium')}`, fileURLToPath(new URL('./fixtures/native-workbench-smoke', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '*', no_proxy: '*' },
  })
  const keepDialogPending = page => page.on('dialog', () => {})
  app.context().on('page', keepDialogPending)
  for (const page of app.context().pages()) keepDialogPending(page)
  let environment = await app.evaluate(async ({ BrowserWindow }) => {
    const owner = new BrowserWindow({ show: true, width: 900, height: 700,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
    await owner.loadURL('data:text/html,<title>Concurrent browser host</title>')
    const manager = new globalThis.__opensquillaNativeWorkbenchSurfaceManager({ getWindow: () => owner, emit() {} })
    const requests = []
    const server = new globalThis.__opensquillaDesktopBrowserServer(
      (request, signal) => manager.executeBrowser(request, signal), undefined,
      (request, signal) => { requests.push(request); return manager.executeBrowserMcp(request, signal) })
    globalThis.faultFixture = { owner, manager, server, requests, events: [] }
    return await server.start()
  })
  let serial = 0
  const call = async (name, args = {}, options = {}) => {
    const id = ++serial
    const response = await fetch(environment.OPENSQUILLA_DESKTOP_BROWSER_URL + '/mcp', {
      method: 'POST', headers: { 'Content-Type': 'application/json',
        Authorization: `Bearer ${options.token || environment.OPENSQUILLA_DESKTOP_BROWSER_TOKEN}`,
        ...(options.deadline ? { 'x-opensquilla-deadline-at-ms': String(options.deadline) } : {}) },
      body: JSON.stringify({ jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: args,
        _meta: { sessionKey: 'synthetic-session', operationId: options.operationId || `op-${id}` } } }),
    })
    return { status: response.status, body: await response.json() }
  }
  const success = response => {
    assert.equal(response.status, 200, JSON.stringify(response))
    assert.equal(response.body.result?.isError, false, JSON.stringify(response))
    return response.body.result.structuredContent
  }
  const failure = (response, code) => {
    assert.equal(response.status, 200, JSON.stringify(response))
    assert.equal(response.body.result?.isError, true, JSON.stringify(response))
    const result = response.body.result.structuredContent
    assert.equal(result.code, code, JSON.stringify(result))
    return result
  }
  const eventually = async (predicate, message) => {
    const deadline = Date.now() + 4000
    while (!await predicate()) {
      assert.ok(Date.now() < deadline, message)
      await new Promise(resolve => setTimeout(resolve, 20))
    }
  }
  const first = success(await call('browser_open', { url: origin }))
  const second = success(await call('browser_open', { url: origin }))

  // Failed navigation is recoverable page state, not an instruction to close a tab.
  const failedOpenArgs = { url: origin + '/disconnect' }
  const failedOpen = failure(await call('browser_open', failedOpenArgs,
    { operationId: 'failed-open-receipt' }), 'NAVIGATION_FAILED')
  assert.equal(typeof failedOpen.targetRef, 'string')
  assert.equal(failedOpen.pageState, 'navigation_failed')
  assert.match(failedOpen.navigation.code, /^ERR_/)
  let targets = success(await call('browser_tabs')).targets
  assert.equal(targets.length, 3)
  const failedTarget = targets.find(target => target.targetRef === failedOpen.targetRef)
  assert.equal(failedTarget.pageState, 'navigation_failed')
  assert.equal(failedTarget.navigationError.code, failedOpen.navigation.code)
  success(await call('browser_inspect', { targetRef: first.targetRef }))
  success(await call('browser_inspect', { targetRef: second.targetRef }))

  const replayedOpen = failure(await call('browser_open', failedOpenArgs,
    { operationId: 'failed-open-receipt' }), 'NAVIGATION_FAILED')
  assert.equal(replayedOpen.targetRef, failedOpen.targetRef)
  assert.equal(success(await call('browser_tabs')).targets.length, 3)
  success(await call('browser_navigate', { targetRef: failedOpen.targetRef, url: origin }))
  success(await call('browser_inspect', { targetRef: failedOpen.targetRef }))
  const failedNavigation = failure(await call('browser_navigate', {
    targetRef: failedOpen.targetRef, url: origin + '/disconnect',
  }), 'NAVIGATION_FAILED')
  assert.equal(failedNavigation.targetRef, failedOpen.targetRef)
  assert.equal(failedNavigation.pageState, 'navigation_failed')
  assert.equal(success(await call('browser_tabs')).targets.length, 3)
  success(await call('browser_navigate', { targetRef: failedOpen.targetRef, url: origin }))

  // Expiring a live request stops Chromium's navigation before releasing its lease.
  const slowNavigation = call('browser_navigate', {
    targetRef: failedOpen.targetRef, url: origin + '/held-navigation',
  }, { deadline: Date.now() + 1500, operationId: 'navigation-expiry' })
  await eventually(() => heldNavigations.length > 0, 'Slow navigation never reached the local server')
  const timedOutNavigation = failure(await slowNavigation, 'TIMEOUT')
  assert.equal(timedOutNavigation.targetRef, failedOpen.targetRef)
  await eventually(() => heldNavigations.every(pending => pending.closed),
    'Navigation deadline did not close the pending network request')
  // Recover to the same URL to catch cancellation races that URL comparison cannot distinguish.
  holdNavigationResponses = false
  const recoveredUrl = origin + '/held-navigation'
  success(await call('browser_navigate', { targetRef: failedOpen.targetRef, url: recoveredUrl },
    { deadline: Date.now() + 4000 }))
  // A response from the cancelled request must never replace or destroy the recovered page.
  for (const pending of heldNavigations) pending.response.end('<title>Late navigation</title>')
  success(await call('browser_inspect', { targetRef: failedOpen.targetRef }))
  targets = success(await call('browser_tabs')).targets
  assert.equal(targets.length, 3)
  assert.equal(targets.find(target => target.targetRef === failedOpen.targetRef).url, recoveredUrl)
  success(await call('browser_inspect', { targetRef: first.targetRef }))

  // The caller can recover an already-created tab even when its initial load expires.
  holdNavigationResponses = true
  const pendingCount = heldNavigations.length
  const slowOpenArgs = { url: origin + '/held-navigation' }
  const slowOpen = call('browser_open', slowOpenArgs,
    { deadline: Date.now() + 1500, operationId: 'open-expiry' })
  await eventually(() => heldNavigations.length > pendingCount, 'Slow open never reached the local server')
  const timedOutOpen = failure(await slowOpen, 'TIMEOUT')
  assert.equal(typeof timedOutOpen.targetRef, 'string')
  assert.notEqual(timedOutOpen.targetRef, failedOpen.targetRef)
  assert.equal(success(await call('browser_tabs')).targets.length, 4)
  const replayedTimeout = failure(await call('browser_open', slowOpenArgs,
    { operationId: 'open-expiry' }), 'TIMEOUT')
  assert.equal(replayedTimeout.targetRef, timedOutOpen.targetRef)
  assert.equal(success(await call('browser_tabs')).targets.length, 4)
  await eventually(() => heldNavigations.every(pending => pending.closed),
    'Open deadline did not close the pending network request')
  holdNavigationResponses = false
  success(await call('browser_navigate', { targetRef: timedOutOpen.targetRef, url: recoveredUrl },
    { deadline: Date.now() + 4000 }))
  success(await call('browser_inspect', { targetRef: timedOutOpen.targetRef }))
  success(await call('browser_tab', { targetRef: timedOutOpen.targetRef, tabAction: 'close' }))

  // A user navigation supersedes a pending tool navigation, including to the same URL.
  holdNavigationResponses = true
  const beforeSupersede = heldNavigations.length
  const supersededNavigation = call('browser_navigate', {
    targetRef: failedOpen.targetRef, url: recoveredUrl,
  }, { deadline: Date.now() + 4000 })
  await eventually(() => heldNavigations.length > beforeSupersede,
    'Superseded navigation never reached the local server')
  holdNavigationResponses = false
  await app.evaluate(async ({}, { targetRef, url }) => {
    const fixture = globalThis.faultFixture
    const record = [...fixture.manager.surfaces.values()].find(record => record.targetRef === targetRef)
    await record.view.webContents.loadURL(url)
  }, { targetRef: failedOpen.targetRef, url: recoveredUrl })
  const superseded = failure(await supersededNavigation, 'PAGE_CHANGED')
  assert.equal(superseded.targetRef, failedOpen.targetRef)
  await eventually(() => heldNavigations.every(pending => pending.closed),
    'Superseded request did not close')
  for (const pending of heldNavigations) {
    if (!pending.response.writableEnded) pending.response.end('<title>Superseded response</title>')
  }
  success(await call('browser_inspect', { targetRef: failedOpen.targetRef }))
  targets = success(await call('browser_tabs')).targets
  assert.equal(targets.length, 3)
  assert.equal(targets.find(target => target.targetRef === failedOpen.targetRef).url, recoveredUrl)
  success(await call('browser_tab', { targetRef: failedOpen.targetRef, tabAction: 'close' }))
  failure(await call('browser_inspect', { targetRef: failedOpen.targetRef }), 'TARGET_NOT_FOUND')
  assert.equal(success(await call('browser_tabs')).targets.length, 2)

  let inspected = success(await call('browser_inspect', { targetRef: first.targetRef }))
  const secondSnapshot = success(await call('browser_inspect', { targetRef: second.targetRef }))
  await app.evaluate(({ }, targetRef) => {
    const fixture = globalThis.faultFixture
    const record = [...fixture.manager.surfaces.values()].find(record => record.targetRef === targetRef)
    fixture.first = record
    for (const name of ['act', 'screenshot']) {
      const original = record.playwright[name].bind(record.playwright)
      record.playwright[name] = async (...args) => {
        fixture.events.push(name + ':start')
        try { return await original(...args) }
        finally { fixture.events.push(name + ':end') }
      }
    }
  }, first.targetRef)
  const waitFor = async (field, needle) => await app.evaluate(async ({}, { field, needle }) => {
    const end = Date.now() + 4000
    while (Date.now() < end) {
      const fixture = globalThis.faultFixture
      const matches = field === 'events' ? fixture.events.includes(needle)
        : field === 'requests' ? fixture.requests.some(request => request.operation === needle && request.targetRef === fixture.first.targetRef)
          : fixture.first.browserDocumentReady
      if (matches) return
      await new Promise(resolve => setTimeout(resolve, 10))
    }
    throw new Error('Timed out waiting for fixture ' + field + ': ' + needle)
  }, { field, needle })
  const enable = async () => await app.evaluate(() => globalThis.faultFixture.first.view.webContents.executeJavaScript(
    'document.getElementById("waiting").disabled=false'))
  const clicks = async () => await app.evaluate(() => globalThis.faultFixture.first.view.webContents.executeJavaScript(
    'Number(localStorage.getItem("clicks")||0)'))
  const clearEvents = async () => await app.evaluate(() => {
    globalThis.faultFixture.events.length = 0
    globalThis.faultFixture.requests.length = 0
  })
  const waitingAction = () => ({ targetRef: first.targetRef, action: 'click',
    ref: inspected.refs.find(item => item.name === 'Waiting button').ref })

  const firstAction = call('browser_act', waitingAction())
  await waitFor('events', 'act:start')
  const screenshot = call('browser_screenshot', { targetRef: first.targetRef })
  await waitFor('requests', 'screenshot')
  const reload = call('browser_reload', { targetRef: first.targetRef })
  await waitFor('requests', 'reload')
  // Another tab completes while the first tab's click is still waiting.
  success(await call('browser_act', { targetRef: second.targetRef, action: 'click',
    ref: secondSnapshot.refs.find(item => item.name === 'Increment').ref }))
  assert.deepEqual(await app.evaluate(() => globalThis.faultFixture.events), ['act:start'])
  await enable()
  success(await firstAction)
  success(await screenshot)
  success(await reload)
  await waitFor('ready')
  assert.deepEqual(await app.evaluate(() => globalThis.faultFixture.events), ['act:start', 'act:end', 'screenshot:start', 'screenshot:end'])
  assert.equal(await clicks(), 1)

  // A request that times out while queued must never dispatch after the queue opens.
  inspected = success(await call('browser_inspect', { targetRef: first.targetRef }))
  await clearEvents()
  const blocker = call('browser_act', waitingAction())
  await waitFor('events', 'act:start')
  const expired = await call('browser_act', { targetRef: first.targetRef, action: 'click',
    ref: inspected.refs.find(item => item.name === 'Increment').ref }, { deadline: Date.now() + 150, operationId: 'queued-expiry' })
  if (expired.status === 200) failure(expired, 'TIMEOUT')
  else {
    assert.equal(expired.status, 504)
    assert.equal(expired.body.code, 'TIMEOUT', JSON.stringify(expired))
  }
  await enable()
  success(await blocker)
  success(await call('browser_inspect', { targetRef: first.targetRef }))
  assert.equal(await clicks(), 2)
  assert.equal(await app.evaluate(() => globalThis.faultFixture.events.filter(event => event === 'act:start').length), 1)

  // Closing the transport invalidates old credentials and aborts pending input.
  success(await call('browser_reload', { targetRef: first.targetRef }))
  await waitFor('ready')
  inspected = success(await call('browser_inspect', { targetRef: first.targetRef }))
  await clearEvents()
  const oldToken = environment.OPENSQUILLA_DESKTOP_BROWSER_TOKEN
  const interrupted = call('browser_act', waitingAction()).then(result => ({ result }), error => ({ error: error.message }))
  await waitFor('events', 'act:start')
  environment = await app.evaluate(async () => {
    const server = globalThis.faultFixture.server
    await server.close()
    return await server.start()
  })
  const interruption = await interrupted
  assert.ok(interruption.error || interruption.result.status !== 200 || interruption.result.body.result?.isError)
  assert.notEqual(environment.OPENSQUILLA_DESKTOP_BROWSER_TOKEN, oldToken)
  assert.equal((await call('browser_tabs', {}, { token: oldToken })).status, 401)
  inspected = success(await call('browser_inspect', { targetRef: first.targetRef }))
  await enable()
  assert.equal(await clicks(), 2)
  success(await call('browser_act', { targetRef: first.targetRef, action: 'click',
    ref: inspected.refs.find(item => item.name === 'Increment').ref }))
  assert.equal(await clicks(), 3)

  const raceTarget = success(await call('browser_open', { url: origin }))
  const raceSnapshot = success(await call('browser_inspect', { targetRef: raceTarget.targetRef }))
  const raceRef = name => raceSnapshot.refs.find(item => item.name === name).ref
  await app.evaluate(({}, { targetRef, waitingRef }) => {
    const fixture = globalThis.faultFixture
    const record = [...fixture.manager.surfaces.values()].find(record => record.targetRef === targetRef)
    const race = fixture.race = { record, events: [], phase: 'host', waitingRef, writeActive: false }
    const originalAct = record.playwright.act.bind(record.playwright)
    record.playwright.act = async (...args) => {
      const queuedWrite = race.phase === 'dialog' && args[0].ref === race.waitingRef
      race.events.push('act:start')
      if (queuedWrite) { race.writeActive = true; race.events.push('write:start') }
      try { return await originalAct(...args) }
      finally {
        race.events.push('act:end')
        if (queuedWrite) { race.writeActive = false; race.events.push('write:end') }
      }
    }
    const originalObserve = record.playwright.observe.bind(record.playwright)
    record.playwright.observe = async (...args) => {
      race.events.push('observe:start')
      try { return await originalObserve(...args) }
      finally { race.events.push('observe:end') }
    }
  }, { targetRef: raceTarget.targetRef, waitingRef: raceRef('Waiting button') })
  const waitForRace = async (kind, value) => await app.evaluate(async ({}, { kind, value }) => {
    const fixture = globalThis.faultFixture
    const deadline = Date.now() + 4000
    do {
      if (kind === 'event' ? fixture.race.events.includes(value)
        : fixture.requests.some(request => request.targetRef === fixture.race.record.targetRef && request.ref === value)) return
      await new Promise(resolve => setTimeout(resolve, 10))
    } while (Date.now() < deadline)
    throw new Error('Queued fixture did not reach ' + kind + ': ' + value)
  }, { kind, value })
  const raceAction = name => ({ targetRef: raceTarget.targetRef, action: 'click', ref: raceRef(name) })
  // B enters the queue before A produces a host permission blocker. Admission
  // must check current blockers again when B actually reaches the renderer.
  const permissionPredecessor = call('browser_act', raceAction('Waiting button'))
  await waitForRace('event', 'act:start')
  const permissionFollower = call('browser_act', raceAction('Increment'))
  await waitForRace('request', raceRef('Increment'))
  await app.evaluate(async () => {
    const record = globalThis.faultFixture.race.record
    const timeout = setTimeout(() => {}, 60_000)
    timeout.unref()
    record.pendingPermissions.set('synthetic-permission', { requestId: 'synthetic-permission',
      origin: new URL(record.view.webContents.getURL()).origin, permission: 'notifications',
      grantPermissions: [], callback() {}, timeout })
    await record.view.webContents.executeJavaScript('document.getElementById("waiting").disabled=false')
  })
  success(await permissionPredecessor)
  const permissionBlocked = success(await permissionFollower)
  assert.equal(permissionBlocked.execution.state, 'blocked')
  assert.equal(permissionBlocked.execution.reason, 'requires_user')
  assert.equal(permissionBlocked.observation.hostBlockers[0].id, 'synthetic-permission')
  assert.equal(await app.evaluate(() => globalThis.faultFixture.race.events.filter(event => event === 'act:start').length), 1)
  assert.equal(await app.evaluate(() => globalThis.faultFixture.race.record.view.webContents.executeJavaScript(
    'Number(localStorage.getItem("clicks")||0)')), 1)

  await app.evaluate(async () => {
    const fixture = globalThis.faultFixture
    fixture.manager.rejectPendingPermissions(fixture.race.record)
    fixture.race.phase = 'dialog'
    fixture.race.events.length = 0
    fixture.requests.length = 0
    await fixture.race.record.view.webContents.executeJavaScript('document.getElementById("waiting").disabled=true')
  })
  // A raises a dialog after B has entered the queue. Responding to the dialog
  // must bypass A's lease, while the response's observation stays behind B.
  const dialogPredecessor = call('browser_act', raceAction('Dialog waiting'))
  await waitForRace('event', 'act:start')
  const dialogFollower = call('browser_act', raceAction('Waiting button'))
  await waitForRace('request', raceRef('Waiting button'))
  await app.evaluate(() => globalThis.faultFixture.race.record.view.webContents.executeJavaScript(
    'document.getElementById("dialog-waiting").disabled=false'))
  const dialogBlocked = success(await dialogPredecessor)
  assert.equal(dialogBlocked.execution.state, 'blocked')
  const resolution = call('browser_handle_dialog', { targetRef: raceTarget.targetRef,
    dialogId: dialogBlocked.browserState.dialogs.pending[0].id, accept: true })
  await waitForRace('event', 'write:start')
  await new Promise(resolve => setTimeout(resolve, 100))
  assert.deepEqual(await app.evaluate(() => ({
    active: globalThis.faultFixture.race.writeActive,
    observing: globalThis.faultFixture.race.events.includes('observe:start'),
  })), { active: true, observing: false })
  await app.evaluate(() => globalThis.faultFixture.race.record.view.webContents.executeJavaScript(
    'document.getElementById("waiting").disabled=false'))
  success(await dialogFollower)
  success(await resolution)
  const raceEvents = await app.evaluate(() => globalThis.faultFixture.race.events)
  assert.ok(raceEvents.indexOf('observe:start') > raceEvents.indexOf('write:end'), JSON.stringify(raceEvents))
  assert.equal(await app.evaluate(() => globalThis.faultFixture.race.record.view.webContents.executeJavaScript(
    'window.dialogCompleted')), true)
  success(await call('browser_tab', { targetRef: raceTarget.targetRef, tabAction: 'close' }))

  success(await call('browser_reload', { targetRef: first.targetRef }))
  await waitFor('ready')
  inspected = success(await call('browser_inspect', { targetRef: first.targetRef }))
  await clearEvents()
  const closingAction = call('browser_act', waitingAction())
  await waitFor('events', 'act:start')
  await app.evaluate(async () => {
    const fixture = globalThis.faultFixture
    await fixture.manager.destroySurface(fixture.first.id)
  })
  const closedResult = await closingAction
  assert.equal(closedResult.body.result.isError, true)
  assert.equal(closedResult.body.result.structuredContent.outcome, 'unknown')
  assert.equal(closedResult.body.result.structuredContent.retryable, false)
  assert.equal(success(await call('browser_tabs')).targets.length, 1)
  assert.equal(await app.evaluate(() => globalThis.faultFixture.owner.isDestroyed()), false)
  console.log('MCP concurrency and failure recovery passed: navigation failure isolation, stable targets, failed-open receipts, navigation cancellation, user navigation supersession, per-tab serialization, queued host blockers, serialized dialog observations, parallel tabs, reload, queued expiry, transport restart, credential rotation and target closure.')
} finally {
  if (app) await app.evaluate(async () => {
    await globalThis.faultFixture?.server.close()
    await globalThis.faultFixture?.manager.destroyAll()
  }).catch(() => {})
  await app?.close().catch(() => {})
  web.closeAllConnections()
  await new Promise(resolve => web.close(resolve))
  await rm(root, { recursive: true, force: true })
}
