import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import ts from '@typescript/typescript6'
import { chromium } from 'playwright'

// Compile in memory so this focused test never rewrites a running desktop's
// compiled modules. Snapshot and element actions use a real headless DOM.
const input = await readFile(new URL('../src/browser-playwright.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(input, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
})
const source = outputText.replace(/^import .*\n/gm, '').replace(/^export /gm, '')
class BrowserFailure extends Error {
  constructor(code, message, status = 409, details = {}) {
    super(message); this.code = code; this.status = status; this.details = details
  }
}
const Driver = new Function('DesktopBrowserError', 'randomUUID', `${source}\nreturn BrowserPlaywrightDriver`)(BrowserFailure, randomUUID)
const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage()
  await page.setContent(`<!doctype html><body>
    <input type="button" value="Show preferences" onclick="window.opened=(window.opened||0)+1">
    <input type="submit" value="Save settings"><input type="reset" value="Reset settings">
    <label for="display-name">Display name</label><input id="display-name" value="Initial name">
    <input type="search" placeholder="Search settings">
    <label><input type="checkbox" value="yes">Receive updates</label>
    <label for="secret">Secret</label><input id="secret" type="password" value="synthetic-private-value">
    <input type="file" aria-label="Upload attachment">
    <input aria-label="Locked field" readonly value="fixed">
    <input aria-label="Aria locked field" aria-readonly="true" value="fixed">
    <fieldset disabled><input aria-label="Disabled field" value="disabled"></fieldset>
    <span id="given-label">Given</span><span id="name-label">name</span>
    <input aria-labelledby="given-label name-label" aria-label="Overridden name">
    <textarea aria-label="Notes">Draft</textarea>
    <label for="choice">Delivery method</label><select id="choice"><option value="mail">Mail</option></select>
    <div role="textbox" contenteditable="true" aria-label="Rich text">Draft text</div>
    <div role="textbox" aria-label="Noneditable text">Fixed text</div>
    <button aria-label="Accessible caption">Visible caption</button>
  </body>`)
  const driver = new Driver({}, () => false, {})
  driver.transport = { releaseMouseButtons: async () => {} }
  driver.run = async (guard, signal, work) => {
    guard()
    signal.throwIfAborted()
    return await work(page)
  }
  const signal = new AbortController().signal
  const guard = () => {}
  const snapshot = await driver.snapshot(1, guard, signal)
  const ref = name => {
    const result = snapshot.refs.find(value => value.name === name && !['label', 'span'].includes(value.tagName))
    assert.ok(result, `Expected named control: ${name}`)
    return result
  }
  for (const [name, type] of [['Show preferences', 'button'], ['Save settings', 'submit'], ['Reset settings', 'reset']]) {
    const control = ref(name)
    assert.equal(control.role, 'button')
    assert.equal(control.type, type)
    assert.equal(control.value, name)
    assert.equal(control.editable, false)
    assert.equal(await page.getByRole('button', { name, exact: true }).count(), 1)
  }
  assert.equal(ref('Display name').role, 'textbox')
  assert.equal(ref('Display name').type, 'text')
  assert.equal(ref('Display name').editable, true)
  assert.equal(ref('Display name').value, 'Initial name')
  assert.equal(ref('Search settings').role, 'searchbox')
  assert.equal(ref('Receive updates').role, 'checkbox')
  assert.equal(ref('Receive updates').editable, false)
  assert.equal(ref('Secret').editable, true)
  assert.equal(Object.hasOwn(ref('Secret'), 'value'), false)
  assert.equal(Object.hasOwn(ref('Upload attachment'), 'value'), false)
  assert.equal(ref('Upload attachment').editable, false)
  assert.equal(ref('Locked field').readonly, true)
  assert.equal(ref('Locked field').editable, false)
  assert.equal(ref('Aria locked field').readonly, true)
  assert.equal(ref('Aria locked field').editable, false)
  assert.equal(ref('Disabled field').disabled, true)
  assert.equal(ref('Disabled field').editable, false)
  assert.equal(ref('Given name').role, 'textbox')
  assert.equal(snapshot.refs.some(value => value.name === 'Overridden name'), false)
  assert.equal(ref('Notes').editable, true)
  assert.equal(ref('Notes').value, 'Draft')
  assert.equal(ref('Delivery method').role, 'combobox')
  assert.equal(ref('Delivery method').editable, false)
  assert.equal(ref('Delivery method').value, 'mail')
  assert.equal(ref('Rich text').editable, true)
  assert.equal(ref('Noneditable text').editable, false)
  assert.equal(ref('Accessible caption').role, 'button')

  await driver.act({ action: 'fill', ref: ref('Display name').ref, text: 'Updated name' }, 1, guard, signal)
  await driver.act({ action: 'click', ref: ref('Show preferences').ref }, 1, guard, signal)
  assert.equal(await page.locator('#display-name').inputValue(), 'Updated name')
  assert.equal(await page.evaluate(() => window.opened), 1)
  const refreshed = await driver.snapshot(1, guard, signal)
  assert.equal(refreshed.refs.find(value => value.name === 'Display name' && value.role === 'textbox').value, 'Updated name')
  assert.equal(JSON.stringify(refreshed.refs).includes('synthetic-private-value'), false)
  driver.invalidate()
  console.log('Browser ref semantics passed: native input buttons, accessible names, field state, editability, secret omission and executable refs.')

  for (const [html, change, action] of [
    ['<button>First target</button>', node => { node.textContent = 'Second target' }, 'click'],
    ['<button aria-label="First target">Caption</button>', node => { node.setAttribute('aria-label', 'Second target') }, 'click'],
    ['<button>First target</button>', node => { node.setAttribute('role', 'link') }, 'click'],
    ['<input aria-label="First target">', node => { node.type = 'password' }, 'fill'],
    ['<a href="/first">First target</a>', node => { node.setAttribute('href', '/second') }, 'click'],
    ['<input type="button" value="First target">', node => { node.value = 'Second target' }, 'click'],
    ['<label for="field">First target</label><input id="field">', node => { node.labels[0].textContent = 'Second target' }, 'fill'],
  ]) {
    await page.setContent(html)
    const observed = await driver.snapshot(1, guard, signal)
    const control = observed.refs.find(value => value.name === 'First target' && ['button', 'input', 'a'].includes(value.tagName))
    assert.ok(control)
    await page.locator('button,input,a').evaluate(change)
    await assert.rejects(driver.act({ action, ref: control.ref, text: 'synthetic-entry' }, 1, guard, signal),
      error => error.code === 'STALE_ELEMENT' && error.details.outcome === 'not_started')
  }
  await page.setContent('<input aria-label="Entry"><div contenteditable="true">Draft</div><p id="status">Idle</p>')
  const editable = await driver.snapshot(1, guard, signal)
  const status = editable.refs.find(value => value.name === 'Idle')
  assert.ok(editable.refs.every(value => !Object.hasOwn(value, 'identity')))
  for (const control of editable.refs.filter(value => value.editable)) {
    await driver.act({ action: 'fill', ref: control.ref, text: 'First edit' }, 1, guard, signal)
    await page.locator('#status').evaluate(node => { node.textContent = 'Unrelated update' })
    await driver.act({ action: 'fill', ref: control.ref, text: 'Second edit' }, 1, guard, signal)
  }
  assert.equal(await page.locator('input').inputValue(), 'Second edit')
  assert.equal(await page.locator('[contenteditable]').innerText(), 'Second edit')
  assert.equal((await driver.readText({ ref: status.ref }, 1, guard, signal)).text, 'Unrelated update')
  console.log('Browser ref identity rejects changed target semantics while preserving field edits and unrelated updates.')

  await page.setContent(`<!doctype html><body>
    <p>Outside marker</p><button onclick="document.body.dataset.clicked='outside'">Same control</button>
    ${Array.from({ length: 197 }, (_, index) => `<button>Head button ${index}</button>`).join('')}
    <section id="tail"><p>Tail prose</p>
      <button id="target" onclick="document.body.dataset.clicked='tail'">Same control</button>
    </section><button>After marker</button>
    <iframe id="child-frame" srcdoc='<button id="child-only">Child control</button>'></iframe>
  </body>`)
  const scopedSnapshot = selector => driver.snapshot(2, guard, signal, selector)
  const wholePage = await driver.snapshot(2, guard, signal)
  assert.equal(wholePage.refs.length, 160)
  assert.equal(wholePage.truncated, true)
  assert.equal(wholePage.refs.filter(value => value.name === 'Same control').length, 1)
  const tail = await scopedSnapshot('#tail')
  assert.equal(tail.truncated, false)
  assert.match(tail.text, /Tail prose/)
  assert.doesNotMatch(tail.text, /Outside marker|Head button|After marker|Child control/)
  const tailControl = tail.refs.find(value => value.role === 'button' && value.name === 'Same control')
  assert.ok(tailControl, 'The control after the full-page ref limit must be available within its scope')
  assert.equal(tail.refs.some(value => value.name.startsWith('Head button')), false)
  await driver.act({ action: 'click', ref: tailControl.ref }, 2, guard, signal)
  assert.equal(await page.locator('body').getAttribute('data-clicked'), 'tail')

  const targetOnly = await scopedSnapshot('#target')
  assert.equal(targetOnly.refs.length, 1, 'An actionable scope root must itself receive a ref')
  assert.equal(targetOnly.refs[0].name, 'Same control')
  await page.locator('body').evaluate(node => { node.dataset.clicked = '' })
  await driver.act({ action: 'click', ref: targetOnly.refs[0].ref }, 2, guard, signal)
  assert.equal(await page.locator('body').getAttribute('data-clicked'), 'tail')
  for (const [selector, code] of [
    ['#missing', 'SCOPE_NOT_FOUND'], ['button', 'SCOPE_AMBIGUOUS'],
    ['[', 'INVALID_SCOPE'], ['#tail >> text=Same control', 'INVALID_SCOPE'],
    ['#child-frame', 'SCOPE_UNSUPPORTED'], ['#child-only', 'SCOPE_NOT_FOUND'],
  ]) {
    await assert.rejects(scopedSnapshot(selector), error => {
      assert.equal(error.code, code)
      assert.equal(error.details.outcome, 'not_started')
      assert.equal(error.details.recovery, 'inspect')
      return true
    })
    assert.equal(driver.anchors.size, 0, 'A failed scope must not fall back to full-page refs')
  }

  await page.setContent('<div id="shadow-host"></div>')
  await page.locator('#shadow-host').evaluate(node => {
    node.attachShadow({ mode: 'open' }).innerHTML = '<button>Shadow control</button>'
  })
  const shadow = await scopedSnapshot('#shadow-host')
  assert.match(shadow.text, /Shadow control/)
  assert.ok(shadow.refs.some(value => value.role === 'button' && value.name === 'Shadow control'))

  await page.setContent(`<section id="large">${Array.from({ length: 201 }, (_, index) => `<button>Local button ${index}</button>`).join('')}</section>`)
  const large = await scopedSnapshot('#large')
  assert.equal(large.refs.length, 160)
  assert.equal(large.truncated, true, 'Scoped snapshots must retain the existing ref ceiling')

  await page.setContent('<section id="changing"><button>Original control</button></section>')
  const frame = page.mainFrame()
  const originalLocator = frame.locator
  frame.locator = function(selector, options) {
    const locator = originalLocator.call(this, selector, options)
    if (selector === 'css=#changing') {
      const originalSnapshot = locator.ariaSnapshot.bind(locator)
      locator.ariaSnapshot = async options => {
        const text = await originalSnapshot(options)
        await page.locator('#changing').evaluate(node => node.replaceWith(node.cloneNode(true)))
        return text
      }
    }
    return locator
  }
  try {
    await assert.rejects(scopedSnapshot('#changing'), error => error.code === 'SCOPE_CHANGED')
    assert.equal(driver.anchors.size, 0, 'A replaced scope root cannot publish refs from another root')
  } finally { frame.locator = originalLocator }

  const originalEvaluateHandle = frame.evaluateHandle
  frame.evaluateHandle = async function(...args) {
    const collection = await originalEvaluateHandle.apply(this, args)
    await page.locator('#changing').evaluate(node => node.replaceWith(node.cloneNode(true)))
    return collection
  }
  try {
    await assert.rejects(scopedSnapshot('#changing'), error => error.code === 'SCOPE_CHANGED')
    assert.equal(driver.anchors.size, 0, 'Late scope replacement must clear partially bound refs')
  } finally { frame.evaluateHandle = originalEvaluateHandle }
  driver.invalidate()
  console.log('Browser scoped snapshots passed: bounded local controls, matching text, root refs, strict CSS, main-frame boundaries and root replacement.')
} finally {
  await browser.close()
}
