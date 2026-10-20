import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_ANNOTATION_UNDER_XVFB !== '1') {
  const result = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_ANNOTATION_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}

const fixture = fileURLToPath(new URL('./fixtures/native-workbench-smoke', import.meta.url))
const documentHtml = await readFile(new URL('./fixtures/native-annotation-layout.html', import.meta.url), 'utf8')
const isolation = await mkdtemp(join(tmpdir(), 'opensquilla-annotation-v2-'))
const server = createServer((_request, response) => {
  response.setHeader('content-type', 'text/html; charset=utf-8')
  response.end(documentHtml)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`
let app
try {
  app = await electron.launch({
    args: [`--user-data-dir=${join(isolation, 'chromium')}`, fixture],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost' },
  })
  const result = await app.evaluate(async ({ BrowserWindow }, origin) => {
    const check = (condition, label) => { if (!condition) throw new Error(label) }
    const wait = async (predicate, label) => {
      const until = Date.now() + 10000
      while (Date.now() < until) {
        if (await predicate()) return
        await new Promise(resolve => setTimeout(resolve, 20))
      }
      throw new Error(`Timed out: ${label}`)
    }
    const events = []
    const owner = new BrowserWindow({ show: true, width: 960, height: 740,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true,
        partition: `annotation-owner:${Date.now()}` } })
    await owner.loadURL('data:text/html,<title>Isolated annotation test</title>')
    const manager = new globalThis.__opensquillaNativeWorkbenchSurfaceManager({
      getWindow: () => owner, emit: event => events.push(event),
    })
    const surfaceId = 'artifact:annotation-v2'
    try {
      const created = await manager.createSurface({ version: 3, surfaceId, kind: 'artifact-preview',
        payload: { launchUrl: `${origin}/index.html`, expectedOrigin: origin, scopeId: 'synthetic:annotation-v2', mode: 'full' } })
      check(created.ok, created.message || 'create failed')
      manager.setSurfaceRect({ surfaceId, x: 40, y: 60, width: 600, height: 500, visible: true })
      const record = manager.surfaces.get(surfaceId)
      const contents = record.view.webContents
      await wait(() => record.view.getVisible(), 'surface visible')
      const capability = await manager.getArtifactAnnotationCapabilities()
      check(capability.overlayEditorVersion === 2, 'editor version not advertised')
      await manager.setArtifactAnnotationMode({ version: 3, surfaceId, enabled: true })
      const point = await contents.executeJavaScript('(()=>{const r=document.querySelector("#target").getBoundingClientRect();return{x:r.x+20,y:r.y+20}})()')
      for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) {
        await contents.debugger.sendCommand('Input.dispatchMouseEvent', {
          type, ...point, button: type === 'mouseMoved' ? 'none' : 'left', clickCount: 1,
        })
      }
      await wait(() => events.some(event => event.type === 'annotation-selected'), 'real DOM selection')
      const selection = events.find(event => event.type === 'annotation-selected').detail.selection
      const copy = { targetLabel: '页面标题', contextLabel: '当前选区', bodyLabel: '修改要求',
        placeholder: '添加修改要求…', newlineHint: 'Enter 记录 · Shift + Enter 换行',
        cancelLabel: '取消本条', submitLabel: '记录到对话', emptyBodyMessage: '请填写修改要求' }
      const state = { batchCount: 2, busy: false, theme: 'dark' }
      const show = async (annotationId, initialBody = '', modern = true) => {
        const shown = await manager.showArtifactAnnotationOverlay({ version: 3, surfaceId,
          annotationId, selectionId: selection.selectionId, initialBody, overlayCopyVersion: 1, copy,
          ...(modern ? { overlayEditorVersion: 2, editorState: state } : {}) })
        check(shown.ok, shown.message || 'show failed')
        const overlay = manager.annotationOverlays.get(owner)
        await wait(() => overlay.view.getVisible(), 'editor visible')
        await wait(async () => await overlay.view.webContents.executeJavaScript(
          `document.querySelector('#annotation-body').value===${JSON.stringify(initialBody)} && document.documentElement.dataset.editorVersion==='${modern ? 2 : 1}'`), 'editor initialized')
        return overlay
      }
      const evaluate = (overlay, script) => overlay.view.webContents.executeJavaScript(script)
      const input = (overlay, body) => evaluate(overlay,
        `(()=>{const t=document.querySelector('#annotation-body');t.value=${JSON.stringify(body)};t.dispatchEvent(new Event('input',{bubbles:true}));})()`)
      const update = (annotationId, changes = {}) => manager.updateArtifactAnnotationOverlay({
        version: 3, surfaceId, annotationId, editorState: { ...state, ...changes },
      })
      const count = type => events.filter(event => event.type === type).length

      let overlay = await show('annotation_modern')
      await wait(() => overlay.view.getBounds().height === 48, 'compact native bounds')
      const compact = overlay.view.getBounds()
      check(compact.width === 352, 'wrong editor width')
      const selectedBounds = async () => {
        const geometry = await contents.executeJavaScript(`(()=>{const r=document.querySelector('#target').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,vw:innerWidth,vh:innerHeight}})()`)
        const surface = record.view.getBounds()
        return { x: surface.x + geometry.x * surface.width / geometry.vw,
          y: surface.y + geometry.y * surface.height / geometry.vh,
          width: geometry.width * surface.width / geometry.vw,
          height: geometry.height * surface.height / geometry.vh }
      }
      const belowTarget = async () => {
        const selected = await selectedBounds()
        const card = overlay.view.getBounds()
        return Math.abs(card.y - selected.y - selected.height - 8) <= 2
      }
      await wait(belowTarget, 'card adjacent below actual HTML heading')
      await input(overlay, '把这个标题改得更简洁')
      await wait(() => overlay.view.getBounds().height > 48, 'expanded native bounds')
      const expanded = overlay.view.getBounds()
      await wait(async () => await evaluate(overlay, "document.activeElement?.id==='annotation-body'"), 'input focus')
      // Real DOM geometry changes, not injected placement callbacks: the
      // native watcher must follow the same selected node and preserve focus.
      await contents.executeJavaScript('window.scrollTo(0,100)')
      await wait(async () => await belowTarget() && overlay.view.getBounds().y < expanded.y - 90, 'card follows page scroll')
      const scrolled = overlay.view.getBounds()
      check(overlay.view.webContents.isFocused(), 'scroll stole native editor focus')
      contents.setZoomFactor(1.25)
      await wait(async () => {
        const selected = await selectedBounds()
        return await belowTarget() && selected.width > 400
      }, 'card follows 125 percent preview zoom')
      const zoomed = overlay.view.getBounds()
      check(overlay.view.webContents.isFocused(), 'zoom stole native editor focus')
      check(await evaluate(overlay, "document.querySelector('#annotation-body').value==='把这个标题改得更简洁'"), 'geometry update lost draft')
      await contents.executeJavaScript('window.scrollTo(0,900)')
      await wait(() => overlay.view.getBounds().y <= record.view.getBounds().y + 17, 'offscreen target docks at nearest top edge')
      const offscreen = overlay.view.getBounds()
      contents.setZoomFactor(1)
      await contents.executeJavaScript('window.scrollTo(0,0)')
      await wait(belowTarget, 'card rejoins target after returning to viewport')
      await evaluate(overlay, `(()=>{const t=document.querySelector('#annotation-body');t.dispatchEvent(new CompositionEvent('compositionstart'));t.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,isComposing:true}));})()`)
      check(count('annotation-record-current') === 0 && count('annotation-batch-submit') === 0, 'IME submitted')
      await evaluate(overlay, `document.querySelector('#annotation-body').dispatchEvent(new CompositionEvent('compositionend'))`)
      overlay.view.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'Enter', modifiers: ['shift'] })
      overlay.view.webContents.sendInputEvent({ type: 'char', keyCode: '\r', modifiers: ['shift'] })
      overlay.view.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'Enter', modifiers: ['shift'] })
      check(count('annotation-record-current') === 0, 'Shift Enter recorded')
      overlay.view.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'Enter' })
      overlay.view.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'Enter' })
      await wait(() => count('annotation-record-current') === 1, 'record intent')
      check(count('annotation-batch-submit') === 0 && count('annotation-submit') === 0, 'record became batch/legacy')
      check(overlay.view.getVisible(), 'intent closed editor before persistence')
      await evaluate(overlay, "document.querySelector('#annotation-submit').click()")
      check(count('annotation-batch-submit') === 0, 'double action was accepted')

      check(update('annotation_modern', { error: '保存失败，请重试' }).ok, 'state update failed')
      await wait(async () => await evaluate(overlay, "document.querySelector('#annotation-error').textContent==='保存失败，请重试'"), 'visible retry error')
      check((await evaluate(overlay, "document.querySelector('#annotation-body').value")).includes('简洁'), 'failure lost text')
      check(!update('annotation_old', { busy: true }).ok, 'stale state reached current editor')
      await evaluate(overlay, "document.querySelector('#annotation-submit').click()")
      await wait(() => count('annotation-record-current') === 2, 'default button records to composer')
      check(count('annotation-batch-submit') === 0, 'default button executed batch')
      update('annotation_modern', { action: 'submit' })
      await wait(async () => await evaluate(overlay, "!document.querySelector('#annotation-submit').disabled"), 'explicit submit enabled')
      await evaluate(overlay, "document.querySelector('#annotation-submit').click()")
      await wait(() => count('annotation-batch-submit') === 1, 'separate batch intent')
      check(events.find(event => event.type === 'annotation-batch-submit').detail.body.includes('简洁'), 'batch lost final body')

      update('annotation_modern', { theme: 'light' })
      await wait(async () => await evaluate(overlay, "document.documentElement.dataset.theme==='light'"), 'theme update')
      await input(overlay, '这是很长的一条修改要求。\n'.repeat(100))
      await wait(() => overlay.view.getBounds().height >= 210 && overlay.view.getBounds().height <= 320, 'bounded long input')
      check(await evaluate(overlay, "document.querySelector('#annotation-body').scrollHeight>document.querySelector('#annotation-body').clientHeight"), 'long input is not scrollable')
      const long = overlay.view.getBounds()
      manager.setSurfaceRect({ surfaceId, x: 40, y: 60, width: 260, height: 160, visible: true })
      await wait(() => overlay.view.getBounds().width <= 260 && overlay.view.getBounds().height <= 160, 'small surface clamp')
      const narrow = overlay.view.getBounds()
      check(narrow.x >= 40 && narrow.y >= 60 && narrow.x + narrow.width <= 300 && narrow.y + narrow.height <= 220, 'overlay escapes surface')
      check((await evaluate(overlay, "document.querySelector('#annotation-body').value")).length > 300, 'resize lost body')
      manager.setSurfaceRect({ surfaceId, x: 40, y: 60, width: 600, height: 500, visible: true })
      await input(overlay, '')
      update('annotation_modern')
      await wait(() => overlay.view.getBounds().height === 48, 'native input area shrinks')
      const shrunk = overlay.view.getBounds()
      check(shrunk.height < expanded.height, 'transparent input region retained old height')

      overlay = await show('annotation_cancel', '取消仍保留此正文')
      await evaluate(overlay, "document.querySelector('#annotation-cancel').click()")
      await wait(() => events.some(event => event.type === 'annotation-cancel' && event.detail.annotationId === 'annotation_cancel'), 'cancel intent')
      check(events.find(event => event.type === 'annotation-cancel').detail.body === '取消仍保留此正文', 'cancel lost current text')

      overlay = await show('annotation_legacy', 'legacy draft', false)
      await evaluate(overlay, "document.querySelector('#annotation-submit').click()")
      await wait(() => count('annotation-submit') === 1, 'legacy still records')
      check(overlay.view.getBounds().height === 160, 'legacy geometry changed')
      check(!update('annotation_legacy').ok, 'v2 state accepted for legacy editor')
      return { compact, expanded, scrolled, zoomed, offscreen, long, narrow, shrunk,
        record: count('annotation-record-current'), batch: count('annotation-batch-submit'), legacy: count('annotation-submit'),
        checks: ['real HTML file DOM selection', 'below-target proximity', 'scroll tracking', '125 percent zoom tracking',
          'offscreen nearest-edge docking', 'geometry updates retain native focus and body',
          'compact/expanding bounds', 'IME', 'Shift Enter', 'native Enter',
          'record/batch distinction', 'duplicate intent guard', 'failure retains text', 'stale state fence',
          'theme update', 'long input scroll', 'narrow clamp', 'shrink click region', 'cancel body', 'legacy editor'] }
    } finally {
      await manager.destroyAll()
      if (!owner.isDestroyed()) owner.destroy()
    }
  }, origin)
  assert.equal(result.record, 2)
  assert.equal(result.batch, 1)
  assert.equal(result.legacy, 1)
  console.log(JSON.stringify(result, null, 2))
} finally {
  await app?.close().catch(() => {})
  await new Promise(resolve => server.close(resolve))
  assert.equal(dirname(resolve(isolation)), resolve(tmpdir()), 'Only remove this test temporary profile')
  assert.ok(basename(isolation).startsWith('opensquilla-annotation-v2-'))
  await rm(isolation, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 })
}
