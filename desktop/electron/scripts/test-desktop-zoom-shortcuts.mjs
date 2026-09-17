import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

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

for (const { description, input, platform, expected } of [
  { description: 'macOS Command plus', input: { meta: true, key: '+', code: 'Equal' }, platform: 'darwin', expected: 'in' },
  { description: 'macOS Command equals', input: { meta: true, key: '=', code: 'Equal' }, platform: 'darwin', expected: 'in' },
  { description: 'macOS Command minus', input: { meta: true, key: '-', code: 'Minus' }, platform: 'darwin', expected: 'out' },
  { description: 'macOS Command zero', input: { meta: true, key: '0', code: 'Digit0' }, platform: 'darwin', expected: 'reset' },
  { description: 'Windows Control plus', input: { control: true, key: '+', code: 'Equal' }, platform: 'win32', expected: 'in' },
  { description: 'Windows Control equals', input: { control: true, key: '=', code: 'Equal' }, platform: 'win32', expected: 'in' },
  { description: 'Windows Control minus', input: { control: true, key: '-', code: 'Minus' }, platform: 'win32', expected: 'out' },
  { description: 'Windows Control zero', input: { control: true, key: '0', code: 'Digit0' }, platform: 'win32', expected: 'reset' },
  { description: 'Linux Control plus', input: { control: true, key: '+', code: 'Equal' }, platform: 'linux', expected: 'in' },
  { description: 'Linux Control equals', input: { control: true, key: '=', code: 'Equal' }, platform: 'linux', expected: 'in' },
  { description: 'Linux Control minus', input: { control: true, key: '-', code: 'Minus' }, platform: 'linux', expected: 'out' },
  { description: 'Linux Control zero', input: { control: true, key: '0', code: 'Digit0' }, platform: 'linux', expected: 'reset' },
  { description: 'numpad add', input: { control: true, code: 'NumpadAdd' }, platform: 'linux', expected: 'in' },
  { description: 'numpad subtract', input: { control: true, code: 'NumpadSubtract' }, platform: 'linux', expected: 'out' },
  { description: 'numpad zero', input: { control: true, code: 'Numpad0' }, platform: 'linux', expected: 'reset' },
  { description: 'Control is not the macOS primary modifier', input: { control: true, key: '=', code: 'Equal' }, platform: 'darwin', expected: null },
  { description: 'Meta is not the Windows primary modifier', input: { meta: true, key: '=', code: 'Equal' }, platform: 'win32', expected: null },
  { description: 'Alt suppresses shortcuts', input: { control: true, alt: true, key: '=', code: 'Equal' }, platform: 'linux', expected: null },
  { description: 'keyUp does not invoke shortcuts', input: { control: true, key: '=', code: 'Equal', type: 'keyUp' }, platform: 'linux', expected: null },
]) {
  assert.equal(desktopZoomCommandForInput(keyInput(input), platform), expected, description)
}
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

if (!process.argv.includes('--contracts-only')) {
  const { _electron: electron } = await import('playwright')
  const fixtureRoot = fileURLToPath(
    new URL('./fixtures/desktop-zoom-shortcuts', import.meta.url),
  )
  let desktopApp
  try {
    desktopApp = await electron.launch({ args: [fixtureRoot] })
    const page = await desktopApp.firstWindow({ timeout: 30_000 })
    await page.waitForSelector('main')
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
      const keyCode = key === 'Equal' ? '=' : key === 'Minus' ? '-' : '0'
      await desktopApp.evaluate(({ BrowserWindow }, input) => {
        BrowserWindow.getAllWindows()[0]?.webContents.sendInputEvent(input)
      }, {
        type: 'keyDown',
        keyCode,
        modifiers: [primaryModifier === 'Meta' ? 'meta' : 'control'],
      })
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
}

console.log('desktop keyboard zoom contract checks passed')
