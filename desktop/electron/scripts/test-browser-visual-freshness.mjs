import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { mock, test } from 'node:test'
import { DesktopBrowserError } from '../dist/desktop-browser.js'

// Exercise the compiled driver with a synthetic renderer and image decoder.
// Real bitmap/canvas checks are covered by the isolated Electron attachment test.
const runtime = await readFile(new URL('../dist/browser-playwright.js', import.meta.url), 'utf8')
const source = runtime.replace(/^import .*\n/gm, '').replace(/^export /gm, '')
const nativeImage = { createFromBuffer: bytes => ({ crop: () => ({ toBitmap: () => bytes }) }) }
const Driver = new Function('DesktopBrowserError', 'nativeImage', `${source}\nreturn BrowserPlaywrightDriver`)(DesktopBrowserError, nativeImage)

function fixture() {
  const viewport = { width: 200, height: 100, deviceScaleFactor: 1, scrollX: 0, scrollY: 0, revision: 1, surfaceWidth: 200, surfaceHeight: 100 }
  const pointer = { currentPosition: () => undefined, update: async () => {} }
  const driver = new Driver({}, () => true, pointer)
  const state = { presses: 0, captures: 0, points: [], authorized: true, pixels: 'original pixels', beforePress: undefined, failureAfterPress: false }
  const page = {
    evaluate: async () => ({ width: 200, height: 100, reducedMotion: true }),
    mouse: {
      click: async (x, y) => {
        state.beforePress?.()
        await driver.transport.mouseCommitGuard?.()
        state.presses++
        state.points.push([x, y])
        if (state.failureAfterPress) throw new Error('Synthetic response lost after input')
      },
    },
  }
  driver.run = async (guard, _signal, work) => { guard(); return await work(page) }
  driver.isSurfaceVisible = () => true
  driver.viewport = async () => ({ ...viewport })
  driver.captureScreenshot = async () => {
    state.captures++
    return { width: 200, height: 100, dataBase64: Buffer.from(state.pixels).toString('base64') }
  }
  driver.transport = {}
  driver.mousePositionPage = page
  driver.latestVisual = {
    observationId: 'observation-original', imageId: 'image-original', documentEpoch: 0, generation: 4,
    viewport: { ...viewport }, capturedAt: Date.now(), imageWidth: 200, imageHeight: 100,
    dataBase64: Buffer.from(state.pixels).toString('base64'),
  }
  driver.observe = async () => ({ observation: { observationId: 'observation-new', imageStatus: 'available' } })
  const request = { action: 'click', x: 50, y: 50, observationId: 'observation-original', imageId: 'image-original' }
  const signal = new AbortController().signal
  const guard = () => { if (!state.authorized) throw new DesktopBrowserError('TARGET_NOT_FOUND', 'The target is no longer authorized.') }
  const act = (overrides = {}, generation = 4) => driver.act({ ...request, ...overrides }, generation, guard, signal)
  return { driver, viewport, state, request, signal, act }
}

test('unchanged screenshot remains actionable after 180 seconds and unrelated DOM updates', async () => {
  mock.timers.enable({ apis: ['Date'], now: 1000 })
  try {
    const { act, state, viewport } = fixture()
    mock.timers.tick(180_001)
    viewport.revision += 50
    assert.equal((await act()).performed, true)
    assert.equal(state.presses, 1)
    assert.equal(state.captures, 2, 'validate once before moving and again before pressing')
  } finally { mock.timers.reset() }
})

for (const [name, change, reason] of [
  ['missing observation', f => { f.driver.latestVisual = undefined }, 'observation_missing'],
  ['different image', f => { f.request.imageId = 'image-other' }, 'observation_mismatch'],
  ['missing image identity', f => { delete f.request.imageId }, 'observation_mismatch'],
  ['different observation', f => { f.request.observationId = 'observation-other' }, 'observation_mismatch'],
  ['new document', f => { f.driver.documentEpoch++ }, 'document_changed'],
  ['new generation', f => { f.driver.latestVisual.generation-- }, 'generation_changed'],
  ['viewport resized', f => { f.viewport.width-- }, 'viewport_changed'],
  ['display scale changed', f => { f.viewport.deviceScaleFactor++ }, 'viewport_changed'],
  ['native surface resized', f => { f.viewport.surfaceWidth-- }, 'viewport_changed'],
  ['scrolled', f => { f.viewport.scrollY++ }, 'scroll_changed'],
  ['target pixels changed', f => { f.state.pixels = 'different pixels' }, 'target_pixels_changed'],
]) {
  test(`${name} rejects old coordinates without input`, async () => {
    const f = fixture()
    change(f)
    await assert.rejects(f.act(), error => error.code === 'STALE_OBSERVATION'
      && error.details.observationReason === reason && error.details.outcome === 'not_started'
      && error.details.retryable === false)
    assert.equal(f.state.presses, 0)
  })
}

for (const legacyMetadata of [{}, { nativeImageEvidence: [] }, { nativeImageEvidence: ['unrelated-image'] }]) {
  test(`valid screenshot coordinates do not depend on model receipts: ${JSON.stringify(legacyMetadata)}`, async () => {
    const f = fixture()
    assert.equal((await f.act(legacyMetadata)).performed, true)
    assert.deepEqual(f.state.points, [[50, 50]])
    assert.equal(f.state.captures, 2)
  })
}

test('legacy model receipts cannot authorize a missing browser observation', async () => {
  const f = fixture()
  f.driver.latestVisual = undefined
  await assert.rejects(f.act({ nativeImageEvidence: ['image-original'] }), error =>
    error.code === 'STALE_OBSERVATION' && error.details.observationReason === 'observation_missing')
  assert.equal(f.state.presses, 0)
})

test('hidden tabs still reject valid screenshot coordinates', async () => {
  const f = fixture()
  f.driver.isSurfaceVisible = () => false
  await assert.rejects(f.act(), error => error.code === 'VISUAL_TARGET_HIDDEN')
  assert.equal(f.state.presses, 0)
})

test('target authorization is checked before input and again before pressing', async () => {
  for (const revokeBeforePress of [false, true]) {
    const f = fixture()
    if (revokeBeforePress) f.state.beforePress = () => { f.state.authorized = false }
    else f.state.authorized = false
    await assert.rejects(f.act(), error => error.code === 'TARGET_NOT_FOUND')
    assert.equal(f.state.presses, 0)
  }
})

test('target changing during pointer movement is checked again before press', async () => {
  const f = fixture()
  f.state.beforePress = () => { f.state.pixels = 'changed during movement' }
  await assert.rejects(f.act(), error => error.code === 'STALE_OBSERVATION'
    && error.details.observationReason === 'target_pixels_changed'
    && error.details.outcome === 'not_started')
  assert.equal(f.state.presses, 0)
})

test('DOM target changing after capture is resampled before input', async () => {
  const f = fixture()
  let reads = 0
  f.driver.viewport = async () => {
    if (++reads === 3) {
      f.viewport.revision++
      f.state.pixels = 'target changed after first capture'
    }
    return { ...f.viewport }
  }
  await assert.rejects(f.act(), error => error.code === 'STALE_OBSERVATION'
    && error.details.observationReason === 'target_pixels_changed')
  assert.equal(f.state.presses, 0)
  assert.equal(f.state.captures, 2, 'resample the changed capture interval before deciding')
})

test('an unrelated update during capture permits one stable resample', async () => {
  const f = fixture()
  let reads = 0
  f.driver.viewport = async () => {
    if (++reads === 3) f.viewport.revision++
    return { ...f.viewport }
  }
  assert.equal((await f.act()).performed, true)
  assert.equal(f.state.presses, 1)
  assert.equal(f.state.captures, 3)
})

test('continually changing capture is bounded and never presses', async () => {
  const f = fixture()
  f.driver.viewport = async () => ({ ...f.viewport, revision: f.viewport.revision++ })
  await assert.rejects(f.act(), error => error.code === 'STALE_OBSERVATION'
    && error.details.observationReason === 'validation_unstable'
    && error.details.outcome === 'not_started')
  assert.equal(f.state.presses, 0)
  assert.equal(f.state.captures, 2)
})

test('stale batch preserves the reason and returns fresh observation without replaying input', async () => {
  const f = fixture()
  f.viewport.scrollX++
  const result = await f.driver.batch({ ...f.request, actions: [f.request] }, 4, () => {}, f.signal)
  assert.equal(result.execution.state, 'failed')
  assert.equal(result.execution.actions[0].observationReason, 'scroll_changed')
  assert.equal(result.execution.actions[0].outcome, 'not_started')
  assert.equal(result.execution.actions[0].recovery, 'observe')
  assert.equal(result.observation.observationId, 'observation-new')
  assert.equal(f.state.presses, 0)
})

test('failure after input remains unknown instead of becoming safe to repeat', async () => {
  const f = fixture()
  f.state.failureAfterPress = true
  const result = await f.driver.batch({ ...f.request, actions: [f.request] }, 4, () => {}, f.signal)
  assert.equal(result.execution.actions[0].outcome, 'unknown')
  assert.equal(result.execution.actions[0].retryable, false)
  assert.equal(f.state.presses, 1)
})
