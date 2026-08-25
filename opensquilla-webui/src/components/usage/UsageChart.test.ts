// @vitest-environment happy-dom

import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import type { ChartRow } from '@/types/usage'
import UsageChart from './UsageChart.vue'

const mounted: Array<ReturnType<typeof createApp>> = []

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

function chartRow(label: string, totalPct: number): ChartRow {
  return {
    sessionKey: null,
    label,
    inputPct: totalPct,
    outputPct: 0,
    totalPct,
    valueLabel: totalPct > 0 ? '1' : '0',
  }
}

function rowByLabel(root: HTMLElement, label: string): HTMLElement {
  const row = Array.from(root.querySelectorAll<HTMLElement>('.usage-bar-row'))
    .find(candidate => candidate.querySelector('.usage-bar-row__label')?.textContent === label)
  if (!row) throw new Error(`Missing usage chart row: ${label}`)
  return row
}

describe('UsageChart endpoint rendering', () => {
  it('omits endpoint caps while preserving row values and widths', async () => {
    i18n.global.locale.value = 'en'
    const root = document.createElement('div')
    document.body.appendChild(root)
    const app = createApp(UsageChart, {
      chartMode: 'tokens',
      range: '7',
      caption: 'Daily usage',
      rows: [
        chartRow('zero', 0),
        chartRow('half', 50),
        chartRow('tiny-positive', 0.01),
      ],
    })
    app.use(i18n)
    app.mount(root)
    mounted.push(app)
    await nextTick()

    const zero = rowByLabel(root, 'zero')
    expect(zero.querySelector('.usage-bar-row__cap')).toBeNull()
    expect(parseFloat(
      zero.querySelector<HTMLElement>('.usage-bar-row__fill--input')?.style.width || '',
    )).toBe(0)
    expect(zero.querySelector('.usage-bar-row__value')?.textContent).toBe('0')

    const half = rowByLabel(root, 'half')
    expect(half.querySelector('.usage-bar-row__cap')).toBeNull()
    expect(parseFloat(
      half.querySelector<HTMLElement>('.usage-bar-row__fill--input')?.style.width || '',
    )).toBe(50)
    expect(half.querySelector('.usage-bar-row__value')?.textContent).toBe('1')

    const tinyPositive = rowByLabel(root, 'tiny-positive')
    expect(tinyPositive.querySelector('.usage-bar-row__cap')).toBeNull()
    expect(parseFloat(
      tinyPositive.querySelector<HTMLElement>('.usage-bar-row__fill--input')?.style.width || '',
    )).toBe(0)
    expect(tinyPositive.querySelector('.usage-bar-row__value')?.textContent).toBe('1')
  })
})
