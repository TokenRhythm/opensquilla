import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_CAPABILITIES_UNDER_XVFB !== '1') {
  const child = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_CAPABILITIES_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (child.error) throw child.error
  process.exit(child.status ?? 1)
}

const original = 'First  line\n\nSecond\tline — 中文 😀\n'
const server = createServer((_request, response) => {
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
  response.end(`<!doctype html><title>Browser capability fixture</title>
  <style>body{margin:0;font:14px sans-serif}button{width:120px;height:36px}
  #source,#target{position:absolute;top:200px;width:100px;height:70px;background:#eee}
  #source{left:20px}#target{left:220px}#canvas{position:absolute;left:400px;top:200px;background:#ddd}
  #panel{position:absolute;left:20px;top:310px;width:260px;height:70px;overflow:auto}
  #panel>div{width:1500px;height:50px}#frame{position:absolute;left:400px;top:330px;width:300px;height:100px}</style>
  <pre id="original">${original}</pre><textarea aria-label="Editable text"></textarea>
  <input type="password" aria-label="Secret" value="synthetic-secret">
  <button id="hold">Hold target</button><button id="context">Context target</button>
  <div id="source" role="button">Drag source</div><div id="target" role="button">Drag destination</div>
  <canvas id="canvas" width="240" height="90" aria-label="Canvas target"></canvas>
  <div id="panel" role="region" aria-label="Horizontal panel"><div>Horizontal content</div></div>
  <input id="file" aria-label="Upload input" type="file">
  <iframe id="frame" srcdoc="<pre>Frame line one&#10;&#10;Frame line two</pre>"></iframe>
  <script>window.events=[];window.changes=0;document.querySelector('textarea').value='Edited  value\\n\\n尾部\\n';
  document.addEventListener('contextmenu',e=>e.preventDefault());
  for(const type of ['mousedown','mouseup','mousemove','click','auxclick','contextmenu'])
    document.addEventListener(type,e=>events.push({type,target:e.target.id,button:e.button,buttons:e.buttons,
      x:e.clientX,y:e.clientY,time:performance.now(),trusted:e.isTrusted}),true);
  document.getElementById('file').addEventListener('change',()=>changes++);</script>`)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`
const directory = await mkdtemp(join(tmpdir(), 'opensquilla-capabilities-'))
let app
try {
  app = await electron.launch({
    args: [...(process.platform === 'linux' && process.getuid?.() === 0 ? ['--no-sandbox'] : []),
      `--user-data-dir=${directory}`, fileURLToPath(new URL('./fixtures/browser-playwright-attach', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '*', no_proxy: '*' },
  })
  await app.evaluate(async ({ BrowserWindow, WebContentsView }, origin) => {
    const owner = new BrowserWindow({ show: true, width: 900, height: 750, webPreferences: { sandbox: true } })
    const view = new WebContentsView({ webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
    owner.contentView.addChildView(view)
    view.setBounds({ x: 0, y: 0, width: 800, height: 650 })
    await view.webContents.loadURL(origin)
    const f = { owner, view, generation: 1 }
    f.driver = new globalThis.__BrowserPlaywrightDriver(view.webContents)
    view.webContents.on('did-navigate', () => { f.generation++; f.driver.invalidate() })
    f.read = code => view.webContents.executeJavaScript(code)
    f.inspect = async () => {
      f.snapshot = await f.driver.snapshot(f.generation, () => {}, new AbortController().signal)
      f.refs = Object.fromEntries(f.snapshot.refs.map(item => [item.name, item.ref]))
    }
    f.act = (action, name, extra = {}, signal = new AbortController().signal) => f.driver.act({
      action, ...(name ? { ref: f.refs[name] } : {}), ...extra,
    }, f.generation, () => {}, signal)
    f.exact = (ref, maxChars) => f.driver.readText({ ref, maxChars }, f.generation, () => {}, new AbortController().signal)
    await f.inspect()
    globalThis.capabilityFixture = f
  }, origin)

  const text = await app.evaluate(async () => {
    const f = globalThis.capabilityFixture
    const pre = f.snapshot.refs.find(item => item.tagName === 'pre' && !item.frameUrl)
    const frame = f.snapshot.refs.find(item => item.tagName === 'pre' && item.frameUrl)
    const full = await f.exact(pre.ref)
    const short = await f.exact(pre.ref, 8)
    const edited = await f.exact(f.refs['Editable text'])
    const framed = await f.exact(frame.ref)
    let secret, hidden, transparent
    try { await f.exact(f.refs.Secret) } catch (error) { secret = error.code }
    await f.read('document.getElementById("original").style.display="none"')
    try { await f.exact(pre.ref) } catch (error) { hidden = error.code }
    await f.read('document.getElementById("original").style.display="block"')
    await f.read('document.getElementById("original").innerHTML="Visible text<span style=\\"display:none\\">Hidden text</span>"')
    const displayHidden = await f.exact(pre.ref)
    await f.read('document.getElementById("original").querySelector("span").style="opacity:0"')
    try { await f.exact(pre.ref) } catch (error) { transparent = error.code }
    await f.read(`document.getElementById('original').textContent=${JSON.stringify(full.text)}`)
    return { full, short, edited, framed, secret, hidden, transparent, displayHidden, readable: pre.readable }
  })
  assert.equal(text.readable, true)
  assert.equal(text.full.text, original)
  assert.equal(text.full.sourceLength, original.length)
  assert.equal(text.full.truncated, false)
  assert.equal(text.short.text, original.slice(0, 8))
  assert.equal(text.short.truncated, true)
  assert.equal(text.edited.value, 'Edited  value\n\n尾部\n')
  assert.equal(text.framed.text, 'Frame line one\n\nFrame line two')
  assert.equal(text.secret, 'TEXT_UNAVAILABLE')
  assert.equal(text.hidden, 'TEXT_UNAVAILABLE')
  assert.equal(text.transparent, 'TEXT_UNAVAILABLE')
  assert.equal(text.displayHidden.text, 'Visible text')

  const gestures = await app.evaluate(async () => {
    const f = globalThis.capabilityFixture
    await f.act('click', 'Context target', { button: 'right' })
    await f.act('click', 'Context target', { button: 'middle' })
    await f.act('hold', 'Hold target', { durationMs: 180 })
    await f.act('drag', 'Drag source', { endRef: f.refs['Drag destination'] })
    return await f.read('window.events')
  })
  assert.ok(gestures.some(event => event.type === 'contextmenu' && event.button === 2))
  assert.ok(gestures.some(event => event.type === 'auxclick' && event.button === 1))
  const hold = gestures.filter(event => event.target === 'hold' && ['mousedown', 'mouseup'].includes(event.type))
  assert.deepEqual(hold.map(event => event.type), ['mousedown', 'mouseup'])
  assert.ok(hold[1].time - hold[0].time >= 150)
  assert.ok(gestures.some(event => event.type === 'mousedown' && event.target === 'source'))
  assert.ok(gestures.some(event => event.type === 'mousemove' && event.target === 'target' && event.buttons === 1))
  assert.ok(gestures.some(event => event.type === 'mouseup' && event.target === 'target'))
  assert.ok(gestures.every(event => event.trusted))

  const scrolling = await app.evaluate(async () => {
    const f = globalThis.capabilityFixture
    const result = await f.act('scroll', 'Horizontal panel', { direction: 'right', amount: 180 })
    return { result, panel: await f.read('document.getElementById("panel").scrollLeft'), page: await f.read('scrollX') }
  })
  assert.ok(scrolling.panel > 0)
  assert.equal(scrolling.page, 0)
  assert.equal(scrolling.result.execution.scroll.changed, true)
  assert.ok(scrolling.result.execution.scroll.after.some(item => item.left > 0))

  const coordinate = await app.evaluate(async () => {
    const f = globalThis.capabilityFixture
    await f.read('window.events=[]')
    const observed = await f.driver.observe(f.generation, () => {}, new AbortController().signal, 'hybrid')
    const result = await f.act('drag', null, { x: 420, y: 230, toX: 600, toY: 230,
      observationId: observed.observation.observationId, imageId: observed.observation.image.imageId })
    const events = await f.read('window.events')
    const stale = await f.driver.observe(f.generation, () => {}, new AbortController().signal, 'hybrid')
    await f.read('document.getElementById("canvas").getContext("2d").fillRect(180,0,60,90);window.events=[]')
    let code
    try { await f.act('drag', null, { x: 420, y: 230, toX: 600, toY: 230,
      observationId: stale.observation.observationId, imageId: stale.observation.image.imageId }) }
    catch (error) { code = error.code }
    return { result, events, code, staleEvents: await f.read('window.events') }
  })
  assert.equal(coordinate.result.performed, true)
  assert.equal(coordinate.events.filter(event => event.type === 'mousedown').length, 1)
  assert.equal(coordinate.events.filter(event => event.type === 'mouseup').length, 1)
  assert.ok(coordinate.events.filter(event => event.type === 'mousemove' && event.buttons === 1).length >= 10)
  assert.equal(coordinate.code, 'STALE_OBSERVATION')
  assert.equal(coordinate.staleEvents.filter(event => event.type === 'mousedown').length, 0)

  const cancelled = await app.evaluate(async () => {
    const f = globalThis.capabilityFixture
    await f.inspect()
    await f.read('window.events=[]')
    const controller = new AbortController()
    const send = f.view.webContents.debugger.sendCommand.bind(f.view.webContents.debugger)
    f.view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      const result = await send(method, params, sessionId)
      if (method === 'Input.dispatchMouseEvent' && params.type === 'mousePressed') controller.abort()
      return result
    }
    let code
    try { await f.act('hold', 'Hold target', { durationMs: 3000 }, controller.signal) }
    catch (error) { code = error.code }
    finally { f.view.webContents.debugger.sendCommand = send }
    return { code, events: await f.read('window.events') }
  })
  assert.equal(cancelled.code, 'TIMEOUT')
  assert.equal(cancelled.events.filter(event => event.type === 'mousedown').length, 1)
  assert.equal(cancelled.events.filter(event => event.type === 'mouseup').length, 1,
    'cancellation must release the accepted mouse button before returning')

  const upload = await app.evaluate(async () => {
    const f = globalThis.capabilityFixture
    await f.inspect()
    const send = f.view.webContents.debugger.sendCommand.bind(f.view.webContents.debugger)
    const interception = []
    f.view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      const result = await send(method, params, sessionId)
      if (method === 'Page.setInterceptFileChooserDialog') interception.push(params.enabled)
      return result
    }
    try { await f.act('click', 'Upload input'); await f.read('true') }
    finally { f.view.webContents.debugger.sendCommand = send }
    const pending = f.driver.pendingFileChooser
    let blocked
    try { await f.act('click', 'Hold target') } catch (error) { blocked = error.code }
    const file = { fileId: 'synthetic-file', name: 'fixture.txt', mimeType: 'text/plain',
      dataBase64: Buffer.from('Synthetic upload\n').toString('base64') }
    const result = await f.driver.uploadFile({ action: 'upload', chooserId: pending.chooserId, uploadFile: file },
      f.generation, () => {}, new AbortController().signal)
    const selected = await f.read('document.getElementById("file").files[0].text()')
    await f.act('click', 'Upload input')
    const second = f.driver.pendingFileChooser
    await f.driver.cancelFileChooser({ action: 'cancelUpload', chooserId: second.chooserId },
      f.generation, () => {}, new AbortController().signal)
    const preserved = await f.read('({name:document.getElementById("file").files[0].name,changes})')
    const direct = await f.driver.uploadFile({ action: 'upload', ref: f.refs['Upload input'], uploadFile: { ...file,
      name: 'replacement.txt', dataBase64: Buffer.from('Replacement').toString('base64') } }, f.generation, () => {}, new AbortController().signal)
    let stale
    try { await f.driver.cancelFileChooser({ chooserId: second.chooserId }, f.generation, () => {}, new AbortController().signal) }
    catch (error) { stale = error.code }
    return { pending, blocked, result, selected, preserved, direct, stale, interception, finalPending: f.driver.pendingFileChooser }
  })
  assert.ok(upload.pending.chooserId)
  assert.deepEqual(upload.interception, [true, false], 'an agent click must restore the native picker before the tab becomes idle')
  assert.equal(upload.blocked, 'FILE_CHOOSER_PENDING')
  assert.equal(upload.result.uploaded.fileId, 'synthetic-file')
  assert.equal(upload.selected, 'Synthetic upload\n')
  assert.deepEqual(upload.preserved, { name: 'fixture.txt', changes: 1 })
  assert.equal(upload.direct.uploaded.name, 'replacement.txt')
  assert.equal(upload.stale, 'STALE_FILE_CHOOSER')
  assert.equal(upload.finalPending, undefined)

  const disconnected = await app.evaluate(async () => {
    const f = globalThis.capabilityFixture
    await f.act('click', 'Upload input')
    const controller = new AbortController()
    const send = f.view.webContents.debugger.sendCommand.bind(f.view.webContents.debugger)
    f.view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      const result = await send(method, params, sessionId)
      if (method === 'Page.captureScreenshot') controller.abort()
      return result
    }
    let code
    try { await f.driver.screenshot(() => {}, controller.signal) } catch (error) { code = error.code }
    finally { f.view.webContents.debugger.sendCommand = send }
    await f.inspect()
    return { code, pending: f.driver.pendingFileChooser, connected: f.snapshot.refs.length > 0 }
  })
  assert.equal(disconnected.code, 'TIMEOUT')
  assert.equal(disconnected.pending, undefined, 'reconnection must not retain handles from a closed file chooser transport')
  assert.equal(disconnected.connected, true)

  const navigated = await app.evaluate(async ({}, origin) => {
    const f = globalThis.capabilityFixture
    const send = f.view.webContents.debugger.sendCommand.bind(f.view.webContents.debugger)
    const input = []
    let navigation
    f.view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
      if (method === 'Input.dispatchMouseEvent' && ['mousePressed', 'mouseReleased'].includes(params.type)) {
        input.push({ type: params.type, time: performance.now() })
      }
      const result = await send(method, params, sessionId)
      if (method === 'Input.dispatchMouseEvent' && params.type === 'mousePressed') navigation = f.view.webContents.loadURL(`${origin}/after`)
      return result
    }
    let code
    try { await f.act('hold', 'Hold target', { durationMs: 2000 }) } catch (error) { code = error.code }
    finally { f.view.webContents.debugger.sendCommand = send }
    await navigation
    await f.inspect()
    return { input, code, connected: f.snapshot.refs.length > 0 }
  }, origin)
  assert.ok(navigated.code, 'a navigation during a gesture must not claim the old-document action completed')
  assert.deepEqual(navigated.input.map(item => item.type), ['mousePressed', 'mouseReleased'])
  assert.ok(navigated.input[1].time - navigated.input[0].time < 1500,
    'navigation must release the button promptly instead of waiting out the hold duration')
  assert.equal(navigated.connected, true)

  const lateControl = await app.evaluate(async () => {
    const f = globalThis.capabilityFixture
    await f.read(`(() => {
      const text = document.createElement('section'); text.id = 'large-text';
      for (let i = 0; i < 2600; i++) { const line = document.createElement('div'); line.textContent = 'Visible line ' + i; text.append(line) }
      document.body.prepend(text);
      const button = document.createElement('button'); button.textContent = 'Late control'; document.body.append(button);
    })()`)
    await f.inspect()
    return { present: !!f.refs['Late control'], count: f.snapshot.refs.length, truncated: f.snapshot.truncated }
  })
  assert.equal(lateControl.present, true, 'supplemental text must not crowd a later control out of the scan or ref budget')
  assert.ok(lateControl.count <= 160)
  assert.equal(lateControl.truncated, true)
  console.log('browser capabilities: exact text, iframe text, mouse buttons, atomic hold/drag, cancellation, horizontal scroll, file chooser and upload passed')
} finally {
  await app?.evaluate(async () => { await globalThis.capabilityFixture?.driver.dispose() }).catch(() => {})
  await app?.close().catch(() => {})
  await new Promise(resolve => server.close(resolve))
  await rm(directory, { recursive: true, force: true })
}
