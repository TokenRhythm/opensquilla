// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { createPinia } from 'pinia'
import WorkbenchHost from './WorkbenchHost.vue'
import { hasOpenDialogLayer } from '@/composables/useDialogA11y'
import { useWorkbenchStore } from '@/workbench/store'
import type { WorkbenchItem } from '@/workbench/types'

const apps: App<Element>[] = []

function item(
  id: string,
  hostKind: WorkbenchItem['hostKind'] = 'dom',
  retention: WorkbenchItem['retention'] = 'keep-alive',
): WorkbenchItem {
  return {
    id,
    kind: 'artifact-preview',
    title: `${id}.html`,
    scope: { type: 'session', id: 'session' },
    hostKind,
    retention,
    payload: {},
  }
}

async function mountHost(
  availableWidth?: number,
  parentWidth?: number,
  renderPanelProbe = false,
  beforeCloseItem?: (item: WorkbenchItem) => boolean | Promise<boolean>,
) {
  const host = document.createElement('div')
  if (typeof parentWidth === 'number') {
    Object.defineProperties(host, {
      clientWidth: { configurable: true, value: parentWidth },
      clientHeight: { configurable: true, value: 700 },
    })
    host.getBoundingClientRect = () => ({
      x: 120,
      y: 48,
      top: 48,
      right: 120 + parentWidth,
      bottom: 748,
      left: 120,
      width: parentWidth,
      height: 700,
      toJSON: () => ({}),
    })
  }
  document.body.appendChild(host)
  const pinia = createPinia()
  const routeActive = ref(true)
  const modalBlocked = ref(false)
  const onSurfaceRect = vi.fn()
  const Root = defineComponent(() => () => h(
    WorkbenchHost,
    {
      availableWidth,
      modalBlocked: modalBlocked.value,
      onSurfaceRect,
      routeActive: routeActive.value,
      beforeCloseItem,
    },
    renderPanelProbe
      ? {
          panel: ({ item, active }: { item: WorkbenchItem; active: boolean }) =>
            h('div', {
              'data-testid': `panel-${item.id}`,
              'data-active': String(active),
            }, item.title),
        }
      : undefined,
  ))
  const app = createApp(Root)
  app.use(pinia)
  apps.push(app)
  const store = useWorkbenchStore(pinia)
  store.openItem(item('one'))
  app.mount(host)
  await nextTick()
  return { host, modalBlocked, onSurfaceRect, routeActive, store }
}

async function mountHostWithDeferredNativeSlot() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const pinia = createPinia()
  const ready = ref(false)
  const onSurfaceRect = vi.fn()
  const setNativeSlotRect = (value: unknown) => {
    if (!(value instanceof HTMLElement)) return
    value.getBoundingClientRect = () => ({
      x: 500,
      y: 50,
      top: 50,
      right: 1100,
      bottom: 650,
      left: 500,
      width: 600,
      height: 600,
      toJSON: () => ({}),
    })
  }
  const Root = defineComponent(() => () => h(
    WorkbenchHost,
    {
      availableWidth: 1200,
      onSurfaceRect,
      routeActive: true,
    },
    {
      'native-surface': () => h(
        'section',
        { 'data-testid': 'native-panel-shell' },
        [
          h('div', [
            ready.value
              ? h('div', {
                  key: 'ready',
                  ref: setNativeSlotRect,
                  'data-workbench-native-surface-slot': '',
                })
              : h(
                  'div',
                  { key: 'loading', 'data-testid': 'native-loading' },
                  'Loading preview',
                ),
          ]),
        ],
      ),
    },
  ))
  const app = createApp(Root)
  app.use(pinia)
  apps.push(app)
  const store = useWorkbenchStore(pinia)
  store.openItem(item('native', 'native-webcontents'))
  app.mount(host)
  await nextTick()
  return { host, onSurfaceRect, ready, store }
}

async function mountMobileHostFromInvoker() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const pinia = createPinia()
  const Root = defineComponent({
    setup() {
      const store = useWorkbenchStore()
      return () => h('div', [
        h('button', {
          id: 'workbench-invoker',
          onClick: () => store.openItem(item('one')),
        }, 'Open workbench'),
        h(WorkbenchHost, { availableWidth: 600 }),
      ])
    },
  })
  const app = createApp(Root)
  app.use(pinia)
  apps.push(app)
  const store = useWorkbenchStore(pinia)
  app.mount(host)
  await nextTick()
  return { host, store }
}

beforeEach(() => {
  localStorage.clear()
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
    callback(performance.now())
    return 1
  })
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined)
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  })
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('WorkbenchHost', () => {
  it('opens with an even split and keeps an explicit user width', async () => {
    const mounted = await mountHost(1200)
    const panel = mounted.host.querySelector<HTMLElement>('[data-testid="workbench-host"]')!

    expect(panel.style.getPropertyValue('--workbench-width')).toBe('600px')

    mounted.store.setWidth(614)
    await nextTick()
    expect(panel.style.getPropertyValue('--workbench-width')).toBe('614px')
  })

  it('keeps a single item light and adds a tab strip only for multiple items', async () => {
    const mounted = await mountHost(1200)

    expect(mounted.host.querySelector('[data-testid="workbench-host"]')).not.toBeNull()
    expect(mounted.host.querySelector('[role="tablist"]')).toBeNull()
    expect(mounted.host.textContent).toContain('one.html')
    expect(mounted.host.querySelector('[data-testid="workbench-resizer"]')).not.toBeNull()

    mounted.store.openItem(item('two'))
    await nextTick()
    expect(mounted.host.querySelector('[role="tablist"]')).not.toBeNull()
    const tabs = mounted.host.querySelectorAll<HTMLElement>('[role="tab"]')
    expect(tabs).toHaveLength(2)
    const activePanel = mounted.host.querySelector<HTMLElement>(
      '[role="tabpanel"]:not([aria-hidden="true"])',
    )!
    expect(tabs[1]?.getAttribute('aria-controls')).toBe(activePanel.id)
    expect(activePanel.getAttribute('aria-labelledby')).toBe(tabs[1]?.id)
    tabs[1]?.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'ArrowLeft',
      bubbles: true,
      cancelable: true,
    }))
    expect(mounted.store.activeItemId).toBe('one')
  })

  it('moves focus to the collapse control when closing from two tabs to one', async () => {
    const mounted = await mountHost(1200)
    mounted.store.openItem(item('two'))
    await nextTick()

    const closeSecond = mounted.host.querySelector<HTMLButtonElement>(
      '[aria-label="Close tab: two.html"]',
    )!
    closeSecond.focus()
    closeSecond.click()
    await nextTick()

    expect(mounted.store.items).toHaveLength(1)
    expect(mounted.host.querySelector('[role="tablist"]')).toBeNull()
    expect(document.activeElement).toBe(
      mounted.host.querySelector('[aria-label="Collapse workbench"]'),
    )
  })

  it('keeps a tab mounted when its pending source save rejects close', async () => {
    const beforeCloseItem = vi.fn(async () => false)
    const mounted = await mountHost(1200, undefined, false, beforeCloseItem)
    mounted.store.openItem(item('two'))
    await nextTick()

    mounted.host.querySelector<HTMLButtonElement>(
      '[aria-label="Close tab: two.html"]',
    )?.click()
    await vi.waitFor(() => expect(beforeCloseItem).toHaveBeenCalledOnce())

    expect(mounted.store.items.map(candidate => candidate.id)).toEqual(['one', 'two'])
    expect(mounted.store.activeItemId).toBe('two')
  })

  it('uses one desktop collapse control and preserves open tabs', async () => {
    const mounted = await mountHost(1200)
    mounted.store.openItem(item('two'))
    await nextTick()
    const panel = mounted.host.querySelector<HTMLElement>('[data-testid="workbench-host"]')!

    expect(panel.querySelector('[aria-label="Close tab: one.html"]')).not.toBeNull()
    expect(panel.querySelector('[aria-label="Close tab: two.html"]')).not.toBeNull()
    const collapse = panel.querySelector<HTMLButtonElement>(
      '[aria-label="Collapse workbench"]',
    )!
    collapse.click()
    await nextTick()

    expect(mounted.store.items).toHaveLength(2)
    expect(mounted.store.expanded).toBe(false)
    expect(mounted.host.querySelector('[data-testid="workbench-host"]')).not.toBeNull()
    expect(panel.style.display).toBe('none')
  })

  it('keeps inactive retained panels mounted and removes them from the accessibility tree', async () => {
    const mounted = await mountHost(1200, undefined, true)
    mounted.store.openItem(item('two'))
    await nextTick()

    const firstLayer = mounted.host.querySelector<HTMLElement>(
      '[data-workbench-item-id="one"]',
    )!
    const secondLayer = mounted.host.querySelector<HTMLElement>(
      '[data-workbench-item-id="two"]',
    )!

    expect(firstLayer).toBeTruthy()
    expect(secondLayer).toBeTruthy()
    expect(firstLayer.style.display).toBe('none')
    expect(firstLayer.getAttribute('aria-hidden')).toBe('true')
    expect(firstLayer.hasAttribute('inert')).toBe(true)
    expect(firstLayer.querySelector('[data-testid="panel-one"]')).toBeTruthy()
    expect(secondLayer.style.display).not.toBe('none')
    expect(secondLayer.hasAttribute('aria-hidden')).toBe(false)
    expect(secondLayer.hasAttribute('inert')).toBe(false)
    expect(secondLayer.querySelector('[data-testid="panel-two"]')).toBeTruthy()

    mounted.store.activateItem('one')
    await nextTick()

    expect(mounted.host.querySelector('[data-workbench-item-id="one"]')).toBe(firstLayer)
    expect(mounted.host.querySelector('[data-workbench-item-id="two"]')).toBe(secondLayer)
    expect(firstLayer.style.display).not.toBe('none')
    expect(firstLayer.hasAttribute('aria-hidden')).toBe(false)
    expect(firstLayer.hasAttribute('inert')).toBe(false)
    expect(firstLayer.querySelector('[data-testid="panel-one"]')?.getAttribute('data-active'))
      .toBe('true')
    expect(secondLayer.style.display).toBe('none')
    expect(secondLayer.getAttribute('aria-hidden')).toBe('true')
    expect(secondLayer.hasAttribute('inert')).toBe(true)
    expect(secondLayer.querySelector('[data-testid="panel-two"]')?.getAttribute('data-active'))
      .toBe('false')
  })

  it('uses a non-modal overlay before switching to a mobile dialog', async () => {
    const overlay = await mountHost(800)
    const overlayPanel = overlay.host.querySelector<HTMLElement>('[data-testid="workbench-host"]')!
    expect(overlayPanel.getAttribute('role')).toBe('complementary')
    expect(overlayPanel.getAttribute('aria-modal')).toBeNull()
    expect(overlayPanel.classList.contains('workbench-host--overlay')).toBe(true)
    expect(overlay.host.querySelector('[data-testid="workbench-resizer"]')).toBeNull()

    const mobile = await mountHost(600)
    const mobilePanel = mobile.host.querySelector<HTMLElement>('[data-testid="workbench-host"]')!
    expect(mobilePanel.getAttribute('role')).toBe('dialog')
    expect(mobilePanel.getAttribute('aria-modal')).toBe('true')
    expect(mobilePanel.classList.contains('workbench-host--mobile-dialog')).toBe(true)
    mobilePanel.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true,
    }))
    expect(mobile.store.expanded).toBe(false)
  })

  it('owns the global mobile dialog, traps focus, closes on Escape, and restores its invoker', async () => {
    const mounted = await mountMobileHostFromInvoker()
    const trigger = mounted.host.querySelector<HTMLButtonElement>('#workbench-invoker')!
    trigger.focus()
    trigger.click()
    await nextTick()
    await nextTick()

    const panel = mounted.host.querySelector<HTMLElement>('[data-testid="workbench-host"]')!
    const collapse = panel.querySelector<HTMLButtonElement>(
      '[aria-label="Collapse workbench"]',
    )!

    expect(panel.getAttribute('role')).toBe('dialog')
    expect(hasOpenDialogLayer()).toBe(true)
    expect(document.activeElement).toBe(collapse)
    expect(panel.querySelectorAll('[aria-label="Collapse workbench"]')).toHaveLength(1)

    const tab = new KeyboardEvent('keydown', {
      key: 'Tab',
      bubbles: true,
      cancelable: true,
    })
    document.dispatchEvent(tab)
    expect(tab.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(collapse)

    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true,
    }))
    await nextTick()

    expect(mounted.store.expanded).toBe(false)
    expect(hasOpenDialogLayer()).toBe(false)
    expect(document.activeElement).toBe(trigger)
  })

  it('measures the workspace parent when no width override is supplied', async () => {
    const mounted = await mountHost(undefined, 820)
    await nextTick()
    const panel = mounted.host.querySelector<HTMLElement>('[data-testid="workbench-host"]')!

    expect(panel.classList.contains('workbench-host--overlay')).toBe(true)
    expect(panel.style.getPropertyValue('--workbench-container-top')).toBe('48px')
    expect(panel.style.getPropertyValue('--workbench-container-height')).toBe('700px')
  })

  it('renders a dedicated native surface slot without putting handles in Pinia', async () => {
    const mounted = await mountHost(1200)
    mounted.store.openItem(item('native', 'native-webcontents'))
    await nextTick()

    expect(mounted.host.querySelector('.workbench-host__surface--native')).not.toBeNull()
    expect(mounted.store.activeItem?.payload).toEqual({})
    expect(JSON.stringify(mounted.store.$state)).not.toMatch(/webContents|Blob|AbortController/)
  })

  it('remeasures when a native surface slot appears after loading', async () => {
    const mounted = await mountHostWithDeferredNativeSlot()
    const panelShell = mounted.host.querySelector('[data-testid="native-panel-shell"]')

    expect(panelShell).toBeInstanceOf(HTMLElement)
    expect(mounted.host.querySelector('[data-workbench-native-surface-slot]')).toBeNull()
    expect(mounted.onSurfaceRect).toHaveBeenLastCalledWith({
      itemId: 'native',
      x: 0,
      y: 0,
      width: 0,
      height: 0,
      visible: false,
    })

    mounted.onSurfaceRect.mockClear()
    mounted.ready.value = true
    await nextTick()

    expect(mounted.host.querySelector('[data-testid="native-panel-shell"]')).toBe(panelShell)
    await vi.waitFor(() => {
      expect(mounted.onSurfaceRect).toHaveBeenLastCalledWith({
        itemId: 'native',
        x: 500,
        y: 50,
        width: 600,
        height: 600,
        visible: true,
      })
    })

    mounted.onSurfaceRect.mockClear()
    mounted.ready.value = false
    await nextTick()

    expect(mounted.host.querySelector('[data-testid="native-panel-shell"]')).toBe(panelShell)
    await vi.waitFor(() => {
      expect(mounted.onSurfaceRect).toHaveBeenLastCalledWith({
        itemId: 'native',
        x: 0,
        y: 0,
        width: 0,
        height: 0,
        visible: false,
      })
    })
  })

  it('tracks size changes on a dynamically inserted native surface slot', async () => {
    let observerIndex = 0
    let surfaceResizeCallback: ResizeObserverCallback | null = null
    const observeSurface = vi.fn()
    class ResizeObserverStub {
      readonly index = observerIndex++
      constructor(callback: ResizeObserverCallback) {
        if (this.index === 0) surfaceResizeCallback = callback
      }
      disconnect() {}
      observe(element: Element) {
        if (this.index === 0) observeSurface(element)
      }
      unobserve() {}
    }
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
    const mounted = await mountHostWithDeferredNativeSlot()

    mounted.ready.value = true
    await nextTick()
    const slot = mounted.host.querySelector<HTMLElement>(
      '[data-workbench-native-surface-slot]',
    )!
    await vi.waitFor(() => expect(observeSurface).toHaveBeenCalledWith(slot))

    slot.getBoundingClientRect = () => ({
      x: 480,
      y: 70,
      top: 70,
      right: 1080,
      bottom: 550,
      left: 480,
      width: 600,
      height: 480,
      toJSON: () => ({}),
    })
    mounted.onSurfaceRect.mockClear()
    surfaceResizeCallback!([], {} as ResizeObserver)

    expect(mounted.onSurfaceRect).toHaveBeenLastCalledWith({
      itemId: 'native',
      x: 480,
      y: 70,
      width: 600,
      height: 480,
      visible: true,
    })
  })

  it('keeps the active item mounted but hidden when the panel is collapsed', async () => {
    const mounted = await mountHost(1200)
    mounted.store.setExpanded(false)
    await nextTick()

    const panel = mounted.host.querySelector<HTMLElement>('[data-testid="workbench-host"]')
    expect(panel).not.toBeNull()
    expect(panel?.style.display).toBe('none')
    expect(mounted.store.items).toHaveLength(1)
    expect(mounted.store.activeItemId).toBe('one')
  })

  it('unmounts dispose-on-suspend panels when collapsed or the route leaves', async () => {
    const mounted = await mountHost(1200, undefined, true)
    mounted.store.openItem(item('volatile', 'dom', 'dispose-on-suspend'))
    await nextTick()
    expect(mounted.host.querySelector('[data-testid="panel-volatile"]')).toBeTruthy()

    mounted.store.setExpanded(false)
    await nextTick()
    expect(mounted.host.querySelector('[data-testid="panel-volatile"]')).toBeNull()

    mounted.store.setExpanded(true)
    await nextTick()
    expect(mounted.host.querySelector('[data-testid="panel-volatile"]')).toBeTruthy()

    mounted.routeActive.value = false
    await nextTick()
    expect(mounted.host.querySelector('[data-testid="panel-volatile"]')).toBeNull()
    expect(mounted.store.hostAvailable).toBe(false)
  })

  it('keeps dispose-on-suspend panels alive while a modal blocks interaction', async () => {
    const mounted = await mountHost(1200, undefined, true)
    mounted.store.openItem(item('volatile', 'dom', 'dispose-on-suspend'))
    await nextTick()

    mounted.modalBlocked.value = true
    await nextTick()

    const workbench = mounted.host.querySelector<HTMLElement>(
      '[data-testid="workbench-host"]',
    )!
    expect(workbench.getAttribute('aria-hidden')).toBe('true')
    expect(workbench.hasAttribute('inert')).toBe(true)
    expect(mounted.store.hostAvailable).toBe(true)
    expect(mounted.host.querySelector('[data-testid="panel-volatile"]')).toBeTruthy()
  })

  it('hides native surfaces without disposing them while a DOM modal blocks them', async () => {
    const mounted = await mountHost(1200)
    mounted.store.openItem(item('native', 'native-webcontents'))
    await nextTick()
    const nativeSlot = mounted.host.querySelector<HTMLElement>(
      '[data-workbench-native-surface-slot]',
    )!
    nativeSlot.getBoundingClientRect = () => ({
      x: 500,
      y: 50,
      top: 50,
      right: 1100,
      bottom: 650,
      left: 500,
      width: 600,
      height: 600,
      toJSON: () => ({}),
    })
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    expect(mounted.store.hostAvailable).toBe(true)
    expect(mounted.onSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ itemId: 'native', visible: true }),
    )

    mounted.modalBlocked.value = true
    await nextTick()
    await nextTick()
    const workbench = mounted.host.querySelector<HTMLElement>(
      '[data-testid="workbench-host"]',
    )!
    expect(workbench.getAttribute('aria-hidden')).toBe('true')
    expect(workbench.hasAttribute('inert')).toBe(true)
    expect(mounted.store.hostAvailable).toBe(true)
    expect(mounted.onSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ itemId: 'native', visible: false }),
    )

    mounted.modalBlocked.value = false
    await nextTick()
    await nextTick()
    expect(workbench.hasAttribute('aria-hidden')).toBe(false)
    expect(workbench.hasAttribute('inert')).toBe(false)
    expect(mounted.store.hostAvailable).toBe(true)
  })
})
