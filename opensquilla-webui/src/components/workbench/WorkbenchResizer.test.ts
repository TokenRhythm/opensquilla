// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import WorkbenchResizer from './WorkbenchResizer.vue'

const apps: App<Element>[] = []

async function mountResizer() {
  const width = ref(520)
  const commit = vi.fn<(value: number) => void>()
  const preview = vi.fn<(value: number) => void>()
  const cancel = vi.fn<(value: number) => void>()
  const reset = vi.fn<(value: number) => void>()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const Root = defineComponent(() => () => h(WorkbenchResizer, {
    width: width.value,
    max: 720,
    onPreview: (value: number) => {
      preview(value)
      width.value = value
    },
    onCommit: commit,
    onCancel: cancel,
    onReset: reset,
  }))
  const app = createApp(Root)
  app.mount(host)
  apps.push(app)
  await nextTick()
  return {
    host,
    handle: host.querySelector<HTMLElement>('[data-testid="workbench-resizer"]')!,
    width,
    commit,
    preview,
    cancel,
    reset,
  }
}

function pointerEvent(type: string, clientX: number): PointerEvent {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    button: 0,
    clientX,
  })
  Object.defineProperties(event, {
    pointerId: { value: 9 },
    pointerType: { value: 'mouse' },
    isPrimary: { value: true },
  })
  return event as unknown as PointerEvent
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  document.documentElement.classList.remove('is-workbench-resizing')
  vi.restoreAllMocks()
})

describe('WorkbenchResizer', () => {
  it('exposes a complete keyboard separator contract', async () => {
    const mounted = await mountResizer()
    const handle = mounted.handle

    expect(handle.getAttribute('role')).toBe('separator')
    expect(handle.tabIndex).toBe(0)
    expect(handle.getAttribute('aria-orientation')).toBe('vertical')
    expect(handle.getAttribute('aria-controls')).toBe('app-main workbench-panel')
    expect(handle.getAttribute('aria-valuemin')).toBe('360')
    expect(handle.getAttribute('aria-valuemax')).toBe('720')
    expect(handle.getAttribute('aria-valuenow')).toBe('520')

    handle.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }))
    expect(mounted.commit).toHaveBeenLastCalledWith(528)
    await nextTick()
    handle.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'ArrowRight',
      shiftKey: true,
      bubbles: true,
    }))
    expect(mounted.commit).toHaveBeenLastCalledWith(496)
    await nextTick()
    handle.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }))
    expect(mounted.commit).toHaveBeenLastCalledWith(360)
    await nextTick()
    handle.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
    expect(mounted.commit).toHaveBeenLastCalledWith(720)
  })

  it('grows the right pane when the divider moves left and rolls back on Escape', async () => {
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
      callback(performance.now())
      return 1
    })
    const mounted = await mountResizer()
    Object.assign(mounted.handle, {
      setPointerCapture: vi.fn(),
      releasePointerCapture: vi.fn(),
      hasPointerCapture: vi.fn(() => true),
    })

    mounted.handle.dispatchEvent(pointerEvent('pointerdown', 600))
    mounted.handle.dispatchEvent(pointerEvent('pointermove', 560))
    expect(mounted.preview).toHaveBeenLastCalledWith(560)
    expect(document.documentElement.classList.contains('is-workbench-resizing')).toBe(true)

    mounted.handle.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true,
    }))
    expect(mounted.cancel).toHaveBeenLastCalledWith(520)
    expect(document.documentElement.classList.contains('is-workbench-resizing')).toBe(false)
  })

  it('resets the saved preference when the responsive width is visually unchanged', async () => {
    const mounted = await mountResizer()

    mounted.handle.dispatchEvent(new MouseEvent('dblclick', {
      bubbles: true,
      button: 0,
    }))

    expect(mounted.reset).toHaveBeenCalledOnce()
    expect(mounted.reset).toHaveBeenCalledWith(520)
  })
})
