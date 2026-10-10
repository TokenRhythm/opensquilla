import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

function waveFixture() {
  const sampleRate = 8000
  const samples = sampleRate / 4
  const body = Buffer.alloc(samples * 2)
  for (let index = 0; index < samples; index += 1) {
    body.writeInt16LE(Math.round(Math.sin(2 * Math.PI * 440 * index / sampleRate) * 8000), index * 2)
  }
  const header = Buffer.alloc(44)
  header.write('RIFF', 0)
  header.writeUInt32LE(36 + body.length, 4)
  header.write('WAVEfmt ', 8)
  header.writeUInt32LE(16, 16)
  header.writeUInt16LE(1, 20)
  header.writeUInt16LE(1, 22)
  header.writeUInt32LE(sampleRate, 24)
  header.writeUInt32LE(sampleRate * 2, 28)
  header.writeUInt16LE(2, 32)
  header.writeUInt16LE(16, 34)
  header.write('data', 36)
  header.writeUInt32LE(body.length, 40)
  return Buffer.concat([header, body])
}

async function until(predicate, description, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const value = await predicate()
    if (value) return value
    await sleep(30)
  }
  throw new Error(`Timed out waiting for ${description}`)
}

if (!process.versions.electron) {
  if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
    && process.env.OPENSQUILLA_FOUNDATIONS_UNDER_XVFB !== '1') {
    const display = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
      env: { ...process.env, OPENSQUILLA_FOUNDATIONS_UNDER_XVFB: '1' }, stdio: 'inherit',
    })
    if (display.error) throw display.error
    process.exit(display.status ?? 1)
  }
  const profile = await mkdtemp(join(tmpdir(), 'opensquilla-browser-foundations-'))
  const require = createRequire(import.meta.url)
  const child = spawn(require('electron'), [
    ...(process.platform === 'linux' && process.getuid?.() === 0 ? ['--no-sandbox'] : []),
    `--user-data-dir=${join(profile, 'chromium')}`, fileURLToPath(import.meta.url),
  ], { env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' }, stdio: 'inherit' })
  const watchdog = setTimeout(() => child.kill('SIGKILL'), 90_000)
  let code
  try {
    code = await new Promise((resolve, reject) => {
      child.once('error', reject)
      child.once('exit', status => resolve(status ?? 1))
    })
  } finally {
    clearTimeout(watchdog)
    await rm(profile, { recursive: true, force: true })
  }
  process.exit(code)
} else {
  const { app, BrowserWindow, Menu, dialog } = await import('electron')
  const { NativeWorkbenchSurfaceManager } = await import('../dist/native-workbench-surface.js')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    await app.whenReady()
    let owner, manager
    let exitCode = 0
    const events = []
    const wave = waveFixture()
    const web = createServer((request, response) => {
      if (request.url === '/image.svg') {
        response.writeHead(200, { 'content-type': 'image/svg+xml' })
        response.end('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="16"><rect width="32" height="16" fill="teal"/></svg>')
        return
      }
      if (request.url === '/tone.wav') {
        response.writeHead(200, { 'content-type': 'audio/wav', 'content-length': wave.length })
        response.end(wave)
        return
      }
      response.setHeader('content-type', 'text/html; charset=utf-8')
      if (request.url === '/login') {
        response.end(`<!doctype html><title>Login</title><button id="login">Sign in</button>
          <script>document.querySelector('#login').onclick = () => {
            document.cookie = 'sid=synthetic; SameSite=Lax; Path=/';
            localStorage.setItem('loginMarker', 'synthetic');
            location.href = '/dashboard';
          }</script>`)
        return
      }
      if (request.url === '/frame') {
        response.end(`<!doctype html><button id="frame-action" onclick="parent.postMessage('frame-acted','*')">Frame action</button>`)
        return
      }
      if (!request.headers.cookie?.includes('sid=synthetic')) {
        response.writeHead(401)
        response.end('<!doctype html><title>Login required</title>Login required')
        return
      }
      if (request.url === '/second' || request.url === '/child' || request.url === '/context') {
        response.end(`<!doctype html><title>${request.url.slice(1)}</title><p id="identity">signed in</p>`)
        return
      }
      response.end(`<!doctype html><title>Dashboard</title>
        <button id="popup" onclick="window.open('/child', '_blank')">Open related tab</button>
        <a id="context-link" href="/context">Context link</a>
        <form id="form"><input id="entry" aria-label="Entry"><button>Submit form</button></form>
        <output id="form-result"></output>
        <iframe title="Nested frame" src="/frame"></iframe>
        <div id="shadow-host"></div><output id="shadow-result"></output>
        <button id="guard" onclick="window.onbeforeunload = () => true">Guard close</button>
        <img id="image" src="/image.svg" alt="Synthetic image">
        <audio id="audio" src="/tone.wav" preload="auto"></audio>
        <button id="play-audio">Play audio</button>
        <video id="video" muted playsinline></video>
        <button id="record-video">Record video</button>
        <output id="media-result"></output>
        <button id="enter-fullscreen" onclick="document.querySelector('#fullscreen-target').requestFullscreen()">Enter fullscreen</button>
        <div id="fullscreen-target" style="background:white;padding:30px">
          <button id="exit-fullscreen" onclick="document.exitFullscreen()">Exit fullscreen</button>
        </div>
        <p>needle one</p><p>needle two</p><p>needle three</p>
        <script>
          document.querySelector('#form').onsubmit = event => {
            event.preventDefault();
            document.querySelector('#form-result').textContent = document.querySelector('#entry').value;
          };
          onmessage = event => {
            if (event.data === 'frame-acted') document.body.dataset.frameActed = 'yes';
          };
          const root = document.querySelector('#shadow-host').attachShadow({mode:'open'});
          root.innerHTML = '<button id="shadow-action">Shadow action</button>';
          root.querySelector('button').onclick = () => {
            document.querySelector('#shadow-result').textContent = 'shadow-acted';
          };
          document.querySelector('#play-audio').onclick = () => {
            document.querySelector('#audio').play().catch(error => {
              document.querySelector('#media-result').textContent = error.name;
            });
          };
          document.querySelector('#record-video').onclick = () => {
            window.recordCompletion = (async () => {
              const canvas = document.createElement('canvas');
              canvas.width = 80; canvas.height = 40;
              const context = canvas.getContext('2d');
              const stream = canvas.captureStream(15);
              const chunks = [];
              const recorder = new MediaRecorder(stream, {mimeType:'video/webm;codecs=vp8'});
              recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
              const stopped = new Promise(resolve => { recorder.onstop = resolve; });
              recorder.start();
              let frame = 0;
              const painter = setInterval(() => {
                context.fillStyle = frame++ % 2 ? 'teal' : 'orange';
                context.fillRect(0, 0, 80, 40);
              }, 50);
              await new Promise(resolve => setTimeout(resolve, 450));
              recorder.stop(); clearInterval(painter);
              await stopped; stream.getTracks().forEach(track => track.stop());
              const blob = new Blob(chunks, {type:'video/webm'});
              const video = document.querySelector('#video');
              video.src = URL.createObjectURL(blob);
              await new Promise((resolve, reject) => {
                video.oncanplay = resolve; video.onerror = () => reject(new Error('Video decode failed'));
              });
              await video.play();
              document.querySelector('#media-result').textContent = String(blob.size);
              return {bytes:blob.size, width:video.videoWidth, height:video.videoHeight};
            })();
          };
        </script>`)
    })
    const originalBuild = Menu.buildFromTemplate
    const originalDialog = dialog.showMessageBoxSync
    try {
      await new Promise(resolve => web.listen(0, '127.0.0.1', resolve))
      const origin = `http://127.0.0.1:${web.address().port}`
      owner = new BrowserWindow({ show: false, width: 1100, height: 800,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
      await owner.loadURL('data:text/html,<title>Browser foundation host</title>')
      manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner,
        emit: event => events.push(event) })
      const call = request => manager.executeBrowserMcp({ sessionKey: 'synthetic-task',
        observationMode: 'dom', ...request }, AbortSignal.timeout(20_000))
      const record = target => [...manager.surfaces.values()].find(value => value.targetRef === target.targetRef)
      const evaluate = (target, expression) => record(target).view.webContents.executeJavaScript(expression)
      const refNamed = (result, name) => {
        const ref = result.observation?.refs?.find(value => value.name === name)?.ref
        assert.ok(ref, `Expected browser observation ref: ${name}`)
        return ref
      }
      const clickNamed = async (target, name) => {
        const observed = await call({ operation: 'observe', targetRef: target.targetRef })
        return await call({ operation: 'act', targetRef: target.targetRef,
          action: 'click', ref: refNamed(observed, name) })
      }

      const parent = await call({ operation: 'open', url: origin + '/login' })
      await clickNamed(parent, 'Sign in')
      await until(() => record(parent).view.webContents.getURL().endsWith('/dashboard'), 'signed-in dashboard')
      assert.equal(await evaluate(parent, "localStorage.getItem('loginMarker')"), 'synthetic')
      assert.match(await evaluate(parent, 'document.cookie'), /sid=synthetic/)

      const manual = { surfaceId: 'synthetic-manual-tab' }
      const created = await manager.createSurface({ version: 2, surfaceId: manual.surfaceId,
        kind: 'url-preview', payload: { url: origin + '/dashboard', scopeId: 'synthetic-task',
          contextTargetRef: parent.targetRef } })
      assert.equal(created.ok, true, created.message)
      const manualRecord = manager.surfaces.get(manual.surfaceId)
      assert.equal(manualRecord.previewSession, record(parent).previewSession)
      assert.equal(await manualRecord.view.webContents.executeJavaScript("localStorage.getItem('loginMarker')"), 'synthetic')
      assert.match(await manualRecord.view.webContents.executeJavaScript('document.cookie'), /sid=synthetic/)

      await clickNamed(parent, 'Open related tab')
      const popup = await until(() => [...manager.surfaces.values()].find(value =>
        value.openerTargetRef === parent.targetRef), 'related popup')
      assert.equal(popup.previewSession, record(parent).previewSession)
      assert.equal(await popup.view.webContents.executeJavaScript("localStorage.getItem('loginMarker')"), 'synthetic')

      assert.equal((await manager.navigateSurface({ version: 2, surfaceId: manual.surfaceId,
        action: 'navigate', url: origin + '/second' })).ok, true)
      assert.equal(manualRecord.view.webContents.getURL(), origin + '/second')
      assert.equal((await manager.navigateSurface({ version: 2, surfaceId: manual.surfaceId, action: 'back' })).ok, true)
      await until(() => manualRecord.view.webContents.getURL().endsWith('/dashboard'), 'history back')
      assert.equal((await manager.navigateSurface({ version: 2, surfaceId: manual.surfaceId, action: 'forward' })).ok, true)
      await until(() => manualRecord.view.webContents.getURL().endsWith('/second'), 'history forward')

      let observed = await call({ operation: 'observe', targetRef: parent.targetRef })
      await call({ operation: 'act', targetRef: parent.targetRef, action: 'fill',
        ref: refNamed(observed, 'Entry'), text: 'synthetic-form-value' })
      await clickNamed(parent, 'Submit form')
      assert.equal(await evaluate(parent, "document.querySelector('#form-result').textContent"), 'synthetic-form-value')
      await clickNamed(parent, 'Frame action')
      await until(() => evaluate(parent, "document.body.dataset.frameActed === 'yes'"), 'iframe action')
      owner.showInactive()
      assert.equal(manager.setSurfaceRect({ surfaceId: record(parent).id,
        x: 12, y: 12, width: 900, height: 720, visible: true }).ok, true)
      await until(() => evaluate(parent, "document.querySelector('#image').naturalWidth === 32"), 'SVG image load')
      await clickNamed(parent, 'Play audio')
      await until(() => evaluate(parent, "document.querySelector('#audio').currentTime > 0"), 'WAV playback')
      assert.ok(await evaluate(parent, "document.querySelector('#audio').readyState >= 2"))
      await clickNamed(parent, 'Record video')
      const media = await evaluate(parent, 'window.recordCompletion')
      assert.ok(media.bytes > 100 && media.width === 80 && media.height === 40,
        `Expected generated WebM to decode: ${JSON.stringify(media)}`)
      await until(() => evaluate(parent, "document.querySelector('#video').currentTime > 0"), 'WebM playback')
        .catch(async error => { throw new Error(`${error.message}: ${JSON.stringify(await evaluate(parent,
          `(() => { const v=document.querySelector('#video'); return {currentTime:v.currentTime,
            readyState:v.readyState,duration:v.duration,paused:v.paused,ended:v.ended,
            videoWidth:v.videoWidth,videoHeight:v.videoHeight,error:v.error?.message}; })()`))}`) })
      await clickNamed(parent, 'Shadow action')
      assert.equal(await evaluate(parent, "document.querySelector('#shadow-result').textContent"), 'shadow-acted')

      const findStates = () => events.filter(value => value.surfaceId === record(parent).id && value.type === 'find-state')
      assert.equal((await manager.navigateSurface({ version: 2, surfaceId: record(parent).id,
        action: 'find', query: 'needle' })).ok, true)
      await until(() => findStates().some(value => value.detail?.findMatches === 3
        && value.detail?.findFinal === true), 'three find matches')
      const firstOrdinal = findStates().at(-1).detail.findActiveMatch
      assert.equal((await manager.navigateSurface({ version: 2, surfaceId: record(parent).id,
        action: 'find-next', forward: true })).ok, true)
      await until(() => findStates().at(-1)?.detail?.findFinal === true
        && findStates().at(-1)?.detail?.findActiveMatch !== firstOrdinal, 'next find match')
      assert.equal(findStates().at(-1).detail.findMatches, 3)
      assert.equal((await manager.navigateSurface({ version: 2, surfaceId: record(parent).id,
        action: 'find-next', forward: false })).ok, true)
      await until(() => findStates().at(-1)?.detail?.findFinal === true
        && findStates().at(-1)?.detail?.findActiveMatch === firstOrdinal, 'previous find match')
      assert.equal(findStates().at(-1).detail.findMatches, 3)
      assert.equal((await manager.navigateSurface({ version: 2, surfaceId: record(parent).id,
        action: 'find-stop' })).ok, true)
      assert.equal(findStates().at(-1).detail.findQuery, '')
      const findRequested = () => events.some(value =>
        value.surfaceId === record(parent).id && value.type === 'find-requested')
      record(parent).view.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'F',
        modifiers: [process.platform === 'darwin' ? 'meta' : 'control'] })
      await until(findRequested, 'native Ctrl/Cmd+F shortcut')

      const workbenchZoom = owner.webContents.getZoomFactor()
      assert.equal((await manager.navigateSurface({ version: 2, surfaceId: manual.surfaceId,
        action: 'zoom', zoomFactor: 1.25 })).ok, true)
      assert.equal(manualRecord.view.webContents.getZoomFactor(), 1.25)
      assert.equal(owner.webContents.getZoomFactor(), workbenchZoom,
        'Page zoom must not change the workbench')

      let contextTemplate
      Menu.buildFromTemplate = template => {
        contextTemplate = template
        return { popup() {} }
      }
      const contextPoint = await evaluate(parent, `(() => { const r = document.querySelector('#context-link')
        .getBoundingClientRect(); return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}; })()`)
      const contents = record(parent).view.webContents
      contents.sendInputEvent({ type: 'mouseDown', x: contextPoint.x, y: contextPoint.y, button: 'right', clickCount: 1 })
      contents.sendInputEvent({ type: 'mouseUp', x: contextPoint.x, y: contextPoint.y, button: 'right', clickCount: 1 })
      await until(() => contextTemplate, 'native page context menu')
      assert.ok(contextTemplate.some(item => /Find in page|在页面中查找/.test(item.label || '')))
      const beforeContextFind = events.length
      contextTemplate.find(item => /Find in page|在页面中查找/.test(item.label || '')).click()
      assert.ok(events.slice(beforeContextFind).some(value => value.type === 'find-requested'))
      const related = contextTemplate.find(item => /Open link in new tab|在新标签页打开链接/.test(item.label || ''))
      assert.ok(related, 'Link context menu has the related tab action')
      related.click()
      await until(() => [...manager.surfaces.values()].some(value => value.contextTargetRef === parent.targetRef
        && value.view.webContents.getURL() === origin + '/context'), 'context menu related tab')
      Menu.buildFromTemplate = originalBuild

      const wasFullscreen = owner.isFullScreen()
      await clickNamed(parent, 'Enter fullscreen')
      await until(() => evaluate(parent, 'Boolean(document.fullscreenElement)'), 'HTML fullscreen entry')
      await until(() => owner.isFullScreen(), 'owner fullscreen expansion')
      await clickNamed(parent, 'Exit fullscreen')
      await until(() => evaluate(parent, '!document.fullscreenElement'), 'HTML fullscreen exit')
      await until(() => owner.isFullScreen() === wasFullscreen, 'owner fullscreen restoration')

      assert.equal((await manager.navigateSurface({ version: 2, surfaceId: manual.surfaceId,
        action: 'navigate', url: origin + '/dashboard' })).ok, true)
      await clickNamed({ targetRef: manualRecord.targetRef }, 'Guard close')
      dialog.showMessageBoxSync = () => 0
      const retained = await manager.destroySurface(manual.surfaceId)
      assert.equal(retained.code, 'CLOSE_CANCELLED')
      assert.equal(manager.surfaces.get(manual.surfaceId), manualRecord)
      assert.equal(manualRecord.contents.isDestroyed(), false)

      await clickNamed(parent, 'Guard close')
      assert.equal(await evaluate(parent, 'navigator.userActivation.hasBeenActive'), true)
      assert.equal(await evaluate(parent, 'typeof window.onbeforeunload'), 'function')
      assert.equal(await manager.closeBrowserTabs(), false,
        'An app quit must stop when a browser tab chooses to stay')
      assert.ok(manager.surfaces.has(record(parent).id))
      let confirmations = 0
      dialog.showMessageBoxSync = () => { confirmations += 1; return 0 }
      const cancelled = await manager.navigateSurface({ version: 2, surfaceId: record(parent).id, action: 'close' })
      assert.equal(cancelled.ok, false)
      assert.equal(cancelled.code, 'CLOSE_CANCELLED')
      assert.ok(manager.surfaces.has(record(parent).id))
      const jsAfterStay = await Promise.race([evaluate(parent, '1+1').catch(error => String(error)),
        sleep(1000).then(() => 'timed out')])
      assert.equal(jsAfterStay, 2, JSON.stringify({ jsAfterStay,
        nativeDialog: record(parent).playwright?.pendingDialog }))
      const stayed = await call({ operation: 'observe', targetRef: parent.targetRef })
      assert.ok(stayed.observation?.refs?.some(value => value.name === 'Guard close'),
        JSON.stringify({ url: record(parent).view.webContents.getURL(),
          destroyed: record(parent).view.webContents.isDestroyed(), jsAfterStay,
          state: stayed.observation }))
      await clickNamed(parent, 'Guard close')
      dialog.showMessageBoxSync = () => { confirmations += 1; return 1 }
      const closingRecord = record(parent)
      const left = await manager.navigateSurface({ version: 2, surfaceId: closingRecord.id, action: 'close' })
      assert.equal(left.ok, true, JSON.stringify({ left, confirmations,
        destroyed: closingRecord.contents.isDestroyed() }))
      assert.equal(closingRecord.contents.isDestroyed(), true)
      assert.ok(confirmations >= 2, 'Both stay and leave used Chromium beforeunload')
      console.log('Browser foundations passed: session context, popup, history, form, iframe, shadow root, SVG, WAV, WebM, find, zoom, context menu, fullscreen, close and quit cancellation.')
    } catch (error) {
      exitCode = 1
      console.error(error)
    } finally {
      Menu.buildFromTemplate = originalBuild
      dialog.showMessageBoxSync = originalDialog
      const shutdown = setTimeout(() => app.exit(exitCode), 3000)
      shutdown.unref()
      await manager?.destroyAll().catch(() => {})
      if (owner && !owner.isDestroyed()) owner.destroy()
      web.closeAllConnections()
      await new Promise(resolve => web.close(resolve))
      app.exit(exitCode)
    }
  })()
}
