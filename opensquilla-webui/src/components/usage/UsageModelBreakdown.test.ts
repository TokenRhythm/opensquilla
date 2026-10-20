// @vitest-environment happy-dom
import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import { formatUsageCost } from '@/composables/usage/nativeBilling'
import type { BreakdownRow, UsageMeterSummary } from '@/types/usage'
import UsageModelBreakdown from './UsageModelBreakdown.vue'

const apps: ReturnType<typeof createApp>[] = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = '' })
const meter = (metric: string, unit: string, quantity: string): UsageMeterSummary => ({
  metric, unit, quantity, dimensions: {}, measurementCount: 1, unknownCount: 0,
})
async function mount(rows: BreakdownRow[]) {
  i18n.global.locale.value = 'en'
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp(UsageModelBreakdown, {
    rows, totalCost: rows.reduce((sum, row) => sum + row.cost, 0), totalTokens: 0,
    nativeSource: {}, anyProrated: false,
    fmtCost: (cost: number | null | undefined, options?: { source?: object }) =>
      formatUsageCost(cost, 'USD', 6.975, 4, options?.source),
    costSourceClasses: () => ({}), costSourceLabel: (row: BreakdownRow) => row.costSource || '',
    costSourceTooltip: () => '',
  })
  app.use(i18n).mount(host)
  apps.push(app)
  await nextTick()
  return host
}

describe('UsageModelBreakdown capability fees', () => {
  it('keeps requested and output video duration distinct without treating media as zero tokens', async () => {
    const host = await mount([{ model: 'wan3.0-video', provider: 'tokenrhythm', name: 'wan3.0-video',
      tokens: 0, cost: 0.172, share: 100, costSource: 'provider_billed', capability: 'video_generation',
      meters: [meter('requests', 'request', '1'), meter('requested_video_seconds', 'second', '2'), meter('video_seconds', 'second', '2')],
    }])
    expect(host.textContent).toContain('Call details')
    expect(host.querySelector('.usage-expand__identity')?.textContent).toBe('tokenrhythm/wan3.0-video')
    expect(host.querySelector('.usage-expand__capability')).toBeNull()
    expect(host.querySelector('.usage-source')?.textContent).toBe('provider_billed')
    expect(host.querySelectorAll('.usage-expand__meter')).toHaveLength(3)
    expect(host.textContent).toContain('Requested video')
    expect(host.textContent).toContain('2')
    expect(host.textContent).toContain('$0.1720')
    expect(host.textContent).not.toContain('0 Tokens')
  })

  it('distinguishes missing monetary receipts from confirmed free calls', async () => {
    const base = { model: 'web_search', provider: 'brave', name: 'web_search', tokens: 0,
      cost: 0, share: 0, capability: 'web_search', meters: [meter('requests', 'request', '1')] }
    const host = await mount([
      { ...base, costSource: 'none', missingCostEntries: 1 },
      { ...base, provider: 'free-service', costSource: 'provider_billed', missingCostEntries: 0 },
    ])
    const cells = [...host.querySelectorAll('.usage-expand__cost')]
    expect(cells[0]?.textContent).toContain('—')
    expect(cells[0]?.textContent).not.toContain('$0')
    expect(cells[1]?.textContent).toContain('$0.0000')
  })

  it('does not represent unpriced calls as zero percent of a partially known total', async () => {
    const base = { model: 'web_search', provider: 'exa', name: 'web_search', tokens: 0,
      share: 0, capability: 'web_search', meters: [] }
    const host = await mount([
      { ...base, cost: 0.008, share: 100, costSource: 'opensquilla_estimate' },
      { ...base, cost: 0, costSource: 'unavailable', missingCostEntries: 1 },
      { ...base, cost: 0, costSource: 'mixed', costSourceCounts: { provider_billed: 1, unavailable: 1 } },
    ])
    expect(host.querySelectorAll('.usage-expand__share-pct')[1]?.textContent).toBe('—')
    expect(host.querySelectorAll('.usage-expand__share-pct')[2]?.textContent).toBe('—')
  })
})
