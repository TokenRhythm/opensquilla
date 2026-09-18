import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { _electron as electron } from 'playwright'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_BROWSER_UNDER_XVFB !== '1') {
  const child = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_BROWSER_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (child.error) throw child.error
  process.exit(child.status ?? 1)
}
let revision = 1
const requests = { working: 0, immutable: 0 }
const server = createServer((request, response) => {
  // Commit a real replacement document, but keep its load pending until stop.
  if (request.url === '/pending-navigation') {
    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    response.write('<!doctype html><title>Pending navigation</title><body>Still loading')
    return
  }
  if (request.url === '/actionability') {
    response.setHeader('content-type', 'text/html; charset=utf-8')
    response.end(`<!doctype html><title>Actionability fixture</title>
      <style>html{scroll-behavior:smooth}body{padding:24px}#below{display:block;margin-top:2400px}
      #covered{position:fixed;top:190px;left:40px;width:150px;height:50px}
      #cover{position:fixed;z-index:100;top:180px;left:30px;width:200px;height:100px;background:red}</style>
      <button disabled>Disabled action</button><div style="opacity:0"><button>Invisible action</button></div>
      <button id="below" onclick="window.belowClicks=(window.belowClicks||0)+1">Below fold</button>
      <button id="covered" onclick="window.coveredClicks=(window.coveredClicks||0)+1">Covered target</button>
      <div id="cover" onclick="window.obstructionClicks=(window.obstructionClicks||0)+1">Obstruction</div>`)
    return
  }
  const working = request.url === '/working'
  if (request.method === 'HEAD') requests[working ? 'working' : 'immutable']++
  response.setHeader('content-type', 'text/html; charset=utf-8')
  response.setHeader('cache-control', 'no-store')
  response.setHeader('etag', `"${working ? revision : 1}"`)
  if (working) response.setHeader('x-opensquilla-working-preview', '1')
  response.end(`<!doctype html><title>Browser fixture</title><style>body{min-height:1500px;padding:24px;background:${request.url === '/hidden-open' ? '#2040d0' : 'white'}}</style>
    <h1>Revision ${working ? revision : 1}</h1><button id="increment" onclick="window.count=(window.count||0)+1">Increment</button>
    <input id="value" aria-label="Value"><select aria-label="Choice"><option value="a">A</option><option value="b">B</option></select>
    <form action="/submitted"><button id="submit">Submit</button></form>`)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const port = server.address().port
const origin = `http://p-${'c'.repeat(32)}.localhost:${port}`
const root = await mkdtemp(join(tmpdir(), 'opensquilla-browser-e2e-'))
let app
let processExit
try {
  app = await electron.launch({ args: [`--user-data-dir=${join(root, 'chromium')}`,
    fileURLToPath(new URL('./fixtures/native-workbench-smoke', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '*', no_proxy: '*' } })
  processExit = new Promise(resolve => app.process().once('exit', (code, signal) => {
    console.log('Browser fixture process exit:', JSON.stringify({ code, signal }))
    resolve({ code, signal })
  }))
  const setup = await app.evaluate(async ({ BrowserWindow, ipcMain }, { origin, preload }) => {
    const owner = new BrowserWindow({ show: true, width: 1000, height: 800,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, preload } })
    await owner.loadURL('data:text/html,<title>Browser test host</title>')
    const manager = new globalThis.__opensquillaNativeWorkbenchSurfaceManager({ getWindow: () => owner, emit() {} })
    const navigationObservations = new Map()
    globalThis.browserFixture = { manager, owner, navigationObservations }
    // Use the shipped preload/channel and real parser/manager. Only the isolated
    // fixture's owner main frame may issue this command; no product boot occurs.
    ipcMain.handle('desktop:workbench:surface:navigate', async (event, payload) => {
      if (event.sender !== owner.webContents || event.senderFrame !== owner.webContents.mainFrame) {
        throw new Error('Unexpected browser fixture IPC sender')
      }
      const request = globalThis.__opensquillaParseNativeWorkbenchNavigationRequest(payload)
      const record = manager.surfaces.get(request.surfaceId)
      if (request.action === 'stop') {
        const contents = record.view.webContents
        navigationObservations.set(record.id, {
          beforeStop: { loading: contents.isLoading(), ready: record.browserDocumentReady },
          stopped: new Promise(resolve => contents.once('did-stop-loading', resolve)),
        })
      }
      return await manager.navigateSurface(request)
    })
    const targets = []
    for (const [surfaceId, sessionKey, path] of [
      ['first', 'session-a', '/'], ['same-url', 'session-a', '/'], ['other-session', 'session-b', '/'],
      ['working', 'session-a', '/working'],
    ]) {
      const result = await manager.createSurface({ version: 4, surfaceId, kind: 'artifact-preview',
        payload: { launchUrl: origin + path, expectedOrigin: origin, scopeId: sessionKey, mode: 'full' } })
      if (!result.ok) throw new Error(result.message)
      manager.setSurfaceRect({ surfaceId, x: 100, y: 80, width: 700, height: 600, visible: true })
      targets.push(manager.getBrowserTarget(surfaceId))
    }
    manager.activateSurface('first')
    const browser = new globalThis.__opensquillaDesktopBrowserServer((request, signal) => manager.executeBrowser(request, signal))
    globalThis.browserFixture.server = browser
    return { targets, environment: await browser.start() }
  }, { origin, preload: fileURLToPath(new URL('../dist/preload.cjs', import.meta.url)) })
  const controlPage = (await Promise.all(app.windows().map(async page => ({ page, title: await page.title() }))))
    .find(({ title }) => title === 'Browser test host')?.page
  assert.ok(controlPage, 'the fixture owner renderer must expose the shipped preload')
  const [first, sameUrl, other, working] = setup.targets
  const invoke = async (body, deadline) => {
    const response = await fetch(setup.environment.OPENSQUILLA_DESKTOP_BROWSER_URL, {
      method: 'POST', headers: { Authorization: `Bearer ${setup.environment.OPENSQUILLA_DESKTOP_BROWSER_TOKEN}`,
        'Content-Type': 'application/json', ...(deadline ? { 'x-opensquilla-deadline-at-ms': String(deadline) } : {}) },
      body: JSON.stringify({ sessionKey: 'session-a', ...body }),
    })
    return { status: response.status, ...(await response.json()) }
  }
  const snapshot = await invoke({ operation: 'snapshot', targetRef: first.targetRef })
  assert.equal(snapshot.status, 200)
  assert.match(snapshot.text, /Revision 1/)
  const button = snapshot.refs.find(item => item.name === 'Increment')
  const input = snapshot.refs.find(item => item.name === 'Value')
  const select = snapshot.refs.find(item => item.name === 'Choice')
  assert.ok(button && input && select)
  assert.equal((await invoke({ operation: 'act', targetRef: first.targetRef, action: 'click', ref: button.ref })).status, 200)
  assert.equal((await invoke({ operation: 'act', targetRef: first.targetRef, action: 'fill', ref: input.ref, text: 'hello preview' })).status, 200)
  assert.equal((await invoke({ operation: 'act', targetRef: first.targetRef, action: 'select', ref: select.ref, text: 'b' })).status, 200)
  assert.equal((await invoke({ operation: 'act', targetRef: first.targetRef, action: 'hover', ref: button.ref })).status, 200)
  assert.equal((await invoke({ operation: 'act', targetRef: first.targetRef, action: 'scroll', direction: 'down', amount: 100 })).status, 200)
  const state = await app.evaluate(async () => {
    const manager = globalThis.browserFixture.manager
    return await Promise.all(['first','same-url'].map(id => manager.surfaces.get(id).view.webContents.executeJavaScript(
      '({count:window.count||0,value:document.querySelector("input").value,choice:document.querySelector("select").value})')))
  })
  assert.deepEqual(state, [{count:1,value:'hello preview',choice:'b'},{count:0,value:'',choice:'a'}])
  assert.notEqual(first.targetRef, sameUrl.targetRef)
  assert.equal((await invoke({operation:'snapshot',targetRef:other.targetRef})).status,404)
  const listing = await invoke({ operation:'list' })
  assert.equal(listing.targets.length,3)
  assert.ok(listing.targets.every(target => target.sessionKey === 'session-a'))
  const screenshot = await invoke({operation:'screenshot',targetRef:first.targetRef})
  assert.equal(screenshot.mimeType,'image/png')
  assert.ok(screenshot.width > 0 && screenshot.height > 0)
  assert.equal(Buffer.from(screenshot.dataBase64,'base64').subarray(1,4).toString(),'PNG')
  await app.evaluate(async () => {
    await globalThis.browserFixture.manager.surfaces.get('first').view.webContents.executeJavaScript(
      'document.querySelector("#increment").replaceWith(document.querySelector("#increment").cloneNode(true))')
  })
  assert.equal((await invoke({operation:'act',targetRef:first.targetRef,action:'click',ref:button.ref})).code,'STALE_ELEMENT')
  const fresh = await invoke({operation:'snapshot',targetRef:first.targetRef})
  const freshButton = fresh.refs.find(item=>item.name==='Increment')
  await app.evaluate(() => {
    const record = globalThis.browserFixture.manager.surfaces.get('first')
    record.cdpQueue = new Promise(resolve => { globalThis.browserFixture.releaseQueue = resolve })
  })
  const cancelled = await invoke({operation:'act',targetRef:first.targetRef,action:'click',ref:freshButton.ref},Date.now()+80)
  assert.equal(cancelled.status,504)
  await app.evaluate(() => globalThis.browserFixture.releaseQueue())
  assert.equal((await invoke({operation:'snapshot',targetRef:first.targetRef})).status,200)
  assert.equal(await app.evaluate(async () => await globalThis.browserFixture.manager.surfaces.get('first').view.webContents.executeJavaScript('window.count')),1)
  const beforeWorking = await app.evaluate(() => globalThis.browserFixture.manager.surfaces.get('working').view.webContents.id)
  const beforeWorkingSnapshot = await invoke({operation:'snapshot',targetRef:working.targetRef})
  revision = 2
  await app.evaluate(async () => {
    const record = globalThis.browserFixture.manager.surfaces.get('working')
    const end = Date.now()+7000
    while(Date.now()<end) {
      if (record.browserDocumentReady && await record.view.webContents.executeJavaScript('document.querySelector("h1")?.innerText') === 'Revision 2') return
      await new Promise(resolve=>setTimeout(resolve,100))
    }
    const probe = await record.previewSession.fetch(record.documentUrl,{method:'HEAD',cache:'no-store',redirect:'error'}).then(r=>({status:r.status,headers:Object.fromEntries(r.headers)}),e=>({error:String(e)}))
    throw new Error('Working file preview did not refresh: '+JSON.stringify({probe,loading:record.view.webContents.isLoading(),ready:record.browserDocumentReady,timer:Boolean(record.revisionTimer)}))
  })
  assert.equal(await app.evaluate(() => globalThis.browserFixture.manager.surfaces.get('working').view.webContents.id),beforeWorking)
  assert.equal((await invoke({operation:'act',targetRef:working.targetRef,action:'click',ref:beforeWorkingSnapshot.refs.find(item=>item.name==='Increment').ref})).code,'STALE_ELEMENT')
  const refreshedSnapshot = await invoke({operation:'snapshot',targetRef:working.targetRef})
  const beforeReload = await app.evaluate(() => {
    const manager = globalThis.browserFixture.manager
    manager.setSurfaceRect({surfaceId:'working',x:100,y:80,width:700,height:600,visible:true})
    manager.activateSurface('working')
    const record = manager.surfaces.get('working')
    return {generation:record.annotationDocumentGeneration,instanceId:record.surfaceInstanceId,
      bounds:record.view.getBounds(),ownerBounds:record.owner.getContentBounds()}
  })
  const reload = await invoke({operation:'reload',targetRef:working.targetRef})
  assert.equal(reload.status,200)
  assert.equal(reload.targetRef,working.targetRef)
  const afterReload = await app.evaluate(async ({}, before) => {
    const manager = globalThis.browserFixture.manager
    const record = manager.surfaces.get('working')
    const end = Date.now()+3000
    while (Date.now()<end) {
      if (record.annotationDocumentGeneration>before.generation && record.browserDocumentReady) {
        return {webContentsId:record.view.webContents.id,instanceId:record.surfaceInstanceId,
          visible:record.view.getVisible(),bounds:record.view.getBounds()}
      }
      await new Promise(resolve=>setTimeout(resolve,20))
    }
    throw new Error('Explicit working preview reload did not become ready')
  },beforeReload)
  assert.equal(afterReload.webContentsId,beforeWorking)
  assert.equal(afterReload.instanceId,beforeReload.instanceId)
  assert.equal(afterReload.visible,true)
  assert.deepEqual(beforeReload.bounds,{x:100,y:80,
    width:Math.min(700,beforeReload.ownerBounds.width-100),
    height:Math.min(600,beforeReload.ownerBounds.height-80)})
  assert.ok(beforeReload.bounds.width>0 && beforeReload.bounds.height>0)
  assert.deepEqual(afterReload.bounds,beforeReload.bounds)
  assert.equal((await invoke({operation:'act',targetRef:working.targetRef,action:'click',ref:refreshedSnapshot.refs.find(item=>item.name==='Increment').ref})).code,'STALE_ELEMENT')
  const afterReloadSnapshot = await invoke({operation:'snapshot',targetRef:working.targetRef})
  assert.equal((await invoke({operation:'act',targetRef:working.targetRef,action:'click',ref:afterReloadSnapshot.refs.find(item=>item.name==='Increment').ref})).status,200)
  assert.equal(await app.evaluate(async () => globalThis.browserFixture.manager.surfaces.get('working').view.webContents.executeJavaScript('window.count')),1)
  assert.equal(requests.immutable,3,'immutable previews should probe once and stop')
  const replacement = await app.evaluate(async ({}, origin) => {
    const manager = globalThis.browserFixture.manager
    await manager.destroySurface('first')
    await manager.createSurface({version:4,surfaceId:'first',kind:'artifact-preview',payload:{launchUrl:origin+'/',expectedOrigin:origin,scopeId:'session-a',mode:'full'}})
    return manager.getBrowserTarget('first')
  },origin)
  assert.notEqual(replacement.targetRef,first.targetRef)
  assert.equal((await invoke({operation:'screenshot',targetRef:first.targetRef})).status,404)
  assert.equal((await invoke({operation:'open',url:'file:///synthetic/denied.html'})).status,409)
  const foregroundBeforeOpen = await app.evaluate(() => {
    const { manager, owner } = globalThis.browserFixture
    const record = manager.surfaces.get(manager.activeSurfaceId)
    return {id:record.id,webContentsId:record.view.webContents.id,visible:record.view.getVisible(),
      bounds:record.view.getBounds(),focused:owner.isFocused(),surfaceCount:manager.surfaces.size}
  })
  const opened = await invoke({operation:'open',url:origin+'/hidden-open'})
  assert.equal(opened.status,200)
  const openedSnapshot = await invoke({operation:'snapshot',targetRef:opened.targetRef})
  assert.equal(openedSnapshot.status,200)
  assert.equal((await invoke({operation:'act',targetRef:opened.targetRef,action:'click',
    ref:openedSnapshot.refs.find(item=>item.name==='Increment').ref})).status,200,
  'a newly opened hidden page must support interaction without a preceding screenshot')
  const hiddenState = await app.evaluate(async ({}, targetRef) => {
    const { manager, owner } = globalThis.browserFixture
    const record = [...manager.surfaces.values()].find(item=>item.targetRef===targetRef)
    const foreground = manager.surfaces.get(manager.activeSurfaceId)
    return {foreground:{id:foreground.id,webContentsId:foreground.view.webContents.id,
      visible:foreground.view.getVisible(),bounds:foreground.view.getBounds(),focused:owner.isFocused(),
      surfaceCount:manager.surfaces.size},id:record.id,webContentsId:record.view.webContents.id,
      targetRef:record.targetRef,visible:record.view.getVisible(),
      viewport:await record.view.webContents.executeJavaScript(
        '({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,count:window.count||0})')}
  },opened.targetRef)
  assert.deepEqual(hiddenState.foreground,{...foregroundBeforeOpen,surfaceCount:foregroundBeforeOpen.surfaceCount+1})
  assert.equal(hiddenState.visible,false)
  assert.deepEqual({width:hiddenState.viewport.width,height:hiddenState.viewport.height,count:hiddenState.viewport.count},
    {width:960,height:720,count:1})
  const assertOpenedScreenshot = async viewport => {
    const shot = await invoke({operation:'screenshot',targetRef:opened.targetRef})
    assert.equal(shot.status,200,JSON.stringify(shot))
    const pixels = await app.evaluate(({nativeImage}, encoded) => {
      const image = nativeImage.createFromBuffer(Buffer.from(encoded,'base64'))
      const {width,height} = image.getSize()
      return {width,height,bgra:[...image.crop({x:Math.floor(width/2),y:Math.floor(height/2),width:1,height:1}).toBitmap()]}
    },shot.dataBase64)
    assert.equal(pixels.width,shot.width)
    assert.equal(pixels.height,shot.height)
    // NativeImage may export logical pixels; CDP may return device pixels.
    const scale = shot.width / viewport.width
    assert.ok(Math.abs(scale-1)<0.01 || Math.abs(scale-viewport.dpr)<0.01)
    assert.ok(Math.abs(shot.height-viewport.height*scale)<=1)
    assert.ok(pixels.bgra[0]>pixels.bgra[1]+60 && pixels.bgra[0]>pixels.bgra[2]+60,
      'the captured pixels must belong to the blue hidden page, not the white foreground')
  }
  await assertOpenedScreenshot(hiddenState.viewport)
  const hiddenReload = await invoke({operation:'reload',targetRef:opened.targetRef})
  assert.equal(hiddenReload.status,200)
  assert.equal(hiddenReload.targetRef,opened.targetRef)
  const reloadedHidden = await app.evaluate(async ({}, identity) => {
    const { manager, owner } = globalThis.browserFixture
    const record = manager.surfaces.get(identity.id)
    const end = Date.now()+3000
    let lastState
    while(Date.now()<end) {
      lastState = { ready: record.browserDocumentReady, bounds: record.view.getBounds(),
        visible: record.view.getVisible(), generation: record.annotationDocumentGeneration,
        cdpReady: record.cdpReady, debuggerAttached: record.view.webContents.debugger.isAttached() }
      if(record.browserDocumentReady) {
        const viewport=await record.view.webContents.executeJavaScript(
          '({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,count:window.count||0})')
        lastState.viewport = viewport
        if(viewport.width===960 && viewport.height===720) return {viewport,
          webContentsId:record.view.webContents.id,targetRef:record.targetRef,visible:record.view.getVisible(),
          active:manager.activeSurfaceId,focused:owner.isFocused()}
      }
      await new Promise(resolve=>setTimeout(resolve,20))
    }
    throw new Error(`Hidden browser reload did not retain its renderer viewport: ${JSON.stringify(lastState)}`)
  },{id:hiddenState.id})
  assert.equal(reloadedHidden.webContentsId,hiddenState.webContentsId)
  assert.equal(reloadedHidden.targetRef,opened.targetRef)
  assert.equal(reloadedHidden.visible,false)
  assert.equal(reloadedHidden.active,foregroundBeforeOpen.id)
  assert.equal(reloadedHidden.focused,foregroundBeforeOpen.focused)
  assert.equal(reloadedHidden.viewport.count,0)
  await assertOpenedScreenshot(reloadedHidden.viewport)

  // Hold the real document initialization after reload, then exercise UI
  // adoption and superseding navigation while its completion is still pending.
  await app.evaluate(() => {
    const fixture = globalThis.browserFixture
    const manager = fixture.manager
    fixture.initializeHiddenViewport = manager.initializeHiddenBrowserViewport
    fixture.waitForLifecycle = async (work, phase) => {
      let deadline
      try {
        return await Promise.race([work, new Promise((_, reject) => {
          deadline = setTimeout(() => reject(new Error(`Hidden viewport lifecycle timed out: ${phase}`)), 3000)
        })])
      } finally { clearTimeout(deadline) }
    }
    fixture.holdReload = async id => {
      const record = manager.surfaces.get(id)
      const gate = new Promise(resolve => { fixture.releaseHiddenViewport = resolve })
      manager.initializeHiddenBrowserViewport = async (candidate, assertCurrent) => {
        if (candidate === record) await gate
        return fixture.initializeHiddenViewport.call(manager, candidate, assertCurrent)
      }
      const loaded = new Promise(resolve => record.view.webContents.once('did-finish-load', resolve))
      record.view.webContents.reload()
      await fixture.waitForLifecycle(loaded, 'held reload document load')
      fixture.pendingHiddenViewport = record.browserViewportReady
      return { ready: record.browserDocumentReady, generation: record.annotationDocumentGeneration }
    }
    fixture.releaseReload = async () => {
      fixture.releaseHiddenViewport()
      const failure = await fixture.pendingHiddenViewport.then(() => null, error => String(error))
      manager.initializeHiddenBrowserViewport = fixture.initializeHiddenViewport
      return failure
    }
  })
  assert.equal((await app.evaluate(async ({}, id) =>
    globalThis.browserFixture.holdReload(id), hiddenState.id)).ready, false)
  const adoptedWhilePending = await app.evaluate(async ({}, id) => {
    const { manager, releaseReload, waitForLifecycle } = globalThis.browserFixture
    const record = manager.surfaces.get(id)
    const contents = record.view.webContents
    const generation = record.annotationDocumentGeneration
    const result = manager.setSurfaceRect({ surfaceId: id, x: 100, y: 80, width: 650, height: 500, visible: true })
    if (!result.ok) throw new Error(result.message)
    const failure = await releaseReload()
    const expected = { width: 650, height: 500 }
    const assertCurrent = () => {
      if (manager.surfaces.get(id) !== record || contents.isDestroyed()
        || record.annotationDocumentGeneration !== generation || !record.browserDocumentReady) {
        throw new Error('Adopted browser document changed while waiting for its renderer viewport')
      }
    }
    // Native setBounds is synchronous; delivery of that size to the renderer is
    // not. Keep the existing lifecycle budget and wait for the real viewport,
    // without emulation or accepting a native-bounds regression as the target.
    const deadline = Date.now() + 3000
    let viewport
    let cancelled = false
    let pollTimer
    try {
      await waitForLifecycle((async () => {
        while (!cancelled) {
          assertCurrent()
          viewport = await contents.executeJavaScript('({width:innerWidth,height:innerHeight})')
          assertCurrent()
          if (cancelled || Date.now() >= deadline) throw new Error('Renderer resize deadline expired')
          if (viewport.width === expected.width && viewport.height === expected.height) return
          await new Promise(resolve => { pollTimer = setTimeout(resolve, 20) })
        }
      })(), 'adopted renderer resize')
    } catch (error) {
      throw new Error(`Adopted browser renderer resize failed: ${JSON.stringify({
        expected, viewport, bounds: record.view.getBounds(), generation,
      })}`, { cause: error })
    } finally {
      cancelled = true
      clearTimeout(pollTimer)
    }
    return { failure, ready: record.browserDocumentReady, viewport, bounds: record.view.getBounds() }
  }, hiddenState.id)
  assert.equal(adoptedWhilePending.failure, null)
  assert.equal(adoptedWhilePending.ready, true)
  assert.deepEqual(adoptedWhilePending.bounds, { x: 100, y: 80, width: 650, height: 500 })
  assert.deepEqual(adoptedWhilePending.viewport, {
    width: adoptedWhilePending.bounds.width, height: adoptedWhilePending.bounds.height,
  })

  // All three independent cases must pass; these are bounded lifecycle cases,
  // not retries after a failed stop.
  for (let stopCase = 0; stopCase < 3; stopCase++) {
    const retiringPage = await invoke({ operation: 'open', url: origin+'/hidden-open' })
    assert.equal(retiringPage.status, 200)
    const retiredInitialization = await app.evaluate(async ({}, { targetRef, origin }) => {
      const trace = phase => process.stderr.write(`hidden-viewport-lifecycle: ${phase}\n`)
      const fixture = globalThis.browserFixture
      const record = [...fixture.manager.surfaces.values()].find(item => item.targetRef === targetRef)
      const old = await fixture.holdReload(record.id)
      trace('reload-held')
      const contents = record.view.webContents
      const started = new Promise(resolve => contents.once('did-start-navigation', resolve))
      void contents.loadURL(origin+'/pending-navigation').catch(() => {})
      await fixture.waitForLifecycle(started, 'replacement navigation start')
      trace('replacement-navigation-started')
      fixture.releaseHiddenViewport()
      const failure = await fixture.pendingHiddenViewport.then(() => null, error => String(error))
      trace('old-initialization-settled')
      const afterLateCompletion = { ready: record.browserDocumentReady, generation: record.annotationDocumentGeneration }
      return { id: record.id, old, failure, afterLateCompletion }
    }, { targetRef: retiringPage.targetRef, origin })
    // Stop through the real renderer preload -> ipcRenderer.invoke boundary,
    // while the replacement document is still loading. This avoids re-entering
    // Chromium from a private did-start-navigation hook in the same main task.
    const stop = await controlPage.evaluate(async surfaceId =>
      window.opensquillaDesktop.navigateWorkbenchSurface({ version: 4, surfaceId, action: 'stop' }),
    retiredInitialization.id)
    assert.equal(stop.ok, true, JSON.stringify(stop))
    const stoppedInitialization = await app.evaluate(async ({}, targetRef) => {
      const fixture = globalThis.browserFixture
      const record = [...fixture.manager.surfaces.values()].find(item => item.targetRef === targetRef)
      const contents = record.view.webContents
      const { beforeStop, stopped } = fixture.navigationObservations.get(record.id)
      await fixture.waitForLifecycle(stopped, 'replacement navigation stop')
      const loadingAfterStop = contents.isLoading()
      const readyAfterStop = record.browserDocumentReady
      // An already stopped/closed document cannot be revived by another late
      // completion of the initializer either.
      await fixture.manager.destroySurface(record.id)
      await fixture.releaseReload()
      return { beforeStop, readyAfterStop, loadingAfterStop,
        destroyed: contents.isDestroyed(), retained: fixture.manager.surfaces.has(record.id) }
    }, retiringPage.targetRef)
    assert.equal(retiredInitialization.old.ready, false)
    assert.match(retiredInitialization.failure, /browser document changed/)
    assert.ok(retiredInitialization.afterLateCompletion.generation > retiredInitialization.old.generation)
    assert.equal(retiredInitialization.afterLateCompletion.ready, false)
    assert.deepEqual(stoppedInitialization.beforeStop, { loading: true, ready: false })
    assert.equal(stoppedInitialization.readyAfterStop, false)
    assert.equal(stoppedInitialization.loadingAfterStop, false)
    assert.equal(stoppedInitialization.destroyed, true)
    assert.equal(stoppedInitialization.retained, false)
    console.log(`Hidden reload renderer IPC stop case ${stopCase + 1}:`, JSON.stringify(stoppedInitialization))
  }
  for (const action of ['stop', 'close']) {
    const pendingPage = await invoke({ operation: 'open', url: origin+'/hidden-open' })
    assert.equal(pendingPage.status, 200)
    const cancelled = await app.evaluate(async ({}, { targetRef, action }) => {
      const fixture = globalThis.browserFixture
      const record = [...fixture.manager.surfaces.values()].find(item => item.targetRef === targetRef)
      await fixture.holdReload(record.id)
      if (action === 'close') await fixture.manager.destroySurface(record.id)
      else await fixture.manager.navigateSurface({ version: 4, surfaceId: record.id, action: 'stop' })
      const failure = await fixture.releaseReload()
      const state = { failure, ready: record.browserDocumentReady,
        retained: fixture.manager.surfaces.has(record.id), recovered: null }
      if (action === 'stop') {
        const loaded = new Promise(resolve => record.view.webContents.once('did-finish-load', resolve))
        await fixture.manager.navigateSurface({ version: 4, surfaceId: record.id, action: 'reload' })
        await fixture.waitForLifecycle(loaded, 'stopped document reload recovery')
        await record.browserViewportReady
        state.recovered = { ready: record.browserDocumentReady, stopped: record.browserNavigationStopped,
          viewport: await record.view.webContents.executeJavaScript('({width:innerWidth,height:innerHeight})') }
        await fixture.manager.destroySurface(record.id)
      }
      return state
    }, { targetRef: pendingPage.targetRef, action })
    assert.match(cancelled.failure, /browser document changed/)
    assert.equal(cancelled.ready, false, `${action} must retire the pending document initializer`)
    assert.equal(cancelled.retained, action === 'stop')
    if (action === 'stop') assert.deepEqual(cancelled.recovered, {
      ready: true, stopped: false, viewport: { width: 960, height: 720 },
    })
  }
  for(const size of [{width:650,height:500},{width:420,height:600},{width:1200,height:900}]) {
    const adopted = await app.evaluate(async ({}, {id,size}) => {
      const { manager, owner } = globalThis.browserFixture
      const record=manager.surfaces.get(id)
      const result=manager.setSurfaceRect({surfaceId:id,x:100,y:80,...size,visible:true})
      if(!result.ok) throw new Error(result.message)
      const end=Date.now()+3000
      while(Date.now()<end) {
        const viewport=await record.view.webContents.executeJavaScript('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})')
        const bounds=record.view.getBounds()
        if(viewport.width===bounds.width && viewport.height===bounds.height) return {viewport,
          webContentsId:record.view.webContents.id,targetRef:record.targetRef,visible:record.view.getVisible(),
          active:manager.activeSurfaceId,bounds,ownerBounds:owner.getContentBounds()}
        await new Promise(resolve=>setTimeout(resolve,20))
      }
      throw new Error('Adopted browser did not follow the real native layout size')
    },{id:hiddenState.id,size})
    assert.equal(adopted.webContentsId,hiddenState.webContentsId)
    assert.equal(adopted.targetRef,opened.targetRef)
    assert.equal(adopted.active,hiddenState.id)
    assert.equal(adopted.visible,true)
    assert.deepEqual(adopted.bounds,{x:100,y:80,
      width:Math.min(size.width,adopted.ownerBounds.width-100),
      height:Math.min(size.height,adopted.ownerBounds.height-80)})
    assert.ok(adopted.bounds.width>0 && adopted.bounds.height>0)
    await assertOpenedScreenshot(adopted.viewport)
  }
  const openedShot = await invoke({operation:'screenshot',targetRef:opened.targetRef})
  assert.equal(openedShot.status,200,JSON.stringify(openedShot))
  const actionPage = await invoke({operation:'open',url:origin+'/actionability'})
  const actionSnapshot = await invoke({operation:'snapshot',targetRef:actionPage.targetRef})
  const actOn = name => invoke({operation:'act',targetRef:actionPage.targetRef,action:'click',
    ref:actionSnapshot.refs.find(item=>item.name===name).ref})
  assert.equal((await actOn('Below fold')).status,200)
  for (const name of ['Covered target','Disabled action','Invisible action']) {
    assert.equal((await actOn(name)).code,'ACTION_UNAVAILABLE',name)
  }
  const actionState = await app.evaluate(async ({}, targetRef) => {
    const record = [...globalThis.browserFixture.manager.surfaces.values()].find(item=>item.targetRef===targetRef)
    return await record.view.webContents.executeJavaScript(
      '({below:window.belowClicks||0,covered:window.coveredClicks||0,obstruction:window.obstructionClicks||0})')
  },actionPage.targetRef)
  assert.deepEqual(actionState,{below:1,covered:0,obstruction:0})
  const crashTarget = await app.evaluate(async ({}, origin) => {
    const { manager } = globalThis.browserFixture
    const created = await manager.createSurface({ version: 4, surfaceId: 'crash-target', kind: 'artifact-preview',
      payload: { launchUrl: origin+'/', expectedOrigin: origin, scopeId: 'session-a', mode: 'full' } })
    if (!created.ok) throw new Error(created.message || 'Crash target did not load.')
    return manager.getBrowserTarget('crash-target')
  }, origin)
  const crashSnapshot = await invoke({ operation: 'snapshot', targetRef: crashTarget.targetRef })
  assert.equal(crashSnapshot.status, 200)
  await app.evaluate(({}, targetRef) => {
    const fixture = globalThis.browserFixture
    fixture.crashGate = new Promise(resolve => { fixture.releaseCrashGate = resolve })
    fixture.manager.surfaceQueues.set(`operation:${targetRef}`, fixture.crashGate)
  }, crashTarget.targetRef)
  const queuedAtCrash = invoke({ operation: 'act', targetRef: crashTarget.targetRef,
    action: 'click', ref: crashSnapshot.refs.find(item => item.name === 'Increment').ref })
  await app.evaluate(async ({}, targetRef) => {
    const fixture = globalThis.browserFixture
    for (let i = 0; i < 200; i++) {
      if (fixture.manager.surfaceQueues.get(`operation:${targetRef}`) !== fixture.crashGate) return
      await new Promise(resolve => setTimeout(resolve, 10))
    }
    throw new Error('Browser action did not enter the target queue.')
  }, crashTarget.targetRef)
  const crashObserved = await app.evaluate(() => {
    const fixture = globalThis.browserFixture
    const record = fixture.manager.surfaces.get('crash-target')
    // Exercise the Electron lifecycle listener deterministically on the actual v4 page.
    // Renderer process termination itself is outside this browser-request regression.
    const handled = record.view.webContents.emit('render-process-gone', {}, { reason: 'crashed', exitCode: 1 })
    fixture.releaseCrashGate()
    return { handled, crashed: record.crashed }
  })
  assert.deepEqual(crashObserved, { handled: true, crashed: true })
  const failedAtCrash = await queuedAtCrash
  assert.equal(failedAtCrash.status, 404)
  assert.equal(failedAtCrash.code, 'TARGET_NOT_FOUND')
  assert.notEqual(failedAtCrash.performed, true)
  assert.equal((await invoke({ operation: 'screenshot', targetRef: crashTarget.targetRef })).code, 'TARGET_NOT_FOUND')
  assert.equal((await invoke({ operation: 'list' })).targets.some(item => item.targetRef === crashTarget.targetRef), false)
  assert.equal(await app.evaluate(({}, targetRef) =>
    globalThis.browserFixture.manager.surfaceQueues.has(`operation:${targetRef}`), crashTarget.targetRef), false)
  const afterCrash = await invoke({ operation: 'open', url: origin+'/' })
  assert.equal(afterCrash.status, 200)
  assert.notEqual(afterCrash.targetRef, crashTarget.targetRef)
  assert.equal((await invoke({ operation: 'snapshot', targetRef: afterCrash.targetRef })).status, 200)
  assert.equal((await invoke({ operation: 'snapshot', targetRef: working.targetRef })).status, 200)
  console.log('Real Electron browser actions, exact targets, cancellation, renderer crash and working-file refresh passed.')
} catch (error) {
  // Preserve the failed assertion even if native profile cleanup also fails.
  console.error('Desktop browser native contract failed:', error)
  throw error
} finally {
  if(app) {
    await app.evaluate(async () => {
      await globalThis.browserFixture?.server?.close()
      await globalThis.browserFixture?.manager?.destroyAll()
    }).catch(()=>{})
    await app.close()
    let exitDeadline
    try {
      const exit = await Promise.race([processExit, new Promise((_, reject) => {
        exitDeadline = setTimeout(() => reject(new Error('Browser fixture process did not exit after close')), 5000)
      })])
      assert.equal(exit.code, 0, JSON.stringify(exit))
      assert.equal(exit.signal, null, JSON.stringify(exit))
    } finally {
      clearTimeout(exitDeadline)
    }
  }
  server.closeAllConnections()
  await new Promise(resolve=>server.close(resolve))
  await rm(root,{recursive:true,force:true})
}
