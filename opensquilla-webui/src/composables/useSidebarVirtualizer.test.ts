// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, createApp, h, nextTick, ref, type App } from 'vue'
import type { SidebarDisplayRow } from '@/utils/sidebarDisplayProjection'
import { useSidebarVirtualizer } from './useSidebarVirtualizer'

const apps: App[] = []
function row(index: number): SidebarDisplayRow {
  return {
    key: `task-${index}`, title: `Task ${index}`, rowKind: 'session', sessionKind: 'chat',
    effectiveAgentId: 'main', agentName: 'Main', depth: 0, runStatus: 'idle', runLabel: 'Idle',
    taskAttention: 'none', updatedAt: index, hasContractGaps: false,
    displayZone: 'recents', displayFamily: 'chats', displayProjectName: '',
  }
}
type Block = { key: string; rows: SidebarDisplayRow[]; collapsed: boolean }
async function flush() {
  await nextTick()
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  await nextTick()
}
async function mount(count = 200) {
  const blocks = ref<Block[]>([{ key: 'recents', rows: Array.from({ length: count }, (_, index) => row(index)), collapsed: false }])
  const retained = ref<string[]>([])
  const focused = ref('')
  const element = document.createElement('div')
  const host = document.createElement('div')
  element.append(host)
  document.body.append(element)
  Object.defineProperties(element, {
    offsetHeight: { value: 176, configurable: true },
    offsetWidth: { value: 260, configurable: true },
    clientHeight: { value: 176, configurable: true },
    scrollHeight: { get: () => blocks.value.reduce((sum, block) => sum + 44 + (block.collapsed ? 0 : block.rows.length * 44), 0) },
    scrollTop: { value: 0, writable: true, configurable: true },
  })
  element.scrollTo = vi.fn(options => {
    element.scrollTop = (options as ScrollToOptions).top || 0
    element.dispatchEvent(new Event('scroll'))
  })
  let api!: ReturnType<typeof useSidebarVirtualizer<Block>>
  const app = createApp({ setup() {
    api = useSidebarVirtualizer(ref(element), computed(() => blocks.value), computed(() => retained.value), focused)
    return () => api.renderedBlocks.value.map(section => h('section', { key: section.block.key }, [
      h('div', { 'data-index': section.headingIndex, ref: (el: unknown) => api.measureElement(el as Element | null) }),
      ...section.rows.map(entry => h('div', {
        key: entry.row.key, 'data-index': entry.index, 'data-key': entry.row.key,
        ref: (el: unknown) => api.measureElement(el as Element | null),
      })),
    ]))
  } })
  app.mount(host)
  apps.push(app)
  await flush()
  return { api, blocks, retained, focused, element, keys: () => api.renderedBlocks.value.flatMap(section => section.rows.map(entry => entry.row.key)) }
}
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return new DOMRect(0, 0, 260, this.hasAttribute('data-index') ? 44 : 176)
  })
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(44)
})
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('sidebar TanStack window', () => {
  it('keeps short lists complete and long lists bounded', async () => {
    const short = await mount(24)
    expect(short.api.virtualized.value).toBe(false)
    expect(short.keys()).toHaveLength(24)
    const long = await mount(3000)
    expect(long.api.virtualized.value).toBe(true)
    expect(long.keys().length).toBeLessThan(20)
    expect(long.api.renderedBlocks.value[0]!.gapAfter).toBeGreaterThan(100_000)
    long.element.scrollTop = 44_000
    long.element.dispatchEvent(new Event('scroll'))
    await flush()
    expect(long.keys()).toContain('task-1000')
    expect(long.keys()).not.toContain('task-0')
    expect(long.keys().length).toBeLessThan(20)
  })

  it('retains only interaction leases and the focused row’s native Tab neighbors', async () => {
    const state = await mount()
    state.retained.value = ['task-150', 'task-180']
    state.focused.value = 'row:task-100'
    await flush()
    expect(state.keys()).toEqual(expect.arrayContaining(['task-99', 'task-100', 'task-101', 'task-150', 'task-180']))
    expect(state.keys().length).toBeLessThan(25)
    state.retained.value = []
    state.focused.value = ''
    await flush()
    expect(state.keys()).not.toContain('task-150')
    expect(state.keys()).not.toContain('task-100')
  })

  it('keeps group headers and accounts for disjoint window gaps exactly once', async () => {
    const state = await mount()
    state.blocks.value.push({ key: 'projects', collapsed: false, rows: Array.from({ length: 200 }, (_, index) => row(index + 200)) })
    state.retained.value = ['task-350']
    await flush()
    expect(state.api.renderedBlocks.value).toHaveLength(2)
    const extent = state.api.renderedBlocks.value.reduce((sum, block) => (
      sum + 44 + block.gapAfter + block.rows.reduce((height, entry) => height + entry.gapBefore + 44, 0)
    ), 0)
    expect(extent).toBe(402 * 44)
    state.blocks.value[0]!.collapsed = true
    await flush()
    expect(state.api.renderedBlocks.value[0]!.rows).toHaveLength(0)
    expect(state.api.renderedBlocks.value[0]!.gapAfter).toBe(0)
    expect(state.api.hasRow('task-100')).toBe(false)
    expect(state.api.hasRow('task-350')).toBe(true)
  })

  it('reveals by stable key and ignores a removed destination', async () => {
    const state = await mount()
    await state.api.revealRow('task-175')
    await flush()
    expect(state.keys()).toContain('task-175')
    const before = state.element.scrollTop
    await state.api.revealRow('missing')
    expect(state.element.scrollTop).toBe(before)
  })

  it('does not retain a deleted leased row or render a duplicate after moving groups', async () => {
    const state = await mount()
    state.retained.value = ['task-150']
    const moved = state.blocks.value[0]!.rows.splice(150, 1)[0]!
    state.blocks.value.unshift({ key: 'pinned', rows: [moved], collapsed: false })
    await flush()
    expect(state.keys().filter(key => key === 'task-150')).toHaveLength(1)
    state.blocks.value.shift()
    await flush()
    expect(state.keys()).not.toContain('task-150')
  })

  it('crosses the small-list threshold in both directions without stale gaps', async () => {
    const state = await mount(99)
    expect(state.keys()).toHaveLength(99)
    state.blocks.value[0]!.rows.push(row(99))
    await flush()
    expect(state.api.virtualized.value).toBe(true)
    expect(state.keys().length).toBeLessThan(20)
    state.blocks.value[0]!.rows.pop()
    await flush()
    expect(state.api.virtualized.value).toBe(false)
    expect(state.keys()).toHaveLength(99)
    expect(state.api.renderedBlocks.value[0]!.gapAfter).toBe(0)
    state.blocks.value = []
    await flush()
    expect(state.api.renderedBlocks.value).toEqual([])
  })
})
