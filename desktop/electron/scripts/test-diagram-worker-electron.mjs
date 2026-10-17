import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { copyFile, mkdtemp, readFile, rm, writeFile, mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import ts from '@typescript/typescript6'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_DIAGRAM_WORKER_XVFB !== '1') {
  const result = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_DIAGRAM_WORKER_XVFB: '1' }, stdio: 'inherit',
  })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}

const compile = source => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const mainSource = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
const secureDocument = mainSource.match(/function secureDesktopRendererDocument\(response: Response\): Response \{[\s\S]*?\n\}/)?.[0]
const handleRequest = mainSource.match(/async function handleDesktopRendererRequest\(request: Request\): Promise<Response> \{[\s\S]*?\n\}/)?.[0]
const registerSchemes = mainSource.match(/protocol\.registerSchemesAsPrivileged\(\[[\s\S]*?\n\]\)/)?.[0]
assert.ok(secureDocument && handleRequest && registerSchemes, 'production security and routing must be available')
const root = await mkdtemp(join(tmpdir(), 'opensquilla-diagram-worker-electron-'))
let app
try {
  const protocolSource = await readFile(new URL('../src/desktop-renderer-protocol.ts', import.meta.url), 'utf8')
  await writeFile(join(root, 'rendererProtocol.mjs'), compile(protocolSource))
  await mkdir(join(root, 'assets'))
  await copyFile(new URL('../../../opensquilla-webui/node_modules/@viz-js/viz/dist/viz.js', import.meta.url), join(root, 'assets', 'viz.js'))
  await writeFile(join(root, 'assets', 'wasm-bytes.js'), 'export const bytes = new Uint8Array([0,97,115,109,1,0,0,0])')
  await writeFile(join(root, 'assets', 'diagram-worker.js'), `
    import { bytes } from './wasm-bytes.js'
    let javascriptEvalBlocked = false
    try { new Function('return 1')() } catch (error) { javascriptEvalBlocked = error.name === 'EvalError' }
    try {
      const module = await WebAssembly.compile(bytes)
      const { instance } = await import('./viz.js')
      const viz = await instance()
      const svg = viz.renderString('digraph { Start -> Finish }', { format: 'svg' })
      postMessage({ wasm: module instanceof WebAssembly.Module, javascriptEvalBlocked,
        graphviz: svg.includes('<svg') && svg.includes('Start') && svg.includes('Finish') })
    } catch (error) { postMessage({ error: String(error), javascriptEvalBlocked }) }
  `)
  await writeFile(join(root, 'desktop.html'), `<!doctype html><title>Diagram worker CSP test</title>
    <script type="module">
      window.workerOutcome = null
      const worker = new Worker(new URL('./assets/diagram-worker.js', location.origin), { type: 'module' })
      worker.onmessage = event => { window.workerOutcome = event.data; worker.terminate() }
      worker.onerror = event => { window.workerOutcome = { error: event.message }; worker.terminate() }
    </script>`)
  await writeFile(join(root, 'main.mjs'), `
    import { app, BrowserWindow, protocol, net as electronNet } from 'electron'
    import { stat } from 'node:fs/promises'
    import { dirname } from 'node:path'
    import { fileURLToPath, pathToFileURL } from 'node:url'
    import { DESKTOP_RENDERER_SCHEME, DESKTOP_RENDERER_ENTRY, DESKTOP_RENDERER_URL,
      routeDesktopRendererRequest, resolveDesktopRendererFile } from './rendererProtocol.mjs'
    const NATIVE_WORKBENCH_ARTIFACT_SCHEME = 'opensquilla-artifact'
    ${compile(registerSchemes)}
    const desktopRendererDistPath = () => dirname(fileURLToPath(import.meta.url))
    const pathIsFile = async path => (await stat(path).catch(() => null))?.isFile() ?? false
    const proxyDesktopRendererRequest = () => { throw new Error('Synthetic test must not use Gateway') }
    ${compile(secureDocument)}
    ${compile(handleRequest)}
    void app.whenReady().then(async () => {
      protocol.handle(DESKTOP_RENDERER_SCHEME, handleDesktopRendererRequest)
      const window = new BrowserWindow({ show: false,
        webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } })
      await window.loadURL(DESKTOP_RENDERER_URL)
    })
  `)
  app = await electron.launch({
    args: ['--disable-gpu', `--user-data-dir=${join(root, 'chromium')}`, join(root, 'main.mjs')],
  })
  const page = await app.firstWindow()
  await page.waitForFunction(() => window.workerOutcome !== null && window.workerOutcome !== undefined)
  assert.equal(page.url(), 'opensquilla-app://desktop/chat/new')
  assert.equal(await page.evaluate(() => window.isSecureContext), true)
  assert.deepEqual(await page.evaluate(() => window.workerOutcome), { wasm: true, javascriptEvalBlocked: true, graphviz: true })
  const sources = await page.evaluate(async () => {
    const document = await fetch('/chat/new')
    const worker = await fetch('/assets/diagram-worker.js')
    return { document: document.headers.get('content-security-policy'), worker: worker.headers.get('content-security-policy') }
  })
  assert.equal(sources.document, sources.worker, 'worker response must carry the production document CSP')
  assert.ok(sources.document.includes("'wasm-unsafe-eval'"))
  assert.ok(!sources.document.includes("'unsafe-eval'"))
  const isolation = await app.evaluate(({ BrowserWindow }) => {
    const preferences = BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences()
    return { sandbox: preferences.sandbox, contextIsolation: preferences.contextIsolation,
      nodeIntegration: preferences.nodeIntegration }
  })
  assert.deepEqual(isolation, { sandbox: true, contextIsolation: true, nodeIntegration: false })
  const versions = await app.evaluate(() => ({ electron: process.versions.electron, chromium: process.versions.chrome }))
  console.log(`Electron diagram worker (${process.platform}; ${JSON.stringify(versions)}): production custom protocol, module import, Graphviz DOT-to-SVG, WASM compilation, no JavaScript eval, sandbox passed`)
} finally {
  await app?.close()
  // Only this test's verified mkdtemp child may be recursively removed.
  assert.equal(dirname(resolve(root)), resolve(tmpdir()))
  assert.ok(resolve(root).startsWith(`${resolve(tmpdir())}${sep}opensquilla-diagram-worker-electron-`))
  await rm(root, { recursive: true, force: true })
}
