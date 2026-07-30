import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createSocket } from 'node:dgram'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { createServer as createTcpServer } from 'node:net'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import { WebSocketServer } from 'ws'

const scriptPath = fileURLToPath(import.meta.url)
const scriptDir = dirname(scriptPath)
const fixtureRoot = join(scriptDir, 'fixtures', 'native-workbench-smoke')
const require = createRequire(import.meta.url)
const [fixtureFont, gsapSource, lottieSource] = await Promise.all([
  readFile(join(
    scriptDir,
    '..',
    '..',
    '..',
    'opensquilla-webui',
    'src',
    'assets',
    'fonts',
    'ibm-plex-sans-400.woff2',
  )),
  readFile(require.resolve('gsap/dist/gsap.min.js')),
  readFile(require.resolve('lottie-web/build/player/lottie.min.js')),
])

if (
  process.platform === 'linux'
  && !process.env.DISPLAY
  && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_WORKBENCH_V2_UNDER_XVFB !== '1'
) {
  const result = spawnSync('xvfb-run', ['-a', process.execPath, scriptPath], {
    env: {
      ...process.env,
      OPENSQUILLA_WORKBENCH_V2_UNDER_XVFB: '1',
    },
    stdio: 'inherit',
  })
  if (result.error) {
    throw new Error(`A display or xvfb-run is required for the Electron smoke test: ${result.error.message}`)
  }
  process.exit(result.status ?? 1)
}

let fixtureFontRequests = 0
let fixtureGsapRequests = 0
let fixtureLottieRequests = 0
let fixtureServiceWorkerRequests = 0
let fixtureWebSocketConnections = 0
let fixtureStunPackets = 0
let privilegedGatewayRequests = 0
let turnTcpConnections = 0
let turnTcpBytes = 0
const turnTcpClients = new Set()

const stunSocket = createSocket('udp4')
stunSocket.on('message', () => {
  fixtureStunPackets += 1
})
const privilegedGatewayServer = createServer((_request, response) => {
  privilegedGatewayRequests += 1
  response.setHeader('content-type', 'text/html; charset=utf-8')
  response.end('<!doctype html><title>Privileged Gateway</title>')
})
const turnTcpSink = createTcpServer(socket => {
  turnTcpConnections += 1
  turnTcpClients.add(socket)
  socket.on('data', data => {
    turnTcpBytes += data.length
  })
  socket.on('close', () => turnTcpClients.delete(socket))
})

const server = createServer((request, response) => {
  const url = new URL(request.url || '/', 'http://fixture.invalid')
  response.setHeader('cache-control', 'no-store')
  if (url.pathname === '/protected') {
    const expected = `Basic ${Buffer.from('fixture-user:fixture-password').toString('base64')}`
    if (request.headers.authorization !== expected) {
      response.statusCode = 401
      response.setHeader('www-authenticate', 'Basic realm="Synthetic preview"')
      response.end('Authentication required')
      return
    }
    response.setHeader('content-type', 'text/html; charset=utf-8')
    response.end('<!doctype html><title>Authenticated preview</title>')
    return
  }
  if (url.pathname === '/index.html') {
    response.setHeader('content-type', 'text/html; charset=utf-8')
    response.end(`<!doctype html>
      <title>Bundle preview</title>
      <style>
        @font-face {
          font-family: "WorkbenchFixtureFont";
          src: url("/fixture-font.woff2") format("woff2");
          font-display: block;
          font-style: normal;
          font-weight: 400;
        }
        #font-probe { font-family: "WorkbenchFixtureFont", sans-serif; }
        #gsap-probe { width: 20px; height: 20px; background: rgb(20, 80, 200); }
        #lottie-probe { width: 100px; height: 100px; }
      </style>
      <div id="font-probe">Synthetic font preview</div>
      <div id="gsap-probe"></div>
      <div id="lottie-probe"></div>
      <canvas id="canvas-probe" width="8" height="8"></canvas>
      <video id="video-probe" autoplay muted playsinline></video>
      <script src="/gsap.min.js"></script>
      <script src="/lottie.min.js"></script>
      <script type="module" src="/module.js"></script>
      <script>
        const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
        const withTimeout = (promise, label, timeoutMs = 5000) => Promise.race([
          promise,
          new Promise((_, reject) => setTimeout(
            () => reject(new Error(label + ' timed out')),
            timeoutMs,
          )),
        ])

        window.__fetchProbe = fetch('/data.json').then(r => r.json()).then(v => v.ok)
        window.__workerProbe = new Promise(resolve => {
          const worker = new Worker('/worker.js')
          worker.onmessage = event => resolve(event.data)
          worker.onerror = () => resolve('worker-error')
          worker.postMessage('probe')
        })
        localStorage.setItem('preview-session-probe', 'stored')
        window.__animationProbe = new Promise(resolve =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))
        window.__wasmProbe = WebAssembly.instantiate(
          new Uint8Array([0,97,115,109,1,0,0,0])
        ).then(() => true, () => false)

        window.__serviceWorkerProbe = (async () => {
          try {
            if (!('serviceWorker' in navigator)) {
              return { status: 'failed', reason: 'Service Worker API unavailable' }
            }
            const registration = await navigator.serviceWorker.register('/service-worker.js', {
              scope: '/',
            })
            const readyRegistration = await withTimeout(
              navigator.serviceWorker.ready,
              'Service Worker activation',
            )
            const worker = readyRegistration.active
              || registration.active
              || registration.waiting
              || registration.installing
            if (!worker) {
              return { status: 'failed', reason: 'Service Worker has no active worker' }
            }
            const reply = await withTimeout(new Promise((resolve, reject) => {
              const channel = new MessageChannel()
              channel.port1.onmessage = event => resolve(event.data)
              channel.port1.onmessageerror = () => reject(new Error('Service Worker reply failed'))
              worker.postMessage({ type: 'preview-probe' }, [channel.port2])
            }), 'Service Worker message')
            return {
              status: 'passed',
              echo: reply && reply.echo,
              scope: readyRegistration.scope,
            }
          } catch (error) {
            return { status: 'failed', reason: String(error && error.message || error) }
          }
        })()

        window.__webSocketProbe = new Promise(resolve => {
          let settled = false
          const socket = new WebSocket('ws://' + location.host + '/socket')
          const finish = result => {
            if (settled) return
            settled = true
            clearTimeout(timeout)
            try { socket.close() } catch {}
            resolve(result)
          }
          const timeout = setTimeout(
            () => finish({ status: 'failed', reason: 'WebSocket echo timed out' }),
            5000,
          )
          socket.onopen = () => socket.send('preview-probe')
          socket.onmessage = event => finish({ status: 'passed', echo: event.data })
          socket.onerror = () => finish({ status: 'failed', reason: 'WebSocket connection failed' })
        })

        window.__fontProbe = document.fonts
          .load('16px "WorkbenchFixtureFont"', 'Synthetic font preview')
          .then(fonts => ({
            status: fonts.length > 0
              && document.fonts.check(
                '16px "WorkbenchFixtureFont"',
                'Synthetic font preview',
              )
              ? 'passed'
              : 'failed',
            count: fonts.length,
            family: getComputedStyle(document.getElementById('font-probe')).fontFamily,
          }))
          .catch(error => ({
            status: 'failed',
            reason: String(error && error.message || error),
          }))

        window.__videoProbe = (async () => {
          const source = document.createElement('canvas')
          source.width = 32
          source.height = 24
          if (typeof source.captureStream !== 'function') {
            return {
              status: 'skipped',
              reason: 'captureStream unavailable in this Chromium graphics build',
            }
          }
          const context = source.getContext('2d')
          const video = document.getElementById('video-probe')
          let running = true
          let frame = 0
          const draw = () => {
            if (!running) return
            frame += 1
            context.fillStyle = 'rgb(' + (frame % 251) + ',40,120)'
            context.fillRect(0, 0, source.width, source.height)
            requestAnimationFrame(draw)
          }
          requestAnimationFrame(draw)
          const stream = source.captureStream(15)
          video.srcObject = stream
          try {
            await video.play()
            await withTimeout(new Promise(resolve => {
              if (video.videoWidth > 0 && video.videoHeight > 0) {
                resolve()
                return
              }
              video.addEventListener('loadedmetadata', resolve, { once: true })
            }), 'captureStream video metadata')
            await wait(120)
            return {
              status: 'passed',
              tagName: video.tagName,
              hasMediaStream: video.srcObject instanceof MediaStream,
              readyState: video.readyState,
              videoWidth: video.videoWidth,
              videoHeight: video.videoHeight,
              drawnFrames: frame,
            }
          } catch (error) {
            return {
              status: 'skipped',
              reason: 'captureStream playback unavailable: '
                + String(error && error.name || error),
            }
          } finally {
            running = false
            for (const track of stream.getTracks()) track.stop()
            video.srcObject = null
          }
        })()

        window.__webglProbe = (() => {
          const canvas = document.createElement('canvas')
          canvas.width = 2
          canvas.height = 2
          const context = canvas.getContext('webgl2') || canvas.getContext('webgl')
          if (!context) {
            return {
              status: 'skipped',
              reason: 'WebGL context unavailable under current Electron graphics backend',
            }
          }
          context.clearColor(0.25, 0.5, 0.75, 1)
          context.clear(context.COLOR_BUFFER_BIT)
          const pixel = new Uint8Array(4)
          context.readPixels(
            0,
            0,
            1,
            1,
            context.RGBA,
            context.UNSIGNED_BYTE,
            pixel,
          )
          return {
            status: 'passed',
            version: context.getParameter(context.VERSION),
            pixel: Array.from(pixel),
          }
        })()

        const lottieAnimation = window.lottie.loadAnimation({
          container: document.getElementById('lottie-probe'),
          renderer: 'svg',
          loop: false,
          autoplay: false,
          animationData: {
            v: '5.13.0',
            fr: 60,
            ip: 0,
            op: 240,
            w: 100,
            h: 100,
            nm: 'Synthetic moving dot',
            ddd: 0,
            assets: [],
            layers: [{
              ddd: 0,
              ind: 1,
              ty: 4,
              nm: 'Moving dot',
              sr: 1,
              ks: {
                o: { a: 0, k: 100 },
                r: { a: 0, k: 0 },
                p: {
                  a: 1,
                  k: [
                    {
                      t: 0,
                      s: [15, 50, 0],
                      e: [85, 50, 0],
                      i: { x: [0.667], y: [1] },
                      o: { x: [0.333], y: [0] },
                    },
                    { t: 240, s: [85, 50, 0] },
                  ],
                },
                a: { a: 0, k: [0, 0, 0] },
                s: { a: 0, k: [100, 100, 100] },
              },
              ao: 0,
              shapes: [
                {
                  ty: 'el',
                  d: 1,
                  s: { a: 0, k: [20, 20] },
                  p: { a: 0, k: [0, 0] },
                  nm: 'Ellipse Path 1',
                },
                {
                  ty: 'fl',
                  c: { a: 0, k: [0.1, 0.35, 0.9, 1] },
                  o: { a: 0, k: 100 },
                  r: 1,
                  nm: 'Fill 1',
                },
              ],
              ip: 0,
              op: 240,
              st: 0,
              bm: 0,
            }],
          },
        })
        const lottieReady = lottieAnimation.isLoaded
          ? Promise.resolve()
          : withTimeout(new Promise(resolve => {
              lottieAnimation.addEventListener('DOMLoaded', resolve)
            }), 'lottie DOM initialization')

        window.__temporalProbe = async () => {
          await lottieReady
          const element = document.getElementById('gsap-probe')
          const canvas = document.getElementById('canvas-probe')
          const context = canvas.getContext('2d', { willReadFrequently: true })
          let running = true
          let canvasFrame = 0
          const draw = () => {
            if (!running) return
            canvasFrame += 1
            context.fillStyle = 'rgb('
              + (canvasFrame % 251) + ','
              + ((canvasFrame * 3) % 251) + ','
              + ((canvasFrame * 7) % 251) + ')'
            context.fillRect(0, 0, canvas.width, canvas.height)
            requestAnimationFrame(draw)
          }
          window.gsap.killTweensOf(element)
          window.gsap.set(element, { x: 0 })
          lottieAnimation.goToAndStop(0, true)
          const tween = window.gsap.to(element, {
            x: 160,
            duration: 4,
            ease: 'none',
          })
          lottieAnimation.goToAndPlay(0, true)
          requestAnimationFrame(draw)
          await new Promise(resolve => requestAnimationFrame(resolve))
          const sample = () => ({
            domX: Number(window.gsap.getProperty(element, 'x')),
            canvasFrame,
            canvasPixel: Array.from(context.getImageData(0, 0, 1, 1).data),
            lottieFrame: Number(lottieAnimation.currentFrame),
          })
          const first = sample()
          await wait(180)
          const second = sample()
          await wait(260)
          const third = sample()
          running = false
          tween.kill()
          lottieAnimation.pause()
          return {
            status: 'passed',
            gsapVersion: window.gsap.version,
            lottieVersion: window.lottie.version,
            samples: [first, second, third],
          }
        }
      </script>`)
    return
  }
  if (url.pathname === '/service-worker.js') {
    fixtureServiceWorkerRequests += 1
    response.setHeader('content-type', 'application/javascript; charset=utf-8')
    response.setHeader('service-worker-allowed', '/')
    response.end(`
      self.addEventListener('install', () => self.skipWaiting())
      self.addEventListener('activate', event => {
        event.waitUntil(self.clients.claim())
      })
      self.addEventListener('message', event => {
        const port = event.ports && event.ports[0]
        if (port) port.postMessage({ echo: event.data && event.data.type })
      })
    `)
    return
  }
  if (url.pathname === '/gsap.min.js') {
    fixtureGsapRequests += 1
    response.setHeader('content-type', 'application/javascript; charset=utf-8')
    response.end(gsapSource)
    return
  }
  if (url.pathname === '/lottie.min.js') {
    fixtureLottieRequests += 1
    response.setHeader('content-type', 'application/javascript; charset=utf-8')
    response.end(lottieSource)
    return
  }
  if (url.pathname === '/fixture-font.woff2') {
    fixtureFontRequests += 1
    response.setHeader('content-type', 'font/woff2')
    response.end(fixtureFont)
    return
  }
  if (url.pathname === '/module.js') {
    response.setHeader('content-type', 'application/javascript; charset=utf-8')
    response.end("import { value } from '/dependency.js'; window.__moduleProbe = value")
    return
  }
  if (url.pathname === '/dependency.js') {
    response.setHeader('content-type', 'application/javascript; charset=utf-8')
    response.end("export const value = 'module-loaded'")
    return
  }
  if (url.pathname === '/worker.js') {
    response.setHeader('content-type', 'application/javascript; charset=utf-8')
    response.end("onmessage = event => postMessage(event.data + '-worker')")
    return
  }
  if (url.pathname === '/data.json') {
    response.setHeader('content-type', 'application/json')
    response.end('{"ok":true}')
    return
  }
  if (url.pathname === '/download.txt') {
    response.setHeader('content-type', 'text/plain; charset=utf-8')
    response.setHeader('content-disposition', 'attachment; filename="preview.txt"')
    response.end('download')
    return
  }
  response.setHeader('content-type', 'text/html; charset=utf-8')
  response.end(`<!doctype html><title>${url.pathname}</title>`)
})

const webSocketServer = new WebSocketServer({ noServer: true })
webSocketServer.on('connection', socket => {
  fixtureWebSocketConnections += 1
  socket.on('message', message => socket.send(`echo:${message.toString()}`))
})
server.on('upgrade', (request, socket, head) => {
  const url = new URL(request.url || '/', 'http://fixture.invalid')
  if (url.pathname !== '/socket') {
    socket.destroy()
    return
  }
  webSocketServer.handleUpgrade(request, socket, head, upgraded => {
    webSocketServer.emit('connection', upgraded, request)
  })
})

await new Promise((resolveListen, rejectListen) => {
  server.once('error', rejectListen)
  server.listen(0, '127.0.0.1', resolveListen)
})
const address = server.address()
if (!address || typeof address === 'string') throw new Error('Fixture server did not bind.')
await new Promise((resolveListen, rejectListen) => {
  stunSocket.once('error', rejectListen)
  stunSocket.bind(0, '127.0.0.1', resolveListen)
})
const stunAddress = stunSocket.address()
if (!stunAddress || typeof stunAddress === 'string') {
  throw new Error('Synthetic STUN endpoint did not bind.')
}
await new Promise((resolveListen, rejectListen) => {
  privilegedGatewayServer.once('error', rejectListen)
  privilegedGatewayServer.listen(0, '127.0.0.1', resolveListen)
})
const privilegedGatewayAddress = privilegedGatewayServer.address()
if (!privilegedGatewayAddress || typeof privilegedGatewayAddress === 'string') {
  throw new Error('Synthetic privileged Gateway did not bind.')
}
await new Promise((resolveListen, rejectListen) => {
  turnTcpSink.once('error', rejectListen)
  turnTcpSink.listen(0, '127.0.0.1', resolveListen)
})
const turnTcpAddress = turnTcpSink.address()
if (!turnTcpAddress || typeof turnTcpAddress === 'string') {
  throw new Error('Synthetic TURN/TCP sink did not bind.')
}

const previewHost = 'p-0123456789abcdef0123456789abcdef.localhost'
const previewOrigin = `http://${previewHost}:${address.port}`
const loopbackOrigin = `http://127.0.0.1:${address.port}`
const privilegedGatewayUrl = `http://127.0.0.1:${privilegedGatewayAddress.port}`
const privilegedGatewayAlias = `http://localhost:${privilegedGatewayAddress.port}`
const isolationRoot = await mkdtemp(join(tmpdir(), 'opensquilla-workbench-v2-smoke-'))
let electronApp

try {
  electronApp = await electron.launch({
    args: [
      `--user-data-dir=${join(isolationRoot, 'chromium')}`,
      '--autoplay-policy=no-user-gesture-required',
      fixtureRoot,
    ],
    env: {
      ...process.env,
      ELECTRON_DISABLE_SECURITY_WARNINGS: 'true',
      NO_PROXY: '127.0.0.1,localhost,.localhost',
      no_proxy: '127.0.0.1,localhost,.localhost',
    },
  })

  const result = await electronApp.evaluate(
    async ({ BrowserWindow, webContents }, fixture) => {
      const Manager = globalThis.__opensquillaNativeWorkbenchSurfaceManager
      if (!Manager) throw new Error('The native Workbench manager fixture was not installed.')
      const events = []
      const owner = new BrowserWindow({
        show: true,
        width: 900,
        height: 700,
        webPreferences: {
          contextIsolation: true,
          nodeIntegration: false,
          partition: `opensquilla-workbench-v2-owner:${Date.now()}`,
          sandbox: true,
        },
      })
      await owner.loadURL('data:text/html,<title>Trusted Control UI fixture</title>')
      let reentrantReplacementPromise = null
      let manager
      manager = new Manager({
        getPrivilegedGatewayUrl: () => fixture.privilegedGatewayUrl,
        getWindow: () => owner,
        emit: event => {
          events.push(event)
          if (
            event.surfaceId === 'browser:terminal-reentry'
            && event.type === 'crashed'
            && !reentrantReplacementPromise
          ) {
            reentrantReplacementPromise = manager.createSurface({
              version: 2,
              surfaceId: 'browser:terminal-reentry',
              kind: 'url-preview',
              payload: {
                url: `${fixture.loopbackOrigin}/terminal-replacement`,
                scopeId: 'synthetic:terminal-replacement',
              },
            })
          }
        },
      })

      async function waitFor(check, label, timeoutMs = 10_000) {
        const deadline = Date.now() + timeoutMs
        while (Date.now() < deadline) {
          const value = check()
          if (value) return value
          await new Promise(resolveWait => setTimeout(resolveWait, 25))
        }
        throw new Error(`Timed out waiting for ${label}.`)
      }

      function view(surfaceId) {
        const resultView = manager.surfaces.get(surfaceId)?.view
        if (!resultView) throw new Error(`Surface view ${surfaceId} was not found.`)
        return resultView
      }

      function emitRendererGone(contents) {
        const handled = contents.emit(
          'render-process-gone',
          {},
          { reason: 'crashed', exitCode: 1 },
        )
        if (!handled) throw new Error('No renderer crash listener was registered.')
      }

      function emitUnresponsive(contents) {
        const handled = contents.emit('unresponsive')
        if (!handled) throw new Error('No renderer unresponsive listener was registered.')
      }

      const full = await manager.createSurface({
        version: 2,
        surfaceId: 'artifact:v2-full',
        kind: 'artifact-preview',
        payload: {
          launchUrl: `${fixture.previewOrigin}/index.html`,
          expectedOrigin: fixture.previewOrigin,
          scopeId: 'synthetic:v2-full',
          mode: 'full',
        },
      })
      if (!full.ok) throw new Error(full.message || 'Full v2 preview failed to load.')
      const fullContents = view('artifact:v2-full').webContents
      await waitFor(
        () => events.some(event =>
          event.surfaceId === 'artifact:v2-full' && event.type === 'ready'),
        'full preview ready event',
      )
      const fullWebPreferences = fullContents.getLastWebPreferences()
      const fullSecurityPreferences = {
        contextIsolation: fullWebPreferences.contextIsolation,
        disableDialogs: fullWebPreferences.disableDialogs,
        nodeIntegration: fullWebPreferences.nodeIntegration,
        preload: fullWebPreferences.preload ?? null,
        safeDialogs: fullWebPreferences.safeDialogs,
        sandbox: fullWebPreferences.sandbox,
        webSecurity: fullWebPreferences.webSecurity,
        webviewTag: fullWebPreferences.webviewTag,
      }
      const fullWebRtcType = await fullContents.executeJavaScript(
        'typeof RTCPeerConnection',
      )
      fullContents.openDevTools({ mode: 'detach', activate: false })
      await new Promise(resolveWait => setTimeout(resolveWait, 100))
      const fullDevToolsBlocked = !fullContents.isDevToolsOpened()
      const fullView = view('artifact:v2-full')
      manager.setSurfaceRect({
        surfaceId: 'artifact:v2-full',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: true,
      })
      await waitFor(() => fullView.getVisible(), 'visible v2 full surface')
      const fullAudioActive = !fullContents.isAudioMuted()
      manager.setSurfaceRect({
        surfaceId: 'artifact:v2-full',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: false,
      })
      const hiddenAudioMuted = fullContents.isAudioMuted()
      manager.setSurfaceRect({
        surfaceId: 'artifact:v2-full',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: true,
      })
      const resumedAudioActive = !fullContents.isAudioMuted()
      const fullProbes = await fullContents.executeJavaScript(`Promise.all([
        window.__fetchProbe,
        window.__workerProbe,
        window.__animationProbe,
        window.__wasmProbe,
        Promise.resolve(window.__moduleProbe),
        window.__serviceWorkerProbe,
        window.__webSocketProbe,
        window.__fontProbe,
        window.__videoProbe,
        Promise.resolve(window.__webglProbe),
        window.__temporalProbe(),
      ]).then(([
        fetchProbe,
        workerProbe,
        animationProbe,
        wasmProbe,
        moduleProbe,
        serviceWorkerProbe,
        webSocketProbe,
        fontProbe,
        videoProbe,
        webglProbe,
        temporalProbe,
      ]) => ({
        fetchProbe,
        workerProbe,
        animationProbe,
        wasmProbe,
        moduleProbe,
        serviceWorkerProbe,
        webSocketProbe,
        fontProbe,
        videoProbe,
        webglProbe,
        temporalProbe,
        storage: localStorage.getItem('preview-session-probe'),
        node: typeof require + ':' + typeof process,
      }))`)
      await fullContents.executeJavaScript(
        "localStorage.setItem('reload-retention-probe', 'retained')",
      )
      const readyEventsBeforeReload = events.filter(event =>
        event.surfaceId === 'artifact:v2-full' && event.type === 'ready').length
      const fullReload = await manager.navigateSurface({
        version: 2,
        surfaceId: 'artifact:v2-full',
        action: 'reload',
      })
      await waitFor(
        () => events.filter(event =>
          event.surfaceId === 'artifact:v2-full' && event.type === 'ready').length
          > readyEventsBeforeReload,
        'full preview reload',
      )
      const fullStorageSurvivedReload = await fullContents.executeJavaScript(
        "localStorage.getItem('reload-retention-probe') === 'retained'",
      )

      let remoteRequests = 0
      await fullContents.session.protocol.handle('https', request => {
        remoteRequests += 1
        return new Response('window.__remoteV2Probe = true', {
          headers: { 'content-type': 'application/javascript; charset=utf-8' },
        })
      })
      const fullRemote = await fullContents.executeJavaScript(`new Promise(resolve => {
        const script = document.createElement('script')
        script.src = 'https://assets.example.test/full.js'
        script.onload = () => resolve(Boolean(window.__remoteV2Probe))
        script.onerror = () => resolve(false)
        document.head.append(script)
      })`)
      const artifactGatewayAccess = await fullContents.executeJavaScript(
        `fetch('${fixture.privilegedGatewayAlias}/api/config', { mode: 'no-cors' })
          .then(() => 'loaded', () => 'blocked')`,
      )
      const artifactGatewayWarning = events.some(event =>
        event.surfaceId === 'artifact:v2-full'
        && event.type === 'blocked-action'
        && event.detail?.action === 'gateway'
        && event.detail?.reason === 'privileged-origin-isolated')
      const popupNull = await fullContents.executeJavaScript(
        "window.open('https://example.test/popup') === null",
      )
      await waitFor(
        () => events.some(event =>
          event.surfaceId === 'artifact:v2-full'
          && event.type === 'blocked-action'
          && event.detail?.action === 'popup'),
        'popup blocked event',
      )
      const originalUrl = fullContents.getURL()
      await fullContents.executeJavaScript("location.href = 'file:///synthetic/secret'").catch(() => {})
      await new Promise(resolveWait => setTimeout(resolveWait, 100))
      const privilegedNavigationBlocked = fullContents.getURL() === originalUrl

      const permissionPromise = fullContents.executeJavaScript(`new Promise(resolve => {
        navigator.geolocation.getCurrentPosition(
          () => resolve('allowed'),
          () => resolve('denied'),
        )
      })`)
      const permissionEvent = await waitFor(
        () => events.find(event =>
          event.surfaceId === 'artifact:v2-full'
          && event.type === 'permission-request'
          && event.detail?.permission === 'geolocation'),
        'geolocation permission request',
      )
      const permissionResponse = manager.respondToPermission({
        version: 2,
        surfaceId: 'artifact:v2-full',
        requestId: permissionEvent.detail.requestId,
        allow: false,
      })
      const permissionResult = await permissionPromise

      fullContents.downloadURL(`${fixture.previewOrigin}/download.txt`)
      await waitFor(
        () => events.some(event =>
          event.surfaceId === 'artifact:v2-full'
          && event.type === 'blocked-action'
          && event.detail?.action === 'download'),
        'automatic download blocked event',
      )
      const fullNavigationEvents = events.filter(event =>
        event.surfaceId === 'artifact:v2-full'
        && event.version === 2
        && event.type === 'navigation-state')

      await manager.destroySurface('artifact:v2-full')
      await waitFor(() => fullContents.isDestroyed(), 'full preview destruction')

      const isolated = await manager.createSurface({
        version: 2,
        surfaceId: 'artifact:v2-isolated',
        kind: 'artifact-preview',
        payload: {
          launchUrl: `${fixture.previewOrigin}/`,
          expectedOrigin: fixture.previewOrigin,
          scopeId: 'synthetic:v2-isolated',
          mode: 'full',
        },
      })
      if (!isolated.ok) throw new Error(isolated.message || 'Isolated preview failed.')
      const isolatedContents = view('artifact:v2-isolated').webContents
      const isolationState = await isolatedContents.executeJavaScript(`Promise.all([
        Promise.resolve(localStorage.getItem('preview-session-probe') === null),
        navigator.serviceWorker.getRegistrations(),
      ]).then(([storageWasCleared, registrations]) => ({
        storageWasCleared,
        serviceWorkerRegistrations: registrations.length,
      }))`)
      await manager.destroySurface('artifact:v2-isolated')

      const offline = await manager.createSurface({
        version: 2,
        surfaceId: 'artifact:v2-offline',
        kind: 'artifact-preview',
        payload: {
          launchUrl: `${fixture.previewOrigin}/index.html`,
          expectedOrigin: fixture.previewOrigin,
          scopeId: 'synthetic:v2-offline',
          mode: 'offline',
        },
      })
      if (!offline.ok) throw new Error(offline.message || 'Offline v2 preview failed.')
      const offlineContents = view('artifact:v2-offline').webContents
      let offlineRemoteRequests = 0
      await offlineContents.session.protocol.handle('https', () => {
        offlineRemoteRequests += 1
        return new Response('window.__offlineRemoteProbe = true', {
          headers: { 'content-type': 'application/javascript; charset=utf-8' },
        })
      })
      const offlineLocal = await offlineContents.executeJavaScript(
        'window.__fetchProbe',
      )
      const offlineRemote = await offlineContents.executeJavaScript(`new Promise(resolve => {
        const script = document.createElement('script')
        script.src = 'https://assets.example.test/offline.js'
        script.onload = () => resolve('loaded')
        script.onerror = () => resolve('blocked')
        document.head.append(script)
      })`)
      const offlinePolicyHeaders = await offlineContents.executeJavaScript(`fetch(location.href)
        .then(response => ({
          csp: response.headers.get('content-security-policy'),
          dnsPrefetch: response.headers.get('x-dns-prefetch-control'),
        }))`)
      const offlineWebRtc = await offlineContents.executeJavaScript(`(async () => {
        const bounded = (promise, label) => Promise.race([
          promise,
          new Promise(resolve => setTimeout(() => resolve(label + '-timeout'), 3000)),
        ])
        const realmType = async source => {
          const frame = document.createElement('iframe')
          const loaded = new Promise(resolve => {
            frame.onload = () => resolve(typeof frame.contentWindow.RTCPeerConnection)
          })
          if (source === 'srcdoc') {
            frame.srcdoc = '<!doctype html><title>fresh srcdoc realm</title>'
          } else {
            frame.src = source
          }
          document.body.append(frame)
          const result = await bounded(loaded, source)
          frame.remove()
          return result
        }
        const blobUrl = URL.createObjectURL(new Blob(
          ['<!doctype html><title>fresh blob realm</title>'],
          { type: 'text/html' },
        ))
        const srcdocType = await realmType('srcdoc')
        const blobType = await realmType(blobUrl)
        URL.revokeObjectURL(blobUrl)
        const workerType = await bounded(new Promise(resolve => {
          const workerUrl = URL.createObjectURL(new Blob(
            ['postMessage(typeof RTCPeerConnection)'],
            { type: 'application/javascript' },
          ))
          const worker = new Worker(workerUrl)
          worker.onmessage = event => {
            worker.terminate()
            URL.revokeObjectURL(workerUrl)
            resolve(event.data)
          }
          worker.onerror = () => {
            worker.terminate()
            URL.revokeObjectURL(workerUrl)
            resolve('worker-error')
          }
        }), 'worker')
        let turnAttempt = 'blocked'
        let setupError = ''
        let peer = null
        try {
          peer = new RTCPeerConnection({
            iceServers: [{
              urls: 'turn:127.0.0.1:${fixture.turnTcpPort}?transport=tcp',
              username: 'synthetic-user',
              credential: 'synthetic-password',
            }],
            iceTransportPolicy: 'relay',
          })
          turnAttempt = 'constructed'
          peer.createDataChannel('offline-turn-probe')
          await peer.setLocalDescription(await peer.createOffer())
        } catch (error) {
          setupError = String(error && error.name || error)
        }
        await new Promise(resolve => setTimeout(resolve, 900))
        if (peer) peer.close()
        return {
          blobType,
          mainType: typeof RTCPeerConnection,
          setupError,
          srcdocType,
          turnAttempt,
          workerType,
        }
      })()`)
      const offlineWebRtcPolicy = offlineContents.getWebRTCIPHandlingPolicy()
      let artifactCertificatePrevented = false
      let artifactCertificateCallback = 'not-called'
      offlineContents.emit(
        'select-client-certificate',
        { preventDefault: () => { artifactCertificatePrevented = true } },
        'https://mtls.example.test/resource',
        [{ subjectName: 'Synthetic certificate' }],
        certificate => {
          artifactCertificateCallback = certificate === undefined ? 'declined' : 'selected'
        },
      )
      const artifactClientCertificateDenied = (
        artifactCertificatePrevented
        && artifactCertificateCallback === 'declined'
      )
      const offlineNetworkWarning = events.some(event =>
        event.surfaceId === 'artifact:v2-offline'
        && event.type === 'blocked-action'
        && event.detail?.action === 'network'
        && event.detail?.reason === 'offline-policy')
      const artifactCertificateWarning = events.some(event =>
        event.surfaceId === 'artifact:v2-offline'
        && event.type === 'blocked-action'
        && event.detail?.action === 'client-certificate'
        && event.detail?.reason === 'host-identity-unavailable')
      await manager.destroySurface('artifact:v2-offline')

      const privilegedBrowser = await manager.createSurface({
        version: 2,
        surfaceId: 'browser:gateway',
        kind: 'url-preview',
        payload: {
          url: `${fixture.privilegedGatewayAlias}/control/chat`,
          scopeId: 'synthetic:gateway-isolation',
        },
      })
      const privilegedBrowserWarning = events.some(event =>
        event.surfaceId === 'browser:gateway'
        && event.type === 'blocked-action'
        && event.detail?.action === 'gateway'
        && event.detail?.reason === 'privileged-origin-isolated')

      const browser = await manager.createSurface({
        version: 2,
        surfaceId: 'browser:v2',
        kind: 'url-preview',
        payload: {
          url: `${fixture.loopbackOrigin}/one`,
          scopeId: 'synthetic:browser',
        },
      })
      if (!browser.ok) throw new Error(browser.message || 'URL preview failed.')
      const browserContents = view('browser:v2').webContents
      const navigation = await manager.navigateSurface({
        version: 2,
        surfaceId: 'browser:v2',
        action: 'navigate',
        url: `${fixture.loopbackOrigin}/two`,
      })
      await waitFor(() => browserContents.getURL().endsWith('/two'), 'URL navigation')
      const back = await manager.navigateSurface({
        version: 2,
        surfaceId: 'browser:v2',
        action: 'back',
      })
      await waitFor(() => browserContents.getURL().endsWith('/one'), 'URL history back')
      let browserCertificatePrevented = false
      let browserCertificateCallback = 'not-called'
      browserContents.emit(
        'select-client-certificate',
        { preventDefault: () => { browserCertificatePrevented = true } },
        'https://mtls.example.test/resource',
        [{ subjectName: 'Synthetic certificate' }],
        certificate => {
          browserCertificateCallback = certificate === undefined ? 'declined' : 'selected'
        },
      )
      const browserClientCertificateDenied = (
        browserCertificatePrevented
        && browserCertificateCallback === 'declined'
      )
      const browserCertificateWarning = events.some(event =>
        event.surfaceId === 'browser:v2'
        && event.type === 'blocked-action'
        && event.detail?.action === 'client-certificate'
        && event.detail?.reason === 'host-identity-unavailable')
      await manager.destroySurface('browser:v2')

      const authenticationCreate = manager.createSurface({
        version: 2,
        surfaceId: 'browser:authenticated',
        kind: 'url-preview',
        payload: {
          url: `${fixture.loopbackOrigin}/protected`,
          scopeId: 'synthetic:authenticated',
        },
      })
      const authenticationPrompt = await waitFor(
        () => BrowserWindow.getAllWindows().find(window =>
          window !== owner
          && window.getParentWindow() === owner
          && window.getTitle() === 'Sign in to preview'),
        'Basic Auth credential prompt',
      )
      await waitFor(
        () => !authenticationPrompt.webContents.isLoading(),
        'loaded Basic Auth credential prompt',
      )
      await authenticationPrompt.webContents.executeJavaScript(`(() => {
        document.getElementById('username').value = 'fixture-user'
        document.getElementById('password').value = 'fixture-password'
        document.getElementById('credentials').requestSubmit()
      })()`)
      const authentication = await authenticationCreate
      if (!authentication.ok) {
        throw new Error(authentication.message || 'Authenticated URL preview failed.')
      }
      const authenticationContents = view('browser:authenticated').webContents
      const authenticationTitle = authenticationContents.getTitle()
      const authPromptClosedAfterSubmit = authenticationPrompt.isDestroyed()
      const windowsBeforeReload = BrowserWindow.getAllWindows().length
      const authReload = await manager.navigateSurface({
        version: 2,
        surfaceId: 'browser:authenticated',
        action: 'reload',
      })
      await waitFor(
        () => !authenticationContents.isLoading(),
        'authenticated preview reload',
      )
      const authStayedInItemMemory = BrowserWindow.getAllWindows().length === windowsBeforeReload
        && authenticationContents.getTitle() === 'Authenticated preview'
      await manager.destroySurface('browser:authenticated')

      const cancellationCreate = manager.createSurface({
        version: 2,
        surfaceId: 'browser:auth-cancel',
        kind: 'url-preview',
        payload: {
          url: `${fixture.loopbackOrigin}/protected`,
          scopeId: 'synthetic:auth-cancel',
        },
      })
      const cancellationPrompt = await waitFor(
        () => BrowserWindow.getAllWindows().find(window =>
          window !== owner
          && window.getParentWindow() === owner
          && window.getTitle() === 'Sign in to preview'),
        'second-item Basic Auth prompt',
      )
      await waitFor(
        () => !cancellationPrompt.webContents.isLoading(),
        'loaded second-item Basic Auth prompt',
      )
      const cancellationDestroy = manager.destroySurface('browser:auth-cancel')
      await waitFor(
        () => cancellationPrompt.isDestroyed(),
        'Basic Auth prompt cancellation on item close',
      )
      await Promise.all([cancellationCreate, cancellationDestroy])
      const authCancelledOnClose = !manager.surfaces.has('browser:auth-cancel')

      const timeoutManager = new Manager({
        authenticationTimeoutMs: 250,
        getWindow: () => owner,
        emit: () => {},
      })
      const timeoutCreate = timeoutManager.createSurface({
        version: 2,
        surfaceId: 'browser:auth-timeout',
        kind: 'url-preview',
        payload: {
          url: `${fixture.loopbackOrigin}/protected`,
          scopeId: 'synthetic:auth-timeout',
        },
      })
      const timeoutPrompt = await waitFor(
        () => BrowserWindow.getAllWindows().find(window =>
          window !== owner
          && window.getParentWindow() === owner
          && window.getTitle() === 'Sign in to preview'),
        'timeout Basic Auth prompt',
      )
      await waitFor(() => timeoutPrompt.isDestroyed(), 'Basic Auth prompt timeout')
      await timeoutCreate
      const authCancelledOnTimeout = timeoutPrompt.isDestroyed()
        && timeoutManager.surfaces.get('browser:auth-timeout')?.pendingAuthentication === null
      await timeoutManager.destroyAll()

      const permissionTimeoutEvents = []
      const permissionTimeoutManager = new Manager({
        getWindow: () => owner,
        emit: event => permissionTimeoutEvents.push(event),
        permissionTimeoutMs: 100,
      })
      const permissionTimeoutSurface = await permissionTimeoutManager.createSurface({
        version: 2,
        surfaceId: 'browser:permission-timeout',
        kind: 'url-preview',
        payload: {
          url: `${fixture.loopbackOrigin}/permission-timeout`,
          scopeId: 'synthetic:permission-timeout',
        },
      })
      if (!permissionTimeoutSurface.ok) throw new Error('Permission timeout surface failed.')
      const permissionTimeoutContents = permissionTimeoutManager.surfaces.get(
        'browser:permission-timeout',
      ).view.webContents
      const permissionTimeoutResult = await permissionTimeoutContents.executeJavaScript(
        `new Promise(resolve => navigator.geolocation.getCurrentPosition(
          () => resolve('allowed'),
          () => resolve('denied'),
        ))`,
      )
      const permissionTimedOut = permissionTimeoutResult === 'denied'
        && permissionTimeoutEvents.some(event => event.type === 'permission-request')
        && permissionTimeoutManager.surfaces.get(
          'browser:permission-timeout',
        ).pendingPermissions.size === 0
      await permissionTimeoutManager.destroyAll()

      const forcedEvents = []
      const forcedManager = new Manager({
        forceArtifactPreviewsOffline: true,
        getWindow: () => owner,
        emit: event => forcedEvents.push(event),
      })
      const forcedOffline = await forcedManager.createSurface({
        version: 2,
        surfaceId: 'artifact:forced-offline',
        kind: 'artifact-preview',
        payload: {
          launchUrl: `${fixture.previewOrigin}/index.html`,
          expectedOrigin: fixture.previewOrigin,
          scopeId: 'synthetic:forced-offline',
          mode: 'full',
        },
      })
      const forcedEffectiveMode = forcedManager.surfaces.get(
        'artifact:forced-offline',
      )?.mode
      await forcedManager.destroyAll()

      const reentrantOriginal = await manager.createSurface({
        version: 2,
        surfaceId: 'browser:terminal-reentry',
        kind: 'url-preview',
        payload: {
          url: `${fixture.loopbackOrigin}/terminal-original`,
          scopeId: 'synthetic:terminal-original',
        },
      })
      if (!reentrantOriginal.ok) throw new Error('Terminal re-entry surface failed.')
      const reentrantOriginalView = view('browser:terminal-reentry')
      const reentrantOriginalContents = reentrantOriginalView.webContents
      const reentrantOriginalUrl = reentrantOriginalContents.getURL()
      emitRendererGone(reentrantOriginalContents)
      await waitFor(
        () => reentrantReplacementPromise,
        'replacement requested from terminal event callback',
      )
      const reentrantReplacement = await reentrantReplacementPromise
      if (!reentrantReplacement.ok) {
        throw new Error(reentrantReplacement.message || 'Terminal re-entry replacement failed.')
      }
      const reentrantReplacementContents = view('browser:terminal-reentry').webContents
      await waitFor(
        () => reentrantOriginalContents.isDestroyed(),
        'terminal re-entry original renderer teardown',
      )
      emitRendererGone(reentrantOriginalContents)
      reentrantOriginalContents.emit(
        'did-fail-load',
        {},
        -2,
        'synthetic repeated terminal failure',
        reentrantOriginalUrl,
        true,
      )
      const reentrantTerminalEventCount = events.filter(event =>
        event.surfaceId === 'browser:terminal-reentry'
        && (event.type === 'error' || event.type === 'crashed' || event.type === 'unresponsive')
      ).length
      const reentrantOriginalDetached = !owner.contentView.children.includes(
        reentrantOriginalView,
      )
      const reentrantReplacementHealthy = (
        reentrantReplacementContents !== reentrantOriginalContents
        && !reentrantReplacementContents.isDestroyed()
        && reentrantReplacementContents.getURL().endsWith('/terminal-replacement')
      )
      await manager.destroySurface('browser:terminal-reentry')

      const capacityResults = []
      for (let index = 0; index < 9; index += 1) {
        capacityResults.push(await manager.createSurface({
          version: 2,
          surfaceId: `browser:capacity-${index}`,
          kind: 'url-preview',
          payload: {
            url: `${fixture.loopbackOrigin}/capacity-${index}`,
            scopeId: `synthetic:capacity-${index}`,
          },
        }))
      }
      const liveSurfaceCountAtLimit = manager.surfaces.size
      const failedCapacityRecord = manager.surfaces.get('browser:capacity-0')
      if (!failedCapacityRecord) throw new Error('Capacity surface was not retained.')
      const failedCapacityView = failedCapacityRecord.view
      const failedCapacityContents = failedCapacityView.webContents
      const failedCapacitySession = failedCapacityRecord.previewSession
      const failedCapacityUrl = failedCapacityContents.getURL()
      await failedCapacityContents.executeJavaScript(
        "localStorage.setItem('terminal-cleanup-probe', 'stored')",
      )
      emitUnresponsive(failedCapacityContents)
      await waitFor(
        () => failedCapacityContents.isDestroyed()
          && !manager.surfaces.has('browser:capacity-0'),
        'unresponsive capacity slot teardown',
      )
      emitUnresponsive(failedCapacityContents)
      emitRendererGone(failedCapacityContents)
      failedCapacityContents.emit(
        'did-fail-load',
        {},
        -2,
        'synthetic failure after unresponsive',
        failedCapacityUrl,
        true,
      )
      const capacityTerminalEventCount = events.filter(event =>
        event.surfaceId === 'browser:capacity-0'
        && (event.type === 'error' || event.type === 'crashed' || event.type === 'unresponsive')
      ).length
      const failedCapacityDetached = !owner.contentView.children.includes(failedCapacityView)
      const capacityReuse = await manager.createSurface({
        version: 2,
        surfaceId: 'browser:capacity-reused',
        kind: 'url-preview',
        payload: {
          url: `${fixture.loopbackOrigin}/capacity-reused`,
          scopeId: 'synthetic:capacity-reused',
        },
      })
      const liveSurfaceCountAfterReuse = manager.surfaces.size
      await manager.destroyAll()
      const storageProbe = new BrowserWindow({
        show: false,
        webPreferences: {
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
          session: failedCapacitySession,
        },
      })
      await storageProbe.loadURL(`${fixture.loopbackOrigin}/storage-cleanup-probe`)
      const failedSessionStorageCleared = await storageProbe.webContents.executeJavaScript(
        "localStorage.getItem('terminal-cleanup-probe') === null",
      )
      storageProbe.destroy()
      owner.destroy()

      return {
        fullProbes,
        fullReload,
        fullStorageSurvivedReload,
        fullSecurityPreferences,
        fullWebRtcType,
        fullDevToolsBlocked,
        fullAudioActive,
        hiddenAudioMuted,
        resumedAudioActive,
        fullRemote,
        remoteRequests,
        artifactGatewayAccess,
        artifactGatewayWarning,
        popupNull,
        privilegedNavigationBlocked,
        permissionResponse,
        permissionResult,
        fullNavigationEventCount: fullNavigationEvents.length,
        isolationState,
        offlineLocal,
        offlineNetworkWarning,
        offlinePolicyHeaders,
        offlineRemote,
        offlineRemoteRequests,
        offlineWebRtc,
        offlineWebRtcPolicy,
        artifactClientCertificateDenied,
        artifactCertificateWarning,
        privilegedBrowser,
        privilegedBrowserWarning,
        browserClientCertificateDenied,
        browserCertificateWarning,
        navigation,
        back,
        authentication,
        authenticationTitle,
        authPromptClosedAfterSubmit,
        authReload,
        authStayedInItemMemory,
        authCancelledOnClose,
        authCancelledOnTimeout,
        permissionTimedOut,
        forcedOffline,
        forcedEffectiveMode,
        reentrantOriginalDetached,
        reentrantReplacementHealthy,
        reentrantTerminalEventCount,
        capacityResults,
        capacityReuse,
        capacityTerminalEventCount,
        failedCapacityDetached,
        failedSessionStorageCleared,
        liveSurfaceCountAfterReuse,
        liveSurfaceCountAtLimit,
      }
    },
    {
      previewOrigin,
      loopbackOrigin,
      privilegedGatewayUrl,
      privilegedGatewayAlias,
      stunPort: stunAddress.port,
      turnTcpPort: turnTcpAddress.port,
    },
  )

  assert.equal(result.fullProbes.fetchProbe, true)
  assert.equal(result.fullProbes.workerProbe, 'probe-worker')
  assert.equal(result.fullProbes.animationProbe, true)
  assert.equal(result.fullProbes.wasmProbe, true)
  assert.equal(result.fullProbes.moduleProbe, 'module-loaded')
  assert.equal(result.fullProbes.storage, 'stored')
  assert.equal(result.fullProbes.node, 'undefined:undefined')
  assert.deepEqual(
    result.fullProbes.serviceWorkerProbe,
    {
      status: 'passed',
      echo: 'preview-probe',
      scope: `${previewOrigin}/`,
    },
    'a same-origin Service Worker must install, activate, and execute in the preview session',
  )
  assert.deepEqual(
    result.fullProbes.webSocketProbe,
    { status: 'passed', echo: 'echo:preview-probe' },
    'a same-origin WebSocket must complete a real handshake and message round-trip',
  )
  assert.equal(
    result.fullProbes.fontProbe.status,
    'passed',
    result.fullProbes.fontProbe.reason || 'the local @font-face asset must load',
  )
  assert.ok(result.fullProbes.fontProbe.count > 0, '@font-face must resolve a real FontFace')
  assert.match(result.fullProbes.fontProbe.family, /WorkbenchFixtureFont/)

  const temporalProbe = result.fullProbes.temporalProbe
  assert.equal(temporalProbe.status, 'passed')
  assert.match(temporalProbe.gsapVersion, /^\d+\.\d+\.\d+/)
  assert.match(temporalProbe.lottieVersion, /^\d+\.\d+\.\d+/)
  assert.equal(temporalProbe.samples.length, 3)
  const [firstAnimationSample, secondAnimationSample, thirdAnimationSample] =
    temporalProbe.samples
  assert.ok(
    firstAnimationSample.domX < secondAnimationSample.domX
      && secondAnimationSample.domX < thirdAnimationSample.domX,
    'real GSAP DOM transforms must advance across three observation points',
  )
  assert.ok(
    firstAnimationSample.canvasFrame < secondAnimationSample.canvasFrame
      && secondAnimationSample.canvasFrame < thirdAnimationSample.canvasFrame,
    'requestAnimationFrame Canvas drawing must advance across three observation points',
  )
  assert.notDeepEqual(
    firstAnimationSample.canvasPixel,
    secondAnimationSample.canvasPixel,
    'Canvas pixels must change between the first two animation samples',
  )
  assert.notDeepEqual(
    secondAnimationSample.canvasPixel,
    thirdAnimationSample.canvasPixel,
    'Canvas pixels must change between the final two animation samples',
  )
  assert.ok(
    firstAnimationSample.lottieFrame < thirdAnimationSample.lottieFrame,
    'the real lottie-web timeline must advance between animation samples',
  )

  const videoProbe = result.fullProbes.videoProbe
  assert.ok(
    videoProbe.status === 'passed' || videoProbe.status === 'skipped',
    videoProbe.reason || 'captureStream video probe returned an invalid status',
  )
  if (videoProbe.status === 'skipped') {
    assert.match(
      videoProbe.reason,
      /^captureStream (unavailable|playback unavailable)/,
      'video skips must identify the unavailable Chromium graphics/media capability',
    )
    console.warn(`video probe skipped: ${videoProbe.reason}`)
  } else {
    assert.equal(videoProbe.tagName, 'VIDEO')
    assert.equal(videoProbe.hasMediaStream, true)
    assert.ok(videoProbe.readyState >= 1)
    assert.ok(videoProbe.videoWidth > 0 && videoProbe.videoHeight > 0)
    assert.ok(videoProbe.drawnFrames > 1)
  }

  const webglProbe = result.fullProbes.webglProbe
  assert.ok(
    webglProbe.status === 'passed' || webglProbe.status === 'skipped',
    webglProbe.reason || 'WebGL probe returned an invalid status',
  )
  if (webglProbe.status === 'skipped') {
    assert.equal(
      webglProbe.reason,
      'WebGL context unavailable under current Electron graphics backend',
      'WebGL skips must be limited to a missing graphics backend',
    )
    console.warn(`WebGL probe skipped: ${webglProbe.reason}`)
  } else {
    assert.match(webglProbe.version, /WebGL/)
    assert.ok(webglProbe.pixel[0] >= 62 && webglProbe.pixel[0] <= 66)
    assert.ok(webglProbe.pixel[1] >= 126 && webglProbe.pixel[1] <= 130)
    assert.ok(webglProbe.pixel[2] >= 189 && webglProbe.pixel[2] <= 193)
    assert.equal(webglProbe.pixel[3], 255)
  }
  assert.ok(fixtureServiceWorkerRequests > 0, 'the synthetic Service Worker must be requested')
  assert.ok(fixtureWebSocketConnections > 0, 'the synthetic WebSocket server must be reached')
  assert.ok(fixtureFontRequests > 0, 'the synthetic font endpoint must be reached')
  assert.ok(fixtureGsapRequests > 0, 'the local GSAP distribution must be requested')
  assert.ok(fixtureLottieRequests > 0, 'the local lottie-web distribution must be requested')
  assert.equal(result.fullReload.ok, true, 'v2 items must support refresh')
  assert.equal(
    result.fullStorageSurvivedReload,
    true,
    'item storage must survive refresh until the item closes',
  )
  assert.deepEqual(
    result.fullSecurityPreferences,
    {
      contextIsolation: true,
      disableDialogs: false,
      nodeIntegration: false,
      preload: null,
      safeDialogs: true,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
    },
    'v2 must expose browser features without privileged Electron capabilities',
  )
  assert.equal(
    result.fullWebRtcType,
    'function',
    'full mode must retain normal Chromium WebRTC support',
  )
  assert.equal(result.fullDevToolsBlocked, true, 'v2 preview DevTools must stay unavailable')
  assert.equal(result.fullAudioActive, true, 'the visible active item must not be muted')
  assert.equal(result.hiddenAudioMuted, true, 'a hidden item must be muted')
  assert.equal(result.resumedAudioActive, true, 'resuming an item must restore its audio')
  assert.equal(result.fullRemote, true, 'full mode must execute active HTTPS resources')
  assert.equal(result.remoteRequests, 1, 'full mode HTTPS must reach Chromium networking')
  assert.equal(
    result.artifactGatewayAccess,
    'blocked',
    'artifact previews must not inherit ambient access to the Desktop-owned Gateway',
  )
  assert.equal(
    result.artifactGatewayWarning,
    true,
    'blocked Gateway access from an artifact must be visible to the Workbench',
  )
  assert.equal(result.popupNull, true, 'preview popups must not create unmanaged windows')
  assert.equal(result.privilegedNavigationBlocked, true, 'file navigation must stay blocked')
  assert.equal(result.permissionResponse.ok, true, 'a current permission request may be answered')
  assert.equal(result.permissionResult, 'denied', 'denied permission must reach web content')
  assert.ok(result.fullNavigationEventCount > 0, 'v2 surfaces must emit navigation state')
  assert.equal(
    result.isolationState.storageWasCleared,
    true,
    'new item sessions must not inherit storage',
  )
  assert.equal(
    result.isolationState.serviceWorkerRegistrations,
    0,
    'new item sessions must not inherit Service Worker registrations',
  )
  assert.equal(result.offlineLocal, true, 'offline mode must keep same-origin bundle fetch')
  assert.equal(result.offlineRemote, 'blocked', 'offline mode must block remote active resources')
  assert.equal(result.offlineRemoteRequests, 0, 'offline blocking must precede protocol dispatch')
  assert.match(
    result.offlinePolicyHeaders.csp,
    /webrtc 'block'/,
    'offline responses must apply CSP WebRTC blocking before page script runs',
  )
  assert.equal(
    result.offlinePolicyHeaders.dnsPrefetch,
    'off',
    'offline responses must disable speculative DNS prefetch',
  )
  assert.equal(result.offlineWebRtc.mainType, 'undefined')
  assert.equal(result.offlineWebRtc.srcdocType, 'undefined')
  assert.equal(result.offlineWebRtc.blobType, 'undefined')
  assert.equal(result.offlineWebRtc.workerType, 'undefined')
  assert.equal(
    result.offlineWebRtc.turnAttempt,
    'blocked',
    'offline preview WebRTC must be unavailable before page script runs',
  )
  assert.equal(
    result.offlineWebRtcPolicy,
    'disable_non_proxied_udp',
    'offline Electron contents must also suppress direct UDP as defense in depth',
  )
  assert.equal(fixtureStunPackets, 0, 'offline preview must not send STUN traffic')
  assert.equal(turnTcpConnections, 0, 'offline preview must not open a TURN/TCP connection')
  assert.equal(turnTcpBytes, 0, 'offline preview must not send TURN/TCP bytes')
  assert.equal(
    result.offlineNetworkWarning,
    true,
    'offline network blocking must be visible as a ready-with-warnings signal',
  )
  assert.equal(
    result.artifactClientCertificateDenied,
    true,
    'artifact previews must not select a certificate from the host store',
  )
  assert.equal(
    result.artifactCertificateWarning,
    true,
    'artifact certificate rejection must be visible to the Workbench',
  )
  assert.equal(
    result.browserClientCertificateDenied,
    true,
    'URL previews must not select a certificate from the host store',
  )
  assert.equal(
    result.browserCertificateWarning,
    true,
    'URL preview certificate rejection must be visible to the Workbench',
  )
  assert.equal(
    result.privilegedBrowser.ok,
    false,
    'URL previews must not navigate into the privileged Desktop-owned Gateway',
  )
  assert.equal(
    result.privilegedBrowserWarning,
    true,
    'blocked Gateway navigation must be visible to the Workbench',
  )
  assert.equal(
    privilegedGatewayRequests,
    0,
    'Gateway isolation must run before any preview request reaches the service',
  )
  assert.equal(result.navigation.ok, true, 'URL surfaces must accept trusted address navigation')
  assert.equal(result.back.ok, true, 'URL surfaces must expose history navigation')
  assert.equal(result.authentication.ok, true, 'Basic Auth credentials must resume the item load')
  assert.equal(result.authenticationTitle, 'Authenticated preview')
  assert.equal(result.authPromptClosedAfterSubmit, true, 'credential UI must close after submit')
  assert.equal(result.authReload.ok, true, 'authenticated items must support reload')
  assert.equal(
    result.authStayedInItemMemory,
    true,
    'Basic Auth may remain cached only inside the current item session',
  )
  assert.equal(
    result.authCancelledOnClose,
    true,
    'closing an item must cancel its pending credential challenge',
  )
  assert.equal(
    result.authCancelledOnTimeout,
    true,
    'an unanswered Basic Auth challenge must cancel at its bounded timeout',
  )
  assert.equal(
    result.permissionTimedOut,
    true,
    'an unanswered device permission must deny and clear at its bounded timeout',
  )
  assert.equal(result.forcedOffline.ok, true, 'forced-offline artifacts must still load')
  assert.equal(
    result.forcedEffectiveMode,
    'offline',
    'the main process kill switch must override a renderer-requested full mode',
  )
  assert.equal(
    result.reentrantTerminalEventCount,
    1,
    'a renderer crash must emit one terminal event even when its callback replaces the item',
  )
  assert.equal(
    result.reentrantOriginalDetached,
    true,
    'terminal callback re-entry must observe the failed native child view already detached',
  )
  assert.equal(
    result.reentrantReplacementHealthy,
    true,
    'a terminal event callback may replace the item without the old teardown destroying it',
  )
  assert.equal(
    result.liveSurfaceCountAtLimit,
    8,
    'the manager must retain at most eight live surfaces',
  )
  assert.equal(result.capacityResults.slice(0, 8).every(entry => entry.ok), true)
  assert.equal(result.capacityResults[8].ok, false, 'the ninth live surface must be rejected')
  assert.equal(
    result.capacityTerminalEventCount,
    1,
    'an unresponsive renderer must emit only its explicit unresponsive terminal event',
  )
  assert.equal(
    result.failedCapacityDetached,
    true,
    'an unresponsive v2 item must detach its native view without a separate close request',
  )
  assert.equal(
    result.capacityReuse.ok,
    true,
    'an unresponsive v2 item must release its capacity slot immediately',
  )
  assert.equal(
    result.liveSurfaceCountAfterReuse,
    8,
    'a replacement may reuse the failed item slot without hidden eviction',
  )
  assert.equal(
    result.failedSessionStorageCleared,
    true,
    'destroyAll must await storage cleanup for failed records already removed from the live map',
  )

  console.log('native Workbench v2 real Electron smoke checks passed')
} finally {
  if (electronApp) await electronApp.close().catch(() => {})
  for (const client of webSocketServer.clients) client.terminate()
  await new Promise(resolveClose => webSocketServer.close(resolveClose))
  await new Promise(resolveClose => server.close(resolveClose))
  await new Promise(resolveClose => privilegedGatewayServer.close(resolveClose))
  await new Promise(resolveClose => stunSocket.close(resolveClose))
  for (const client of turnTcpClients) client.destroy()
  await new Promise(resolveClose => turnTcpSink.close(resolveClose))
  await rm(isolationRoot, { recursive: true, force: true })
}
