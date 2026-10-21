import assert from 'node:assert/strict'
import { execFile, spawnSync } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { _electron as electron } from 'playwright'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_TEXT_MENU_UNDER_XVFB !== '1') {
  const result = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_TEXT_MENU_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}

const root = await mkdtemp(join(tmpdir(), 'opensquilla-text-context-menu-'))
const sentinel = 'Synthetic context-menu clipboard sentinel'
const samples = {
  main: '主对话 selected Alpha 42',
  auxiliary: '辅助对话 selected Beta 17',
  markdown: 'Markdown 预览 selected Gamma 63',
  multiline: '第一行中文 selected Eta 71\n第二行中文 selected Theta 82',
  frame: 'Iframe 预览 selected Delta 91',
  view: 'Native preview selected Epsilon 58',
  browser: 'Browser page selected Iota 34',
  input: 'Editable input selected Zeta 29',
}
const style = '<style>body{margin:28px;font:20px/1.8 Arial,sans-serif}p{margin:14px 0}span{white-space:pre}iframe{width:760px;height:130px;border:0}input{font:inherit;width:680px}#blank{height:48px;margin-top:12px}</style>'
let app
let browserServer

async function until(read, expected, label) {
  for (let attempt = 0; attempt < 100; attempt++) {
    const actual = await read()
    if (actual === expected) return
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  assert.equal(await read(), expected, label)
}

// Invoke only a menu item belonging to this isolated Electron process. This
// avoids sending global keystrokes or mouse clicks to the user's active app.
async function invokeWindowsCopyMenu(processId) {
  const script = `
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $processCondition = [System.Windows.Automation.PropertyCondition]::new(
      [System.Windows.Automation.AutomationElement]::ProcessIdProperty, ${processId})
    $typeCondition = [System.Windows.Automation.PropertyCondition]::new(
      [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
      [System.Windows.Automation.ControlType]::MenuItem)
    $condition = [System.Windows.Automation.AndCondition]::new($processCondition, $typeCondition)
    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
      $items = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants, $condition)
      foreach ($item in $items) {
        if ($item.Current.Name -match 'Copy|复制') {
          $pattern = $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
          $pattern.Invoke()
          Write-Output 'synthetic-native-copy-invoked'
          exit 0
        }
      }
      Start-Sleep -Milliseconds 50
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The isolated Electron Copy menu item was not accessible'
  `
  const { stdout } = await promisify(execFile)('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
    windowsHide: true, timeout: 12_000,
  })
  assert.match(stdout, /synthetic-native-copy-invoked/)
}

try {
  await writeFile(join(root, 'index.html'), `<!doctype html><title>Text menu fixture</title>${style}
    <p><span id="main">${samples.main}</span></p>
    <p><span id="auxiliary">${samples.auxiliary}</span></p>
    <p><span id="markdown">Markdown <strong>预览</strong> selected Gamma 63</span></p>
    <p><span id="multiline">${samples.multiline.replaceAll('\n', '<br>')}</span></p>
    <iframe src="frame.html"></iframe>
    <input id="input" value="${samples.input}">
    <div id="blank">Empty selection target</div>
    <span id="custom" oncontextmenu="event.preventDefault();window.customMenuCount=(window.customMenuCount||0)+1">Custom DOM menu target</span>`)
  await writeFile(join(root, 'frame.html'), `<!doctype html>${style}<span id="frame">${samples.frame}</span>`)
  await writeFile(join(root, 'main.mjs'), `
    import { app, BrowserWindow, Menu, protocol, webContents } from 'electron'
    import { installTextContextMenu } from ${JSON.stringify(new URL('../dist/text-context-menu.js', import.meta.url).href)}
    import { NativeWorkbenchSurfaceManager } from ${JSON.stringify(new URL('../dist/native-workbench-surface.js', import.meta.url).href)}
    protocol.registerSchemesAsPrivileged([{ scheme: 'opensquilla-artifact', privileges: {
      standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true,
    } }])
    app.commandLine.appendSwitch('disable-gpu')
    app.on('window-all-closed', () => {})
    globalThis.fixture = { events: [], popups: [], nativeNext: false }
    const originalPopup = Menu.prototype.popup
    Menu.prototype.popup = function (options) {
      const focused = webContents.getFocusedWebContents()
      const record = { menu: this, options, focusedContentsId: focused?.id,
        focusedFrameRoutingId: focused?.focusedFrame.routingId,
        focusedFrameProcessId: focused?.focusedFrame.processId,
        native: globalThis.fixture.nativeNext }
      globalThis.fixture.popups.push(record)
      if (record.native) {
        globalThis.fixture.nativeNext = false
        return originalPopup.call(this, options)
      }
    }
    const observe = contents => {
      contents.on('context-menu', (_event, params) => globalThis.fixture.events.push({
        contentsId: contents.id, selectionText: params.selectionText, isEditable: params.isEditable,
        canCopy: params.editFlags.canCopy, frameRoutingId: params.frame?.routingId,
        frameProcessId: params.frame?.processId,
      }))
    }
    // Do not hold Playwright's Electron loader handshake on app.whenReady().
    void app.whenReady().then(async () => {
    Menu.setApplicationMenu(process.platform === 'darwin'
      ? Menu.buildFromTemplate([{ role: 'appMenu' }, { role: 'editMenu' }]) : null)
    const owner = new BrowserWindow({ width: 1000, height: 760, show: true,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } })
    globalThis.fixture.owner = owner
    observe(owner.webContents)
    installTextContextMenu(owner.webContents)
    await owner.loadFile(${JSON.stringify(join(root, 'index.html'))})
    owner.focus()
    globalThis.fixture.createView = async () => {
      const manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner, emit: () => {} })
      globalThis.fixture.manager = manager
      const surfaceId = 'artifact:text-context-menu'
      const created = await manager.createSurface({ version: 1, surfaceId, kind: 'artifact-html',
        payload: { data: new TextEncoder().encode(${JSON.stringify(`<!doctype html><title>Native preview fixture</title>${style}<span id="view">${samples.view}</span>`)}),
          name: 'synthetic-context-menu.html', mime: 'text/html', scopeId: 'synthetic:text-menu',
          allowRemoteResources: false } })
      if (!created.ok) throw new Error(created.message || 'Managed artifact surface failed')
      manager.setSurfaceRect({ surfaceId, x: 0, y: 0, width: 950, height: 220, visible: true })
      const view = manager.surfaces.get(surfaceId).view
      globalThis.fixture.view = view
      // Only observe here: configureWebContents must install the real menu.
      observe(view.webContents)
      view.webContents.focus()
      return { id: view.webContents.id, url: view.webContents.getURL() }
    }
    globalThis.fixture.createBrowser = async url => {
      const manager = globalThis.fixture.manager
      await manager.destroySurface('artifact:text-context-menu', true)
      const surfaceId = 'browser:text-context-menu'
      const created = await manager.createSurface({ version: 2, surfaceId, kind: 'url-preview',
        payload: { url, scopeId: 'synthetic:text-menu' } })
      if (!created.ok) throw new Error(created.message || 'Managed browser surface failed')
      manager.setSurfaceRect({ surfaceId, x: 0, y: 0, width: 950, height: 220, visible: true })
      const view = manager.surfaces.get(surfaceId).view
      observe(view.webContents)
      view.webContents.focus()
      return { id: view.webContents.id, url: view.webContents.getURL() }
    }
    })
  `)
  app = await electron.launch({
    args: [
      ...(process.platform === 'linux' && process.getuid?.() === 0 ? ['--no-sandbox'] : []),
      `--user-data-dir=${join(root, 'chromium')}`, join(root, 'main.mjs'),
    ],
    env: { ...process.env, NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost' },
  })
  const page = await app.firstWindow()
  await page.locator('#main').waitFor()
  const reset = () => app.evaluate(({ clipboard }) => {
    clipboard.writeText('Synthetic context-menu clipboard sentinel')
    globalThis.fixture.events = []
    globalThis.fixture.popups = []
  })
  const readClipboard = () => app.evaluate(({ clipboard }) => clipboard.readText())
  const inspect = () => app.evaluate(() => {
    const popup = globalThis.fixture.popups.at(-1)
    return {
      event: globalThis.fixture.events.at(-1), popupCount: globalThis.fixture.popups.length,
      popup: popup && { roles: popup.menu.items.map(item => item.role || item.type),
        labels: popup.menu.items.map(item => item.label),
        enabled: popup.menu.items.map(item => item.enabled),
        ownerId: popup.options.window?.id, frameRoutingId: popup.options.frame?.routingId,
        frameProcessId: popup.options.frame?.processId, focusedContentsId: popup.focusedContentsId,
        focusedFrameRoutingId: popup.focusedFrameRoutingId, focusedFrameProcessId: popup.focusedFrameProcessId },
      expectedOwnerId: globalThis.fixture.owner.id,
    }
  })
  async function dragAndRightClick(activePage, frame, selector, expected) {
    await app.evaluate(() => globalThis.fixture.owner.focus())
    await activePage.bringToFront()
    await until(() => app.evaluate(() => globalThis.fixture.owner.isFocused()), true,
      'fixture window owns focus before pointer input')
    const locator = frame.locator(selector)
    await frame.evaluate(async () => {
      await document.fonts.ready
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    })
    const box = await locator.boundingBox()
    assert.ok(box)
    const ends = await locator.evaluate(element => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
      const nodes = []
      while (walker.nextNode()) if (walker.currentNode.textContent.length) nodes.push(walker.currentNode)
      const first = document.createRange()
      first.setStart(nodes[0], 0); first.setEnd(nodes[0], 1)
      const lastNode = nodes.at(-1)
      const last = document.createRange()
      last.setStart(lastNode, lastNode.length - 1); last.setEnd(lastNode, lastNode.length)
      const a = first.getBoundingClientRect(), b = last.getBoundingClientRect(), root = element.getBoundingClientRect()
      return { startX: a.x - root.x + 0.25, startY: a.y - root.y + a.height / 2,
        endX: b.right - root.x - 0.25, endY: b.y - root.y + b.height / 2 }
    })
    await activePage.mouse.move(box.x + ends.startX, box.y + ends.startY)
    await activePage.mouse.down()
    await activePage.mouse.move(box.x + ends.endX, box.y + ends.endY, { steps: 16 })
    // Wait for Chromium to apply the final drag position before releasing it.
    await until(() => frame.evaluate(() => window.getSelection().toString()), expected,
      `${selector} drag reaches the full selection`)
    await activePage.mouse.up()
    assert.equal(await frame.evaluate(() => window.getSelection().toString()), expected,
      JSON.stringify({ selector, box, ends, metrics: await frame.evaluate(() => ({
        innerWidth, innerHeight, devicePixelRatio, scrollX, scrollY,
      })) }))
    await activePage.mouse.click(box.x + ends.startX + 8, box.y + ends.startY, { button: 'right' })
    await until(() => app.evaluate(() => globalThis.fixture.events.length), 1, 'one real context-menu event')
  }
  function checkCopyPopup(actual, expected, browser = false) {
    assert.equal(actual.event.selectionText, expected)
    assert.equal(actual.event.canCopy, true)
    assert.equal(actual.popupCount, 1)
    if (browser) {
      assert.equal(actual.popup.roles.filter(role => role === 'copy').length, 1)
      assert.equal(actual.popup.enabled[actual.popup.roles.indexOf('copy')], true)
      assert.ok(actual.popup.roles.includes('selectall'))
      assert.ok(actual.popup.labels.some(label => /Find in page|在页面中查找/.test(label)))
      assert.ok(actual.popup.labels.some(label => /Reload|刷新/.test(label)))
    } else {
      assert.deepEqual(actual.popup.roles, ['copy'])
      assert.deepEqual(actual.popup.enabled, [true])
    }
    assert.equal(actual.popup.ownerId, actual.expectedOwnerId)
    assert.equal(actual.popup.focusedContentsId, actual.event.contentsId)
    assert.equal(actual.popup.frameRoutingId, actual.event.frameRoutingId)
    assert.equal(actual.popup.frameProcessId, actual.event.frameProcessId)
    assert.equal(actual.popup.focusedFrameRoutingId, actual.event.frameRoutingId)
    assert.equal(actual.popup.focusedFrameProcessId, actual.event.frameProcessId)
  }
  const invokeCapturedRole = (role = 'copy') => app.evaluate(({ BrowserWindow, Menu, webContents }, role) => {
    const popup = globalThis.fixture.popups.at(-1)
    const item = popup.menu.items.find(candidate => candidate.role === role.toLowerCase())
    if (!item?.enabled) throw new Error('No enabled ' + role + ' menu item')
    if (process.platform === 'darwin') {
      // Electron skips native macOS editing roles in its JavaScript dispatcher.
      // Exercise their native first-responder action without pretending this
      // is an automated click on an AppKit menu. Keep actual focus as the target.
      const selectors = { copy: 'copy:', cut: 'cut:', paste: 'paste:', selectall: 'selectAll:' }
      const selector = selectors[item.role]
      if (!selector) throw new Error('Unsupported native editing role ' + item.role)
      Menu.sendActionToFirstResponder(selector)
    } else {
      // Electron's real role dispatcher receives the current focus, not an
      // artificially supplied target. A missing contents.focus() must fail here.
      item.click({}, BrowserWindow.getFocusedWindow(), webContents.getFocusedWebContents())
    }
  }, role)

  for (const id of ['main', 'auxiliary', 'markdown', 'multiline']) {
    await reset()
    await dragAndRightClick(page, page, '#' + id, samples[id])
    checkCopyPopup(await inspect(), samples[id])
    await invokeCapturedRole()
    const clipboardExpected = process.platform === 'win32' ? samples[id].replaceAll('\n', '\r\n') : samples[id]
    await until(readClipboard, clipboardExpected, id + ' copy role writes exact selection')
    if (id === 'main') {
      await app.evaluate(({ clipboard }) => clipboard.writeText('Synthetic context-menu clipboard sentinel'))
      await page.keyboard.press(process.platform === 'darwin' ? 'Meta+C' : 'Control+C')
      await until(readClipboard, samples.main, 'standard Copy shortcut still uses the selected main text')
    }
    if (id === 'markdown') {
      const html = await app.evaluate(({ clipboard }) => clipboard.readHTML())
      assert.match(html, /预览/)
      assert.match(html, /strong|font-weight|<b[ >]/, 'native Copy preserves rich text')
    }
  }

  const frame = page.frames().find(candidate => candidate.url().endsWith('/frame.html'))
  assert.ok(frame, 'iframe fixture loaded')
  await reset()
  await dragAndRightClick(page, frame, '#frame', samples.frame)
  const frameResult = await inspect()
  checkCopyPopup(frameResult, samples.frame)
  assert.notEqual(frameResult.event.frameRoutingId,
    await app.evaluate(() => globalThis.fixture.owner.webContents.mainFrame.routingId), 'iframe origin retained')
  await invokeCapturedRole()
  await until(readClipboard, samples.frame, 'iframe copy does not use parent selection')

  await reset()
  await page.locator('#input').focus()
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A')
  await page.locator('#input').click({ button: 'right' })
  await until(() => app.evaluate(() => globalThis.fixture.events.length), 1, 'input context-menu event')
  const inputResult = await inspect()
  assert.equal(inputResult.event.isEditable, true)
  assert.ok(inputResult.popup.roles.includes('cut'))
  assert.ok(inputResult.popup.roles.includes('copy'))
  assert.ok(inputResult.popup.roles.includes('paste'))
  await invokeCapturedRole()
  await until(readClipboard, samples.input, 'editable copy remains available')
  await invokeCapturedRole('cut')
  await until(() => page.locator('#input').inputValue(), '', 'native Cut edits only the input')
  await invokeCapturedRole('paste')
  await until(() => page.locator('#input').inputValue(), samples.input, 'native Paste restores the synthetic input')
  await app.evaluate(() => { globalThis.fixture.events = []; globalThis.fixture.popups = [] })
  await page.locator('#input').click({ button: 'right' })
  await until(() => app.evaluate(() => globalThis.fixture.events.length), 1, 'fresh input context menu after paste')
  await invokeCapturedRole('selectAll')
  assert.deepEqual(await page.locator('#input').evaluate(input => [input.selectionStart, input.selectionEnd]),
    [0, samples.input.length], 'native Select All remains scoped to the input')

  await reset()
  await page.locator('#blank').click()
  await page.locator('#blank').click({ button: 'right' })
  await until(() => app.evaluate(() => globalThis.fixture.events.length), 1, 'empty context-menu event')
  assert.equal((await inspect()).popupCount, 0, 'no editing menu for unselected readonly content')
  assert.equal(await readClipboard(), sentinel)

  await reset()
  await page.locator('#custom').click({ button: 'right' })
  assert.equal(await page.evaluate(() => window.customMenuCount), 1)
  assert.equal((await inspect()).popupCount, 0, 'DOM-owned context menu does not also open a native menu')

  // The Windows branch also opens an actual OS popup and invokes its Copy
  // item through process-scoped accessibility, independently of role dispatch.
  if (process.platform === 'win32') {
    await reset()
    await app.evaluate(() => { globalThis.fixture.nativeNext = true })
    await dragAndRightClick(page, page, '#main', samples.main)
    checkCopyPopup(await inspect(), samples.main)
    await invokeWindowsCopyMenu(await app.evaluate(() => process.pid))
    await until(readClipboard, samples.main, 'native popup Copy writes exact selection')
  }

  const viewInfo = await app.evaluate(() => globalThis.fixture.createView())
  await until(() => app.context().pages().some(candidate => candidate.url() === viewInfo.url),
    true, 'WebContentsView Playwright page attached')
  const viewPage = app.context().pages().find(candidate => candidate.url() === viewInfo.url)
  await reset()
  await dragAndRightClick(viewPage, viewPage, '#view', samples.view)
  const viewResult = await inspect()
  checkCopyPopup(viewResult, samples.view)
  assert.equal(viewResult.event.contentsId, viewInfo.id)
  await invokeCapturedRole()
  await until(readClipboard, samples.view, 'WebContentsView copy does not use owner selection')

  browserServer = createServer((_request, response) => {
    response.setHeader('content-type', 'text/html; charset=utf-8')
    response.end(`<!doctype html><title>Browser menu fixture</title>${style}<span id="browser">${samples.browser}</span>`)
  })
  await new Promise(resolve => browserServer.listen(0, '127.0.0.1', resolve))
  const browserInfo = await app.evaluate((_electron, url) => globalThis.fixture.createBrowser(url),
    `http://127.0.0.1:${browserServer.address().port}/browser.html`)
  await until(() => app.context().pages().some(candidate => candidate.url() === browserInfo.url),
    true, 'managed URL preview Playwright page attached')
  const browserPage = app.context().pages().find(candidate => candidate.url() === browserInfo.url)
  await reset()
  await dragAndRightClick(browserPage, browserPage, '#browser', samples.browser)
  const browserResult = await inspect()
  checkCopyPopup(browserResult, samples.browser, true)
  assert.equal(browserResult.event.contentsId, browserInfo.id)
  await invokeCapturedRole()
  await until(readClipboard, samples.browser, 'URL preview browser menu retains native Copy behavior')

  const versions = await app.evaluate(() => ({ electron: process.versions.electron, chromium: process.versions.chrome }))
  const nativeVerification = process.platform === 'win32' ? 'native Windows popup Copy invoked'
    : process.platform === 'darwin' ? 'native first-responder actions verified (AppKit menu clicks not automated)'
      : 'role dispatch verified (native OS popup invocation not automated)'
  console.log(`Electron text context menus (${process.platform}; ${JSON.stringify(versions)}): main/auxiliary/Markdown drag selection, rich HTML and multiline Chinese, iframe focus/frame, managed artifact and URL preview owners, input Cut/Copy/Paste/Select All, empty selection and DOM menu ownership passed; ${nativeVerification}`)
} finally {
  await app?.close()
  if (browserServer) await new Promise(resolve => browserServer.close(resolve))
  await rm(root, { recursive: true, force: true })
}
