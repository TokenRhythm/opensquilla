import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { runInNewContext } from 'node:vm'
import ts from '@typescript/typescript6'

import {
  buildDesktopQuitDialogHtml,
  escapeQuitDialogHtml,
  parseDesktopQuitDialogResponse,
} from '../dist/desktop-quit-dialog.js'

assert.equal(escapeQuitDialogHtml(`<x a="b">&'`), '&lt;x a=&quot;b&quot;&gt;&amp;&#39;')
assert.equal(parseDesktopQuitDialogResponse('confirm'), 'confirm')
assert.equal(parseDesktopQuitDialogResponse('cancel'), 'cancel')
assert.equal(parseDesktopQuitDialogResponse('background'), 'background')
assert.equal(parseDesktopQuitDialogResponse({ response: 'confirm', remember: true }), 'confirm')
assert.equal(parseDesktopQuitDialogResponse({ response: 'background', remember: false }), 'background')
for (const invalid of [
  { response: 'confirm' }, { response: 'confirm', remember: 'yes' },
  { response: 'confirm', remember: true, extra: true }, { response: 'other', remember: false },
]) assert.equal(parseDesktopQuitDialogResponse(invalid), null)
assert.equal(parseDesktopQuitDialogResponse('confirm '), null)
assert.equal(parseDesktopQuitDialogResponse(null), null)

const html = buildDesktopQuitDialogHtml({
  lang: 'zh-Hans',
  title: '退出 OpenSquilla？',
  message: '当前还有未完成的任务。',
  detail: '退出将停止任务，并保存已收到的内容。',
  keepRunning: '继续运行',
  confirm: '停止任务并退出',
})
assert.match(html, /role="dialog"/)
assert.match(html, /<html lang="zh-Hans">/)
assert.match(html, /aria-modal="true"/)
assert.match(html, /停止任务并退出/)
assert.match(html, /send\('confirm'\)/)
assert.match(html, /send\('cancel'\)/)
assert.match(html, /document\.getElementById\('keep'\)\.focus\(\)/)

const escaped = buildDesktopQuitDialogHtml({
  lang: 'en" onload="unsafe',
  title: '<unsafe>',
  message: 'a & b',
  detail: '"quoted"',
  keepRunning: "don't",
  confirm: 'go',
})
assert.match(escaped, /&lt;unsafe&gt;/)
assert.match(escaped, /a &amp; b/)
assert.doesNotMatch(escaped, /<unsafe>/)
assert.match(escaped, /lang="en&quot; onload=&quot;unsafe"/)

const ask = buildDesktopQuitDialogHtml({
  title: '关闭 OpenSquilla？', message: '当前还有未完成的任务。',
  detail: '后台运行会保留任务和网页。\n\n退出将停止任务并关闭网页。',
  keepRunning: '取消', background: '继续在后台运行', confirm: '停止任务并退出', remember: '记住我的选择',
})
assert.equal((ask.match(/<button /g) || []).length, 3)
assert.match(ask, /id="remember" type="checkbox"/)
assert.match(ask, /white-space: pre-line/)

function runDialog(documentHtml) {
  const sent = [], keys = new Map(), elements = new Map()
  const document = { activeElement: null,
    getElementById: id => elements.get(id),
    querySelectorAll: selector => [...elements.values()].filter(element =>
      selector === 'button' ? element.tag === 'button' : true),
  }
  for (const [tag, id] of [...documentHtml.matchAll(/<(button|input)[^>]*id="([^"]+)"/g)].map(match => [match[1], match[2]])) {
    const handlers = new Map()
    const element = { tag, id, checked: false, disabled: false,
      addEventListener: (name, handler) => handlers.set(name, handler),
      focus: () => { document.activeElement = element },
      click: () => handlers.get('click')?.(),
    }
    elements.set(id, element)
  }
  runInNewContext(documentHtml.match(/<script>([\s\S]*?)<\/script>/i)[1], {
    document, window: { opensquillaDesktop: { quitDialogRespond: value => sent.push(JSON.parse(JSON.stringify(value))) },
      addEventListener: (name, handler) => keys.set(name, handler) },
  })
  return { sent, elements, document,
    key: (key, shiftKey = false) => keys.get('keydown')({ key, shiftKey, preventDefault() {} }),
  }
}

{
  const dialog = runDialog(ask)
  assert.equal(dialog.document.activeElement.id, 'background', 'Ask defaults to preserving tasks and pages')
  dialog.key('Enter')
  assert.deepEqual(dialog.sent, [], 'A document-level Enter must not choose destructive Quit')
  dialog.elements.get('remember').checked = true
  dialog.elements.get('background').click()
  assert.deepEqual(dialog.sent, [{ response: 'background', remember: true }])
  dialog.elements.get('confirm').click()
  assert.equal(dialog.sent.length, 1, 'Repeated activation cannot send a second decision')
}
{
  const dialog = runDialog(ask)
  dialog.elements.get('confirm').focus()
  dialog.key('Tab')
  assert.equal(dialog.document.activeElement.id, 'remember', 'Tab remains inside the dialog')
  dialog.key('Tab', true)
  assert.equal(dialog.document.activeElement.id, 'confirm')
  dialog.key('Escape')
  assert.deepEqual(dialog.sent, [{ response: 'cancel', remember: false }])
  const quit = runDialog(html)
  assert.equal(quit.document.activeElement.id, 'keep', 'Explicit Quit defaults to keeping the app open')
}

// Execute the production window wrapper as well: a modal must follow its
// actual parent instead of centering on a large desktop's primary display.
const main = ts.createSourceFile('main.js', readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
let showSource
function collect(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'showDesktopQuitConfirmation') {
    showSource = node.getText(main)
  }
  ts.forEachChild(node, collect)
}
collect(main)
assert.ok(showSource)

async function checkPlacement({ owner, area = { x: 0, y: 0, width: 1920, height: 1080 },
  destroyed = false, destroyBeforeReady = false, expected, platform = 'win32', sheetHeightLoss = 0 }) {
  const ipcMain = new EventEmitter()
  const displays = []
  let child
  const parent = owner ? { isDestroyed: () => destroyed, getBounds: () => owner } : undefined
  class Window extends EventEmitter {
    constructor(options) {
      super()
      child = this
      this.options = options
      this.webContents = new EventEmitter()
      this.webContents.id = 42
      this.destroyed = false
      this.centered = 0
      this.shown = 0
      this.contentSize = [options.width, options.height]
      this.resizes = []
    }
    isDestroyed() { return this.destroyed }
    getBounds() { return { x: 0, y: 0, width: this.contentSize[0], height: this.contentSize[1] } }
    getContentSize() { return [...this.contentSize] }
    setContentSize(width, height) { this.contentSize = [width, height]; this.resizes.push([width, height]) }
    setPosition(x, y) { this.position = [x, y] }
    center() { this.centered++ }
    show() { this.shown++; this.contentSize[1] -= sheetHeightLoss }
    focus() {}
    close() { this.destroyed = true; this.emit('closed') }
    async loadURL() {
      if (destroyBeforeReady) destroyed = true
      this.emit('ready-to-show')
    }
  }
  const show = new Function('env', `with (env) { ${showSource}; return showDesktopQuitConfirmation }`)({
    BrowserWindow: Window, ipcMain, join, __dirname: '/fixture',
    process: { platform }, desktopLocale: 'en',
    buildDesktopQuitDialogHtml, parseDesktopQuitDialogResponse, desktopLog() {},
    screen: { getDisplayMatching: bounds => { displays.push(bounds); return { workArea: area } } },
  })
  const result = show({ parent, title: 'Close?', message: 'Choose', detail: 'Details',
    keepRunning: 'Cancel', background: 'Background', confirm: 'Quit' })
  if (expected) {
    assert.deepEqual(child.position, expected)
    assert.equal(child.centered, 0, 'A live parent supplies the modal position')
    assert.deepEqual(displays, Array(child.resizes.length + 1).fill(owner),
      'Choose the parent display again after any native sheet resize')
    assert.ok(child.position[0] >= area.x && child.position[1] >= area.y)
    assert.ok(child.position[0] + child.options.width <= area.x + area.width)
    assert.ok(child.position[1] + child.options.height <= area.y + area.height)
  } else {
    assert.equal(child.centered, child.resizes.length + 1,
      'An absent or destroyed parent uses standalone centering, including after a native resize')
    assert.equal(child.position, undefined)
    assert.deepEqual(displays, [])
  }
  assert.equal(child.shown, 1)
  assert.deepEqual(child.getContentSize(), [520, 288], 'The visible viewport retains the requested content size')
  assert.deepEqual(child.resizes, sheetHeightLoss ? [[520, 288]] : [],
    'Only an actual native size change needs correction; do not hardcode titlebar pixels')
  ipcMain.emit('desktop:quit-dialog-response', { sender: child.webContents }, 'cancel')
  assert.deepEqual(await result, { response: 'cancel', remember: false })
}

await checkPlacement({ owner: { x: 460, y: 80, width: 980, height: 720 }, expected: [690, 296] })
await checkPlacement({ owner: { x: -1200, y: -800, width: 980, height: 720 }, expected: [0, 0] })
await checkPlacement({ owner: { x: 2300, y: 1100, width: 980, height: 720 }, expected: [1400, 792] })
await checkPlacement({ owner: { x: -1900, y: 80, width: 980, height: 720 },
  area: { x: -1920, y: 0, width: 1920, height: 1080 }, expected: [-1670, 296] })
await checkPlacement({ owner: { x: 460, y: 80, width: 980, height: 720 }, destroyed: true })
await checkPlacement({ owner: { x: 460, y: 80, width: 980, height: 720 }, destroyBeforeReady: true })
await checkPlacement({})
for (const sheetHeightLoss of [0, 32, 48]) {
  await checkPlacement({ platform: 'darwin', sheetHeightLoss,
    owner: { x: 460, y: 80, width: 980, height: 720 }, expected: [690, 296] })
}

console.log('desktop quit dialog tests passed, including parent-relative and offscreen placement')
