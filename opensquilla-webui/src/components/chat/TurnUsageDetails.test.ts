// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatMessageMeta } from '@/types/chat'
import TurnUsageDetails from './TurnUsageDetails.vue'

const mountedApps: ReturnType<typeof createApp>[] = []

function messageMeta(overrides: Partial<ChatMessageMeta> = {}): ChatMessageMeta {
  return {
    model: 'tokenrhythm/kimi-k2.7-code',
    modelShort: 'kimi-k2.7-code',
    input: 1_234,
    output: 56,
    hasTokens: true,
    cachedTokens: 0,
    reasoningTokens: 0,
    costUsd: 0.050328,
    hasSaved: false,
    savedLabel: '',
    ...overrides,
  }
}

async function mountDetails(
  meta: ChatMessageMeta,
  fmtTok: (value: number) => string = value => String(value),
) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(TurnUsageDetails, { meta, fmtTok })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  await nextTick()
  return host
}

function rows(host: HTMLElement): string[] {
  return Array.from(host.querySelectorAll('.turn-usage-details__row'))
    .map(row => row.textContent?.replace(/\s+/g, ' ').trim() || '')
}

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('TurnUsageDetails', () => {
  it('renders settled single-model usage as static disclosure content', async () => {
    const fmtTok = vi.fn((value: number) => value.toLocaleString('en-US'))
    const host = await mountDetails(messageMeta({
      cachedTokens: 900,
      reasoningTokens: 21,
    }), fmtTok)

    const root = host.querySelector<HTMLElement>('.turn-usage-details')
    expect(root).not.toBeNull()
    expect(root?.hasAttribute('data-turn-usage-details')).toBe(true)
    expect(root?.getAttribute('role')).toBe('group')
    expect(root?.getAttribute('aria-label')).toBe('Usage details')
    expect(rows(host)).toEqual([
      'modelkimi-k2.7-code',
      'cost$0.050328',
      'tokens↑1,234 ↓56',
      'cache900',
      'think21',
    ])
    expect(fmtTok).toHaveBeenCalledWith(1_234)
    expect(fmtTok).toHaveBeenCalledWith(56)
    expect(fmtTok).toHaveBeenCalledWith(900)
    expect(fmtTok).toHaveBeenCalledWith(21)
  })

  it('renders ensemble totals, fallback state, and member details', async () => {
    const host = await mountDetails(messageMeta({
      costUsd: 0.9,
      ensemble: {
        profile: 'router_dynamic/c1',
        modelCount: 2,
        totalCandidates: 2,
        requestCount: 3,
        fallbackUsed: true,
        fallbackReason: 'private upstream detail',
        costUsd: 0.371989,
        savedUsd: 0,
        savedPct: 0,
        models: [
          {
            role: 'proposer',
            label: 'proposal',
            provider: 'provider-a',
            model: 'provider-a/model-long-name',
            modelShort: 'model-a',
            input: 100,
            output: 20,
            costUsd: 0.1,
          },
          {
            role: 'fallback_single',
            label: 'single',
            provider: 'provider-b',
            model: 'provider-b/model-b',
            modelShort: 'model-b',
            input: 120,
            output: 30,
            costUsd: 0.00001,
          },
        ],
      },
    }))

    expect(rows(host)).toEqual([
      'tokens↑1234 ↓56',
      'ensemblerouter_dynamic/c1 · 3 requests',
      'cost$0.371989',
      'fallbackused',
    ])
    const members = Array.from(host.querySelectorAll('.turn-usage-details__model'))
      .map(row => row.textContent?.replace(/\s+/g, ' ').trim())
    expect(members).toEqual([
      'proposermodel-a$0.1',
      'fallbackmodel-b<$0.0001',
    ])
    expect(host.querySelector('.turn-usage-details__models')?.getAttribute('aria-label'))
      .toBe('Ensemble models')
    expect(host.querySelector<HTMLElement>('.turn-usage-details__model-name')?.title)
      .toBe('provider-a/model-long-name')
    expect(host.textContent).not.toContain('private upstream detail')
  })

  it('omits absent and zero-valued optional facts', async () => {
    const host = await mountDetails(messageMeta({
      model: '',
      modelShort: '',
      hasTokens: false,
      cachedTokens: 0,
      reasoningTokens: 0,
      costUsd: 0,
    }))

    expect(host.querySelectorAll('.turn-usage-details__row')).toHaveLength(0)
    expect(host.querySelector('.turn-usage-details__models')).toBeNull()
  })

  it('labels measured values as an incomplete known subtotal', async () => {
    const host = await mountDetails(messageMeta({
      coverageStatus: 'usage_unknown',
      usageUnknown: true,
      unknownUsageEvents: 1,
      hasKnownUsage: true,
    }))

    const coverage = host.querySelector<HTMLElement>('[data-turn-usage-coverage="incomplete"]')
    expect(coverage?.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'coverageIncomplete · shown values are a known subtotal · 1 provider call has unknown usage',
    )
    expect(host.textContent).toContain('$0.050328')
  })

  it('shows unknown-only coverage without presenting an exact zero bill', async () => {
    const host = await mountDetails(messageMeta({
      input: 0,
      output: 0,
      hasTokens: false,
      costUsd: 0,
      coverageStatus: 'usage_unknown',
      usageUnknown: true,
      unknownUsageEvents: 1,
      hasKnownUsage: false,
    }))

    expect(rows(host)).toEqual([
      'modelkimi-k2.7-code',
      'coverageIncomplete · exact usage total unavailable · 1 provider call has unknown usage',
    ])
    expect(host.textContent).not.toContain('$0')
  })

  it('contains no nested controls or interactive links', async () => {
    const host = await mountDetails(messageMeta())

    expect(host.querySelector('button, a, input, select, textarea, details, summary')).toBeNull()
    expect(host.querySelector('[tabindex]')).toBeNull()
  })
})
