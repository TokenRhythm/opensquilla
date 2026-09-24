import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { createServer } from 'node:http'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron, chromium } from 'playwright'

const scriptPath = fileURLToPath(import.meta.url)
if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_ATTACH_UNDER_XVFB !== '1') {
  const child = spawnSync('xvfb-run', ['-a', process.execPath, scriptPath], {
    env: { ...process.env, OPENSQUILLA_ATTACH_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (child.error) throw child.error
  process.exit(child.status ?? 1)
}

const server = createServer((request, response) => {
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
  if (request.url === '/frame') {
    response.end('<!doctype html><button onclick="this.textContent=\'Frame clicked\'">Frame action</button><button onclick="if(confirm(\'Frame confirmation\'))this.textContent=\'Frame confirmed\'">Frame confirm</button>')
    return
  }
  if (request.url === '/opening-dialog') {
    response.end('<!doctype html><script>window.accepted=confirm("Initial fixture dialog")</script><h1>Loaded after initial dialog</h1>')
    return
  }
  response.end(`<!doctype html><title>Attachment fixture</title><style>body{padding:20px;min-height:1400px}button{appearance:none;background:#eee;border:1px solid #888;color:#222}</style>
    <h1>${request.url === '/next' ? 'Next document' : 'Existing document'}</h1>
    <button onclick="window.clicks=(window.clicks||0)+1">Increment</button>
    <label>Value<input aria-label="Value"></label>
    <select aria-label="Choice"><option value="a">First</option><option value="b">Second</option></select>
    <button disabled id="delayed" onclick="window.delayedClicks=(window.delayedClicks||0)+1">Delayed action</button>
    <a href="/next">Next page</a>
    <button id="confirm" onclick="window.confirmed=confirm('Continue fixture?')">Confirm fixture</button>
    <button id="alert" onclick="alert('Fixture alert');window.alerted=true">Alert fixture</button>`)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`
const isolatedRoot = await mkdtemp(join(tmpdir(), 'opensquilla-playwright-attach-'))
let app
let rootBrowser
try {
  const executablePath = process.env.OPENSQUILLA_ELECTRON_EXECUTABLE
  const require = createRequire(import.meta.url)
  const loader = join(dirname(require.resolve('playwright-core/package.json')), 'lib/server/electron/loader.js')
  // The test harness uses Electron automation for assertions in the main process.
  // The production driver itself opens no remote debugging port or browser process.
  app = await electron.launch({
    ...(executablePath ? { executablePath } : {}),
    args: [...(executablePath ? ['-r', loader] : []), `--user-data-dir=${join(isolatedRoot, 'chromium')}`,
      fileURLToPath(new URL('./fixtures/browser-playwright-attach', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '*', no_proxy: '*' },
  })
  // The harness's second browser connection must not auto-dismiss dialogs that
  // belong to the independently attached production driver.
  app.context().on('page', page => page.on('dialog', () => {}))
  for (const page of app.context().pages()) page.on('dialog', () => {})
  const before = await app.evaluate(async ({ BrowserWindow, WebContentsView }, origin) => {
    const owner = new BrowserWindow({ show: true, width: 900, height: 700,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
    await owner.loadURL('data:text/html,<title>Excluded control fixture</title>')
    const records = []
    for (const [index, marker] of ['first-login', 'second-login'].entries()) {
      const view = new WebContentsView({ webPreferences: { sandbox: true, contextIsolation: true,
        nodeIntegration: false, partition: `attach-fixture-${index}` } })
      owner.contentView.addChildView(view)
      view.setBounds({ x: 0, y: 0, width: 800, height: 600 })
      await view.webContents.loadURL(origin)
      await view.webContents.executeJavaScript(`localStorage.setItem('syntheticLogin', ${JSON.stringify(marker)});document.cookie='syntheticLogin=${marker}; SameSite=Lax'`)
      // The workbench already owns a debugger connection for annotations.
      view.webContents.debugger.attach('1.3')
      const { targetInfo } = await view.webContents.debugger.sendCommand('Target.getTargetInfo')
      const record = { view, targetInfo, generation: 1, events: 0,
        driver: new globalThis.__BrowserPlaywrightDriver(view.webContents) }
      view.webContents.debugger.on('message', () => { record.events++ })
      view.webContents.on('did-navigate', () => { record.generation++; record.driver.invalidate() })
      records.push(record)
    }
    records[1].view.setVisible(false)
    globalThis.attachFixture = { owner, records }
    return records.map(record => ({ contentsId: record.view.webContents.id, targetInfo: record.targetInfo }))
  }, origin)
  assert.notEqual(before[0].contentsId, before[1].contentsId)
  assert.notEqual(before[0].targetInfo.targetId, before[1].targetInfo.targetId)
  assert.notEqual(before[0].targetInfo.browserContextId, before[1].targetInfo.browserContextId)
  console.log('Existing isolated Electron pages created.')

  // Verify actual Electron/CDP compatibility separately from the restricted driver.
  const [debugPort, debugPath] = (await readFile(join(isolatedRoot, 'chromium', 'DevToolsActivePort'), 'utf8')).trim().split('\n')
  rootBrowser = await chromium.connectOverCDP(`ws://127.0.0.1:${debugPort}${debugPath}`, { noDefaults: true })
  console.log('Root CDP connection attached.')
  const mapping = []
  for (const page of rootBrowser.contexts().flatMap(context => context.pages())) {
    if (page.url() !== origin + '/') continue
    const cdp = await page.context().newCDPSession(page)
    const { targetInfo } = await cdp.send('Target.getTargetInfo')
    mapping.push({ targetId: targetInfo.targetId, marker: await page.evaluate(() => localStorage.getItem('syntheticLogin')) })
    await cdp.detach()
  }
  assert.equal(mapping.length, 2)
  assert.equal(mapping.find(value => value.targetId === before[0].targetInfo.targetId)?.marker, 'first-login')
  assert.equal(mapping.find(value => value.targetId === before[1].targetInfo.targetId)?.marker, 'second-login')
  await rootBrowser.close()
  rootBrowser = undefined
  assert.equal(await app.evaluate(() => globalThis.attachFixture.owner.isDestroyed()), false)
  console.log('Root connection mapped both partitions and disconnected without closing Electron.')

  const actions = await app.evaluate(async () => {
    const { records } = globalThis.attachFixture
    const record = records[0]
    const signal = new AbortController().signal
    const guard = () => { if (record.view.webContents.isDestroyed()) throw new Error('Page closed') }
    const snapshot = await record.driver.snapshot(record.generation, guard, signal)
    const ref = name => snapshot.refs.find(item => item.name === name).ref
    const act = (action, name, extra = {}) => record.driver.act({ operation: 'act', sessionKey: 'synthetic',
      action, ref: ref(name), ...extra }, record.generation, guard, signal)
    await act('click', 'Increment')
    await act('fill', 'Value', { text: 'retained value' })
    await act('select', 'Choice', { text: 'b' })
    // Exercise Playwright's waiting on an initially disabled element.
    setTimeout(() => { void record.view.webContents.executeJavaScript('document.getElementById("delayed").disabled=false') }, 120)
    await act('click', 'Delayed action')
    const screenshot = await record.driver.screenshot(guard, signal)
    const targets = await record.driver.transport.dispatch({ method: 'Target.getTargets' })
    let refused = false
    try { await record.driver.transport.dispatch({ method: 'Target.attachToTarget', params: { targetId: records[1].targetInfo.targetId, flatten: true } }) }
    catch { refused = true }
    const denied = []
    for (const command of [
      { method: 'Browser.close' },
      { method: 'Target.createBrowserContext' },
      { method: 'Target.getTargetInfo', params: { targetId: records[1].targetInfo.targetId } },
      { method: 'Storage.getCookies', sessionId: record.driver.transport.sessionId },
      { method: 'Target.getTargets', sessionId: record.driver.transport.sessionId },
      { method: 'Page.navigate', sessionId: 'unbound-session', params: { url: 'about:blank' } },
    ]) {
      try { await record.driver.transport.dispatch(command); denied.push(false) }
      catch { denied.push(true) }
    }
    const states = await Promise.all(records.map(item => item.view.webContents.executeJavaScript(
      '({login:localStorage.getItem("syntheticLogin"),cookie:document.cookie,clicks:window.clicks||0,delayedClicks:window.delayedClicks||0,value:document.querySelector("input").value,choice:document.querySelector("select").value})')))
    return { text: snapshot.text, screenshot, states, targets: targets.targetInfos, refused, denied,
      attached: record.view.webContents.debugger.isAttached(), events: record.events }
  })
  console.log('Scoped driver actions completed.')
  assert.match(actions.text, /Existing document/)
  assert.equal(actions.targets.length, 1)
  assert.equal(actions.targets[0].targetId, before[0].targetInfo.targetId)
  assert.equal(actions.refused, true)
  assert.ok(actions.denied.every(Boolean), 'browser-wide and unbound session commands must be rejected')
  assert.equal(actions.attached, true)
  assert.ok(actions.events > 0, 'the existing debugger listener must continue receiving events')
  assert.deepEqual(actions.states.map(value => ({ ...value, cookie: undefined })), [
    { login: 'first-login', cookie: undefined, clicks: 1, delayedClicks: 1, value: 'retained value', choice: 'b' },
    { login: 'second-login', cookie: undefined, clicks: 0, delayedClicks: 0, value: '', choice: 'a' },
  ])
  assert.match(actions.states[0].cookie, /first-login/)
  assert.match(actions.states[1].cookie, /second-login/)
  assert.ok(actions.screenshot.width > 0 && actions.screenshot.height > 0)
  assert.equal(Buffer.from(actions.screenshot.dataBase64, 'base64').subarray(1, 4).toString(), 'PNG')

  const observationChecks = await app.evaluate(async () => {
    const record = globalThis.attachFixture.records[0]
    const signal = new AbortController().signal
    const guard = () => { if (record.view.webContents.isDestroyed()) throw new Error('Page closed') }
    const observe = async (mode = 'auto') => {
      let result
      for (let attempt = 0; attempt < 3; attempt++) {
        result = await record.driver.observe(record.generation, guard, signal, mode)
        if (result.observation.consistency !== 'changed') return result
        await new Promise(resolve => setTimeout(resolve, 50))
      }
      return result
    }
    let observed = await observe()
    if (!observed.observation.image) throw new Error(JSON.stringify(observed))
    const image = observed.observation.image
    const point = await record.view.webContents.executeJavaScript('(()=>{const r=document.querySelector("button").getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()')
    const coordinate = () => ({ action: 'click', ...point, observationId: observed.observation.observationId, imageId: observed.observation.image.imageId })
    const failureDetails = error => ({ code: error.code, message: error.message, ...error.details })
    const beforeCoordinateClick = await record.view.webContents.executeJavaScript('window.clicks')
    const withoutReceipt = await record.driver.act(coordinate(), record.generation, guard, signal)
    const afterCoordinateClick = await record.view.webContents.executeJavaScript('window.clicks')
    const consumedFrame = coordinate()
    let consumedFrameFailure
    try { await record.driver.act(consumedFrame, record.generation, guard, signal) }
    catch (error) { consumedFrameFailure = failureDetails(error) }
    const afterConsumedFrame = await record.view.webContents.executeJavaScript('window.clicks')
    observed = await observe()
    const originalNow = Date.now
    let clicked
    try {
      Date.now = () => originalNow() + 180_001
      clicked = await record.driver.batch({ actions: [coordinate()] }, record.generation, guard, signal)
    } finally { Date.now = originalNow }
    const afterOldImageClick = await record.view.webContents.executeJavaScript('window.clicks')
    observed = await observe()
    await record.view.webContents.executeJavaScript('document.querySelector("h1").textContent="Unrelated counter update"')
    const unrelatedMutation = await record.driver.act({ ...coordinate() }, record.generation, guard, signal)
    observed = await observe()
    record.view.setVisible(false)
    let hiddenCoordinate
    try { await record.driver.act({ ...coordinate() }, record.generation, guard, signal) }
    catch (error) { hiddenCoordinate = error.code }
    record.view.setVisible(true)
    observed = await observe()
    record.view.setBounds({ x: 0, y: 0, width: 760, height: 600 })
    let resized
    try { await record.driver.act({ ...coordinate() }, record.generation, guard, signal) }
    catch (error) { resized = failureDetails(error) }
    record.view.setBounds({ x: 0, y: 0, width: 800, height: 600 })
    observed = await observe()
    await record.view.webContents.executeJavaScript('window.scrollTo(0,100)')
    let scrolled
    try { await record.driver.act({ ...coordinate() }, record.generation, guard, signal) }
    catch (error) { scrolled = failureDetails(error) }
    await record.view.webContents.executeJavaScript('window.scrollTo(0,0)')
    observed = await observe()
    const mutationObservationId = observed.observation.observationId
    const beforeRejectedClick = await record.view.webContents.executeJavaScript('window.clicks')
    const mutationPoint = await record.view.webContents.executeJavaScript('(()=>{const r=document.querySelector("button").getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()')
    await record.view.webContents.executeJavaScript('document.querySelector("button").style.background="red"')
    const mutated = await record.driver.batch({ actions: [{ ...coordinate(), ...mutationPoint }] }, record.generation, guard, signal)
    const afterRejectedClick = await record.view.webContents.executeJavaScript('window.clicks')
    await record.view.webContents.executeJavaScript('document.querySelector("button").style.background=""')
    const dom = await observe('dom')
    const initialClicks = await record.view.webContents.executeJavaScript('window.clicks')
    const failedBatch = await record.driver.batch({ actions: [
      { action: 'fill', ref: 'missing-ref', text: 'unused' },
      { action: 'click', ref: dom.observation.refs.find(value => value.name === 'Increment').ref },
    ], observationMode: 'dom' }, record.generation, guard, signal)
    const afterClicks = await record.view.webContents.executeJavaScript('window.clicks')
    const dialogResults = []
    for (const [name, answer] of [['Confirm fixture', undefined], ['Alert fixture', undefined]]) {
      const snapshot = await observe('dom')
      const ref = snapshot.observation.refs.find(value => value.name === name).ref
      const started = Date.now()
      const blocked = await record.driver.act({ action: 'click', ref }, record.generation, guard, signal)
      const elapsed = Date.now() - started
      const pending = record.driver.pendingDialog
      if (!pending) throw new Error(JSON.stringify({ name, blocked, state: record.driver.browserState(), elapsed }))
      const observedBlocked = await observe()
      let staleDialog
      try { await record.driver.handleDialog({ dialogId: 'wrong-dialog', accept: true }, guard, signal) }
      catch (error) { staleDialog = error.code }
      const handled = await record.driver.handleDialog({ dialogId: pending.id, accept: true, ...(answer ? { promptText: answer } : {}) }, guard, signal)
      await record.driver.waitForIdle()
      const settled = await observe('dom')
      dialogResults.push({ blocked, elapsed, pending, observedBlocked, handled, staleDialog, settled: settled.observation.consistency })
    }
    const dialogState = await record.view.webContents.executeJavaScript('({confirmed:window.confirmed,alerted:window.alerted})')
    await record.view.webContents.executeJavaScript(`(()=>{
      const noise=document.createElement('div');noise.id='noise';for(let i=0;i<180;i++){const b=document.createElement('button');b.textContent='Background '+i;noise.append(b)}document.body.append(noise);
      const dialog=document.createElement('dialog');dialog.innerHTML='<button id="modal-choice">Modal choice</button>';
      dialog.querySelector('button').onclick=()=>dialog.close();document.body.append(dialog);dialog.showModal();
    })()`)
    const modalObservation = await observe('dom')
    const modalRef = modalObservation.observation.refs.find(item => item.name === 'Modal choice')
    await record.driver.act({ action: 'click', ref: modalRef.ref }, record.generation, guard, signal)
    await record.view.webContents.executeJavaScript(`document.querySelector('#noise').remove();document.querySelector('dialog').remove();
      (()=>{const frame=document.createElement('iframe');frame.src='/frame';frame.sandbox='allow-scripts allow-modals';document.body.append(frame)})()`)
    for (let attempt = 0; attempt < 30 && !record.driver.page.frames().some(frame => frame.url().endsWith('/frame')); attempt++) {
      await new Promise(resolve => setTimeout(resolve, 50))
    }
    const framed = await observe('dom')
    const frameRef = framed.observation.refs.find(item => item.name === 'Frame action')
    await record.driver.act({ action: 'click', ref: frameRef.ref }, record.generation, guard, signal)
    const frameAfter = await observe('dom')
    const frameConfirmRef = frameAfter.observation.refs.find(item => item.name === 'Frame confirm').ref
    const frameBlocked = await record.driver.act({ action: 'click', ref: frameConfirmRef }, record.generation, guard, signal)
    await record.driver.handleDialog({ dialogId: record.driver.pendingDialog.id, accept: true }, guard, signal)
    await record.driver.waitForIdle()
    const frameConfirmed = await observe('dom')
    await record.view.webContents.executeJavaScript(`document.querySelector('iframe').remove();
      (()=>{const c=document.createElement('canvas');c.id='pixel-fixture';c.width=100;c.height=100;c.style='position:fixed;left:15px;top:15px;z-index:1000';document.body.append(c);c.getContext('2d').fillRect(0,0,100,100)})()`)
    const canvas = await observe()
    await record.view.webContents.executeJavaScript(`(()=>{const c=document.querySelector('canvas').getContext('2d');c.fillStyle='red';c.fillRect(0,0,100,100)})()`)
    let changedPixels
    try { await record.driver.act({ action: 'click', x: 40, y: 40, observationId: canvas.observation.observationId,
      imageId: canvas.observation.image.imageId }, record.generation, guard, signal) }
    catch (error) { changedPixels = failureDetails(error) }
    await record.view.webContents.executeJavaScript(`(()=>{const canvas=document.querySelector('canvas');
      canvas.onmousemove=()=>{const c=canvas.getContext('2d');c.fillStyle='blue';c.fillRect(0,0,100,100)};
      canvas.onmousedown=()=>window.unexpectedPresses=(window.unexpectedPresses||0)+1})()`)
    const beforeHover = await observe()
    let changedDuringMovement
    try { await record.driver.act({ action: 'click', x: 40, y: 40, observationId: beforeHover.observation.observationId,
      imageId: beforeHover.observation.image.imageId }, record.generation, guard, signal) }
    catch (error) { changedDuringMovement = failureDetails(error) }
    const unexpectedPresses = await record.view.webContents.executeJavaScript('window.unexpectedPresses||0')
    await record.view.webContents.executeJavaScript('document.querySelector("canvas").remove()')
    const beforeNavigation = await observe()
    await record.view.webContents.loadURL(record.view.webContents.getURL())
    let navigated
    try { await record.driver.act({ action: 'click', ...point, observationId: beforeNavigation.observation.observationId,
      imageId: beforeNavigation.observation.image.imageId }, record.generation, guard, signal) }
    catch (error) { navigated = failureDetails(error) }
    return { consistency: clicked.observation.consistency, image, clicked: clicked.execution, withoutReceipt, hiddenCoordinate, resized, mutated, navigated,
      beforeCoordinateClick, afterCoordinateClick, consumedFrameFailure, afterConsumedFrame, afterOldImageClick, unrelatedMutation, scrolled, mutationObservationId, beforeRejectedClick, afterRejectedClick,
      domImageStatus: dom.observation.imageStatus, domHasBytes: 'dataBase64' in dom, failedBatch, initialClicks, afterClicks, dialogResults, dialogState,
      modalRefs: modalObservation.observation.refs, frameRef, frameAfter: frameAfter.observation.text,
      frameBlocked, frameConfirmed: frameConfirmed.observation.text, changedPixels, changedDuringMovement, unexpectedPresses }
  })
  assert.equal(observationChecks.withoutReceipt.performed, true)
  assert.equal(observationChecks.afterCoordinateClick, observationChecks.beforeCoordinateClick + 1,
    'current screenshot coordinates execute without provider delivery receipts')
  assert.equal(observationChecks.consumedFrameFailure.code, 'STALE_OBSERVATION')
  assert.equal(observationChecks.consumedFrameFailure.observationReason, 'observation_missing')
  assert.equal(observationChecks.consumedFrameFailure.outcome, 'not_started')
  assert.equal(observationChecks.afterConsumedFrame, observationChecks.afterCoordinateClick,
    'a consumed frame cannot repeat the click')
  assert.equal(observationChecks.afterOldImageClick, observationChecks.afterCoordinateClick + 1, 'an unchanged screenshot remains usable after three virtual minutes')
  assert.equal(observationChecks.unrelatedMutation.performed, true, 'unrelated DOM changes do not invalidate unchanged target pixels')
  assert.equal(observationChecks.hiddenCoordinate, 'VISUAL_TARGET_HIDDEN')
  assert.equal(observationChecks.clicked.state, 'completed', JSON.stringify(observationChecks.clicked))
  assert.equal(observationChecks.consistency, 'consistent')
  assert.equal(observationChecks.resized.code, 'STALE_OBSERVATION')
  assert.equal(observationChecks.resized.observationReason, 'viewport_changed')
  assert.equal(observationChecks.scrolled.observationReason, 'scroll_changed')
  assert.equal(observationChecks.mutated.execution.actions[0].code, 'STALE_OBSERVATION')
  assert.equal(observationChecks.mutated.execution.actions[0].observationReason, 'target_pixels_changed')
  assert.equal(observationChecks.mutated.execution.actions[0].outcome, 'not_started')
  assert.notEqual(observationChecks.mutated.observation.observationId, observationChecks.mutationObservationId)
  assert.equal(observationChecks.mutated.observation.imageStatus, 'available')
  assert.equal(observationChecks.beforeRejectedClick, observationChecks.afterRejectedClick, 'a fresh returned observation must not execute the old coordinates')
  assert.equal(observationChecks.navigated.code, 'STALE_OBSERVATION')
  assert.equal(observationChecks.domImageStatus, 'omitted')
  assert.equal(observationChecks.domHasBytes, false)
  assert.equal(observationChecks.failedBatch.execution.state, 'failed')
  assert.equal(observationChecks.failedBatch.execution.actions[1].state, 'not_started')
  assert.equal(observationChecks.initialClicks, observationChecks.afterClicks)
  for (const dialog of observationChecks.dialogResults) {
    assert.equal(dialog.blocked.execution.state, 'blocked')
    assert.ok(dialog.elapsed < 5000, 'dialogs should report blockage without the ordinary action timeout')
    assert.equal(dialog.observedBlocked.observation.consistency, 'blocked')
    assert.equal(dialog.observedBlocked.observation.imageStatus, 'unavailable')
    assert.equal(dialog.staleDialog, 'STALE_DIALOG')
    assert.equal(dialog.handled.performed, true)
    assert.equal(dialog.settled, 'consistent')
  }
  assert.deepEqual(observationChecks.dialogState, { confirmed: true, alerted: true })
  assert.equal(observationChecks.modalRefs[0].name, 'Modal choice')
  assert.equal(observationChecks.modalRefs[0].modal, true)
  assert.equal(observationChecks.modalRefs.some(ref => ref.name.startsWith('Background ')), false)
  assert.match(observationChecks.frameRef.frameUrl, /\/frame$/)
  assert.match(observationChecks.frameAfter, /Frame clicked/)
  assert.equal(observationChecks.frameBlocked.execution.state, 'blocked')
  assert.match(observationChecks.frameConfirmed, /Frame confirmed/)
  assert.equal(observationChecks.changedPixels.code, 'STALE_OBSERVATION')
  assert.equal(observationChecks.changedPixels.observationReason, 'target_pixels_changed')
  assert.equal(observationChecks.changedDuringMovement.code, 'STALE_OBSERVATION')
  assert.equal(observationChecks.changedDuringMovement.observationReason, 'target_pixels_changed')
  assert.equal(observationChecks.changedDuringMovement.outcome, 'not_started')
  assert.equal(observationChecks.unexpectedPresses, 0)
  assert.equal(observationChecks.image.width, observationChecks.image.viewportWidth)
  assert.equal(observationChecks.image.coordinateSpace, 'image-pixels')
  console.log('Observations, native image delivery, coordinate freshness, batches, and blocking dialog control verified.')

  const initialDialog = await app.evaluate(async ({ BrowserWindow, WebContentsView }, origin) => {
    const owner = new BrowserWindow({ show: true, webPreferences: { sandbox: true } })
    const view = new WebContentsView({ webPreferences: { sandbox: true, partition: 'initial-dialog-fixture' } })
    owner.contentView.addChildView(view)
    view.setBounds({ x: 0, y: 0, width: 800, height: 600 })
    await view.webContents.loadURL('about:blank')
    const driver = new globalThis.__BrowserPlaywrightDriver(view.webContents)
    const signal = new AbortController().signal
    const guard = () => { if (view.webContents.isDestroyed()) throw new Error('Page closed') }
    await driver.initialize(guard, signal)
    const cancelled = new AbortController()
    const waiting = driver.waitForPendingDialog(cancelled.signal).catch(error => error.code)
    cancelled.abort()
    const cancelledCode = await waiting
    const awaitingDialog = driver.waitForPendingDialog(signal)
    const navigation = view.webContents.loadURL(origin + '/opening-dialog')
    const pending = await awaitingDialog
    const blocked = await driver.observe(1, guard, signal)
    await driver.handleDialog({ dialogId: pending.id, accept: true }, guard, signal)
    await navigation
    const loaded = await driver.observe(1, guard, signal, 'dom')
    const accepted = await view.webContents.executeJavaScript('window.accepted')
    await driver.dispose()
    owner.destroy()
    return { cancelledCode, pending, blocked: blocked.observation.consistency, loaded: loaded.observation.text, accepted }
  }, origin)
  assert.equal(initialDialog.cancelledCode, 'TIMEOUT')
  assert.equal(initialDialog.pending.message, 'Initial fixture dialog')
  assert.equal(initialDialog.blocked, 'blocked')
  assert.equal(initialDialog.accepted, true)
  assert.match(initialDialog.loaded, /Loaded after initial dialog/)

  const lifecycle = await app.evaluate(async ({}, origin) => {
    const { records, owner } = globalThis.attachFixture
    const record = records[0]
    const signal = new AbortController().signal
    const guard = () => { if (record.view.webContents.isDestroyed()) throw new Error('Page closed') }
    let snapshot = await record.driver.snapshot(record.generation, guard, signal)
    const oldRef = snapshot.refs.find(item => item.name === 'Increment').ref
    await record.view.webContents.executeJavaScript('document.querySelector("button").replaceWith(document.querySelector("button").cloneNode(true))')
    let staleReplacement
    try { await record.driver.act({ action: 'click', ref: oldRef }, record.generation, guard, signal) }
    catch (error) { staleReplacement = error.code }
    record.view.setVisible(false)
    snapshot = await record.driver.snapshot(record.generation, guard, signal)
    await record.driver.act({ action: 'click', ref: snapshot.refs.find(item => item.name === 'Increment').ref }, record.generation, guard, signal)
    const hiddenImage = await record.driver.screenshot(guard, signal)
    const stillHidden = !record.view.getVisible()
    const previousGeneration = record.generation
    await record.view.webContents.loadURL(origin + '/next')
    let staleNavigation
    try { await record.driver.act({ action: 'click', ref: oldRef }, record.generation, guard, signal) }
    catch (error) { staleNavigation = error.code }
    const navigated = await record.driver.snapshot(record.generation, guard, signal)
    await record.driver.dispose()
    const retained = !owner.isDestroyed() && !record.view.webContents.isDestroyed()
    const stillAttached = record.view.webContents.debugger.isAttached()
    record.driver = new globalThis.__BrowserPlaywrightDriver(record.view.webContents)
    const reconnected = await record.driver.snapshot(record.generation, guard, signal)
    const login = await record.view.webContents.executeJavaScript('localStorage.getItem("syntheticLogin")')
    const cancellation = new AbortController()
    const pending = record.driver.act({ action: 'click', ref: reconnected.refs.find(item => item.name === 'Delayed action').ref },
      record.generation, guard, cancellation.signal)
    setTimeout(() => cancellation.abort(), 100)
    let cancelled
    try { await pending } catch (error) { cancelled = error.code }
    await record.view.webContents.executeJavaScript('document.getElementById("delayed").disabled=false')
    const afterCancel = await record.driver.snapshot(record.generation, guard, signal)
    await record.driver.act({ action: 'click', ref: afterCancel.refs.find(item => item.name === 'Increment').ref }, record.generation, guard, signal)
    const afterCancelCounts = await record.view.webContents.executeJavaScript('({clicks:window.clicks||0,delayedClicks:window.delayedClicks||0})')
    await Promise.all(records.map(item => item.driver.dispose()))
    return { staleReplacement, staleNavigation, stillHidden, hiddenWidth: hiddenImage.width,
      generationAdvanced: record.generation > previousGeneration, navigated: navigated.text,
      reconnected: reconnected.text, login, retained, stillAttached, cancelled, afterCancelCounts, contentsId: record.view.webContents.id }
  }, origin)
  assert.equal(lifecycle.staleReplacement, 'STALE_ELEMENT')
  assert.equal(lifecycle.staleNavigation, 'STALE_ELEMENT')
  assert.equal(lifecycle.stillHidden, true)
  assert.ok(lifecycle.hiddenWidth > 0)
  assert.equal(lifecycle.generationAdvanced, true)
  assert.match(lifecycle.navigated, /Next document/)
  assert.match(lifecycle.reconnected, /Next document/)
  assert.equal(lifecycle.login, 'first-login')
  assert.equal(lifecycle.retained, true)
  assert.equal(lifecycle.stillAttached, true)
  assert.equal(lifecycle.contentsId, before[0].contentsId)
  assert.equal(lifecycle.cancelled, 'TIMEOUT')
  assert.deepEqual(lifecycle.afterCancelCounts, { clicks: 1, delayedClicks: 0 })
  const failures = await app.evaluate(async ({ BrowserWindow, WebContentsView }, origin) => {
    const owner = new BrowserWindow({ show: true, webPreferences: { sandbox: true } })
    const view = new WebContentsView({ webPreferences: { sandbox: true, partition: 'failure-fixture' } })
    owner.contentView.addChildView(view)
    view.setBounds({ x: 0, y: 0, width: 800, height: 600 })
    await view.webContents.loadURL(origin)
    view.webContents.debugger.attach('1.3')
    const driver = new globalThis.__BrowserPlaywrightDriver(view.webContents)
    let generation = 1
    view.webContents.on('did-navigate', () => { generation++; driver.invalidate() })
    const signal = new AbortController().signal
    const guard = () => { if (view.webContents.isDestroyed()) throw new Error('The fixture page closed.') }
    const beforeListeners = view.webContents.debugger.listenerCount('message')
    const alreadyAborted = new AbortController()
    alreadyAborted.abort()
    let preCancelled
    try { await driver.snapshot(generation, guard, alreadyAborted.signal) }
    catch (error) { preCancelled = error.code }
    const listenersAfterPreCancel = view.webContents.debugger.listenerCount('message')
    const nativeSend = view.webContents.debugger.sendCommand.bind(view.webContents.debugger)
    const initializing = new AbortController()
    view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      const result = await nativeSend(method, params, sessionId)
      if (method === 'Target.attachToTarget') initializing.abort()
      return result
    }
    let attachCancelled
    try { await driver.snapshot(generation, guard, initializing.signal) }
    catch (error) { attachCancelled = error.code }
    view.webContents.debugger.sendCommand = nativeSend
    const recovered = await driver.snapshot(generation, guard, signal)
    // Replace the real document after Chromium has captured pixels but before
    // its response reaches the driver. The old screenshot must be discarded.
    let moved = false
    view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      const result = await nativeSend(method, params, sessionId)
      if (method === 'Page.captureScreenshot' && !moved) {
        moved = true
        await view.webContents.loadURL(origin + '/next')
      }
      return result
    }
    let screenshotChanged
    try { await driver.screenshot(guard, signal) }
    catch (error) { screenshotChanged = error.code }
    view.webContents.debugger.sendCommand = nativeSend
    const next = await driver.snapshot(generation, guard, signal)
    const waitRef = next.refs.find(node => node.name === 'Delayed action').ref
    const waiting = driver.act({ action: 'click', ref: waitRef }, generation, guard, signal)
    setTimeout(() => view.webContents.close(), 100)
    let closedRejected = false
    try { await waiting } catch { closedRejected = true }
    await driver.dispose()
    owner.destroy()
    return { preCancelled, attachCancelled, beforeListeners, listenersAfterPreCancel,
      recovered: recovered.text, screenshotChanged, moved, closedRejected }
  }, origin)
  assert.equal(failures.preCancelled, 'TIMEOUT')
  assert.equal(failures.attachCancelled, 'TIMEOUT')
  assert.equal(failures.listenersAfterPreCancel, failures.beforeListeners)
  assert.match(failures.recovered, /Existing document/)
  assert.equal(failures.moved, true)
  assert.equal(failures.screenshotChanged, 'PAGE_CHANGED')
  assert.equal(failures.closedRejected, true)
  console.log('Playwright attachment verified: existing partitions, exact targets, retained state, scoped commands, actions, hidden capture, navigation, debugger ownership, reconnect.')
} catch (error) {
  console.error(error)
  throw error
} finally {
  await rootBrowser?.close().catch(() => {})
  if (app) {
    const closing = app.close().catch(() => {})
    const timer = setTimeout(() => app.process().kill('SIGKILL'), 5000)
    timer.unref()
    await closing
    clearTimeout(timer)
  }
  server.closeAllConnections()
  await new Promise(resolve => server.close(resolve))
  await rm(isolatedRoot, { recursive: true, force: true })
}
