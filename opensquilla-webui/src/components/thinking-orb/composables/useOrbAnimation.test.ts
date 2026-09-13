// @vitest-environment happy-dom
import { createApp, nextTick, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useOrbAnimation } from './useOrbAnimation'
import { modeRegistry } from '../registry'
import { resetClock } from '../engine/clock'
import { registerBuiltinModes, type OrbState } from '../presets'

const mountedApps: ReturnType<typeof createApp>[] = []

let tickCb: FrameRequestCallback | null = null

function fakeCtx() {
  return {
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    strokeStyle: '',
    fillStyle: '',
    lineWidth: 1,
    globalAlpha: 1,
  }
}

function makeCanvas(getContext: () => CanvasRenderingContext2D | null) {
  const canvas = document.createElement('canvas')
  canvas.getContext = getContext as unknown as HTMLCanvasElement['getContext']
  Object.defineProperty(canvas, 'width', { value: 0, writable: true })
  Object.defineProperty(canvas, 'height', { value: 0, writable: true })
  return canvas
}

/** Mount a throwaway host so onMounted/onUnmounted lifecycle hooks run. */
function setupOrb(canvas: HTMLCanvasElement, reduced = false) {
  const canvasRef = ref<HTMLCanvasElement | null>(canvas)
  const state = ref<OrbState>('working')
  const size = ref(28)
  const dark = ref(true)
  const speed = ref(1)
  const paused = ref(false)
  const reducedRef = ref(reduced)

  let controls: ReturnType<typeof useOrbAnimation> | null = null
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    setup() {
      controls = useOrbAnimation(canvasRef, state, size, dark, speed, paused, reducedRef)
      return () => null
    },
  })
  mountedApps.push(app)
  app.mount(host)
  return { controls: controls!, reducedRef, state, paused }
}

/** Manually advance the fake rAF clock by one tick. */
function pump(now: number) {
  const cb = tickCb
  tickCb = null
  cb?.(now)
}

beforeEach(() => {
  document.body.innerHTML = ''
  tickCb = null
  vi.stubGlobal('requestAnimationFrame', vi.fn((cb: FrameRequestCallback) => {
    tickCb = cb
    return 1
  }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  vi.stubGlobal('IntersectionObserver', undefined)
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
  resetClock() // engine/clock is a module-level singleton; hard-isolate tests
  vi.unstubAllGlobals()
  // Undo any test-local registry surgery (patched instances live in _modes).
  modeRegistry.unregister('web')
  registerBuiltinModes()
})

describe('useOrbAnimation degraded modes', () => {
  it('reports renderError when the 2D context is unavailable', async () => {
    const canvas = makeCanvas(() => null)
    const { controls } = setupOrb(canvas)
    await nextTick()
    expect(controls!.renderError.value).toBe(true)
    expect(controls!.isRunning.value).toBe(false)
  })

  it('joins the shared clock in motion mode and leaves it on unmount', async () => {
    const ctx = fakeCtx()
    const canvas = makeCanvas(() => ctx as unknown as CanvasRenderingContext2D)
    const { controls } = setupOrb(canvas)
    await nextTick()
    expect(controls!.isRunning.value).toBe(true)
    expect(requestAnimationFrame).toHaveBeenCalled()
    expect(ctx.clearRect).toHaveBeenCalled()

    mountedApps.pop()?.unmount()
    expect(controls!.isRunning.value).toBe(false)
  })

  it('reduced motion paints exactly one static frame and never joins the clock', async () => {
    const ctx = fakeCtx()
    const canvas = makeCanvas(() => ctx as unknown as CanvasRenderingContext2D)
    const { controls } = setupOrb(canvas, true)
    await nextTick()
    expect(controls!.isRunning.value).toBe(false)
    expect(controls!.renderError.value).toBe(false)
    expect(requestAnimationFrame).not.toHaveBeenCalled()
    // exactly one frame drawn…
    expect(ctx.clearRect).toHaveBeenCalledTimes(1)
  })

  it('static frame repaints when the orb state changes under reduced motion', async () => {
    const ctx = fakeCtx()
    const canvas = makeCanvas(() => ctx as unknown as CanvasRenderingContext2D)
    const { controls, state } = setupOrb(canvas, true)
    await nextTick()
    const before = ctx.clearRect.mock.calls.length
    state.value = 'searching'
    await nextTick()
    expect(ctx.clearRect.mock.calls.length).toBeGreaterThan(before)
    expect(controls!.isRunning.value).toBe(false)
  })

  it('toggling reduced motion at runtime stops the loop', async () => {
    const ctx = fakeCtx()
    const canvas = makeCanvas(() => ctx as unknown as CanvasRenderingContext2D)
    const { controls, reducedRef } = setupOrb(canvas, false)
    await nextTick()
    expect(controls!.isRunning.value).toBe(true)
    reducedRef.value = true
    await nextTick()
    expect(controls!.isRunning.value).toBe(false)
    expect(cancelAnimationFrame).toHaveBeenCalled()
  })

  it('three consecutive mode failures escalate to renderError instead of an endless blank canvas', async () => {
    const ctx = fakeCtx()
    const canvas = makeCanvas(() => ctx as unknown as CanvasRenderingContext2D)
    const { controls } = setupOrb(canvas)
    await nextTick()
    // Mount paints frame #1 with the healthy builtin mode. Now sabotage the
    // cached instance (registry.get returns it on every following frame):
    // setup-time registerBuiltinModes() would override any constructor we
    // inject before mount, so patch the instance instead.
    const cached = modeRegistry.get('working')
    cached!.update = () => { throw new Error('mode exploded') }
    // clock tick #1 → failure #1, #2 → failure #2 (still tolerating),
    // #3 → escalate to renderError.
    pump(1000)
    expect(controls!.renderError.value).toBe(false)
    pump(1200)
    expect(controls!.renderError.value).toBe(false)
    pump(1400)
    expect(controls!.renderError.value).toBe(true)
    expect(controls!.isRunning.value).toBe(false)
  })

  it('hides the canvas after a render error (host sees the v-if removal)', async () => {
    // ThinkingOrb contract: renderError → emit('error') and unmount the
    // canvas so the host falls back to its CSS indicator. Asserted at the
    // composable level: the flag flips and stays flipped.
    const canvas = makeCanvas(() => null)
    const { controls } = setupOrb(canvas)
    await nextTick()
    expect(controls!.renderError.value).toBe(true)
    pump(1000)
    expect(controls!.renderError.value).toBe(true)
  })
})
