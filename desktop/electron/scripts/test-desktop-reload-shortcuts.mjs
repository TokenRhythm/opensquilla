import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'

import {
  desktopReloadCommandForInput,
  installDesktopReloadShortcuts,
} from '../dist/desktop-reload-shortcuts.js'

function keyInput(overrides = {}) {
  return {
    type: 'keyDown',
    key: '',
    code: '',
    control: false,
    alt: false,
    meta: false,
    shift: false,
    ...overrides,
  }
}

for (const { description, input, platform, expected } of [
  { description: 'Windows Control r', input: { control: true, key: 'r', code: 'KeyR' }, platform: 'win32', expected: 'reload' },
  { description: 'Windows Control uppercase r', input: { control: true, key: 'R' }, platform: 'win32', expected: 'reload' },
  { description: 'Linux Control r', input: { control: true, key: 'r', code: 'KeyR' }, platform: 'linux', expected: 'reload' },
  { description: 'macOS is handled by the native menu', input: { control: true, key: 'r', code: 'KeyR' }, platform: 'darwin', expected: null },
  { description: 'missing Control', input: { key: 'r', code: 'KeyR' }, platform: 'win32', expected: null },
  { description: 'Alt suppresses reload', input: { control: true, alt: true, key: 'r', code: 'KeyR' }, platform: 'win32', expected: null },
  { description: 'Meta suppresses reload', input: { control: true, meta: true, key: 'r', code: 'KeyR' }, platform: 'win32', expected: null },
  { description: 'Shift suppresses reload', input: { control: true, shift: true, key: 'r', code: 'KeyR' }, platform: 'win32', expected: null },
  { description: 'keyUp does not reload', input: { type: 'keyUp', control: true, key: 'r', code: 'KeyR' }, platform: 'win32', expected: null },
  { description: 'other keys do not reload', input: { control: true, key: 't', code: 'KeyT' }, platform: 'win32', expected: null },
]) {
  assert.equal(
    desktopReloadCommandForInput(keyInput(input), platform),
    expected,
    description,
  )
}

if (process.platform !== 'darwin') {
  let listener
  let removedListener = null
  let reloads = 0
  const inputContents = {
    on(eventName, nextListener) {
      assert.equal(eventName, 'before-input-event')
      listener = nextListener
    },
    removeListener(eventName, nextListener) {
      assert.equal(eventName, 'before-input-event')
      removedListener = nextListener
    },
  }
  const reloadContents = { reload() { reloads += 1 } }
  let prevented = false
  const dispose = installDesktopReloadShortcuts(
    inputContents,
    reloadContents,
    () => false,
  )
  listener({ preventDefault() { prevented = true } }, keyInput({ control: true, key: 'r', code: 'KeyR' }))
  assert.equal(prevented, false, 'onboarding guard must block reload')
  assert.equal(reloads, 0, 'onboarding guard must not reload')
  dispose()
  assert.equal(removedListener, listener, 'dispose must remove the input listener')
}

const mainSource = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
assert.match(
  mainSource,
  /installDesktopReloadShortcuts\(\s*window\.webContents,\s*window\.webContents,\s*\(\) => currentOnboardingWindow\(\) === null,?\s*\)/,
)

if (!process.argv.includes('--contracts-only')) {
  const { _electron: electron } = await import('playwright')
  const fixtureRoot = fileURLToPath(
    new URL('./fixtures/desktop-reload-shortcuts', import.meta.url),
  )
  let desktopApp
  try {
    desktopApp = await electron.launch({ args: [fixtureRoot] })
    const page = await desktopApp.firstWindow({ timeout: 30_000 })
    await page.waitForSelector('#reload-count')
    await page.waitForFunction(() => document.querySelector('#reload-count')?.textContent === 'Reload count 1')

    await desktopApp.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows()[0]?.webContents.sendInputEvent({
        type: 'keyDown',
        keyCode: 'R',
        modifiers: ['control'],
      })
    })
    await page.waitForFunction(() => document.querySelector('#reload-count')?.textContent === 'Reload count 2')
  } finally {
    await desktopApp?.close().catch(() => {})
  }
}

console.log('desktop keyboard reload contract checks passed')
