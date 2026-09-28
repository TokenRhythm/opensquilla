import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import ts from '@typescript/typescript6'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_IMAGE_CLIPBOARD_XVFB !== '1') {
  const result = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_IMAGE_CLIPBOARD_XVFB: '1' }, stdio: 'inherit',
  })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}

const compile = source => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const mainSource = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
// Apply the actual renderer document CSP so this probe cannot drift into a
// weaker environment. The scheme and BrowserWindow settings match main.ts.
const secureDocument = mainSource.match(/function secureDesktopRendererDocument\(response: Response\): Response \{[\s\S]*?\n\}/)?.[0]
assert.ok(secureDocument, 'production renderer document security wrapper must be available')
const root = await mkdtemp(join(tmpdir(), 'opensquilla-image-clipboard-electron-'))
let app
try {
  const converter = await readFile(new URL('../../../opensquilla-webui/src/utils/imageClipboard.ts', import.meta.url), 'utf8')
  await writeFile(join(root, 'imageClipboard.js'), compile(converter))
  const protocolSource = await readFile(new URL('../src/desktop-renderer-protocol.ts', import.meta.url), 'utf8')
  await writeFile(join(root, 'rendererProtocol.mjs'), compile(protocolSource))
  await writeFile(join(root, 'main.mjs'), `
    import { app, BrowserWindow, protocol } from 'electron'
    import { readFile } from 'node:fs/promises'
    import { DESKTOP_RENDERER_SCHEME, DESKTOP_RENDERER_URL } from './rendererProtocol.mjs'
    protocol.registerSchemesAsPrivileged([{ scheme: DESKTOP_RENDERER_SCHEME,
      privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true } }])
    ${compile(secureDocument)}
    void app.whenReady().then(async () => {
    protocol.handle(DESKTOP_RENDERER_SCHEME, async request => {
      const path = new URL(request.url).pathname
      if (path === '/imageClipboard.js') return new Response(
        await readFile(new URL('./imageClipboard.js', import.meta.url)),
        { headers: { 'content-type': 'text/javascript' } })
      if (path !== '/chat/new') return new Response('Not found', { status: 404 })
      return secureDesktopRendererDocument(new Response(
        await readFile(new URL('./index.html', import.meta.url)),
        { headers: { 'content-type': 'text/html' } }))
    })
    const window = new BrowserWindow({ show: true, width: 640, height: 480,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } })
    await window.loadURL(DESKTOP_RENDERER_URL)
    window.focus()
    })
  `)
  await writeFile(join(root, 'index.html'), `<!doctype html>
    <title>Synthetic image clipboard test</title>
    <button id="copy-png">Copy PNG</button>
    <button id="copy-svg">Copy SVG image</button>
    <button id="copy-viewbox">Copy viewBox image</button>
    <button id="copy-source">Copy SVG source</button>
    <div id="paste" contenteditable="true" aria-label="Synthetic paste target">Paste here</div>
    <script type="module">
      import { prepareClipboardBlob, writePreparedClipboard } from '/imageClipboard.js'
      const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAwAAAAICAYAAADN5B7xAAAAFklEQVR4nGP4z8DwHxtmwAVGNdBCAwC4C1+hkVaFpQAAAABJRU5ErkJggg=='), char => char.charCodeAt(0))
      const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8" viewBox="0 0 12 8">\\n  <rect width="6" height="8" fill="#ff0000"/>\\n</svg>\\n'
      window.syntheticSvg = svg
      for (const id of ['png', 'svg', 'viewbox', 'source']) {
        document.querySelector('#copy-' + id).addEventListener('click', () => {
          window.copyState = 'busy'
          const mode = id === 'source' ? 'svg-source' : 'image'
          const source = id === 'png' ? new Blob([png], { type: 'image/png' })
            : new Blob([id === 'viewbox' ? svg.replace(' width="12" height="8"', '') : svg], { type: 'text/plain' })
          const descriptor = id === 'png' ? { name: 'synthetic.png', mime: 'image/png' }
            : { name: 'synthetic.svg', mime: 'text/plain' }
          void writePreparedClipboard(mode, async () => {
            // Force preparation past the click's synchronous stack, as a
            // historical attachment HTTP request does in the real interface.
            await new Promise(resolve => setTimeout(resolve, 25))
            return prepareClipboardBlob(source, descriptor, mode, new AbortController().signal)
          }).then(() => { window.copyState = 'ok' }, error => { window.copyState = String(error) })
        })
      }
      document.querySelector('#paste').addEventListener('paste', async event => {
        event.preventDefault()
        const text = event.clipboardData.getData('text/plain')
        const files = []
        for (const file of event.clipboardData.files) {
          const image = new Image()
          const url = URL.createObjectURL(file)
          try {
            image.src = url
            await image.decode()
            const canvas = document.createElement('canvas')
            canvas.width = image.naturalWidth
            canvas.height = image.naturalHeight
            const context = canvas.getContext('2d')
            context.drawImage(image, 0, 0)
            files.push({ type: file.type, width: canvas.width, height: canvas.height,
              left: Array.from(context.getImageData(1, 1, 1, 1).data),
              right: Array.from(context.getImageData(canvas.width - 1, 1, 1, 1).data) })
          } finally { URL.revokeObjectURL(url) }
        }
        window.pasted = { text, files }
      })
      window.fixtureReady = true
    </script>`)
  app = await electron.launch({
    args: [`--user-data-dir=${join(root, 'chromium')}`, join(root, 'main.mjs')],
  })
  const page = await app.firstWindow()
  await page.waitForFunction(() => window.fixtureReady)
  assert.equal(page.url(), 'opensquilla-app://desktop/chat/new')
  const isolation = await app.evaluate(({ BrowserWindow }) => {
    const preferences = BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences()
    return { sandbox: preferences.sandbox, contextIsolation: preferences.contextIsolation,
      nodeIntegration: preferences.nodeIntegration }
  })
  assert.deepEqual(isolation, { sandbox: true, contextIsolation: true, nodeIntegration: false })
  assert.equal(await page.evaluate(() => window.isSecureContext), true)
  for (const id of ['png', 'svg', 'viewbox', 'source']) {
    await page.evaluate(() => { delete window.pasted })
    await page.locator(`#copy-${id}`).click()
    await page.waitForFunction(() => window.copyState !== 'busy')
    assert.equal(await page.evaluate(() => window.copyState), 'ok', `${id} native clipboard write`)
    // Inspect the OS clipboard only after our own synthetic write completed.
    const native = await app.evaluate(({ clipboard }, mode) => mode === 'source'
      ? clipboard.readText() : clipboard.readImage().getSize(), id)
    if (id === 'source') assert.equal(native, await page.evaluate(() => window.syntheticSvg))
    else assert.deepEqual(native, { width: 12, height: 8 })
    await page.locator('#paste').focus()
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+V' : 'Control+V')
    await page.waitForFunction(() => window.pasted)
    const pasted = await page.evaluate(() => window.pasted)
    if (id === 'source') {
      assert.equal(pasted.text, await page.evaluate(() => window.syntheticSvg))
      assert.deepEqual(pasted.files, [])
    } else {
      assert.deepEqual(pasted.files, [{ type: 'image/png', width: 12, height: 8,
        left: [255, 0, 0, 255], right: [0, 0, 0, 0] }])
    }
  }
  const versions = await app.evaluate(() => ({ electron: process.versions.electron, chromium: process.versions.chrome }))
  console.log(`Electron image clipboard (${process.platform}; ${JSON.stringify(versions)}): secure renderer, async original PNG/SVG/viewBox PNG, transparency and exact source native paste passed`)
} finally {
  await app?.close()
  await rm(root, { recursive: true, force: true })
}
