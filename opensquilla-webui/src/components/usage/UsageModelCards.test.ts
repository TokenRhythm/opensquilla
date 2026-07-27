// @vitest-environment happy-dom

import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import type { ModelCard } from '@/types/usage'
import UsageModelCards from './UsageModelCards.vue'

const mounted: Array<ReturnType<typeof createApp>> = []

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('UsageModelCards metric layout', () => {
  it('keeps total tokens and task count together, then pairs token details', async () => {
    i18n.global.locale.value = 'en'
    const root = document.createElement('div')
    document.body.appendChild(root)
    const model: ModelCard = {
      model: 'deepseek-v4-pro',
      provider: 'deepseek',
      name: 'deepseek-v4-pro',
      inputTokens: 100,
      outputTokens: 20,
      cacheReadTokens: 30,
      cacheWriteTokens: 4,
      costUsd: 1.25,
      sessions: 6,
      share: 50,
      totalTokens: 120,
      costSource: 'opensquilla_estimate',
      anyCacheBlind: false,
    }
    const app = createApp(UsageModelCards, {
      modelCards: [model],
      modelsMeta: '1 model',
      fmtCost: () => '$1.2500',
      costSourceClassesForModelCard: () => ({}),
      costSourceLabelForModelCard: () => 'Estimated',
      costSourceTooltipForModelCard: () => 'Estimated locally',
    })
    app.use(i18n)
    app.mount(root)
    mounted.push(app)
    await nextTick()

    const rows = Array.from(root.querySelectorAll<HTMLElement>('.usage-model-card__rows > div'))
    expect(rows.map(row => row.querySelector('dt')?.textContent?.trim())).toEqual([
      'Tokens',
      'Input',
      'Output',
      'Cache R',
      'Cache W',
      'Cost',
    ])
    expect(rows[0]?.classList.contains('usage-model-card__total')).toBe(true)
    expect(rows[0]?.textContent).toContain('Tokens120')
    expect(rows[0]?.textContent).toContain('Tasks6')
  })
})
