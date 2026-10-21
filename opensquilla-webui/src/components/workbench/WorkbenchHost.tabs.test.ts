// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import { createPinia } from 'pinia'
import WorkbenchHost from './WorkbenchHost.vue'
import { useWorkbenchStore } from '@/workbench/store'
import { useNativeSurfaceOcclusionState } from '@/composables/useDialogA11y'
import type { WorkbenchItem } from '@/workbench/types'

let app: App | undefined
beforeEach(() => vi.useFakeTimers())
afterEach(() => { app?.unmount(); document.body.innerHTML = ''; vi.useRealTimers() })
const descriptor = (id: string): WorkbenchItem => ({ id, kind: 'artifact-preview', title: `${id}.pptx`,
  scope: { type: 'session', id: 'a' }, hostKind: 'dom', retention: 'keep-alive', payload: {} })
async function mount() {
  const pinia = createPinia(), store = useWorkbenchStore(pinia)
  store.openItem(descriptor('one')); store.openItem(descriptor('two'))
  const host = document.createElement('div'); document.body.append(host)
  const menu = vi.fn(), loading = ref(false), occluded = useNativeSurfaceOcclusionState()
  app = createApp({ render: () => h(WorkbenchHost, { availableWidth: 1400, onTabMenu: menu,
    tabInfo: () => ({ subtitle: 'PPTX · 42 KB', thumbnailUrl: 'blob:already-rendered', loading: loading.value }) }) })
  app.use(pinia); app.mount(host); await nextTick()
  const wraps = () => [...host.querySelectorAll<HTMLElement>('.workbench-host__tab-wrap')]
  return { store, host, menu, loading, occluded, wraps }
}
async function tick(ms: number) { await vi.advanceTimersByTimeAsync(ms); await nextTick() }

describe('workbench tab interactions', () => {
  it('keeps different source files distinguishable from a historical revision', async () => {
    const f = await mount()
    f.store.openItem({ ...descriptor('original'), title: 'report.docx', payload: { sourcePath: 'preview-gallery/report.docx' } })
    f.store.openItem({ ...descriptor('copy'), title: 'report.docx', payload: { sourcePath: 'unregistered/report.docx' } })
    f.store.openItem({ ...descriptor('history'), title: 'report.docx', payload: { sourcePath: 'unregistered/report.docx', historical: true } })
    await nextTick()
    expect([...f.host.querySelectorAll('.workbench-host__tab-source')].map(el => el.textContent))
      .toEqual(['preview-gallery', 'unregistered', 'unregistered'])
    expect(f.host.querySelector('[aria-label="report.docx · History · unregistered"]')).not.toBeNull()
    expect(f.host.querySelectorAll('.workbench-host__tab-version')).toHaveLength(1)
  })

  it('waits 400 ms and only shows cached imagery for inactive tabs, without activating them', async () => {
    const f = await mount(), activate = vi.spyOn(f.store, 'activateItem')
    f.wraps()[0]!.dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'mouse' }))
    await tick(399); expect(document.querySelector('[role="tooltip"]')).toBeNull()
    await tick(1)
    expect(document.querySelector('[role="tooltip"] strong')?.textContent).toBe('one.pptx')
    expect(document.querySelector('[role="tooltip"] img')?.getAttribute('src')).toBe('blob:already-rendered')
    expect(f.store.activeItemId).toBe('two'); expect(activate).not.toHaveBeenCalled()
    f.wraps()[1]!.dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'mouse' }))
    await tick(400)
    expect(document.querySelector('[role="tooltip"] strong')?.textContent).toBe('two.pptx')
    expect(document.querySelector('[role="tooltip"] img')).toBeNull()
  })
  it('supports keyboard preview and Escape without losing focus, and occludes native surfaces only while shown', async () => {
    const f = await mount(), tab = f.host.querySelector<HTMLElement>('[role="tab"]')!
    tab.focus(); await tick(400)
    expect(document.querySelector('[role="tooltip"]')).not.toBeNull()
    expect(f.occluded.value).toBe(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await nextTick()
    expect(document.querySelector('[role="tooltip"]')).toBeNull()
    expect(f.occluded.value).toBe(false); expect(document.activeElement).toBe(tab)
  })
  it('right-click and Shift+F10 address the target tab without switching its content', async () => {
    const f = await mount(), tab = f.host.querySelector<HTMLElement>('[role="tab"]')!
    f.wraps()[0]!.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))
    expect(f.menu.mock.calls[0]?.[0].id).toBe('one')
    tab.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true, cancelable: true }))
    expect(f.menu.mock.calls[1]?.[0].id).toBe('one')
    expect(f.store.activeItemId).toBe('two')
  })
  it('cancels brief pointer visits and clears an open card when changing sessions', async () => {
    const f = await mount()
    f.wraps()[0]!.dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'mouse' }))
    await tick(200); f.wraps()[0]!.dispatchEvent(new PointerEvent('pointerleave')); await tick(500)
    expect(document.querySelector('[role="tooltip"]')).toBeNull()
    f.wraps()[0]!.dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'mouse' }))
    await tick(400); f.store.setSessionScope('b'); await nextTick()
    expect(document.querySelector('[role="tooltip"]')).toBeNull()
    expect(f.occluded.value).toBe(false)
  })
  it('projects genuine loading into the existing tab without replacing its title node', async () => {
    const f = await mount(), title = f.host.querySelector('.workbench-host__tab-title')
    f.loading.value = true; await nextTick()
    expect(f.host.querySelector('[role="tab"]')?.getAttribute('aria-busy')).toBe('true')
    expect(f.host.querySelector('.workbench-host__tab-spinner')).not.toBeNull()
    expect(f.host.querySelector('.workbench-host__tab-title')).toBe(title)
    f.loading.value = false; await nextTick()
    expect(f.host.querySelector('.workbench-host__tab-spinner')).toBeNull()
  })
})


it('keeps the native snapshot decoded beneath WebContents before an overlay hides it', async () => {
  const pinia = createPinia(), store = useWorkbenchStore(pinia)
  store.openItem({ ...descriptor('native'), hostKind: 'native-webcontents' })
  const host = document.createElement('div'); document.body.append(host)
  const blocked = ref(false), rect = vi.fn()
  const geometry = (left: number, top: number, width: number, height: number) => ({ left, top, x: left, y: top, width, height, right: left + width, bottom: top + height, toJSON: () => ({}) })
  app = createApp({ render: () => h(WorkbenchHost, { availableWidth: 1400, nativeBlocked: blocked.value,
    nativeSnapshotUrl: 'blob:completed-page', onSurfaceRect: rect }, {
    'native-surface': () => h('div', [h('div', { class: 'toolbar' }, 'Address bar'),
      h('div', { 'data-workbench-native-surface-slot': '', ref: (el: unknown) => {
        if (el instanceof HTMLElement) el.getBoundingClientRect = () => geometry(600, 150, 700, 500)
      } })]),
  }) })
  app.use(pinia); app.mount(host); await nextTick()
  const surface = host.querySelector<HTMLElement>('[data-testid="workbench-surface"]')!
  surface.getBoundingClientRect = () => geometry(600, 100, 700, 550)
  await tick(30)
  const backdrop = host.querySelector<HTMLImageElement>('.workbench-host__native-backdrop')!
  expect(backdrop.src).toBe('blob:completed-page')
  blocked.value = true; await nextTick(); await tick(30)
  expect(host.querySelector('.workbench-host__native-backdrop')).toBe(backdrop)
  expect(backdrop.style.top).toBe('50px'); expect(backdrop.style.height).toBe('500px')
  expect(rect).toHaveBeenLastCalledWith(expect.objectContaining({ visible: false }))
  expect(host.querySelector('[data-testid="workbench-host"]')?.hasAttribute('inert')).toBe(false)
  blocked.value = false; await nextTick(); await tick(30)
  expect(host.querySelector('.workbench-host__native-backdrop')).toBe(backdrop)
  expect(rect).toHaveBeenLastCalledWith(expect.objectContaining({ visible: true }))
})
