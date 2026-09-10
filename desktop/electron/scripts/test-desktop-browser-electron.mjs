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
try {
  app = await electron.launch({ args: [`--user-data-dir=${join(root, 'chromium')}`,
    fileURLToPath(new URL('./fixtures/native-workbench-smoke', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '*', no_proxy: '*' } })
  const setup = await app.evaluate(async ({ BrowserWindow }, origin) => {
    const owner = new BrowserWindow({ show: true, width: 1000, height: 800,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
    await owner.loadURL('data:text/html,<title>Browser test host</title>')
    const manager = new globalThis.__opensquillaNativeWorkbenchSurfaceManager({ getWindow: () => owner, emit() {} })
    globalThis.browserFixture = { manager, owner }
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
  }, origin)
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
    return {generation:record.annotationDocumentGeneration,instanceId:record.surfaceInstanceId}
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
  assert.deepEqual(afterReload.bounds,{x:100,y:80,width:700,height:600})
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
    while(Date.now()<end) {
      if(record.browserDocumentReady) {
        const viewport=await record.view.webContents.executeJavaScript(
          '({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,count:window.count||0})')
        if(viewport.width===960 && viewport.height===720) return {viewport,
          webContentsId:record.view.webContents.id,targetRef:record.targetRef,visible:record.view.getVisible(),
          active:manager.activeSurfaceId,focused:owner.isFocused()}
      }
      await new Promise(resolve=>setTimeout(resolve,20))
    }
    throw new Error('Hidden browser reload did not retain its renderer viewport')
  },{id:hiddenState.id})
  assert.equal(reloadedHidden.webContentsId,hiddenState.webContentsId)
  assert.equal(reloadedHidden.targetRef,opened.targetRef)
  assert.equal(reloadedHidden.visible,false)
  assert.equal(reloadedHidden.active,foregroundBeforeOpen.id)
  assert.equal(reloadedHidden.focused,foregroundBeforeOpen.focused)
  assert.equal(reloadedHidden.viewport.count,0)
  await assertOpenedScreenshot(reloadedHidden.viewport)
  for(const size of [{width:650,height:500},{width:420,height:600}]) {
    const adopted = await app.evaluate(async ({}, {id,size}) => {
      const { manager } = globalThis.browserFixture
      const record=manager.surfaces.get(id)
      const result=manager.setSurfaceRect({surfaceId:id,x:100,y:80,...size,visible:true})
      if(!result.ok) throw new Error(result.message)
      const end=Date.now()+3000
      while(Date.now()<end) {
        const viewport=await record.view.webContents.executeJavaScript('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})')
        if(viewport.width===size.width && viewport.height===size.height) return {viewport,
          webContentsId:record.view.webContents.id,targetRef:record.targetRef,visible:record.view.getVisible(),
          active:manager.activeSurfaceId,bounds:record.view.getBounds()}
        await new Promise(resolve=>setTimeout(resolve,20))
      }
      throw new Error('Adopted browser did not follow the real native layout size')
    },{id:hiddenState.id,size})
    assert.equal(adopted.webContentsId,hiddenState.webContentsId)
    assert.equal(adopted.targetRef,opened.targetRef)
    assert.equal(adopted.active,hiddenState.id)
    assert.equal(adopted.visible,true)
    assert.deepEqual(adopted.bounds,{x:100,y:80,...size})
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
} finally {
  if(app) {
    await app.evaluate(async () => {
      await globalThis.browserFixture?.server?.close()
      await globalThis.browserFixture?.manager?.destroyAll()
    }).catch(()=>{})
    await app.close()
  }
  server.closeAllConnections()
  await new Promise(resolve=>server.close(resolve))
  await rm(root,{recursive:true,force:true})
}
