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
const workbenchE2eMode = process.env.OPENSQUILLA_WORKBENCH_E2E_MODE || 'stress'
if (!['smoke', 'stress'].includes(workbenchE2eMode)) {
  throw new Error(
    `OPENSQUILLA_WORKBENCH_E2E_MODE must be smoke or stress, got ${workbenchE2eMode}.`,
  )
}
const stressMode = workbenchE2eMode === 'stress'
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
        body { min-height: 1600px; }
        @font-face {
          font-family: "WorkbenchFixtureFont";
          src: url("/fixture-font.woff2") format("woff2");
          font-display: block;
          font-style: normal;
          font-weight: 400;
        }
        #font-probe { font-family: "WorkbenchFixtureFont", sans-serif; margin-top: 240px; }
        #gsap-probe { width: 20px; height: 20px; background: rgb(20, 80, 200); }
        #lottie-probe { width: 100px; height: 100px; }
      </style>
      <div id="font-probe">Synthetic font preview</div>
      <div id="gsap-probe"></div>
      <div id="lottie-probe"></div>
      <canvas id="canvas-probe" width="8" height="8"></canvas>
      <video id="video-probe" autoplay muted playsinline></video>
      <main data-label="😀"><svg viewBox="0 0 10 10"><path aria-label="星😀" d="M0 0"></path></svg></main>
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
        window.__lottieReady = lottieReady.then(() => true)

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
    async ({ app, BrowserWindow, webContents }, fixture) => {
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
      const candidateReleaseHandles = []
      const previewPinReleases = []
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
        resolveCandidatePreview: async candidateHandle => ({
          candidateHandle,
          candidateArtifactId: `art-${candidateHandle}`,
          leaseId: `apl-${candidateHandle}`,
          launchUrl: `${fixture.previewOrigin}/binding-candidate`,
          expectedOrigin: fixture.previewOrigin,
          scopeId: candidateHandle.includes('binding_a')
            ? 'synthetic:v4-binding-a'
            : 'synthetic:v4-binding-b',
          mode: 'offline',
        }),
        releaseCandidatePreview: async candidateHandle => {
          candidateReleaseHandles.push(candidateHandle)
        },
        pinArtifactPreview: grant => {
          let released = false
          const currentGrant = { ...grant }
          return {
            currentGrant: () => ({ ...currentGrant }),
            ensureCurrent: async () => released ? null : { ...currentGrant },
            release: async () => {
              if (released) return
              released = true
              const surfaceId = currentGrant.scopeId.endsWith('-a')
                ? 'artifact:v4-binding-a'
                : 'artifact:v4-binding-b'
              previewPinReleases.push({
                scopeId: currentGrant.scopeId,
                surfacePresent: manager.surfaces.has(surfaceId),
              })
            },
          }
        },
      })

      async function waitFor(check, label, timeoutMs = 10_000, diagnose = null) {
        const deadline = Date.now() + timeoutMs
        while (Date.now() < deadline) {
          const value = await check()
          if (value) return value
          await new Promise(resolveWait => setTimeout(resolveWait, 25))
        }
        let diagnostic = null
        if (diagnose) {
          try {
            diagnostic = await diagnose()
          } catch (error) {
            diagnostic = { diagnosticError: error?.message || String(error) }
          }
        }
        const suffix = diagnostic === null ? '' : ` Diagnostics: ${JSON.stringify(diagnostic)}`
        throw new Error(`Timed out waiting for ${label}.${suffix}`)
      }

      async function trustedAnnotationInputState(overlay) {
        const contents = overlay?.view?.webContents
        const nativeState = {
          ownerFocused: owner.isFocused(),
          ownerVisible: owner.isVisible(),
          ownerMinimized: owner.isMinimized(),
          nativeFocused: Boolean(contents && !contents.isDestroyed() && contents.isFocused()),
          webContentsDestroyed: Boolean(!contents || contents.isDestroyed()),
          overlayVisible: Boolean(overlay?.view?.getVisible()),
        }
        if (!contents || contents.isDestroyed()) return nativeState
        const rendererState = await contents.executeJavaScript(`(() => {
          const textarea = document.getElementById('annotation-body')
          return {
            activeElement: document.activeElement?.id || document.activeElement?.tagName || null,
            documentFocused: document.hasFocus(),
            editorFocused: document.activeElement === textarea,
            selectionStart: textarea?.selectionStart ?? null,
            selectionEnd: textarea?.selectionEnd ?? null,
            value: textarea?.value ?? null,
            inputProbe: window.__opensquillaNativeInputProbe ?? null,
          }
        })()`)
        return { ...nativeState, ...rendererState }
      }

      function trustedAnnotationFocusReady(state) {
        return Boolean(
          state.ownerFocused
          && state.nativeFocused
          && state.documentFocused
          && state.editorFocused,
        )
      }

      async function restoreTrustedAnnotationInputFocus(overlay, label) {
        const recover = async () => {
          const contents = overlay?.view?.webContents
          if (!contents || contents.isDestroyed()) {
            throw new Error(`TRUSTED_OVERLAY_FOCUS_CONTRACT_FAILED: ${label}: renderer is gone.`)
          }
          if (process.platform === 'darwin') app.focus({ steal: true })
          if (owner.isMinimized()) owner.restore()
          owner.show()
          owner.focus()
          contents.focus()
          await contents.executeJavaScript(`(() => {
            const textarea = document.getElementById('annotation-body')
            if (!textarea) throw new Error('annotation textarea is missing')
            textarea.focus({ preventScroll: true })
          })()`, true)
        }

        try {
          await recover()
          const state = await waitFor(async () => {
            const candidate = await trustedAnnotationInputState(overlay)
            if (trustedAnnotationFocusReady(candidate)) return candidate
            await recover()
            return null
          }, label, 10_000, () => trustedAnnotationInputState(overlay))
          annotationNativeOwnerFocusAvailable = true
          return state
        } catch (error) {
          const state = await trustedAnnotationInputState(overlay).catch(diagnosticError => ({
            diagnosticError: diagnosticError?.message || String(diagnosticError),
          }))
          const classification = state.ownerFocused
            ? 'TRUSTED_OVERLAY_FOCUS_CONTRACT_FAILED'
            : 'ELECTRON_FOREGROUND_PREREQUISITE_MISSING'
          throw new Error(
            `${classification}: ${label}. ${error?.message || error} `
            + `Final state: ${JSON.stringify(state)}`,
          )
        }
      }

      async function requireRetainedTrustedAnnotationFocus(overlay, label) {
        try {
          return await waitFor(async () => {
            const state = await trustedAnnotationInputState(overlay)
            return trustedAnnotationFocusReady(state) ? state : null
          }, label, 10_000, () => trustedAnnotationInputState(overlay))
        } catch (error) {
          const state = await trustedAnnotationInputState(overlay).catch(diagnosticError => ({
            diagnosticError: diagnosticError?.message || String(diagnosticError),
          }))
          const classification = state.ownerFocused
            ? 'TRUSTED_OVERLAY_FOCUS_CONTRACT_FAILED'
            : 'ELECTRON_FOREGROUND_PREREQUISITE_MISSING'
          throw new Error(`${classification}: ${error?.message || error}`)
        }
      }

      async function armTrustedAnnotationInputProbe(overlay, label) {
        const token = `${label}:${Date.now()}:${Math.random()}`
        const contents = overlay.view.webContents
        return await contents.executeJavaScript(`(() => {
          const textarea = document.getElementById('annotation-body')
          if (!textarea) throw new Error('annotation textarea is missing')
          if (!window.__opensquillaNativeInputProbeInstalled) {
            const observe = (event) => {
              if (event.target?.id !== 'annotation-body') return
              const probe = window.__opensquillaNativeInputProbe
              if (!probe) return
              const entry = {
                data: typeof event.data === 'string' ? event.data : null,
                inputType: event.inputType || null,
                isComposing: Boolean(event.isComposing),
                value: event.target.value,
              }
              probe[event.type].push(entry)
            }
            document.addEventListener('beforeinput', observe, true)
            document.addEventListener('input', observe, true)
            window.__opensquillaNativeInputProbeInstalled = true
          }
          window.__opensquillaNativeInputProbe = {
            token: ${JSON.stringify(token)},
            initialValue: textarea.value,
            beforeinput: [],
            input: [],
          }
          return window.__opensquillaNativeInputProbe
        })()`)
      }

      async function waitForTrustedAnnotationInput(overlay, probe, expectedValue, label) {
        let observedState
        try {
          observedState = await waitFor(async () => {
            const state = await trustedAnnotationInputState(overlay)
            const observed = state.inputProbe
            return (
              observed?.token === probe.token
              && observed.input.length > 0
              && state.value === expectedValue
            ) ? state : null
          }, label, 10_000, () => trustedAnnotationInputState(overlay))
        } catch (error) {
          const state = await trustedAnnotationInputState(overlay).catch(diagnosticError => ({
            diagnosticError: diagnosticError?.message || String(diagnosticError),
          }))
          const classification = !state.ownerFocused
            ? 'ELECTRON_FOREGROUND_PREREQUISITE_MISSING'
            : 'TRUSTED_OVERLAY_INPUT_CONTRACT_FAILED'
          throw new Error(`${classification}: ${error?.message || error}`)
        }
        // The renderer event proves the real input path ran. Re-establish the
        // native foreground precondition before the caller proceeds so an
        // unrelated host focus steal cannot poison a later assertion.
        const focusedState = await restoreTrustedAnnotationInputFocus(
          overlay,
          `${label} post-input focus`,
        )
        return {
          ...focusedState,
          inputProbe: observedState.inputProbe,
        }
      }

      async function sendTrustedAnnotationCharacter(overlay, character, expectedValue, label) {
        await restoreTrustedAnnotationInputFocus(overlay, `${label} focus`)
        const probe = await armTrustedAnnotationInputProbe(overlay, label)
        overlay.view.webContents.sendInputEvent({
          type: 'char',
          keyCode: character,
        })
        return await waitForTrustedAnnotationInput(overlay, probe, expectedValue, label)
      }

      async function insertTrustedAnnotationText(overlay, text, expectedValue, label) {
        await restoreTrustedAnnotationInputFocus(overlay, `${label} focus`)
        const probe = await armTrustedAnnotationInputProbe(overlay, label)
        // insertText uses Chromium's text-input/IME commit path rather than
        // changing the DOM value directly.
        overlay.view.webContents.insertText(text)
        return await waitForTrustedAnnotationInput(overlay, probe, expectedValue, label)
      }

      async function closeAnnotationOverlayAndDrain(request, label) {
        const record = manager.surfaces.get(request.surfaceId)
        const candidate = record?.annotationCandidate ?? null
        const result = manager.closeArtifactAnnotationOverlay(request)
        if (record && candidate) {
          // Closing stops the interval synchronously, but a geometry CDP call
          // that already started may still finish through its stale-selection
          // cleanup. Do not let that cleanup cancel the next picker rearm.
          await waitFor(async () => {
            if (candidate.geometryRefreshPending) return false
            await record.cdpQueue
            return !candidate.geometryRefreshPending && record.annotationCandidate === null
          }, `${label} geometry cleanup`)
        }
        return result
      }

      // WebContents.isFocused() is only meaningful while the Electron app owns
      // native foreground focus. Make that lifecycle precondition explicit so
      // the trusted child-view contract is not coupled to whichever host app
      // happened to be frontmost when the smoke process launched.
      if (process.platform === 'darwin') app.focus({ steal: true })
      owner.show()
      owner.focus()
      await new Promise(resolveWait => setTimeout(resolveWait, 100))
      let annotationNativeOwnerFocusAvailable = owner.isFocused()

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
      const v2ArtifactBridgeUnavailable = manager.getActiveArtifactBridgeTarget() === null

      await manager.destroySurface('artifact:v2-full')
      await waitFor(() => fullContents.isDestroyed(), 'full preview destruction')

      const activePreviewArtifactId = 'art-synthetic-v3-bridge'
      const v3 = await manager.createSurface({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        kind: 'artifact-preview',
        payload: {
          launchUrl: `${fixture.previewOrigin}/index.html`,
          expectedOrigin: fixture.previewOrigin,
          scopeId: 'synthetic:v3-bridge',
          mode: 'full',
        },
      }, activePreviewArtifactId)
      if (!v3.ok) throw new Error(v3.message || 'Protocol-v3 preview failed to load.')
      const v3View = view('artifact:v3-bridge')
      const v3Contents = v3View.webContents
      manager.setSurfaceRect({
        surfaceId: 'artifact:v3-bridge',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: true,
      })
      await waitFor(() => v3View.getVisible(), 'visible protocol-v3 surface')
      const v3BridgeTarget = manager.getActiveArtifactBridgeTarget()
      if (!v3BridgeTarget) throw new Error('Protocol-v3 bridge target was unavailable.')
      const v3BridgeCapabilities = v3BridgeTarget.capabilities
      const v3AnnotationCapabilities = await manager.getArtifactAnnotationCapabilities()
      // This fixture intentionally loads a DOM-rendered animation library for
      // broader preview coverage. Wait for its one-time SVG construction so
      // the target geometry is stable before entering inspect mode.
      await v3Contents.executeJavaScript('window.__lottieReady')
      const annotationUnrelatedNodeCount = await v3Contents.executeJavaScript(`(() => {
        const branch = document.createElement('section')
        branch.id = 'runtime-only-large-branch'
        branch.hidden = true
        const fragment = document.createDocumentFragment()
        for (let index = 0; index < 50010; index += 1) {
          fragment.append(document.createElement('span'))
        }
        branch.append(fragment)
        document.body.append(branch)
        return branch.childElementCount
      })()`)
      await v3Contents.executeJavaScript(`(() => {
        window.__annotationPageClicks = 0
        window.__annotationPreviewKeys = 0
        document.getElementById('font-probe').addEventListener(
          'click',
          () => { window.__annotationPageClicks += 1 },
        )
        document.addEventListener('keydown', () => { window.__annotationPreviewKeys += 1 })
      })()`)
      const annotationTargetRect = await v3Contents.executeJavaScript(`(() => {
        const rect = document.getElementById('font-probe').getBoundingClientRect()
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
      })()`)
      const annotationPicker = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: true,
      })
      const annotationX = Math.floor(
        annotationTargetRect.x + Math.max(1, annotationTargetRect.width / 2),
      )
      const annotationY = Math.floor(
        annotationTargetRect.y + Math.max(1, annotationTargetRect.height / 2),
      )
      v3Contents.focus()
      await v3Contents.debugger.sendCommand('Input.dispatchMouseEvent', {
        type: 'mouseMoved',
        x: annotationX,
        y: annotationY,
        button: 'none',
      })
      await v3Contents.debugger.sendCommand('Input.dispatchMouseEvent', {
        type: 'mousePressed',
        x: annotationX,
        y: annotationY,
        button: 'left',
        clickCount: 1,
      })
      await v3Contents.debugger.sendCommand('Input.dispatchMouseEvent', {
        type: 'mouseReleased',
        x: annotationX,
        y: annotationY,
        button: 'left',
        clickCount: 1,
      })
      const annotationSelectedEvent = await waitFor(
        () => events.find(event =>
          event.surfaceId === 'artifact:v3-bridge'
          && (
            event.type === 'annotation-selected'
            || (
              event.type === 'blocked-action'
              && event.detail?.action === 'annotation-picker'
            )
          )),
        'protocol-v3 DOM annotation selection or rejection',
      )
      if (annotationSelectedEvent.type !== 'annotation-selected') {
        throw new Error(`DOM annotation selection was rejected: ${JSON.stringify(annotationSelectedEvent)}`)
      }
      const selected = annotationSelectedEvent.detail.selection
      const annotationPageClicks = await v3Contents.executeJavaScript(
        'Number(window.__annotationPageClicks || 0)',
      )
      // A runtime-only change outside the selected element's ancestor chain
      // must not invalidate its source-backed authorization proof.
      await v3Contents.executeJavaScript(
        "document.getElementById('lottie-probe').setAttribute('data-runtime-state', 'ready')",
      )
      const resolvedSelection = await v3BridgeTarget.resolveAnnotationSelection(
        {
          version: 3,
          activePreviewArtifactId,
          selectionId: selected.selectionId,
          tagName: selected.tagName,
          elementPath: selected.elementPath,
          elementProofSha256: selected.elementProofSha256,
        },
        new AbortController().signal,
      )
      const annotationWrongArtifactResolveRejected =
        await v3BridgeTarget.resolveAnnotationSelection(
          {
            version: 3,
            activePreviewArtifactId: 'art-synthetic-other-preview',
            selectionId: selected.selectionId,
            tagName: selected.tagName,
            elementPath: selected.elementPath,
            elementProofSha256: selected.elementProofSha256,
          },
          new AbortController().signal,
        ).then(() => false, () => true)
      const annotationOverlayResult = await manager.showArtifactAnnotationOverlay({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        selectionId: selected.selectionId,
        annotationId: 'annotation_electron_fixture',
        initialBody: 'Initial annotation',
        overlayCopyVersion: 1,
        copy: {
          targetLabel: '区域：示例',
          contextLabel: '当前选区',
          bodyLabel: '页面批注',
          placeholder: '描述希望进行的修改…',
          newlineHint: process.platform === 'darwin' ? '⇧ Return 换行' : 'Shift + Enter 换行',
          cancelLabel: '取消',
          submitLabel: '添加批注',
          emptyBodyMessage: '请描述希望的修改。',
        },
      })
      const annotationOverlay = manager.annotationOverlays.get(owner)
      if (!annotationOverlay) throw new Error('Trusted annotation overlay was not created.')
      await annotationOverlay.ready
      await waitFor(() => annotationOverlay.view.getVisible(), 'trusted annotation overlay')
      const overlayPreferences = annotationOverlay.view.webContents.getLastWebPreferences()
      const annotationOverlaySecurity = {
        contextIsolation: overlayPreferences.contextIsolation,
        nodeIntegration: overlayPreferences.nodeIntegration,
        sandbox: overlayPreferences.sandbox,
        webSecurity: overlayPreferences.webSecurity,
        webviewTag: overlayPreferences.webviewTag,
      }
      const annotationOverlayVisualStructure =
        await annotationOverlay.view.webContents.executeJavaScript(`(() => {
          const form = document.getElementById('annotation-form')
          const textarea = document.getElementById('annotation-body')
          const target = document.getElementById('annotation-target')
          const newlineHint = document.getElementById('annotation-newline-hint')
          const cancel = document.getElementById('annotation-cancel')
          const submit = document.getElementById('annotation-submit')
          const cardStyle = getComputedStyle(form)
          const textareaStyle = getComputedStyle(textarea)
          const submitStyle = getComputedStyle(submit)
          return {
            role: form.getAttribute('role'),
            ariaModal: form.getAttribute('aria-modal'),
            labelledBy: form.getAttribute('aria-labelledby'),
            textareaLabel: textarea.getAttribute('aria-label'),
            targetText: target.textContent,
            newlineHint: newlineHint.textContent,
            initialBody: textarea.value,
            submitDisabled: submit.disabled,
            cardRadius: cardStyle.borderRadius,
            textareaHeight: textareaStyle.height,
            submitHeight: submitStyle.height,
            tabOrder: [textarea.id, cancel.id, submit.id],
          }
        })()`)
      annotationOverlay.view.webContents.openDevTools({ mode: 'detach', activate: false })
      await new Promise(resolveWait => setTimeout(resolveWait, 100))
      const annotationOverlayDevToolsBlocked =
        !annotationOverlay.view.webContents.isDevToolsOpened()
      // sendInputEvent does not activate a WebContentsView the way a native
      // OS pointer event does. Restore the trusted view after the deliberately
      // blocked DevTools request before exercising click and geometry focus.
      await restoreTrustedAnnotationInputFocus(
        annotationOverlay,
        'trusted annotation textarea focus after blocked DevTools request',
      )
      annotationOverlay.view.webContents.sendInputEvent({
        type: 'mouseDown',
        x: 24,
        y: 64,
        button: 'left',
        clickCount: 1,
      })
      annotationOverlay.view.webContents.sendInputEvent({
        type: 'mouseUp',
        x: 24,
        y: 64,
        button: 'left',
        clickCount: 1,
      })
      await waitFor(
        async () => await annotationOverlay.view.webContents.executeJavaScript(
          "document.activeElement?.id === 'annotation-body'",
        ),
        'trusted annotation textarea focus after blocked DevTools request',
      )
      const annotationOverlayBounds = annotationOverlay.view.getBounds()
      const annotationOverlayOnTop = owner.contentView.children.at(-1) === annotationOverlay.view
      const annotationOverlayFocusCycles = []
      let previousOverlayBounds = annotationOverlayBounds
      const annotationGeometryDeltas = fixture.stressMode
        ? [96, -24, 24, -32, 32]
        : [96]
      for (const [index, scrollDelta] of annotationGeometryDeltas.entries()) {
        await restoreTrustedAnnotationInputFocus(
          annotationOverlay,
          `trusted annotation focus before geometry refresh ${index + 1}`,
        )
        await v3Contents.executeJavaScript(`window.scrollBy(0, ${scrollDelta})`)
        const movedBounds = await waitFor(() => {
          const bounds = annotationOverlay.view.getBounds()
          return bounds.y !== previousOverlayBounds.y ? bounds : null
        }, `trusted annotation overlay geometry refresh ${index + 1}`)
        // The first cycle proves geometry refresh itself retains focus. Later
        // stress cycles actively recover foreground ownership, which keeps the
        // repetition useful without coupling it to unrelated host focus steals.
        const focusState = index === 0
          ? await requireRetainedTrustedAnnotationFocus(
            annotationOverlay,
            'trusted annotation native focus after geometry refresh 1',
          )
          : await restoreTrustedAnnotationInputFocus(
            annotationOverlay,
            `trusted annotation native focus after geometry refresh ${index + 1}`,
          )
        annotationOverlayFocusCycles.push({
          bounds: movedBounds,
          ...focusState,
        })
        previousOverlayBounds = movedBounds
      }
      const annotationOverlayMovedBounds = annotationOverlayFocusCycles[0].bounds
      const annotationOverlayFocusedAfterGeometry =
        annotationOverlayFocusCycles.every(cycle =>
          cycle.editorFocused && cycle.ownerFocused && cycle.nativeFocused)
      await restoreTrustedAnnotationInputFocus(
        annotationOverlay,
        'trusted annotation selection before native keyboard input',
      )
      await annotationOverlay.view.webContents.executeJavaScript(
        "document.getElementById('annotation-body').select()",
      )
      const annotationInputHandshakes = []
      let expectedAnnotationValue = ''
      for (const [index, character] of [...'ASCII annotation '].entries()) {
        expectedAnnotationValue += character
        const state = await sendTrustedAnnotationCharacter(
          annotationOverlay,
          character,
          expectedAnnotationValue,
          `trusted overlay native character ${index + 1}`,
        )
        annotationInputHandshakes.push({
          kind: 'char',
          beforeinputCount: state.inputProbe.beforeinput.length,
          inputCount: state.inputProbe.input.length,
          value: state.value,
        })
      }
      expectedAnnotationValue += '中文输入'
      const annotationImeState = await insertTrustedAnnotationText(
        annotationOverlay,
        '中文输入',
        expectedAnnotationValue,
        'trusted overlay real keyboard and IME input',
      )
      annotationInputHandshakes.push({
        kind: 'ime',
        beforeinputCount: annotationImeState.inputProbe.beforeinput.length,
        inputCount: annotationImeState.inputProbe.input.length,
        value: annotationImeState.value,
      })
      const annotationOverlayTypedValue =
        await annotationOverlay.view.webContents.executeJavaScript(
          "document.getElementById('annotation-body').value",
        )
      const annotationPreviewKeys = await v3Contents.executeJavaScript(
        'Number(window.__annotationPreviewKeys || 0)',
      )
      const annotationSubmitEventsBeforeEmpty = events.filter(event =>
        event.type === 'annotation-submit'
        && event.detail?.annotationId === 'annotation_electron_fixture').length
      await annotationOverlay.view.webContents.executeJavaScript(`(() => {
        const textarea = document.getElementById('annotation-body')
        textarea.value = '   '
        textarea.dispatchEvent(new Event('input', { bubbles: true }))
        document.querySelector('button[type=submit]').click()
      })()`)
      await new Promise(resolveWait => setTimeout(resolveWait, 100))
      const annotationEmptySubmitRetained =
        annotationOverlay.view.getVisible()
        && annotationOverlay.binding?.annotationId === 'annotation_electron_fixture'
        && manager.surfaces.get('artifact:v3-bridge')?.annotationCandidate?.selection.selectionId
          === selected.selectionId
        && events.filter(event =>
          event.type === 'annotation-submit'
          && event.detail?.annotationId === 'annotation_electron_fixture').length
          === annotationSubmitEventsBeforeEmpty
      await annotationOverlay.view.webContents.executeJavaScript(`(() => {
        const textarea = document.getElementById('annotation-body')
        textarea.value = 'Make the heading concise.'
        textarea.dispatchEvent(new Event('input', { bubbles: true }))
      })()`)
      await waitFor(
        () => events.some(event =>
          event.type === 'annotation-draft-change'
          && event.detail?.annotationId === 'annotation_electron_fixture'
          && event.detail?.body === 'Make the heading concise.'),
        'trusted overlay draft relay',
      )
      await annotationOverlay.view.webContents.executeJavaScript(
        "document.querySelector('button[type=submit]').click()",
      )
      await waitFor(
        () => events.some(event =>
          event.type === 'annotation-submit'
          && event.detail?.annotationId === 'annotation_electron_fixture'
          && event.detail?.body === 'Make the heading concise.'),
        'trusted overlay submit relay',
      )
      const annotationOverlayRetainedAfterSubmit =
        annotationOverlay.view.getVisible()
        && annotationOverlay.binding?.annotationId === 'annotation_electron_fixture'
        && manager.surfaces.get('artifact:v3-bridge')?.annotationCandidate?.selection.selectionId
          === selected.selectionId
      await annotationOverlay.view.webContents.executeJavaScript(
        "document.getElementById('annotation-cancel').click()",
      )
      await waitFor(
        () => events.some(event =>
          event.type === 'annotation-cancel'
          && event.detail?.annotationId === 'annotation_electron_fixture'),
        'trusted overlay cancel relay',
      )
      const annotationOverlayRetainedAfterCancel =
        annotationOverlay.view.getVisible()
        && annotationOverlay.binding?.annotationId === 'annotation_electron_fixture'
        && manager.surfaces.get('artifact:v3-bridge')?.annotationCandidate?.selection.selectionId
          === selected.selectionId
      const annotationWrongAcknowledgement = manager.closeArtifactAnnotationOverlay({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        annotationId: 'annotation_wrong_fixture',
      })
      const annotationOverlayRetainedAfterWrongAcknowledgement =
        annotationOverlay.view.getVisible()
        && annotationOverlay.binding?.annotationId === 'annotation_electron_fixture'
      // Simulate an IME composition interrupted by the editor being fenced.
      // The reused trusted view must reset this state on its next init.
      await annotationOverlay.view.webContents.executeJavaScript(`(() => {
        const textarea = document.getElementById('annotation-body')
        textarea.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
      })()`)
      const annotationOverlayAcknowledgement = await closeAnnotationOverlayAndDrain({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        annotationId: 'annotation_electron_fixture',
      }, 'trusted annotation acknowledgement')
      const annotationOverlayClosedAfterAcknowledgement =
        !annotationOverlay.view.getVisible()
        && annotationOverlay.binding === null
        && manager.surfaces.get('artifact:v3-bridge')?.annotationCandidate === null
      const annotationRearmEventsBefore = events.length
      const annotationPickerRearm = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: true,
      })
      const annotationRearmDocument = await v3Contents.debugger.sendCommand('DOM.getDocument', {
        depth: -1,
        pierce: false,
      })
      const annotationRearmNode = await v3Contents.debugger.sendCommand('DOM.querySelector', {
        nodeId: annotationRearmDocument.root.nodeId,
        selector: '#font-probe',
      })
      const annotationRearmDescription = await v3Contents.debugger.sendCommand(
        'DOM.describeNode',
        { nodeId: annotationRearmNode.nodeId },
      )
      await manager.handleAnnotationNodeSelected(
        manager.surfaces.get('artifact:v3-bridge'),
        annotationRearmDescription.node.backendNodeId,
      )
      const annotationRearmSelectionEvent = await waitFor(
        () => events.slice(annotationRearmEventsBefore).find(event =>
          event.surfaceId === 'artifact:v3-bridge'
          && event.type === 'annotation-selected'),
        'rearmed annotation selection',
      )
      const annotationRearmSelection = annotationRearmSelectionEvent.detail.selection
      const annotationRearmOverlayResult = await manager.showArtifactAnnotationOverlay({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        selectionId: annotationRearmSelection.selectionId,
        annotationId: 'annotation_rearmed_fixture',
        initialBody: '',
        overlayCopyVersion: 1,
        copy: {
          targetLabel: 'Button: Rearmed target',
          contextLabel: 'Selected area',
          bodyLabel: 'Page annotation',
          placeholder: 'Describe the next change…',
          newlineHint: process.platform === 'darwin'
            ? '⇧ Return for a new line'
            : 'Shift + Enter for a new line',
          cancelLabel: 'Cancel',
          submitLabel: 'Add annotation',
          emptyBodyMessage: 'Describe the next requested change.',
        },
      })
      await restoreTrustedAnnotationInputFocus(
        annotationOverlay,
        'rearmed annotation textarea focus',
      )
      const annotationRearmCopy =
        await annotationOverlay.view.webContents.executeJavaScript(`(() => ({
          target: document.getElementById('annotation-target').textContent,
          placeholder: document.getElementById('annotation-body').placeholder,
          newlineHint: document.getElementById('annotation-newline-hint').textContent,
          newlineHintTitle: document.getElementById('annotation-newline-hint').title,
          submitTitle: document.getElementById('annotation-submit').title,
        }))()`)
      const annotationRearmLayout =
        await annotationOverlay.view.webContents.executeJavaScript(`(() => {
          const footer = document.querySelector('footer')
          const hint = document.getElementById('annotation-newline-hint')
          const submit = document.getElementById('annotation-submit')
          const submitText = submit.firstChild
          const range = document.createRange()
          range.selectNodeContents(submitText)
          const footerRect = footer.getBoundingClientRect()
          const submitRect = submit.getBoundingClientRect()
          return {
            hintOverflow: getComputedStyle(hint).overflow,
            hintTextOverflow: getComputedStyle(hint).textOverflow,
            submitWhiteSpace: getComputedStyle(submit).whiteSpace,
            submitTextLineCount: range.getClientRects().length,
            submitContained: submitRect.top >= footerRect.top
              && submitRect.right <= footerRect.right
              && submitRect.bottom <= footerRect.bottom,
          }
        })()`)
      const annotationRearmEmptyBodyMessage =
        await annotationOverlay.view.webContents.executeJavaScript(`(() => {
          const form = document.getElementById('annotation-form')
          const textarea = document.getElementById('annotation-body')
          form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
          return textarea.validationMessage
        })()`)
      const annotationRearmInputState = await insertTrustedAnnotationText(
        annotationOverlay,
        'Rearmed input',
        'Rearmed input',
        'rearmed annotation overlay input',
      )
      const annotationRearmTypedValue = annotationRearmInputState.value
      annotationInputHandshakes.push({
        kind: 'rearm-ime',
        beforeinputCount: annotationRearmInputState.inputProbe.beforeinput.length,
        inputCount: annotationRearmInputState.inputProbe.input.length,
        value: annotationRearmInputState.value,
      })
      const annotationRearmSubmitEventsBeforeNewline = events.filter(event =>
        event.type === 'annotation-submit'
        && event.detail?.annotationId === 'annotation_rearmed_fixture').length
      const annotationRearmShiftEnter =
        await annotationOverlay.view.webContents.executeJavaScript(`(() => {
          const textarea = document.getElementById('annotation-body')
          const event = new KeyboardEvent('keydown', {
            key: 'Enter',
            shiftKey: true,
            bubbles: true,
            cancelable: true,
          })
          const defaultAllowed = textarea.dispatchEvent(event)
          if (defaultAllowed) {
            textarea.setRangeText('\\n', textarea.selectionStart, textarea.selectionEnd, 'end')
            textarea.dispatchEvent(new InputEvent('input', {
              bubbles: true,
              data: '\\n',
              inputType: 'insertLineBreak',
            }))
          }
          return { defaultAllowed, value: textarea.value }
        })()`)
      await new Promise(resolveWait => setTimeout(resolveWait, 50))
      const annotationRearmShiftEnterDidNotSubmit = events.filter(event =>
        event.type === 'annotation-submit'
        && event.detail?.annotationId === 'annotation_rearmed_fixture').length
        === annotationRearmSubmitEventsBeforeNewline
      await annotationOverlay.view.webContents.executeJavaScript(`(() => {
        const textarea = document.getElementById('annotation-body')
        textarea.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter',
          bubbles: true,
          cancelable: true,
        }))
      })()`)
      const annotationRearmSubmitAfterInterruptedComposition = await waitFor(
        () => events.some(event =>
          event.type === 'annotation-submit'
          && event.detail?.annotationId === 'annotation_rearmed_fixture'
          && event.detail?.body === 'Rearmed input\n'),
        'reused trusted overlay IME state reset',
      )
      const annotationRearmOverlayClose = await closeAnnotationOverlayAndDrain({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        annotationId: 'annotation_rearmed_fixture',
      }, 'rearmed annotation acknowledgement')
      const annotationRearmFocusCycles = []
      const annotationRearmCycleCount = fixture.stressMode ? 3 : 1
      for (let cycle = 0; cycle < annotationRearmCycleCount; cycle += 1) {
        const cycleEventsBefore = events.length
        const cyclePicker = await manager.setArtifactAnnotationMode({
          version: 3,
          surfaceId: 'artifact:v3-bridge',
          enabled: true,
        })
        const cycleDocument = await v3Contents.debugger.sendCommand('DOM.getDocument', {
          depth: -1,
          pierce: false,
        })
        const cycleNode = await v3Contents.debugger.sendCommand('DOM.querySelector', {
          nodeId: cycleDocument.root.nodeId,
          selector: '#font-probe',
        })
        const cycleDescription = await v3Contents.debugger.sendCommand(
          'DOM.describeNode',
          { nodeId: cycleNode.nodeId },
        )
        await manager.handleAnnotationNodeSelected(
          manager.surfaces.get('artifact:v3-bridge'),
          cycleDescription.node.backendNodeId,
        )
        const cycleSelectionEvent = await waitFor(
          () => events.slice(cycleEventsBefore).find(event =>
            event.surfaceId === 'artifact:v3-bridge'
            && event.type === 'annotation-selected'),
          `annotation rearm selection cycle ${cycle + 1}`,
        )
        const cycleAnnotationId = `annotation_rearm_stress_${cycle + 1}`
        const cycleOverlayResult = await manager.showArtifactAnnotationOverlay({
          version: 3,
          surfaceId: 'artifact:v3-bridge',
          selectionId: cycleSelectionEvent.detail.selection.selectionId,
          annotationId: cycleAnnotationId,
          initialBody: '',
        })
        await restoreTrustedAnnotationInputFocus(
          annotationOverlay,
          `annotation rearm editor focus cycle ${cycle + 1}`,
        )
        const cycleBody = `Rearmed IME ${cycle + 1} 中文输入`
        const cycleInputState = await insertTrustedAnnotationText(
          annotationOverlay,
          cycleBody,
          cycleBody,
          `annotation rearm IME input cycle ${cycle + 1}`,
        )
        annotationInputHandshakes.push({
          kind: 'stress-ime',
          beforeinputCount: cycleInputState.inputProbe.beforeinput.length,
          inputCount: cycleInputState.inputProbe.input.length,
          value: cycleInputState.value,
        })
        const cycleFocusState = cycleInputState
        const cycleClose = await closeAnnotationOverlayAndDrain({
          version: 3,
          surfaceId: 'artifact:v3-bridge',
          annotationId: cycleAnnotationId,
        }, `annotation rearm close cycle ${cycle + 1}`)
        annotationRearmFocusCycles.push({
          picker: cyclePicker.ok,
          overlay: cycleOverlayResult.ok,
          ...cycleFocusState,
          typedValue: cycleInputState.value,
          closed: cycleClose.ok,
        })
      }
      const v3Record = manager.surfaces.get('artifact:v3-bridge')
      const annotationScrollBeforeFocus = await v3Contents.executeJavaScript('window.scrollY')
      const annotationFocus = await v3BridgeTarget.focusAnnotation(
        {
          version: 3,
          activePreviewArtifactId,
          annotationId: 'annotation_electron_fixture',
          scopeId: 'synthetic:v3-bridge',
          tagName: selected.tagName,
          elementPath: selected.elementPath,
          elementProofSha256: selected.elementProofSha256,
        },
        new AbortController().signal,
      )
      const annotationScrollAfterFocus = await v3Contents.executeJavaScript('window.scrollY')
      const annotationFocusHighlightArmed = Boolean(v3Record.annotationFocusTimer)
      const annotationWrongScopeRejected = await v3BridgeTarget.focusAnnotation(
        {
          version: 3,
          activePreviewArtifactId,
          annotationId: 'annotation_electron_fixture',
          scopeId: 'synthetic:other-scope',
          tagName: selected.tagName,
          elementPath: selected.elementPath,
          elementProofSha256: selected.elementProofSha256,
        },
        new AbortController().signal,
      ).then(() => false, () => true)
      const annotationWrongArtifactFocusRejected = await v3BridgeTarget.focusAnnotation(
        {
          version: 3,
          activePreviewArtifactId: 'art-synthetic-other-preview',
          annotationId: 'annotation_electron_fixture',
          scopeId: 'synthetic:v3-bridge',
          tagName: selected.tagName,
          elementPath: selected.elementPath,
          elementProofSha256: selected.elementProofSha256,
        },
        new AbortController().signal,
      ).then(() => false, () => true)
      await v3Contents.executeJavaScript(
        "document.getElementById('font-probe').setAttribute('data-focus-mismatch', '1')",
      )
      const annotationDomMismatchRejected = await v3BridgeTarget.focusAnnotation(
        {
          version: 3,
          activePreviewArtifactId,
          annotationId: 'annotation_electron_fixture',
          scopeId: 'synthetic:v3-bridge',
          tagName: selected.tagName,
          elementPath: selected.elementPath,
          elementProofSha256: selected.elementProofSha256,
        },
        new AbortController().signal,
      ).then(() => false, () => true)
      await v3Contents.executeJavaScript(
        "document.getElementById('font-probe').removeAttribute('data-focus-mismatch')",
      )
      await v3Contents.executeJavaScript(
        "document.body.setAttribute('data-ancestor-mismatch', '1')",
      )
      const annotationAncestorMismatchRejected = await v3BridgeTarget.focusAnnotation(
        {
          version: 3,
          activePreviewArtifactId,
          annotationId: 'annotation_electron_fixture',
          scopeId: 'synthetic:v3-bridge',
          tagName: selected.tagName,
          elementPath: selected.elementPath,
          elementProofSha256: selected.elementProofSha256,
        },
        new AbortController().signal,
      ).then(() => false, () => true)
      await v3Contents.executeJavaScript(
        "document.body.removeAttribute('data-ancestor-mismatch')",
      )
      const annotationRefocus = await v3BridgeTarget.focusAnnotation(
        {
          version: 3,
          activePreviewArtifactId,
          annotationId: 'annotation_electron_fixture',
          scopeId: 'synthetic:v3-bridge',
          tagName: selected.tagName,
          elementPath: selected.elementPath,
          elementProofSha256: selected.elementProofSha256,
        },
        new AbortController().signal,
      )
      const annotationFallbackShow = await manager.showArtifactAnnotationOverlay({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        selectionId: selected.selectionId,
        annotationId: 'annotation_fallback_fixture',
        initialBody: 'Preserved fallback draft',
      })
      const annotationFallbackEvent = await waitFor(
        () => events.find(event =>
          event.surfaceId === 'artifact:v3-bridge'
          && event.type === 'annotation-overlay-fallback'
          && event.detail?.annotationId === 'annotation_fallback_fixture'),
        'trusted annotation overlay fallback event',
      )
      const annotationPreviewHiddenForFallback = !v3View.getVisible()
      const annotationFallbackClose = manager.closeArtifactAnnotationOverlay({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        annotationId: 'annotation_fallback_fixture',
      })
      const annotationPreviewRestoredAfterFallback = v3View.getVisible()
      const annotationGoldenEventsBefore = events.length
      const annotationGoldenPicker = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: true,
      })
      if (!annotationGoldenPicker.ok) {
        throw new Error(annotationGoldenPicker.message || 'Golden annotation picker failed.')
      }
      const annotationDocument = await v3Contents.debugger.sendCommand('DOM.getDocument', {
        depth: -1,
        pierce: false,
      })
      const annotationGoldenNode = await v3Contents.debugger.sendCommand('DOM.querySelector', {
        nodeId: annotationDocument.root.nodeId,
        selector: 'main[data-label] path',
      })
      const annotationGoldenDescription = await v3Contents.debugger.sendCommand(
        'DOM.describeNode',
        { nodeId: annotationGoldenNode.nodeId },
      )
      await manager.handleAnnotationNodeSelected(
        v3Record,
        annotationGoldenDescription.node.backendNodeId,
      )
      const annotationGoldenSelectionEvent = await waitFor(
        () => events.slice(annotationGoldenEventsBefore).find(event =>
          event.surfaceId === 'artifact:v3-bridge'
          && event.type === 'annotation-selected'
          && event.detail?.selection?.tagName === 'path'),
        'Unicode and SVG element proof selection',
      )
      const annotationGoldenElementProof =
        annotationGoldenSelectionEvent.detail.selection.elementProofSha256

      const annotationRollbackCommands = []
      const originalAnnotationCdpCommand = manager.cdpCommand.bind(manager)
      let forceAnnotationPostconditionFailure = true
      manager.cdpCommand = async (record, method, params) => {
        annotationRollbackCommands.push([
          method,
          params?.mode ?? null,
          Boolean(params?.highlightConfig),
        ])
        const result = await originalAnnotationCdpCommand(record, method, params)
        if (
          forceAnnotationPostconditionFailure
          && method === 'Overlay.setInspectMode'
          && params?.mode === 'searchForNode'
        ) {
          forceAnnotationPostconditionFailure = false
          record.annotationPickerActive = false
        }
        return result
      }
      const annotationPostconditionFailure = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: true,
      })
      manager.cdpCommand = originalAnnotationCdpCommand

      const annotationPickerBeforeHideFailure = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: true,
      })
      manager.cdpCommand = async (record, method, params) => {
        if (method === 'Overlay.hideHighlight') {
          throw new Error('Synthetic compatibility cleanup failure')
        }
        return await originalAnnotationCdpCommand(record, method, params)
      }
      const annotationPickerOffWithHideFailure = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: false,
      })
      manager.cdpCommand = originalAnnotationCdpCommand

      const annotationPickerBeforeInspectModeFailure = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: true,
      })
      manager.cdpCommand = async (record, method, params) => {
        if (method === 'Overlay.setInspectMode' && params?.mode === 'none') {
          throw new Error('Synthetic authoritative cleanup failure')
        }
        return await originalAnnotationCdpCommand(record, method, params)
      }
      const annotationPickerOffWithInspectModeFailure =
        await manager.setArtifactAnnotationMode({
          version: 3,
          surfaceId: 'artifact:v3-bridge',
          enabled: false,
        })
      manager.cdpCommand = originalAnnotationCdpCommand
      const annotationPickerCleanupRecovery = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: false,
      })

      const annotationPickerOffEventsBefore = events.length
      const annotationPageClicksBeforePickerOff = await v3Contents.executeJavaScript(
        'Number(window.__annotationPageClicks || 0)',
      )
      const annotationPickerBeforeOff = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: true,
      })
      const annotationPickerOff = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: false,
      })
      const annotationTargetAfterOff = await v3Contents.executeJavaScript(`(() => {
        const rect = document.getElementById('font-probe').getBoundingClientRect()
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
      })()`)
      const annotationOffX = Math.floor(
        annotationTargetAfterOff.x + Math.max(1, annotationTargetAfterOff.width / 2),
      )
      const annotationOffY = Math.floor(
        annotationTargetAfterOff.y + Math.max(1, annotationTargetAfterOff.height / 2),
      )
      v3Contents.focus()
      await v3Contents.debugger.sendCommand('Input.dispatchMouseEvent', {
        type: 'mouseMoved',
        x: annotationOffX,
        y: annotationOffY,
        button: 'none',
      })
      await v3Contents.debugger.sendCommand('Input.dispatchMouseEvent', {
        type: 'mousePressed',
        x: annotationOffX,
        y: annotationOffY,
        button: 'left',
        clickCount: 1,
      })
      await v3Contents.debugger.sendCommand('Input.dispatchMouseEvent', {
        type: 'mouseReleased',
        x: annotationOffX,
        y: annotationOffY,
        button: 'left',
        clickCount: 1,
      })
      await new Promise(resolveWait => setTimeout(resolveWait, 100))
      const annotationPageClicksAfterPickerOff = await v3Contents.executeJavaScript(
        'Number(window.__annotationPageClicks || 0)',
      )
      const annotationSelectionAfterPickerOff = events
        .slice(annotationPickerOffEventsBefore)
        .some(event =>
          event.surfaceId === 'artifact:v3-bridge'
          && event.type === 'annotation-selected')
      const annotationPickerActiveAfterOff =
        manager.surfaces.get('artifact:v3-bridge')?.annotationPickerActive
      const v3Screenshot = await v3BridgeTarget.screenshot(
        { version: 3 },
        new AbortController().signal,
      )
      const v3ReadyEventsBeforeReload = events.filter(event =>
        event.surfaceId === 'artifact:v3-bridge' && event.type === 'ready').length
      const v3Reload = await v3BridgeTarget.reloadSurface(
        { version: 3 },
        new AbortController().signal,
      )
      const annotationFocusClearedOnReload = v3Record.annotationFocusTimer === null
      await waitFor(
        () => events.filter(event =>
          event.surfaceId === 'artifact:v3-bridge' && event.type === 'ready').length
          > v3ReadyEventsBeforeReload,
        'protocol-v3 bridge reload',
      )
      const v3NavigationEvents = events.filter(event =>
        event.surfaceId === 'artifact:v3-bridge'
        && event.version === 3
        && event.type === 'navigation-state')
      await manager.destroySurface('artifact:v3-bridge')
      await waitFor(() => v3Contents.isDestroyed(), 'protocol-v3 preview destruction')
      const annotationExpiredModeResult = await manager.setArtifactAnnotationMode({
        version: 3,
        surfaceId: 'artifact:v3-bridge',
        enabled: true,
      })

      const v4BindingA = await manager.createSurface({
        version: 4,
        surfaceId: 'artifact:v4-binding-a',
        kind: 'artifact-preview',
        payload: {
          launchUrl: `${fixture.previewOrigin}/binding-a`,
          expectedOrigin: fixture.previewOrigin,
          scopeId: 'synthetic:v4-binding-a',
          mode: 'full',
        },
      }, 'art-v4-binding-a')
      if (!v4BindingA.ok) throw new Error(v4BindingA.message || 'v4 binding A failed.')
      const v4BindingAView = view('artifact:v4-binding-a')
      manager.setSurfaceRect({
        surfaceId: 'artifact:v4-binding-a',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: true,
      })
      await waitFor(() => v4BindingAView.getVisible(), 'visible v4 binding A')
      const turnBindingA = await manager.acquireArtifactBridgeTargetBinding()
      if (!turnBindingA) throw new Error('v4 turn binding A was unavailable.')
      const sameSurfaceSecondBinding = await manager.acquireArtifactBridgeTargetBinding()
      const bindingAInitial = await turnBindingA.target.browserInspect(
        { version: 5, scope: 'document', maxNodes: 8 },
        new AbortController().signal,
      )

      const v4BindingB = await manager.createSurface({
        version: 4,
        surfaceId: 'artifact:v4-binding-b',
        kind: 'artifact-preview',
        payload: {
          launchUrl: `${fixture.previewOrigin}/binding-b`,
          expectedOrigin: fixture.previewOrigin,
          scopeId: 'synthetic:v4-binding-b',
          mode: 'full',
        },
      }, 'art-v4-binding-b')
      if (!v4BindingB.ok) throw new Error(v4BindingB.message || 'v4 binding B failed.')
      const v4BindingBView = view('artifact:v4-binding-b')
      manager.setSurfaceRect({
        surfaceId: 'artifact:v4-binding-b',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: true,
      })
      await waitFor(() => v4BindingBView.getVisible(), 'visible v4 binding B')
      const turnBindingB = await manager.acquireArtifactBridgeTargetBinding()
      if (!turnBindingB) throw new Error('v4 turn binding B was unavailable.')
      const bindingSwitchStress = []
      for (let iteration = 0; iteration < 20; iteration += 1) {
        const activeSurfaceId = iteration % 2 === 0
          ? 'artifact:v4-binding-a'
          : 'artifact:v4-binding-b'
        const activation = manager.activateSurface(activeSurfaceId)
        if (!activation.ok) throw new Error(activation.message || 'v4 binding switch failed.')
        const [bindingAStress, bindingBStress] = await Promise.all([
          turnBindingA.target.browserInspect(
            { version: 5, scope: 'document', maxNodes: 8 },
            new AbortController().signal,
          ),
          turnBindingB.target.browserInspect(
            { version: 5, scope: 'document', maxNodes: 8 },
            new AbortController().signal,
          ),
        ])
        bindingSwitchStress.push({
          activeSurfaceId,
          bindingAScopeId: bindingAStress.scopeId,
          bindingAGeneration: bindingAStress.bindingGeneration,
          bindingBScopeId: bindingBStress.scopeId,
          bindingBGeneration: bindingBStress.bindingGeneration,
        })
      }
      manager.activateSurface('artifact:v4-binding-b')
      const detachedBindingA = await manager.destroySurface('artifact:v4-binding-a')
      const bindingAStayedPinned = manager.surfaces.has('artifact:v4-binding-a')
        && !v4BindingAView.getVisible()
      const [bindingAAfterSwitch, bindingBAfterSwitch] = await Promise.all([
        turnBindingA.target.browserInspect(
          { version: 5, scope: 'document', maxNodes: 8 },
          new AbortController().signal,
        ),
        turnBindingB.target.browserInspect(
          { version: 5, scope: 'document', maxNodes: 8 },
          new AbortController().signal,
        ),
      ])
      const candidateHandleA = 'candidate_v4_binding_a_1234'
      const bindingACandidate = await turnBindingA.target.bindCandidatePreview(
        { version: 5, candidateHandle: candidateHandleA },
        new AbortController().signal,
      )
      const candidateSnapshotBeforeCrash = await turnBindingA.target.browserInspect(
        {
          version: 5,
          scope: 'document',
          maxNodes: 8,
          candidateHandle: candidateHandleA,
        },
        new AbortController().signal,
      )
      const originalCdpCommand = manager.cdpCommand.bind(manager)
      let droppedActionReply = false
      manager.cdpCommand = async (record, method, params) => {
        const value = await originalCdpCommand(record, method, params)
        if (method === 'Runtime.callFunctionOn' && !droppedActionReply) {
          droppedActionReply = true
          throw new Error('synthetic action reply loss')
        }
        return value
      }
      let actionResultUnknownCode = ''
      try {
        await turnBindingA.target.browserAct(
          {
            version: 5,
            action: 'press',
            key: 'Enter',
            candidateHandle: candidateHandleA,
          },
          new AbortController().signal,
        )
      } catch (error) {
        actionResultUnknownCode = error?.code || ''
      } finally {
        manager.cdpCommand = originalCdpCommand
      }
      const candidateSnapshotAfterUnknownAction = await turnBindingA.target.browserInspect(
        {
          version: 5,
          scope: 'document',
          maxNodes: 8,
          candidateHandle: candidateHandleA,
        },
        new AbortController().signal,
      )
      const bindingAContentsBeforeCrash = view('artifact:v4-binding-a').webContents
      emitRendererGone(bindingAContentsBeforeCrash)
      const candidateSnapshotAfterRecovery = await turnBindingA.target.browserInspect(
        {
          version: 5,
          scope: 'document',
          maxNodes: 8,
          candidateHandle: candidateHandleA,
        },
        new AbortController().signal,
      )
      const bindingAContentsAfterRecovery = view('artifact:v4-binding-a').webContents
      const candidateReboundAfterRecovery = manager.surfaces
        .get('artifact:v4-binding-a')?.candidatePreview?.handle === candidateHandleA
      emitRendererGone(bindingAContentsAfterRecovery)
      let secondBindingFailureCode = ''
      try {
        await turnBindingA.target.browserInspect(
          {
            version: 5,
            scope: 'document',
            maxNodes: 8,
            candidateHandle: candidateHandleA,
          },
          new AbortController().signal,
        )
      } catch (error) {
        secondBindingFailureCode = error?.code || ''
      }
      await turnBindingA.release()
      const bindingADestroyedBeforePinRelease = previewPinReleases.some(entry =>
        entry.scopeId === 'synthetic:v4-binding-a'
        && entry.surfacePresent === false)
      const bindingAReleasedEventCount = events.filter(event =>
        event.surfaceId === 'artifact:v4-binding-a'
        && event.type === 'agent-edit-released').length
      await turnBindingB.release()
      const bindingBSurfaceRetained = manager.surfaces.has('artifact:v4-binding-b')
      await manager.destroySurface('artifact:v4-binding-b')

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
        stressMode: fixture.stressMode,
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
        v2ArtifactBridgeUnavailable,
        v3BridgeCapabilities,
        v3AnnotationCapabilities,
        annotationPicker,
        annotationUnrelatedNodeCount,
        annotationSelected: {
          tagName: selected.tagName,
          hasBoundedPath: selected.elementPath.length > 0 && selected.elementPath.length <= 4096,
          omitsWholeDomDigest: selected.domSha256 === undefined,
          hasElementProof: /^[a-f0-9]{64}$/.test(selected.elementProofSha256),
          rect: selected.rect,
        },
        annotationPageClicks,
        resolvedSelection,
        annotationWrongArtifactResolveRejected,
        annotationOverlayResult,
        annotationOverlaySecurity,
        annotationOverlayVisualStructure,
        annotationOverlayDevToolsBlocked,
        annotationOverlayBounds,
        annotationOverlayMovedBounds,
        annotationOverlayOnTop,
        annotationNativeOwnerFocusAvailable,
        annotationOverlayFocusCycles,
        annotationOverlayFocusedAfterGeometry,
        annotationInputHandshakes,
        annotationOverlayTypedValue,
        annotationPreviewKeys,
        annotationEmptySubmitRetained,
        annotationOverlayRetainedAfterSubmit,
        annotationOverlayRetainedAfterCancel,
        annotationWrongAcknowledgement,
        annotationOverlayRetainedAfterWrongAcknowledgement,
        annotationOverlayAcknowledgement,
        annotationOverlayClosedAfterAcknowledgement,
        annotationPickerRearm,
        annotationRearmOverlayResult,
        annotationRearmCopy,
        annotationRearmLayout,
        annotationRearmEmptyBodyMessage,
        annotationRearmTypedValue,
        annotationRearmShiftEnter,
        annotationRearmShiftEnterDidNotSubmit,
        annotationRearmSubmitAfterInterruptedComposition,
        annotationRearmOverlayClose,
        annotationRearmFocusCycles,
        annotationFocus,
        annotationScrollBeforeFocus,
        annotationScrollAfterFocus,
        annotationFocusHighlightArmed,
        annotationFocusClearedOnReload,
        annotationWrongScopeRejected,
        annotationWrongArtifactFocusRejected,
        annotationDomMismatchRejected,
        annotationAncestorMismatchRejected,
        annotationRefocus,
        annotationFallbackShow,
        annotationFallbackEvent,
        annotationPreviewHiddenForFallback,
        annotationFallbackClose,
        annotationPreviewRestoredAfterFallback,
        annotationGoldenElementProof,
        annotationPostconditionFailure,
        annotationRollbackCommands: annotationRollbackCommands.filter(
          ([method]) => method.startsWith('Overlay.'),
        ),
        annotationPickerBeforeHideFailure,
        annotationPickerOffWithHideFailure,
        annotationPickerBeforeInspectModeFailure,
        annotationPickerOffWithInspectModeFailure,
        annotationPickerCleanupRecovery,
        annotationPickerBeforeOff,
        annotationPickerOff,
        annotationPickerActiveAfterOff,
        annotationPageClicksBeforePickerOff,
        annotationPageClicksAfterPickerOff,
        annotationSelectionAfterPickerOff,
        annotationExpiredModeResult,
        v3NavigationEventCount: v3NavigationEvents.length,
        v3Reload,
        v3Screenshot: {
          mime: v3Screenshot.mime,
          byteLength: v3Screenshot.data.byteLength,
          width: v3Screenshot.width,
          height: v3Screenshot.height,
        },
        v4BindingA,
        sameSurfaceSecondBindingWasRejected: sameSurfaceSecondBinding === null,
        bindingAInitial: {
          scopeId: bindingAInitial.scopeId,
          generation: bindingAInitial.bindingGeneration,
        },
        detachedBindingA,
        bindingAStayedPinned,
        bindingAAfterSwitch: {
          scopeId: bindingAAfterSwitch.scopeId,
          generation: bindingAAfterSwitch.bindingGeneration,
        },
        bindingBAfterSwitch: {
          scopeId: bindingBAfterSwitch.scopeId,
          generation: bindingBAfterSwitch.bindingGeneration,
        },
        bindingSwitchStress,
        bindingACandidate,
        candidateSnapshotBeforeCrash: {
          scopeId: candidateSnapshotBeforeCrash.scopeId,
          candidateHandle: candidateSnapshotBeforeCrash.candidateHandle,
          generation: candidateSnapshotBeforeCrash.bindingGeneration,
        },
        actionResultUnknownCode,
        candidateSnapshotAfterUnknownAction: {
          candidateHandle: candidateSnapshotAfterUnknownAction.candidateHandle,
          generation: candidateSnapshotAfterUnknownAction.bindingGeneration,
        },
        candidateSnapshotAfterRecovery: {
          scopeId: candidateSnapshotAfterRecovery.scopeId,
          candidateHandle: candidateSnapshotAfterRecovery.candidateHandle,
          generation: candidateSnapshotAfterRecovery.bindingGeneration,
        },
        bindingAContentsReplaced:
          bindingAContentsAfterRecovery.id !== bindingAContentsBeforeCrash.id,
        candidateReboundAfterRecovery,
        secondBindingFailureCode,
        bindingADestroyedBeforePinRelease,
        bindingAReleasedEventCount,
        bindingBSurfaceRetained,
        candidateReleaseHandles,
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
      stressMode,
    },
  )

  if (process.env.OPENSQUILLA_REQUIRE_ELECTRON_FOREGROUND === '1') {
    assert.equal(
      result.annotationNativeOwnerFocusAvailable,
      true,
      'the real Electron workbench gate requires an unlocked foreground GUI session; '
        + 'unlock the macOS login session and rerun with Electron allowed to take focus',
    )
  }

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
    result.v2ArtifactBridgeUnavailable,
    true,
    'v2 surfaces must not gain protocol-v3 agent capabilities implicitly',
  )
  assert.deepEqual(
    result.v3BridgeCapabilities,
    {
      captureSelection: false,
      resolveAnnotationSelection: true,
      focusAnnotation: true,
      browserInspect: false,
      browserAct: false,
      bindCandidatePreview: false,
      restoreCanonicalPreview: false,
      // Legacy v3 annotation capture/reload remain available to old clients;
      // autonomous browser inspection/action and candidate preview binding are
      // still v4-only.
      screenshot: true,
      officeFlush: false,
      reloadSurface: true,
    },
    'v3 capabilities must default closed except for implemented host operations',
  )
  assert.deepEqual(result.v3AnnotationCapabilities, {
    version: 3,
    available: true,
    picker: true,
    trustedOverlay: true,
    overlayCopyVersion: 1,
  })
  assert.equal(result.annotationPicker.ok, true)
  assert.equal(
    result.annotationUnrelatedNodeCount,
    50010,
    'an unrelated runtime-only branch larger than the old DOM hash limit must not block selection',
  )
  assert.equal(result.annotationSelected.tagName, 'div')
  assert.equal(result.annotationSelected.hasBoundedPath, true)
  assert.equal(result.annotationSelected.omitsWholeDomDigest, true)
  assert.equal(result.annotationSelected.hasElementProof, true)
  assert.ok(result.annotationSelected.rect.width > 0)
  assert.ok(result.annotationSelected.rect.height > 0)
  assert.equal(
    result.annotationPageClicks,
    0,
    'CDP inspect mode must consume the click before artifact handlers receive it',
  )
  assert.equal(result.resolvedSelection.selectionId.length > 0, true)
  assert.equal(result.resolvedSelection.tagName, 'div')
  assert.equal(result.resolvedSelection.scopeId, 'synthetic:v3-bridge')
  assert.equal(result.resolvedSelection.activePreviewArtifactId, 'art-synthetic-v3-bridge')
  assert.equal(result.annotationWrongArtifactResolveRejected, true)
  assert.equal(result.annotationOverlayResult.ok, true)
  assert.deepEqual(result.annotationOverlaySecurity, {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    webSecurity: true,
    webviewTag: false,
  })
  const {
    textareaHeight: annotationTextareaHeight,
    ...annotationOverlayVisualStructure
  } = result.annotationOverlayVisualStructure
  assert.deepEqual(annotationOverlayVisualStructure, {
    role: 'dialog',
    ariaModal: 'false',
    labelledBy: 'annotation-title',
    textareaLabel: '页面批注',
    targetText: '区域：示例',
    newlineHint: process.platform === 'darwin' ? '⇧ Return 换行' : 'Shift + Enter 换行',
    initialBody: 'Initial annotation',
    submitDisabled: false,
    cardRadius: '12px',
    submitHeight: '32px',
    tabOrder: ['annotation-body', 'annotation-cancel', 'annotation-submit'],
  })
  assert.ok(
    Number.parseFloat(annotationTextareaHeight) >= 73
      && Number.parseFloat(annotationTextareaHeight) < 74,
    'annotation textarea height must remain within one fractional Windows DPI pixel',
  )
  assert.equal(result.annotationOverlayDevToolsBlocked, true)
  assert.ok(result.annotationOverlayBounds.width <= 304)
  assert.ok(result.annotationOverlayBounds.height <= 160)
  assert.notEqual(
    result.annotationOverlayMovedBounds.y,
    result.annotationOverlayBounds.y,
    'trusted overlay must follow selected-element geometry changes',
  )
  assert.equal(result.annotationOverlayOnTop, true)
  assert.equal(
    result.annotationOverlayFocusCycles.length,
    result.stressMode ? 5 : 1,
    'trusted annotation focus must survive repeated geometry refreshes',
  )
  assert.equal(
    result.annotationOverlayFocusedAfterGeometry,
    true,
    'the trusted annotation editor must retain native keyboard focus across geometry refreshes',
  )
  assert.equal(
    result.annotationInputHandshakes.every(handshake => handshake.inputCount > 0),
    true,
    'every native character and IME commit must be acknowledged by a renderer input event',
  )
  assert.equal(result.annotationOverlayTypedValue, 'ASCII annotation 中文输入')
  assert.equal(
    result.annotationPreviewKeys,
    0,
    'keyboard input for the trusted annotation editor must not reach the untrusted preview',
  )
  assert.equal(
    result.annotationEmptySubmitRetained,
    true,
    'an empty submit must stay in the trusted editor without emitting an intent',
  )
  assert.equal(
    result.annotationOverlayRetainedAfterSubmit,
    true,
    'submit is only an intent; the trusted editor must remain until acknowledged',
  )
  assert.equal(
    result.annotationOverlayRetainedAfterCancel,
    true,
    'cancel is only an intent; the trusted editor must remain until acknowledged',
  )
  assert.equal(result.annotationWrongAcknowledgement.ok, false)
  assert.equal(result.annotationOverlayRetainedAfterWrongAcknowledgement, true)
  assert.equal(result.annotationOverlayAcknowledgement.ok, true)
  assert.equal(result.annotationOverlayClosedAfterAcknowledgement, true)
  assert.equal(result.annotationPickerRearm.ok, true)
  assert.equal(result.annotationRearmOverlayResult.ok, true)
  assert.deepEqual(result.annotationRearmCopy, {
    target: 'Button: Rearmed target',
    placeholder: 'Describe the next change…',
    newlineHint: process.platform === 'darwin'
      ? '⇧ Return for a new line'
      : 'Shift + Enter for a new line',
    newlineHintTitle: process.platform === 'darwin'
      ? '⇧ Return for a new line'
      : 'Shift + Enter for a new line',
    submitTitle: 'Add annotation',
  })
  assert.deepEqual(result.annotationRearmLayout, {
    hintOverflow: 'hidden',
    hintTextOverflow: 'ellipsis',
    submitWhiteSpace: 'nowrap',
    submitTextLineCount: 1,
    submitContained: true,
  })
  assert.equal(
    result.annotationRearmEmptyBodyMessage,
    'Describe the next requested change.',
    'a reused overlay must validate with the latest localized copy',
  )
  assert.equal(result.annotationRearmTypedValue, 'Rearmed input')
  assert.deepEqual(result.annotationRearmShiftEnter, {
    defaultAllowed: true,
    value: 'Rearmed input\n',
  })
  assert.equal(result.annotationRearmShiftEnterDidNotSubmit, true)
  assert.equal(result.annotationRearmSubmitAfterInterruptedComposition, true)
  assert.equal(result.annotationRearmOverlayClose.ok, true)
  assert.equal(result.annotationRearmFocusCycles.length, result.stressMode ? 3 : 1)
  assert.equal(
    result.annotationRearmFocusCycles.every(cycle =>
      cycle.picker
      && cycle.overlay
      && cycle.editorFocused
      && cycle.ownerFocused
      && cycle.nativeFocused
      && cycle.closed
      && cycle.typedValue.includes('中文输入')),
    true,
    'trusted annotation editor must survive repeated close/rearm/IME cycles',
  )
  assert.deepEqual(result.annotationFocus, {
    focused: true,
    activePreviewArtifactId: 'art-synthetic-v3-bridge',
  })
  assert.notEqual(
    result.annotationScrollAfterFocus,
    result.annotationScrollBeforeFocus,
    'trusted annotation focus must scroll the selected element into view',
  )
  assert.equal(result.annotationFocusHighlightArmed, true)
  assert.equal(
    result.annotationFocusClearedOnReload,
    true,
    'navigation must synchronously fence a pending annotation highlight',
  )
  assert.equal(result.annotationWrongScopeRejected, true)
  assert.equal(result.annotationWrongArtifactFocusRejected, true)
  assert.equal(result.annotationDomMismatchRejected, true)
  assert.equal(result.annotationAncestorMismatchRejected, true)
  assert.deepEqual(result.annotationRefocus, {
    focused: true,
    activePreviewArtifactId: 'art-synthetic-v3-bridge',
  })
  assert.equal(result.annotationFallbackShow.ok, false)
  assert.equal(result.annotationFallbackEvent.detail.reason, 'selection-stale')
  assert.equal(result.annotationPreviewHiddenForFallback, true)
  assert.equal(result.annotationFallbackClose.ok, true)
  assert.equal(result.annotationPreviewRestoredAfterFallback, true)
  assert.equal(
    result.annotationGoldenElementProof,
    '26992606963b33b7d475a826bf0a48ae802e9ac7bfe43ed5cab3aa97b7f0c5c8',
    'Electron and Gateway element-proof serialization must stay byte-identical',
  )
  assert.equal(result.annotationPostconditionFailure.ok, false)
  assert.deepEqual(result.annotationRollbackCommands, [
    ['Overlay.setInspectMode', 'searchForNode', true],
    ['Overlay.setInspectMode', 'none', true],
    ['Overlay.hideHighlight', null, false],
  ])
  assert.equal(result.annotationPickerBeforeHideFailure.ok, true)
  assert.equal(
    result.annotationPickerOffWithHideFailure.ok,
    true,
    'a compatibility-only hideHighlight failure must not reject a confirmed picker stop',
  )
  assert.equal(result.annotationPickerBeforeInspectModeFailure.ok, true)
  assert.equal(
    result.annotationPickerOffWithInspectModeFailure.ok,
    false,
    'a failed setInspectMode(none) must never be reported as a successful picker stop',
  )
  assert.equal(result.annotationPickerCleanupRecovery.ok, true)
  assert.deepEqual(result.annotationExpiredModeResult, {
    ok: false,
    code: 'PREVIEW_CAPABILITY_EXPIRED',
    retryable: true,
    message: 'Only the active protocol-v4 HTML artifact preview supports annotations.',
  })
  assert.equal(result.annotationPickerBeforeOff.ok, true)
  assert.equal(result.annotationPickerOff.ok, true)
  assert.equal(result.annotationPickerActiveAfterOff, false)
  assert.equal(result.annotationSelectionAfterPickerOff, false)
  assert.equal(
    result.annotationPageClicksAfterPickerOff,
    result.annotationPageClicksBeforePickerOff + 1,
    'disabling the picker must restore ordinary preview clicks without a residual inspect overlay',
  )
  assert.equal(result.v3Screenshot.mime, 'image/png')
  assert.ok(result.v3Screenshot.byteLength > 0, 'v3 screenshot must return bounded PNG bytes')
  assert.ok(result.v3Screenshot.width > 0 && result.v3Screenshot.height > 0)
  assert.equal(result.v3Reload.reloaded, true, 'v3 reload must stay on the active surface')
  assert.ok(result.v3NavigationEventCount > 0, 'v3 surfaces must preserve v2 navigation events')
  assert.equal(result.v4BindingA.ok, true)
  assert.equal(
    result.sameSurfaceSecondBindingWasRejected,
    true,
    'one native surface must admit only one editing turn binding',
  )
  assert.deepEqual(result.bindingAInitial, {
    scopeId: 'synthetic:v4-binding-a',
    generation: 1,
  })
  assert.equal(result.detachedBindingA.ok, true)
  assert.equal(result.detachedBindingA.code, 'AGENT_EDIT_IN_PROGRESS')
  assert.equal(
    result.bindingAStayedPinned,
    true,
    'UI detach must hide rather than destroy a turn-bound surface',
  )
  assert.equal(result.bindingAAfterSwitch.scopeId, 'synthetic:v4-binding-a')
  assert.equal(result.bindingBAfterSwitch.scopeId, 'synthetic:v4-binding-b')
  assert.equal(result.bindingSwitchStress.length, 20)
  for (const [iteration, snapshot] of result.bindingSwitchStress.entries()) {
    assert.equal(
      snapshot.activeSurfaceId,
      iteration % 2 === 0 ? 'artifact:v4-binding-a' : 'artifact:v4-binding-b',
    )
    assert.equal(snapshot.bindingAScopeId, 'synthetic:v4-binding-a')
    assert.equal(snapshot.bindingAGeneration, result.bindingAInitial.generation)
    assert.equal(snapshot.bindingBScopeId, 'synthetic:v4-binding-b')
    assert.equal(snapshot.bindingBGeneration, result.bindingBAfterSwitch.generation)
  }
  assert.equal(
    result.bindingAAfterSwitch.generation,
    result.bindingAInitial.generation,
    'switching the active UI surface must not mutate the old binding generation',
  )
  assert.equal(result.bindingACandidate.bound, true)
  assert.equal(
    result.candidateSnapshotBeforeCrash.candidateHandle,
    'candidate_v4_binding_a_1234',
  )
  assert.equal(
    result.actionResultUnknownCode,
    'action-result-unknown',
    'a lost browser action reply must require inspection rather than replay',
  )
  assert.equal(
    result.candidateSnapshotAfterUnknownAction.candidateHandle,
    'candidate_v4_binding_a_1234',
    'the binding must accept a fresh inspection after an uncertain action',
  )
  assert.equal(result.candidateSnapshotAfterRecovery.scopeId, 'synthetic:v4-binding-a')
  assert.equal(
    result.candidateSnapshotAfterRecovery.candidateHandle,
    'candidate_v4_binding_a_1234',
  )
  assert.ok(
    result.candidateSnapshotAfterRecovery.generation
      > result.candidateSnapshotBeforeCrash.generation,
    'surface recovery must invalidate the old binding generation',
  )
  assert.equal(result.bindingAContentsReplaced, true)
  assert.equal(result.candidateReboundAfterRecovery, true)
  assert.equal(
    result.secondBindingFailureCode,
    'binding-terminal-unavailable',
    'a second surface failure must terminate the binding without another rebuild',
  )
  assert.equal(
    result.bindingADestroyedBeforePinRelease,
    true,
    'a UI-detached surface must be destroyed before its canonical preview pin is released',
  )
  assert.equal(result.bindingAReleasedEventCount, 1)
  assert.equal(
    result.bindingBSurfaceRetained,
    true,
    'releasing a still-UI-owned binding must leave its canonical surface available',
  )
  assert.deepEqual(
    result.candidateReleaseHandles,
    ['candidate_v4_binding_a_1234'],
    'candidate cleanup must remain exactly once across recovery and terminal release',
  )
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
