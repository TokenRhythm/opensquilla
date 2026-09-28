// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { KeepAlive, createApp, defineComponent, h, nextTick, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import * as virtualizerLayout from '@/utils/virtualizerLayout'

const rpcMocks = vi.hoisted(() => ({
  call: vi.fn(),
  ready: vi.fn(),
}))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({
    client: {},
    call: rpcMocks.call,
    ready: rpcMocks.ready,
  }),
}))

const messages: Record<string, string> = {
  'usageLogs.logs.breadcrumbLabel': 'Breadcrumb',
  'usageLogs.logs.title': 'Logs',
  'usageLogs.logs.subtitle': 'Gateway logs for diagnosis.',
  'usageLogs.logs.loading': 'Loading logs…',
  'usageLogs.logs.empty': 'No logs have been recorded yet.',
  'usageLogs.logs.loadFailed': 'Could not load logs.',
  'usageLogs.logs.retry': 'Retry',
  'usageLogs.logs.noMatch': 'No lines match the current filter.',
  'usageLogs.logs.fileLogOn': 'File log on',
  'usageLogs.logs.rawOff': 'Raw capture off',
  'nav.overview': 'Overview',
  'nav.logs': 'Logs',
}

vi.mock('vue-i18n', async (importOriginal) => {
  const actual = await importOriginal<typeof import('vue-i18n')>()
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string) => messages[key] ?? key,
    }),
  }
})

vi.mock('@/components/Icon.vue', () => ({
  default: defineComponent({
    name: 'IconStub',
    props: { name: { type: String, default: '' } },
    setup(props) {
      return () => h('span', { 'data-icon': props.name })
    },
  }),
}))

vi.mock('@/components/ControlSwitch.vue', () => ({
  default: defineComponent({
    name: 'ControlSwitchStub',
    props: { checked: Boolean },
    emits: ['update:checked'],
    setup(props, { emit }) {
      return () => h('button', {
        type: 'button', 'data-testid': 'control-switch',
        onClick: () => emit('update:checked', !props.checked),
      })
    },
  }),
}))

vi.mock('@/components/SupportDiagnosticsMenu.vue', () => ({
  default: defineComponent({
    name: 'SupportDiagnosticsMenuStub',
    setup() {
      return () => h('button', { type: 'button', 'data-testid': 'support-diagnostics' }, 'Support')
    },
  }),
}))

vi.mock('@/components/run/RunTrace.vue', () => ({
  default: defineComponent({ name: 'RunTraceStub', setup: () => () => h('div') }),
}))

import LogsView from './LogsView.vue'

interface MountedLogs {
  el: HTMLElement
  setVisible: (visible: boolean) => Promise<void>
  unmount: () => void
}

const mounted: MountedLogs[] = []
let viewportWidth = 800
let viewportHeight = 240
let rowHeight = 24
let resizeObservers: { callback: ResizeObserverCallback; targets: Set<Element> }[] = []

function resize(target: Element) {
  const bounds = target.getBoundingClientRect()
  const size = [{ inlineSize: bounds.width, blockSize: bounds.height }]
  const entry: ResizeObserverEntry = {
    target, contentRect: bounds, borderBoxSize: size,
    contentBoxSize: size, devicePixelContentBoxSize: size,
  }
  for (const observer of resizeObservers) {
    if (observer.targets.has(target)) observer.callback([entry], {} as ResizeObserver)
  }
}

function normalStatus() {
  return {
    gateway_file_log: { enabled: true, path: '/tmp/debug.log' },
    raw_turn_call_log: { enabled: false, source: 'off', directory: { path: '/tmp/logs' } },
  }
}

async function flush() {
  for (let index = 0; index < 8; index++) await Promise.resolve()
  await nextTick()
}

async function mountLogs(padding = 0): Promise<MountedLogs> {
  const styles = document.createElement('style')
  styles.textContent = `.lg-display { padding: ${padding}px; }`
  document.head.appendChild(styles)
  const visible = ref(true)
  const Host = defineComponent({
    name: 'LogsKeepAliveHost',
    setup() {
      return () => h(KeepAlive, null, {
        default: () => visible.value ? h(LogsView) : null,
      })
    },
  })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Host)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/logs', component: { render: () => null } },
      { path: '/overview', component: { render: () => null } },
    ],
  })
  await router.push('/logs')
  await router.isReady()
  app.use(router)
  app.provide(OBSERVABILITY_KEY, {
    logStatus: () => rpcMocks.call('logs.status', {}),
    async tailLogs(options: { cursor: number; limit?: number; level?: string | null }) {
      const data = await rpcMocks.call('logs.tail', {
        cursor: options.cursor,
        limit: options.limit ?? 500,
        level: options.level ?? null,
      })
      return {
        entries: data.lines || data.entries || [],
        cursor: data.cursor ?? null,
      }
    },
  } as never)
  app.mount(el)
  const result: MountedLogs = {
    el,
    async setVisible(nextVisible: boolean) {
      visible.value = nextVisible
      await nextTick()
      await flush()
    },
    unmount() {
      app.unmount()
      el.remove()
      styles.remove()
    },
  }
  mounted.push(result)
  await flush()
  return result
}

function tailCalls() {
  return rpcMocks.call.mock.calls.filter(([method]) => method === 'logs.tail')
}

beforeEach(() => {
  vi.useFakeTimers()
  viewportWidth = 800
  viewportHeight = 240
  rowHeight = 24
  resizeObservers = []
  vi.stubGlobal('ResizeObserver', class {
    targets = new Set<Element>()
    constructor(callback: ResizeObserverCallback) { resizeObservers.push({ callback, targets: this.targets }) }
    observe(target: Element) { this.targets.add(target) }
    unobserve(target: Element) { this.targets.delete(target) }
    disconnect() { this.targets.clear() }
  })
  // Keep TanStack's real range, key, measurement and scroll implementation. Only
  // supply the geometry missing from happy-dom, not a mock virtualizer.
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(() => viewportWidth)
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.classList.contains('lg-display') ? viewportHeight : Math.round(rowHeight)
  })
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.classList.contains('lg-display') ? viewportHeight : Math.round(rowHeight)
  })
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const height = this.classList.contains('lg-display') ? viewportHeight : rowHeight
    const display = this.closest<HTMLElement>('.lg-display')
    const top = this.classList.contains('lg-line') && display
      ? Number.parseFloat(getComputedStyle(display).paddingTop || '0')
        + Number.parseFloat(this.style.transform.slice(11) || '0') - display.scrollTop : 0
    return { x: 0, y: top, top, left: 0, right: viewportWidth, bottom: top + height,
      width: viewportWidth, height, toJSON: () => ({}) }
  })
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
    if (!this.classList.contains('lg-display')) return 24
    const window = this.querySelector<HTMLElement>('.lg-window')
    const styles = getComputedStyle(this)
    const padding = Number.parseFloat(styles.paddingTop || '0') + Number.parseFloat(styles.paddingBottom || '0')
    return window ? Number.parseFloat(window.style.height || '0') + padding : viewportHeight
  })
  vi.spyOn(HTMLElement.prototype, 'scrollTo').mockImplementation(function (this: HTMLElement, options?: ScrollToOptions | number) {
    if (!options || typeof options !== 'object') return
    this.scrollTop = Math.max(0, Math.min(options.top ?? 0, this.scrollHeight - this.clientHeight))
    this.dispatchEvent(new Event('scroll'))
  })
  rpcMocks.call.mockReset()
  rpcMocks.ready.mockReset()
  rpcMocks.ready.mockResolvedValue(undefined)
  rpcMocks.call.mockImplementation(async (method: string) => {
    if (method === 'logs.status') return normalStatus()
    if (method === 'logs.tail') return { lines: [], cursor: 0 }
    throw new Error(`unexpected RPC method: ${method}`)
  })
  Object.defineProperty(document, 'hidden', { configurable: true, value: false })
  window.localStorage.clear()
  window.matchMedia = vi.fn(() => ({
    matches: true,
    media: '',
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
})

afterEach(() => {
  mounted.splice(0).forEach(item => item.unmount())
  document.body.innerHTML = ''
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('LogsView states', () => {
  it('always renders its header, status labels, and diagnostics action', async () => {
    const { el } = await mountLogs()

    expect(el.querySelector('h1')?.textContent).toBe('Logs')
    const overviewLink = el.querySelector<HTMLAnchorElement>('.lg-breadcrumb__link')
    expect(overviewLink?.textContent?.trim()).toBe('Overview')
    expect(overviewLink?.getAttribute('href')).toBe('/overview')
    expect(el.querySelector('[aria-current="page"]')?.textContent).toBe('Logs')
    expect(el.textContent).toContain('File log on')
    expect(el.textContent).toContain('Raw capture off')
    expect(el.querySelector('[data-testid="support-diagnostics"]')).not.toBeNull()
    expect(el.textContent).toContain('No logs have been recorded yet.')
  })

  it('distinguishes loading from a successful empty response', async () => {
    let resolveTail!: (value: { lines: []; cursor: number }) => void
    rpcMocks.call.mockImplementation((method: string) => {
      if (method === 'logs.status') return Promise.resolve(normalStatus())
      if (method === 'logs.tail') {
        return new Promise(resolve => { resolveTail = resolve })
      }
      return Promise.reject(new Error(`unexpected RPC method: ${method}`))
    })

    const { el } = await mountLogs()
    expect(el.textContent).toContain('Loading logs…')
    expect(el.textContent).not.toContain('No logs have been recorded yet.')

    resolveTail({ lines: [], cursor: 0 })
    await flush()

    expect(el.textContent).not.toContain('Loading logs…')
    expect(el.textContent).toContain('No logs have been recorded yet.')
  })

  it('shows a retryable failure when the initial tail request fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    let tailShouldFail = true
    rpcMocks.call.mockImplementation(async (method: string) => {
      if (method === 'logs.status') return normalStatus()
      if (method === 'logs.tail') {
        if (tailShouldFail) throw new Error('tail unavailable')
        return { lines: [], cursor: 0 }
      }
      throw new Error(`unexpected RPC method: ${method}`)
    })

    const { el } = await mountLogs()
    expect(el.textContent).toContain('Could not load logs.')
    expect(el.querySelector('[role="alert"]')).not.toBeNull()
    expect(warn).toHaveBeenCalledTimes(1)

    tailShouldFail = false
    const retry = Array.from(el.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Retry')
    retry?.click()
    await flush()

    expect(el.textContent).not.toContain('Could not load logs.')
    expect(el.textContent).toContain('No logs have been recorded yet.')
    expect(tailCalls()).toHaveLength(2)
  })

  it('keeps buffered lines visible when a later read fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    let tailCall = 0
    rpcMocks.call.mockImplementation(async (method: string) => {
      if (method === 'logs.status') return normalStatus()
      if (method === 'logs.tail') {
        tailCall += 1
        if (tailCall === 1) {
          return { lines: [{ level: 'INFO', message: 'gateway ready' }], cursor: 1 }
        }
        if (tailCall === 2) throw new Error('tail unavailable')
        return { lines: [], cursor: 1 }
      }
      throw new Error(`unexpected RPC method: ${method}`)
    })

    const { el } = await mountLogs()
    await vi.advanceTimersByTimeAsync(3_000)
    await flush()

    expect(el.textContent).toContain('gateway ready')
    expect(el.textContent).toContain('Could not load logs.')
    expect(el.querySelector('[role="alert"]')).not.toBeNull()

    const retry = Array.from(el.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Retry')
    retry?.click()
    await flush()

    expect(el.textContent).toContain('gateway ready')
    expect(el.textContent).not.toContain('Could not load logs.')
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('shows the no-match state only when buffered lines are filtered out', async () => {
    rpcMocks.call.mockImplementation(async (method: string) => {
      if (method === 'logs.status') return normalStatus()
      if (method === 'logs.tail') {
        return { lines: [{ level: 'INFO', message: 'gateway ready' }], cursor: 1 }
      }
      throw new Error(`unexpected RPC method: ${method}`)
    })

    const { el } = await mountLogs()
    expect(el.textContent).toContain('gateway ready')

    const input = el.querySelector<HTMLInputElement>('input[type="search"]')!
    input.value = 'not present'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await vi.advanceTimersByTimeAsync(150)
    await flush()

    expect(el.textContent).toContain('No lines match the current filter.')
    expect(el.textContent).not.toContain('No logs have been recorded yet.')
  })

  it('uses bracketed log levels before error-like message text', async () => {
    rpcMocks.call.mockImplementation(async (method: string) => {
      if (method === 'logs.status') return normalStatus()
      if (method === 'logs.tail') {
        return {
          lines: [
            '2026-08-10 [INFO] opensquilla: migrations_applied migration=V019__turn_errors',
            '2026-08-10 [INFO] opensquilla: skill_catalog.refreshed errors=0',
            '2026-08-10 [WARNING] opensquilla: github.search_failed error="request failed"',
            '2026-08-10 [ERROR] opensquilla: actual failure',
          ],
          cursor: 4,
        }
      }
      throw new Error(`unexpected RPC method: ${method}`)
    })

    const { el } = await mountLogs()

    expect(Array.from(el.querySelectorAll('.lg-line__lvl'), level => level.textContent)).toEqual([
      'INFO',
      'INFO',
      'WARN',
      'ERROR',
    ])
    expect(el.querySelector('.lg-level-btn--info .lg-level-btn__count')?.textContent).toBe('2')
    expect(el.querySelector('.lg-level-btn--warn .lg-level-btn__count')?.textContent).toBe('1')
    expect(el.querySelector('.lg-level-btn--error .lg-level-btn__count')?.textContent).toBe('1')

    el.querySelector<HTMLButtonElement>('.lg-level-btn--debug')?.click()
    el.querySelector<HTMLButtonElement>('.lg-level-btn--info')?.click()
    el.querySelector<HTMLButtonElement>('.lg-level-btn--warn')?.click()
    await flush()

    const visibleLines = Array.from(el.querySelectorAll('.lg-line'), line => line.textContent)
    expect(visibleLines).toHaveLength(1)
    expect(visibleLines[0]).toContain('actual failure')
  })
})

describe('LogsView KeepAlive lifecycle', () => {
  it('polls once initially and only polls or listens for visibility while active', async () => {
    const view = await mountLogs()
    expect(tailCalls()).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(3_000)
    await flush()
    expect(tailCalls()).toHaveLength(2)

    await view.setVisible(false)
    const callsWhileHidden = tailCalls().length
    await vi.advanceTimersByTimeAsync(9_000)
    document.dispatchEvent(new Event('visibilitychange'))
    await flush()
    expect(tailCalls()).toHaveLength(callsWhileHidden)

    await view.setVisible(true)
    expect(tailCalls()).toHaveLength(callsWhileHidden + 1)

    document.dispatchEvent(new Event('visibilitychange'))
    await flush()
    expect(tailCalls()).toHaveLength(callsWhileHidden + 2)
  })
})

describe('LogsView virtualization', () => {
  function mockLogPages(count = 500) {
    let read = 0
    rpcMocks.call.mockImplementation(async (method: string) => {
      if (method === 'logs.status') return normalStatus()
      if (method === 'logs.tail') {
        const start = read++ * count
        return {
          lines: Array.from({ length: count }, (_, index) => ({
            level: index % 2 ? 'WARN' : 'INFO', message: `synthetic log ${start + index}`,
          })),
          cursor: start + count,
        }
      }
      throw new Error(`unexpected RPC method: ${method}`)
    })
  }

  it('mounts a bounded range and follows appended pages using the real virtualizer', async () => {
    mockLogPages()
    const { el } = await mountLogs()
    await vi.advanceTimersByTimeAsync(100)
    await flush()

    expect(el.querySelectorAll('.lg-line').length).toBeLessThan(40)
    expect(el.querySelector('.lg-line:last-child')?.textContent).toContain('synthetic log 499')

    await vi.advanceTimersByTimeAsync(3_000)
    await flush()
    expect(el.querySelectorAll('.lg-line').length).toBeLessThan(40)
    expect(el.querySelector('.lg-line:last-child')?.textContent).toContain('synthetic log 999')
  })

  it('does not jump to appended lines when Auto follow is disabled', async () => {
    mockLogPages()
    const { el } = await mountLogs()
    await vi.advanceTimersByTimeAsync(100)
    el.querySelector<HTMLButtonElement>('[data-testid="control-switch"]')!.click()
    await flush()
    const display = el.querySelector<HTMLElement>('.lg-display')!
    display.scrollTo({ top: 2_400 })
    await flush()
    const readingOffset = display.scrollTop
    const firstLine = el.querySelector('.lg-line')?.textContent

    await vi.advanceTimersByTimeAsync(3_000)
    await flush()
    expect(display.scrollTop).toBe(readingOffset)
    expect(el.querySelector('.lg-line')?.textContent).toBe(firstLine)
    expect(el.textContent).not.toContain('synthetic log 999')
  })

  it('retires a pending end seek when Auto follow is disabled before a height-only resize', async () => {
    mockLogPages()
    const { el } = await mountLogs()
    const display = el.querySelector<HTMLElement>('.lg-display')!
    expect(display.scrollHeight - display.clientHeight - display.scrollTop).toBe(0)
    // Do not advance RAF yet: the initial scrollToEnd still has an index seek
    // awaiting reconciliation when the reader disables Auto follow.
    el.querySelector<HTMLButtonElement>('[data-testid="control-switch"]')!.click()
    await flush()
    display.dispatchEvent(new WheelEvent('wheel', { deltaY: -9_360 }))
    display.scrollTo({ top: 2_400 })
    await flush()
    const before = display.scrollTop
    expect(el.querySelector('[data-index="100"]')?.textContent).toContain('synthetic log 100')

    // Only height changes, so width-anchor restoration cannot mask an obsolete
    // scrollToIndex(last) choosing a new bottom when the native RAF runs.
    viewportHeight = 320
    resize(display)
    await flush()
    await vi.advanceTimersByTimeAsync(32)
    await flush()
    expect(display.scrollTop).toBe(before)
    expect(el.querySelector('[data-index="100"]')?.textContent).toContain('synthetic log 100')
    expect(el.textContent).not.toContain('synthetic log 499')
  })

  it('virtualizes wrapped narrow-screen rows instead of mounting the entire buffer', async () => {
    viewportWidth = 390
    rowHeight = 76.5
    mockLogPages()
    const { el } = await mountLogs()
    await vi.advanceTimersByTimeAsync(100)
    await flush()
    el.querySelectorAll('.lg-line').forEach(row => resize(row))
    await flush()
    expect(el.querySelectorAll('.lg-line').length).toBeLessThan(40)
    expect(el.querySelector('.lg-line:last-child')?.textContent).toContain('synthetic log 499')
    const rows = el.querySelectorAll<HTMLElement>('.lg-line')
    const offset = (row: HTMLElement) => Number.parseFloat(row.style.transform.slice(11))
    expect(offset(rows[rows.length - 1]) - offset(rows[rows.length - 2])).toBe(76.5)
  })

  it('invalidates offscreen sizes while preserving the reading anchor across a width change', async () => {
    mockLogPages()
    const { el } = await mountLogs()
    await vi.advanceTimersByTimeAsync(100)
    el.querySelector<HTMLButtonElement>('[data-testid="control-switch"]')!.click()
    await flush()
    const display = el.querySelector<HTMLElement>('.lg-display')!
    display.scrollTo({ top: 2_405.5 })
    await flush()
    const position = (row: HTMLElement) => Number.parseFloat(row.style.transform.slice(11)) - display.scrollTop
    const anchor = Array.from(el.querySelectorAll<HTMLElement>('.lg-line')).find(row => position(row) + rowHeight > 0)!
    const anchorText = anchor.textContent
    const before = position(anchor)
    viewportWidth = 390
    rowHeight = 76.5
    resize(display)
    for (let index = 0; index < 8; index++) await flush()
    await vi.advanceTimersByTimeAsync(32)
    await flush()
    const after = Array.from(el.querySelectorAll<HTMLElement>('.lg-line')).find(row => row.textContent === anchorText)!
    expect(after).toBeDefined()
    expect(position(after)).toBeCloseTo(before, 4)
    expect(el.querySelectorAll('.lg-line').length).toBeLessThan(40)
  })

  it('retains the reading row when row resize notifications precede the viewport width notification', async () => {
    viewportWidth = 390
    rowHeight = 120
    mockLogPages()
    const { el } = await mountLogs()
    await vi.advanceTimersByTimeAsync(100)
    el.querySelector<HTMLButtonElement>('[data-testid="control-switch"]')!.click()
    await flush()
    const display = el.querySelector<HTMLElement>('.lg-display')!
    display.scrollTo({ top: 4_805.5 })
    await flush()
    el.querySelectorAll('.lg-line').forEach(row => resize(row))
    await flush()
    const start = (row: HTMLElement) => Number.parseFloat(row.style.transform.slice(11))
    const visible = Array.from(el.querySelectorAll<HTMLElement>('.lg-line'))
      .find(row => start(row) + rowHeight > display.scrollTop)!
    display.scrollTo({ top: start(visible) + 5.5 })
    await flush()
    const anchorText = visible.textContent
    const before = start(visible) - display.scrollTop

    viewportWidth = 800
    rowHeight = 24
    // Browsers may deliver row RO entries before the container RO. Shrinking
    // previously measured rows changes the range before width invalidation.
    el.querySelectorAll('.lg-line').forEach(row => resize(row))
    await flush()
    display.scrollTo({ top: display.scrollTop })
    resize(display)
    for (let index = 0; index < 8; index++) await flush()
    await vi.advanceTimersByTimeAsync(32)
    await flush()
    const after = Array.from(el.querySelectorAll<HTMLElement>('.lg-line'))
      .find(row => row.textContent === anchorText)!
    expect(after).toBeDefined()
    expect(start(after) - display.scrollTop).toBeCloseTo(before, 4)
    expect(el.querySelectorAll('.lg-line').length).toBeLessThan(40)
  })

  it('preserves the actual visible row inside the container padding boundary', async () => {
    mockLogPages()
    const { el } = await mountLogs(12)
    await vi.advanceTimersByTimeAsync(100)
    el.querySelector<HTMLButtonElement>('[data-testid="control-switch"]')!.click()
    await flush()
    const display = el.querySelector<HTMLElement>('.lg-display')!
    expect(getComputedStyle(display).paddingTop).toBe('12px')
    expect(display.scrollHeight - display.clientHeight - display.scrollTop).toBe(0)
    // Model row 100 starts at 2400, but its DOM top is 2412. At 2429.5
    // it is still visible while ignoring padding would select row 101.
    display.scrollTo({ top: 2_429.5 })
    await flush()
    const visible = Array.from(el.querySelectorAll<HTMLElement>('.lg-line'))
      .find(row => row.getBoundingClientRect().bottom > 0)!
    const before = visible.getBoundingClientRect().top
    expect(visible.textContent).toContain('synthetic log 100')
    expect(before).toBe(-17.5)

    viewportWidth = 390
    rowHeight = 76.5
    resize(display)
    for (let index = 0; index < 8; index++) await flush()
    await vi.advanceTimersByTimeAsync(32)
    await flush()
    const after = Array.from(el.querySelectorAll<HTMLElement>('.lg-line'))
      .find(row => row.textContent === visible.textContent)!
    expect(after).toBeDefined()
    expect(after.getBoundingClientRect().top).toBeCloseTo(before, 4)
  })

  it('retains row identity and reading position when the capped buffer evicts older logs', async () => {
    mockLogPages(500)
    const { el } = await mountLogs()
    await vi.advanceTimersByTimeAsync(9_100)
    await flush()
    const retainedRow = Array.from(el.querySelectorAll('.lg-line'))
      .find(row => row.textContent?.includes('synthetic log 1999'))!
    expect(retainedRow).toBeDefined()

    el.querySelector<HTMLButtonElement>('[data-testid="control-switch"]')!.click()
    await flush()
    await vi.advanceTimersByTimeAsync(3_000)
    await flush()
    const afterEviction = Array.from(el.querySelectorAll('.lg-line'))
      .find(row => row.textContent?.includes('synthetic log 1999'))
    expect(afterEviction).toBe(retainedRow)
  })

  it('retains the last row identity when a level filter changes its index', async () => {
    mockLogPages()
    const { el } = await mountLogs()
    await vi.advanceTimersByTimeAsync(100)
    await flush()
    const retainedRow = el.querySelector('.lg-line:last-child')
    expect(retainedRow?.textContent).toContain('synthetic log 499')
    el.querySelector<HTMLButtonElement>('.lg-level-btn--info')!.click()
    await flush()
    await vi.advanceTimersByTimeAsync(100)
    expect(el.querySelector('.lg-line:last-child')).toBe(retainedRow)
  })

  it('clears discarded measurements on every buffer trim across repeated tail pages', async () => {
    const remeasure = vi.spyOn(virtualizerLayout, 'remeasureVirtualizer')
    mockLogPages()
    const { el } = await mountLogs()
    await vi.advanceTimersByTimeAsync(9_100)
    await flush()
    expect(remeasure).not.toHaveBeenCalled()
    for (let page = 1; page <= 4; page++) {
      await vi.advanceTimersByTimeAsync(3_000)
      await flush()
      expect(remeasure).toHaveBeenCalledTimes(page)
      expect(el.querySelector('.lg-line:last-child')?.textContent)
        .toContain(`synthetic log ${1_999 + page * 500}`)
      expect(el.querySelectorAll('.lg-line').length).toBeLessThan(40)
      const sizes = remeasure.mock.calls[remeasure.mock.calls.length - 1]![0].takeSnapshot()
      expect(sizes.every(item => Number(item.key) >= page * 500)).toBe(true)
    }
  })
})
