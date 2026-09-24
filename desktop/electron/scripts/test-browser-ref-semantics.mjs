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
  constructor(code, message) { super(message); this.code = code }
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
  driver.transport = {}
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
} finally {
  await browser.close()
}
