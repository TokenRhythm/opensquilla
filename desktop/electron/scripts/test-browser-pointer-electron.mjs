import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_POINTER_UNDER_XVFB !== '1') {
  const child = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_POINTER_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (child.error) throw child.error
  process.exit(child.status ?? 1)
}

const server = createServer((request, response) => {
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
  response.end(`<!doctype html><title>Mouse fixture</title>
    <style>body{margin:0;min-height:1800px}button,a{position:absolute;width:100px;height:40px}
    #far{left:600px;top:420px}#near{left:20px;top:20px}#remove{left:500px;top:20px}
    #blocked{left:500px;top:100px}#disabled{left:500px;top:180px}#next{left:500px;top:260px}
    #cover{position:absolute;left:500px;top:100px;width:100px;height:40px;z-index:2;background:#aaa}
    #panel{position:absolute;left:100px;top:100px;width:200px;height:180px;overflow:auto}</style>
    <h1>${request.url === '/next' ? 'Next document' : 'Mouse document'}</h1>
    <button id="far">Far button</button><button id="near">Near button</button>
    <button id="remove" onclick="this.remove()">Remove button</button>
    <button id="blocked">Covered button</button><div id="cover"></div>
    <button id="disabled" disabled>Disabled button</button><a id="next" href="/next">Next document</a>
    <div id="panel" role="region" aria-label="Scroll panel"><div style="height:1200px">Scrollable content</div></div>`)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`
const root = await mkdtemp(join(tmpdir(), 'opensquilla-pointer-'))
let app
try {
  app = await electron.launch({
    args: [`--user-data-dir=${root}`, fileURLToPath(new URL('./fixtures/browser-playwright-attach', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '*', no_proxy: '*' },
  })
  await app.evaluate(async ({ BrowserWindow, WebContentsView }, origin) => {
    const owner = new BrowserWindow({ show: true, width: 900, height: 700, webPreferences: { sandbox: true } })
    const view = new WebContentsView({ webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
    owner.contentView.addChildView(view)
    view.setBounds({ x: 0, y: 0, width: 800, height: 600 })
    await view.webContents.loadURL(origin)
    const record = { owner, view, generation: 1, visible: true }
    record.driver = new globalThis.__BrowserPlaywrightDriver(view.webContents, () => record.visible)
    view.webContents.on('did-navigate', () => { record.generation++; record.driver.invalidate() })
    record.read = code => view.webContents.executeJavaScript(code)
    record.inspect = async () => {
      const snapshot = await record.driver.snapshot(record.generation, () => {}, new AbortController().signal)
      record.refs = Object.fromEntries(snapshot.refs.map(item => [item.name, item.ref]))
    }
    record.act = (action, name, extra = {}, signal = new AbortController().signal) => record.driver.act({
      action, ...(name ? { ref: record.refs[name] } : {}), ...extra,
    }, record.generation, () => {}, signal)
    await record.read(`window.events=[];for(const type of ['mousemove','mousedown','mouseup','click','wheel'])
      document.addEventListener(type,event=>window.events.push({type,x:event.clientX,y:event.clientY,
        target:event.target.id,trusted:event.isTrusted,time:performance.now()}),true)`)
    await record.driver.pointer.setTask('synthetic-turn')
    await record.driver.pointer.touch()
    await record.inspect()
    globalThis.pointerFixture = record
  }, origin)

  const movement = await app.evaluate(async () => {
    const f = globalThis.pointerFixture
    const first = await f.act('click', 'Far button')
    const events = await f.read('window.events')
    const pointer = await f.read(`(()=>{const host=document.getElementById('__opensquilla-browser-pointer');
      return {present:!!host,transform:host?.shadowRoot.querySelector('svg').style.transform,
        transition:host?.shadowRoot.querySelector('svg').style.transition,
        hit:document.elementFromPoint(650,440).id}})()`)
    await f.read('window.events=[]')
    await f.inspect() // Fresh refs must not reset the page's mouse position.
    await f.act('click', 'Near button')
    return { first, events, pointer, second: await f.read('window.events') }
  })
  assert.equal(movement.first.performed, true)
  const moves = movement.events.filter(event => event.type === 'mousemove')
  assert.ok(moves.length >= 10, 'visible travel must contain actual intermediate browser mousemove events')
  assert.ok(moves.at(-1).time - moves[0].time >= 100, 'movement should remain visible over several frames')
  assert.ok(moves.at(-1).time - moves[0].time < 3000, 'movement must be bounded')
  const firstPoint = moves[0], lastPoint = moves.at(-1)
  const distance = Math.hypot(lastPoint.x - firstPoint.x, lastPoint.y - firstPoint.y)
  const bend = Math.max(...moves.map(point => Math.abs(
    (lastPoint.x - firstPoint.x) * (point.y - firstPoint.y)
      - (lastPoint.y - firstPoint.y) * (point.x - firstPoint.x),
  ) / distance))
  assert.ok(bend > 2, 'accepted browser events must trace a visible curve rather than a straight line')
  const travel = moves.slice(1).map((point, index) => Math.hypot(point.x - moves[index].x, point.y - moves[index].y))
    .filter(length => length > .5)
  assert.ok(Math.max(...travel) > Math.min(...travel) * 1.8,
    'the trajectory must vary its progress across frames instead of moving at a rigid constant pace')
  for (const type of ['mousedown', 'mouseup', 'click']) {
    assert.equal(movement.events.filter(event => event.type === type).length, 1, `${type} must occur exactly once`)
  }
  assert.ok(movement.events.every(event => event.trusted))
  const pressIndex = movement.events.findIndex(event => event.type === 'mousedown')
  const lastMoveIndex = movement.events.findLastIndex(event => event.type === 'mousemove')
  assert.ok(lastMoveIndex < pressIndex, 'the full curved path must finish before the button is pressed')
  assert.deepEqual([lastPoint.x, lastPoint.y], [650, 440], 'curved travel must end at the checked click point')
  assert.equal(movement.events.at(-1).target, 'far')
  assert.equal(movement.pointer.present, true)
  assert.equal(movement.pointer.transition, 'none')
  assert.equal(movement.pointer.transform, 'translate3d(648px, 438px, 0px)')
  assert.equal(movement.pointer.hit, 'far', 'display must not intercept website input')
  assert.ok(movement.second.find(event => event.type === 'mousemove').x > 500,
    'the next move must start near the previous page-local position')

  const feedback = await app.evaluate(async () => {
    const f = globalThis.pointerFixture
    await f.act('click', 'Near button')
    const animation = await f.read(`(() => {
      const root = document.getElementById('__opensquilla-browser-pointer').shadowRoot;
      const cursor = root.querySelector('svg'), arrow = root.querySelector('path'), ring = root.querySelector('div');
      const press = arrow.getAnimations()[0], pulse = ring.getAnimations()[0];
      if (!press || !pulse) return { animated: false };
      press.pause(); pulse.pause(); press.currentTime = 0; pulse.currentTime = 0;
      const start = { arrow: getComputedStyle(arrow).transform, ring: parseFloat(getComputedStyle(ring).width) };
      const pressDuration = Number(press.effect.getTiming().duration);
      press.currentTime = pressDuration * .55; pulse.currentTime = Number(pulse.effect.getTiming().duration) * .7;
      const middle = { arrow: getComputedStyle(arrow).transform, ring: parseFloat(getComputedStyle(ring).width) };
      press.finish(); pulse.finish();
      return { animated: true, start, middle, end: getComputedStyle(arrow).transform,
        position: cursor.style.transform, origin: getComputedStyle(arrow).transformOrigin,
        hit: document.elementFromPoint(70, 40).id };
    })()`)
    await f.read('new Promise(resolve => setTimeout(resolve, 1700))')
    const afterThinking = await f.read(`(() => {
      const host = document.getElementById('__opensquilla-browser-pointer');
      return { present: !!host, opacity: host ? getComputedStyle(host).opacity : null,
        activeAnimations: host ? host.shadowRoot.getAnimations().length : 0 };
    })()`)
    await f.view.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', {
      features: [{ name: 'prefers-reduced-motion', value: 'reduce' }],
    })
    let reduced
    try {
      await f.read('window.events=[]')
      await f.act('click', 'Far button')
      reduced = await f.read(`(() => {
        const root = document.getElementById('__opensquilla-browser-pointer').shadowRoot;
        return { frames: Array.from(root.querySelectorAll('*')).flatMap(node => node.getAnimations())
          .flatMap(animation => animation.effect.getKeyframes()),
          events: window.events, hit: document.elementFromPoint(650,440).id };
      })()`)
    } finally {
      await f.view.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', { features: [] })
    }
    await f.act('hover', 'Near button')
    return { animation, afterThinking, reduced }
  })
  assert.equal(feedback.animation.animated, true, 'a real click must animate both its arrow and ripple')
  assert.notEqual(feedback.animation.start.arrow, feedback.animation.middle.arrow,
    'the arrow must visibly press and rebound')
  assert.notEqual(feedback.animation.start.arrow, feedback.animation.end,
    'the arrow must recover from its pressed shape')
  assert.ok(feedback.animation.middle.ring > feedback.animation.start.ring + 10,
    'the click ripple must expand enough to remain visible')
  assert.equal(feedback.animation.position, 'translate3d(68px, 38px, 0px)',
    'click animation must not displace the real input hotspot')
  assert.equal(feedback.animation.origin, '2px 2px')
  assert.equal(feedback.animation.hit, 'near')
  assert.equal(feedback.afterThinking.present, true, 'the cursor must remain during model thinking gaps')
  assert.equal(feedback.afterThinking.opacity, '1')
  assert.equal(feedback.afterThinking.activeAnimations, 0, 'a resting cursor must not keep pulsing')
  assert.equal(feedback.reduced.hit, 'far')
  assert.ok(feedback.reduced.frames.every(frame => !frame.transform && !frame.width && !frame.height),
    'reduced-motion feedback must not scale the arrow or expand a ripple')
  const reducedMoves = feedback.reduced.events.filter(event => event.type === 'mousemove')
  assert.equal(reducedMoves.length, 1, 'reduced motion must go directly to the target without an animated path')
  assert.deepEqual([reducedMoves[0].x, reducedMoves[0].y], [650, 440])
  assert.equal(feedback.reduced.events.filter(event => event.type === 'click').length, 1)

  const actions = await app.evaluate(async () => {
    const f = globalThis.pointerFixture
    await f.read('window.events=[]')
    await f.act('hover', 'Far button')
    const hover = await f.read('window.events')
    await f.act('scroll', 'Scroll panel', { direction: 'down', amount: 160 })
    await f.read('new Promise(resolve=>setTimeout(resolve,120))')
    const nestedScroll = await f.read('({panel:document.getElementById("panel").scrollTop,page:scrollY,event:window.events.filter(e=>e.type==="wheel").at(-1)})')
    await f.read('window.events=[]')
    await f.act('scroll', null, { direction: 'down', amount: 160 })
    await f.read('new Promise(resolve=>setTimeout(resolve,120))')
    const pageScroll = await f.read('({page:scrollY,event:window.events.find(e=>e.type==="wheel")})')
    await f.read('scrollTo(0,0)')
    await f.inspect()
    const removal = await f.act('click', 'Remove button')
    const removed = await f.read('!document.getElementById("remove")')
    return { hover, nestedScroll, pageScroll, removal, removed }
  })
  assert.ok(actions.hover.filter(event => event.type === 'mousemove').length >= 10)
  assert.equal(actions.hover.filter(event => event.type === 'click').length, 0)
  assert.ok(actions.nestedScroll.panel > 0)
  assert.equal(actions.nestedScroll.page, 0)
  assert.ok(actions.nestedScroll.event.x >= 100 && actions.nestedScroll.event.x <= 300)
  assert.ok(actions.pageScroll.page > 0)
  assert.equal(actions.pageScroll.event.x, 400)
  assert.equal(actions.pageScroll.event.y, 300)
  assert.equal(actions.removal.performed, true)
  assert.equal(actions.removed, true, 'removing the clicked node must not turn success into a retry')

  const interruptedMotion = await app.evaluate(async ({}, origin) => {
    const f = globalThis.pointerFixture
    await f.inspect()
    await f.read('window.events=[]')
    const controller = new AbortController()
    const send = f.view.webContents.debugger.sendCommand.bind(f.view.webContents.debugger)
    let acceptedMoves = 0
    f.view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      const result = await send(method, params, sessionId)
      if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved' && ++acceptedMoves === 3) {
        controller.abort()
      }
      return result
    }
    let abortCode
    try { await f.act('click', 'Far button', {}, controller.signal) }
    catch (error) { abortCode = error.code }
    finally { f.view.webContents.debugger.sendCommand = send }
    const abortedEvents = await f.read('window.events')
    await f.read('new Promise(resolve=>setTimeout(resolve,150))')
    const eventsAfterAbort = await f.read('window.events.length')

    await f.inspect()
    await f.act('hover', 'Near button')
    const reconnectedSend = f.view.webContents.debugger.sendCommand.bind(f.view.webContents.debugger)
    let moved = 0, navigated = false, inputAfterNavigation = 0
    f.view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      if (navigated && method === 'Input.dispatchMouseEvent') inputAfterNavigation++
      const result = await reconnectedSend(method, params, sessionId)
      if (!navigated && method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved' && ++moved === 3) {
        await f.view.webContents.loadURL(origin + '/next')
        await f.read(`window.events=[];for(const type of ['mousemove','mousedown','mouseup','click','wheel'])
          document.addEventListener(type,event=>window.events.push({type,x:event.clientX,y:event.clientY,
            target:event.target.id,trusted:event.isTrusted,time:performance.now()}),true)`)
        navigated = true
      }
      return result
    }
    let navigationError
    try { await f.act('click', 'Far button') }
    catch (error) { navigationError = error.code }
    finally { f.view.webContents.debugger.sendCommand = reconnectedSend }
    await f.read('new Promise(resolve=>setTimeout(resolve,150))')
    const nextEvents = await f.read('window.events')
    await f.inspect()
    const recovered = await f.act('click', 'Near button')
    return { abortCode, acceptedMoves, abortedEvents, eventsAfterAbort, navigated,
      inputAfterNavigation, navigationError, nextEvents, recovered }
  }, origin)
  assert.equal(interruptedMotion.abortCode, 'TIMEOUT')
  assert.equal(interruptedMotion.acceptedMoves, 3, 'cancellation must stop an in-flight curved path')
  assert.equal(interruptedMotion.abortedEvents.filter(event => ['mousedown', 'mouseup', 'click'].includes(event.type)).length, 0,
    'cancellation during travel must prevent the pending click')
  assert.equal(interruptedMotion.eventsAfterAbort, interruptedMotion.abortedEvents.length,
    'cancelled motion must not leave a background trail')
  assert.equal(interruptedMotion.navigated, true)
  assert.ok(interruptedMotion.navigationError, 'navigation during travel must reject the old target action')
  assert.equal(interruptedMotion.inputAfterNavigation, 0, 'old motion must not dispatch input into the next document')
  assert.equal(interruptedMotion.nextEvents.length, 0)
  assert.equal(interruptedMotion.recovered.performed, true, 'fresh inspection must allow work on the new document')

  for (const name of ['Covered button', 'Disabled button']) {
    const blocked = await app.evaluate(async ({}, name) => {
      const f = globalThis.pointerFixture
      await f.inspect()
      await f.read('window.events=[]')
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 300)
      let code
      try { await f.act('click', name, {}, controller.signal) }
      catch (error) { code = error.code }
      finally { clearTimeout(timer) }
      return { code, events: await f.read('window.events') }
    }, name)
    assert.equal(blocked.code, 'TIMEOUT')
    assert.equal(blocked.events.filter(event => event.type === 'click').length, 0, `${name} must never receive a click`)
  }

  const lifecycle = await app.evaluate(async () => {
    const f = globalThis.pointerFixture
    await f.inspect()
    await f.act('click', 'Far button')
    let presentAtCapture
    const send = f.view.webContents.debugger.sendCommand.bind(f.view.webContents.debugger)
    f.view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      if (method === 'Page.captureScreenshot') presentAtCapture = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
      return send(method, params, sessionId)
    }
    const image = await f.driver.screenshot(() => {}, new AbortController().signal)
    f.view.webContents.debugger.sendCommand = send
    const restoredAfterCapture = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    let captureFailure
    try {
      await f.driver.pointer.pauseForScreenshot(async () => {
        throw new Error('Synthetic capture failure')
      })
    } catch (error) { captureFailure = error.message }
    const restoredAfterFailure = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    let collapsedDuringMove = false
    f.view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      const result = await send(method, params, sessionId)
      if (!collapsedDuringMove && method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved') {
        collapsedDuringMove = true
        f.visible = false
        f.view.setVisible(false)
      }
      return result
    }
    const collapsedAction = await f.act('click', 'Near button')
    f.view.webContents.debugger.sendCommand = send
    const hiddenAction = await f.act('click', 'Near button')
    const hiddenHover = await f.act('hover', 'Far button')
    const hiddenScroll = await f.act('scroll', null, { direction: 'down', amount: 80 })
    await f.read('new Promise(resolve=>setTimeout(resolve,120))')
    const hiddenScrollY = await f.read('scrollY')
    await f.read('scrollTo(0,0)')
    const capturePage = f.view.webContents.capturePage.bind(f.view.webContents)
    f.view.webContents.capturePage = async () => { throw new Error('Synthetic unavailable frame') }
    const failedFrameScroll = await f.act('scroll', null, { direction: 'down', amount: 40 })
    f.view.webContents.capturePage = capturePage
    const hiddenPointer = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    f.visible = true
    f.view.setVisible(true)
    await f.driver.pointer.sync()
    const restoredAfterReopen = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    await f.driver.pointer.setTask(null)
    await f.read('new Promise(resolve=>setTimeout(resolve,260))')
    const afterTask = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    // A failed decoration may not change the receipt of an accepted action.
    await f.driver.pointer.setTask('synthetic-collision-turn')
    await f.read('const collision=document.createElement("div");collision.id="__opensquilla-browser-pointer";document.body.append(collision);window.events=[]')
    const undecorated = await f.act('click', 'Far button')
    const undecoratedClicks = await f.read('window.events.filter(e=>e.type==="click").length')
    await f.read('document.getElementById("__opensquilla-browser-pointer").remove()')
    await f.driver.pointer.setTask(null)
    const navigation = await f.act('click', 'Next document')
    const next = await f.read('({title:document.querySelector("h1").textContent,pointer:!!document.getElementById("__opensquilla-browser-pointer")})')
    return { presentAtCapture, restoredAfterCapture, captureFailure, restoredAfterFailure,
      width: image.width, collapsedDuringMove, collapsedAction, hiddenAction, hiddenHover,
      hiddenScroll, hiddenScrollY, failedFrameScroll, hiddenPointer, restoredAfterReopen, afterTask,
      undecorated, undecoratedClicks, navigation, next }
  })
  assert.equal(lifecycle.presentAtCapture, false)
  assert.equal(lifecycle.restoredAfterCapture, true, 'capturing pixels must not end the active cursor')
  assert.equal(lifecycle.captureFailure, 'Synthetic capture failure')
  assert.equal(lifecycle.restoredAfterFailure, true, 'a failed capture must also restore the active cursor')
  assert.ok(lifecycle.width > 0)
  assert.equal(lifecycle.collapsedDuringMove, true)
  assert.equal(lifecycle.collapsedAction.performed, true)
  assert.equal(lifecycle.hiddenAction.performed, true)
  assert.equal(lifecycle.hiddenHover.performed, true)
  assert.equal(lifecycle.hiddenScroll.performed, true)
  assert.ok(lifecycle.hiddenScrollY > 0)
  assert.equal(lifecycle.failedFrameScroll.performed, true, 'frame failure must not invite a duplicate wheel')
  assert.equal(lifecycle.hiddenPointer, false)
  assert.equal(lifecycle.restoredAfterReopen, true, 'reopening the active surface must restore the cursor')
  assert.equal(lifecycle.afterTask, false, 'finishing the task must remove the cursor')
  assert.equal(lifecycle.undecorated.performed, true)
  assert.equal(lifecycle.undecoratedClicks, 1)
  assert.equal(lifecycle.navigation.performed, true)
  assert.equal(lifecycle.next.title, 'Next document')
  assert.equal(lifecycle.next.pointer, false, 'old action coordinates must not appear on the next document')
  const taskEnd = await app.evaluate(async ({}, origin) => {
    const f = globalThis.pointerFixture
    await f.inspect()
    await f.read(`window.taskEndClicks=[];document.addEventListener('click',event=>window.taskEndClicks.push({
      target:event.target.id,x:event.clientX,y:event.clientY,trusted:event.isTrusted}),true)`)
    const send = f.view.webContents.debugger.sendCommand.bind(f.view.webContents.debugger)
    let acceptedRelease
    f.view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      const result = await send(method, params, sessionId)
      if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseReleased') {
        acceptedRelease = { x: params.x, y: params.y }
      }
      return result
    }
    try { await f.act('click', 'Near button') }
    finally { f.view.webContents.debugger.sendCommand = send }
    const clicks = await f.read('window.taskEndClicks')
    const inactive = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    await f.driver.pointer.setTask('synthetic-next-turn')
    await f.driver.pointer.touch()
    const activeAgain = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    f.driver.pointer.navigationStarted()
    await f.view.webContents.loadURL(origin)
    await f.driver.pointer.documentReady()
    const activeAfterNavigation = await f.read(`(() => {
      const root = document.getElementById('__opensquilla-browser-pointer')?.shadowRoot;
      return { present: !!root, transform: root?.querySelector('svg').style.transform,
        animations: root?.getAnimations().length || 0 };
    })()`)
    await f.driver.pointer.pauseForScreenshot(async () => {
      await f.driver.pointer.setTask(null)
    })
    await f.read('new Promise(resolve => setTimeout(resolve, 260))')
    const stoppedDuringCapture = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    await f.driver.pointer.setTask('synthetic-next-turn')
    const reconnected = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    await f.driver.pointer.setTask('synthetic-untouched-turn')
    const untouchedTurn = await f.read('!!document.getElementById("__opensquilla-browser-pointer")')
    return { inactive, activeAgain, activeAfterNavigation, stoppedDuringCapture, reconnected, untouchedTurn,
      acceptedRelease, clicks }
  }, origin)
  assert.equal(taskEnd.inactive, false, 'a completed task must not regain a cursor from late actions')
  assert.equal(taskEnd.activeAgain, true, 'a new task must be able to start a fresh cursor')
  assert.equal(taskEnd.activeAfterNavigation.present, true, 'the task cursor must return on its next document')
  assert.equal(taskEnd.clicks.length, 1)
  assert.equal(taskEnd.clicks[0].target, 'near')
  assert.equal(taskEnd.clicks[0].trusted, true)
  assert.deepEqual(taskEnd.acceptedRelease, { x: taskEnd.clicks[0].x, y: taskEnd.clicks[0].y })
  assert.equal(taskEnd.activeAfterNavigation.transform,
    `translate3d(${taskEnd.acceptedRelease.x - 2}px, ${taskEnd.acceptedRelease.y - 2}px, 0px)`,
    'navigation must retain the actual viewport mouse position during the active task')
  assert.equal(taskEnd.activeAfterNavigation.animations, 0, 'navigation must not replay the previous click')
  assert.equal(taskEnd.stoppedDuringCapture, false, 'capture cleanup must not revive a finished task')
  assert.equal(taskEnd.reconnected, true, 'reconnecting the same task restores the cursor without another action')
  assert.equal(taskEnd.untouchedTurn, false, 'a different task waits for its first browser use')
  console.log('Playwright Mouse passed: curved/eased trusted movement, ordered single clicks, mid-motion cancellation/navigation, press/rebound feedback, persistent task cursor, reduced motion, hidden/reopened surfaces and clean screenshots.')
} finally {
  await app?.close().catch(() => {})
  await new Promise(resolve => server.close(resolve))
  await rm(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
