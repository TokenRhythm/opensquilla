import assert from 'node:assert/strict'

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

console.log('desktop keyboard zoom contract checks passed')
