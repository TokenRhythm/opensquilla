import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

import {
  desktopZoomCommandForInput,
  desktopZoomFactor,
} from '../dist/desktop-zoom-shortcuts.js'

function keyInput(overrides = {}) {
  return {
    type: 'keyDown',
    key: '',
    code: '',
    control: false,
    alt: false,
    meta: false,
    ...overrides,
  }
}

assert.equal(desktopZoomCommandForInput(keyInput({ meta: true, key: '=', code: 'Equal' }), 'darwin'), 'in')
assert.equal(desktopZoomCommandForInput(keyInput({ control: true, key: '-', code: 'Minus' }), 'win32'), 'out')
assert.equal(desktopZoomCommandForInput(keyInput({ control: true, key: '0', code: 'Digit0' }), 'linux'), 'reset')
assert.equal(desktopZoomCommandForInput(keyInput({ control: true, alt: true, key: '=', code: 'Equal' }), 'linux'), null)
assert.equal(desktopZoomCommandForInput(keyInput({ control: true, key: '=', code: 'Equal', type: 'keyUp' }), 'linux'), null)
assert.equal(desktopZoomFactor(1, 'in'), 1.2)
assert.equal(desktopZoomFactor(1, 'out'), 1 / 1.2)
assert.equal(desktopZoomFactor(2, 'reset'), 1)
assert.equal(desktopZoomFactor(3, 'in'), 3)
assert.equal(desktopZoomFactor(0.5, 'out'), 0.5)

const mainSource = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const workbenchSource = readFileSync(
  new URL('../src/native-workbench-surface.ts', import.meta.url),
  'utf8',
)
assert.match(
  mainSource,
  /installDesktopZoomShortcuts\(\s*window\.webContents,\s*window\.webContents,\s*\(\) => nativeWorkbenchSurfaces\.refreshBounds\(window\),?\s*\)/,
)
assert.match(
  workbenchSource,
  /refreshBounds\(owner: BrowserWindow\): void \{\s*this\.reapplyActiveBounds\(owner\)\s*\}/,
)

const fixtureRoot = fileURLToPath(
  new URL('./fixtures/desktop-zoom-shortcuts', import.meta.url),
)
let desktopApp
try {
  desktopApp = await electron.launch({ args: [fixtureRoot] })
  const page = await desktopApp.firstWindow({ timeout: 30_000 })
  const platform = await desktopApp.evaluate(() => process.platform)
  const primaryModifier = platform === 'darwin' ? 'Meta' : 'Control'
  const zoomFactor = () => desktopApp.evaluate(({ BrowserWindow }) => (
    BrowserWindow.getAllWindows()[0]?.webContents.getZoomFactor()
  ))
  const waitForFactor = async expected => {
    for (let attempt = 0; attempt < 50; attempt += 1) {
      const actual = await zoomFactor()
      if (Math.abs(actual - expected) < 1e-6) return
      await new Promise(resolve => setTimeout(resolve, 20))
    }
    assert.ok(Math.abs((await zoomFactor()) - expected) < 1e-6)
  }
  const pressShortcut = async key => {
    await page.keyboard.down(primaryModifier)
    await page.keyboard.press(key)
    await page.keyboard.up(primaryModifier)
  }

  await pressShortcut('Equal')
  await waitForFactor(1.2)
  await pressShortcut('Minus')
  await waitForFactor(1)
  await pressShortcut('Equal')
  await waitForFactor(1.2)
  await pressShortcut('Digit0')
  await waitForFactor(1)
} finally {
  await desktopApp?.close().catch(() => {})
}

console.log('desktop keyboard zoom contract checks passed')
