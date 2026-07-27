// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import RouterFxStrip from './RouterFxStrip.vue'

const originalMatchMedia = window.matchMedia

function ensembleStrip(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    id: 'router-turn-1',
    role: 'router',
    displayRole: 'router',
    roleLabel: 'Router',
    text: '',
    timeStr: '',
    ts: null,
    showHeader: false,
    isRouterStrip: true,
    routerPanel: 'llm-ensemble',
    routerMode: 'llm_ensemble',
    routerSource: 'llm_ensemble',
    routerSettled: false,
    gridCells: [],
    winnerIdx: -1,
    messageId: 'router-empty-ensemble',
    ...overrides,
  }
}

function routerStrip(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    id: 'router-turn-1',
    role: 'router',
    displayRole: 'router',
    roleLabel: 'Router',
    text: '',
    timeStr: '',
    ts: null,
    showHeader: false,
    isRouterStrip: true,
    routerPanel: 'real-candidates',
    routerMode: 'squilla_router',
    routerSource: 'model_profile',
    routerSettled: false,
    gridCells: [
      { kind: 'real', tier: 'c0', tiers: ['c0'], displayName: 'claude-opus-4.8' },
      { kind: 'decoy', tier: '', tiers: [], displayName: 'legacy-placeholder' },
      { kind: 'real', tier: 'c1', tiers: ['c1'], displayName: 'deepseek-v4-flash' },
      { kind: 'real', tier: 'c2', tiers: ['c2'], displayName: 'glm-5.2' },
    ],
    winnerIdx: 2,
    messageId: 'router-live-result',
    ...overrides,
  }
}

function mockReducedMotion(matches: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)' ? matches : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(() => true),
    } satisfies MediaQueryList)),
  })
}

async function mountStrip(message: ChatRenderedMessage) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(RouterFxStrip, { message })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: originalMatchMedia,
  })
})

describe('RouterFxStrip model selection motion', () => {
  it('scans real candidates, locks the winner, and announces only the result', async () => {
    vi.useFakeTimers()
    const { app, el } = await mountStrip(routerStrip())
    const root = el.querySelector<HTMLElement>('.router-fx')
    const announcer = el.querySelector<HTMLElement>('.router-fx-sr-only')

    expect(root?.dataset.phase).toBe('scanning')
    expect(root?.getAttribute('aria-busy')).toBe('true')
    expect(el.querySelector('.router-fx-cell.win')).toBeFalsy()
    expect(announcer?.textContent).toBe('')

    const first = el.querySelector<HTMLElement>('[data-scan-active="true"]')
    expect(first?.dataset.cellIdx).toBe('0')
    expect(first?.textContent).not.toContain('legacy-placeholder')

    await vi.advanceTimersByTimeAsync(190)
    await nextTick()
    const second = el.querySelector<HTMLElement>('[data-scan-active="true"]')
    expect(second?.dataset.cellIdx).toBe('2')
    expect(el.querySelector('.router-fx-cell.win')).toBeFalsy()
    expect(announcer?.textContent).toBe('')

    await vi.advanceTimersByTimeAsync(410)
    await nextTick()

    expect(root?.dataset.phase).toBe('locked')
    expect(root?.getAttribute('aria-busy')).toBe('false')
    expect(el.querySelector('[data-scan-active="true"]')).toBeFalsy()
    expect(el.querySelector('.router-fx-selector')).toBeFalsy()
    expect(el.querySelectorAll('.router-fx-cell.win')).toHaveLength(1)
    expect(el.querySelector<HTMLElement>('.router-fx-cell.win')?.dataset.cellIdx).toBe('2')
    expect(announcer?.textContent).toBe('Router selected deepseek-v4-flash')
    expect(root?.getAttribute('aria-label')).toBe('Router selected deepseek-v4-flash')
    app.unmount()
  })

  it('keeps restored router results static and silent', async () => {
    vi.useFakeTimers()
    const { app, el } = await mountStrip(routerStrip({ routerStatic: true }))
    const root = el.querySelector<HTMLElement>('.router-fx')

    expect(root?.dataset.phase).toBe('static')
    expect(el.querySelector('.router-fx-selector')).toBeFalsy()
    expect(el.querySelector<HTMLElement>('.router-fx-cell.win')?.dataset.cellIdx).toBe('2')
    expect(el.querySelector('.router-fx-sr-only')?.textContent).toBe('')
    expect(vi.getTimerCount()).toBe(0)

    await vi.advanceTimersByTimeAsync(2_000)
    expect(root?.dataset.phase).toBe('static')
    app.unmount()
  })

  it('skips scanning for reduced motion while preserving the selected result', async () => {
    vi.useFakeTimers()
    mockReducedMotion(true)
    const { app, el } = await mountStrip(routerStrip())
    const root = el.querySelector<HTMLElement>('.router-fx')

    expect(root?.dataset.phase).toBe('static')
    expect(el.querySelector('.router-fx-selector')).toBeFalsy()
    expect(el.querySelector<HTMLElement>('.router-fx-cell.win')?.dataset.cellIdx).toBe('2')
    expect(el.querySelector('.router-fx-sr-only')?.textContent).toBe('Router selected deepseek-v4-flash')
    expect(vi.getTimerCount()).toBe(0)
    app.unmount()
  })

  it('settles immediately when the live router result becomes terminal', async () => {
    vi.useFakeTimers()
    const message = reactive(routerStrip())
    const { app, el } = await mountStrip(message)
    expect(el.querySelector<HTMLElement>('.router-fx')?.dataset.phase).toBe('scanning')

    message.routerSettled = true
    await nextTick()

    expect(el.querySelector<HTMLElement>('.router-fx')?.dataset.phase).toBe('static')
    expect(el.querySelector<HTMLElement>('.router-fx-cell.win')?.dataset.cellIdx).toBe('2')
    expect(el.querySelector('.router-fx-sr-only')?.textContent).toBe('Router selected deepseek-v4-flash')
    expect(vi.getTimerCount()).toBe(0)
    app.unmount()
  })

  it('clears scan timers when the strip unmounts', async () => {
    vi.useFakeTimers()
    const { app } = await mountStrip(routerStrip())
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    app.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('RouterFxStrip ensemble panel', () => {
  it('keeps an empty pending ensemble panel openable', async () => {
    const { app, el } = await mountStrip(ensembleStrip())

    const button = el.querySelector<HTMLButtonElement>('[data-testid="router-ensemble-toggle"]')
    expect(button).toBeTruthy()
    expect(button?.disabled).toBe(false)

    button?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()

    expect(el.querySelector('[data-testid="router-ensemble-inspector"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="router-ensemble-detail-unavailable"]')).toBeTruthy()
    expect(el.textContent).toContain('trace pending')
    expect(el.textContent).toContain('telemetry pending')
    expect(el.textContent).not.toContain('pool 0')
    expect(el.textContent).not.toContain('0 candidates')
    app.unmount()
  })

  it('shows handoff copy instead of selecting copy once the agent phase has started', async () => {
    const { app, el } = await mountStrip(ensembleStrip({ routerState: 'handoff' }))

    const button = el.querySelector<HTMLButtonElement>('[data-testid="router-ensemble-toggle"]')
    expect(button).toBeTruthy()
    expect(el.textContent).toContain('handed off to agent')
    expect(el.textContent).not.toContain('selecting candidates')

    button?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()

    expect(el.querySelector('[data-testid="router-ensemble-inspector"]')).toBeTruthy()
    expect(el.textContent).toContain('trace unavailable')
    expect(el.textContent).not.toContain('trace pending')
    app.unmount()
  })

  it('shows candidate failures and waits for the aggregator before completing', async () => {
    const message = reactive(ensembleStrip({
      ensemble: {
        profile: 'llm_ensemble',
        modelCount: 2,
        totalCandidates: 2,
        requestCount: 3,
        fallbackUsed: false,
        fallbackReason: '',
        costUsd: 0,
        savedUsd: 0,
        savedPct: 0,
        models: [
          {
            role: 'proposer',
            label: 'anchor',
            provider: 'openrouter',
            model: 'qwen/qwen3.7-plus',
            modelShort: 'qwen3.7-plus',
            input: 100,
            output: 20,
            costUsd: 0,
            status: 'done',
            elapsedMs: 105_000,
          },
          {
            role: 'proposer',
            label: 'critic',
            provider: 'openrouter',
            model: 'z-ai/glm-5.2',
            modelShort: 'glm-5.2',
            input: 0,
            output: 0,
            costUsd: 0,
            status: 'failed',
            elapsedMs: 118_000,
            error: 'provider timed out',
          },
          {
            role: 'aggregator',
            label: 'aggregator',
            provider: 'openrouter',
            model: 'anthropic/claude-sonnet',
            modelShort: 'claude-sonnet',
            input: 0,
            output: 0,
            costUsd: 0,
            status: 'running',
          },
        ],
      },
    }))
    const { app, el } = await mountStrip(message)

    el.querySelector<HTMLButtonElement>('[data-testid="router-ensemble-toggle"]')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()

    expect(el.querySelectorAll('[data-status="done"]')).toHaveLength(1)
    expect(el.querySelectorAll('[data-status="failed"]')).toHaveLength(1)
    expect(el.querySelectorAll('[data-status="running"]')).toHaveLength(1)
    expect(el.textContent).toContain('120 tok · 105s')
    expect(el.textContent).toContain('failed · 118s')
    expect(el.querySelector('[data-status="failed"] .router-fx-inspector__usage')?.getAttribute('title'))
      .toBe('provider timed out')
    expect(el.querySelector('.router-fx-ensemble__scan')).toBeTruthy()
    expect(el.textContent).toContain('2 candidates synthesizing')

    const aggregator = message.ensemble?.models.find(model => model.role === 'aggregator')
    if (!aggregator) throw new Error('expected aggregator row')
    aggregator.status = 'done'
    aggregator.input = 200
    aggregator.output = 40
    aggregator.elapsedMs = 12_000
    await nextTick()

    expect(el.textContent).toContain('240 tok · 12s')
    expect(el.textContent).toContain('2 candidates synthesized')
    expect(el.querySelector('.router-fx-ensemble__scan')).toBeFalsy()
    expect(el.querySelector('[data-testid="router-ensemble-toggle"]')?.getAttribute('aria-busy')).toBe('false')
    app.unmount()
  })

  it('shows a quorum-cancelled candidate as a neutral skipped row', async () => {
    const { app, el } = await mountStrip(ensembleStrip({
      routerSettled: true,
      ensemble: {
        profile: 'llm_ensemble',
        modelCount: 1,
        totalCandidates: 1,
        requestCount: 2,
        fallbackUsed: false,
        fallbackReason: '',
        costUsd: 0,
        savedUsd: 0,
        savedPct: 0,
        models: [
          {
            role: 'critic',
            label: 'critic',
            provider: 'openrouter',
            model: 'z-ai/glm-5.2',
            modelShort: 'glm-5.2',
            input: 0,
            output: 0,
            costUsd: 0,
            status: 'skipped',
            elapsedMs: 21_000,
            error: 'proposer cancelled after ensemble quorum grace',
            errorCode: 'quorum_cancelled',
          },
          {
            role: 'aggregator',
            label: 'aggregator',
            provider: 'openrouter',
            model: 'anthropic/claude-sonnet',
            modelShort: 'claude-sonnet',
            input: 200,
            output: 40,
            costUsd: 0,
            status: 'done',
            elapsedMs: 12_000,
          },
        ],
      },
    }))

    el.querySelector<HTMLButtonElement>('[data-testid="router-ensemble-toggle"]')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()

    const skipped = el.querySelector('[data-status="skipped"]')
    expect(skipped).toBeTruthy()
    expect(skipped?.classList.contains('router-fx-inspector__row--failed')).toBe(false)
    expect(skipped?.classList.contains('router-fx-inspector__row--skipped')).toBe(true)
    expect(skipped?.textContent).toContain('threshold met · skipped · 21s')
    expect(skipped?.textContent).not.toContain('failed')
    expect(skipped?.querySelector('.router-fx-inspector__usage')?.getAttribute('title'))
      .toBe('The candidate threshold was already met, so waiting for this candidate was cancelled.')
    app.unmount()
  })
})
