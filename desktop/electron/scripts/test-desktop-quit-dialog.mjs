import assert from 'node:assert/strict'

import {
  buildDesktopQuitDialogHtml,
  escapeQuitDialogHtml,
  parseDesktopQuitDialogResponse,
} from '../dist/desktop-quit-dialog.js'

assert.equal(escapeQuitDialogHtml(`<x a="b">&'`), '&lt;x a=&quot;b&quot;&gt;&amp;&#39;')
assert.equal(parseDesktopQuitDialogResponse('confirm'), 'confirm')
assert.equal(parseDesktopQuitDialogResponse('cancel'), 'cancel')
assert.equal(parseDesktopQuitDialogResponse('confirm '), null)
assert.equal(parseDesktopQuitDialogResponse(null), null)

const html = buildDesktopQuitDialogHtml({
  title: '退出 OpenSquilla？',
  message: '当前还有未完成的任务。',
  detail: '退出将停止任务，并保存已收到的内容。',
  keepRunning: '继续运行',
  confirm: '停止任务并退出',
})
assert.match(html, /role="dialog"/)
assert.match(html, /aria-modal="true"/)
assert.match(html, /停止任务并退出/)
assert.match(html, /send\('confirm'\)/)
assert.match(html, /send\('cancel'\)/)
assert.match(html, /document\.getElementById\('keep'\)\.focus\(\)/)

const escaped = buildDesktopQuitDialogHtml({
  title: '<unsafe>',
  message: 'a & b',
  detail: '"quoted"',
  keepRunning: "don't",
  confirm: 'go',
})
assert.match(escaped, /&lt;unsafe&gt;/)
assert.match(escaped, /a &amp; b/)
assert.doesNotMatch(escaped, /<unsafe>/)

console.log('desktop quit dialog tests passed')
