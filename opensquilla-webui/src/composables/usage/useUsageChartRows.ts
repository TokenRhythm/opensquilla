import { computed, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'
import type { ChartRow, SessionRow, UsageDay } from '@/types/usage'

const t = i18n.global.t

export function useUsageChartRows(options: {
  visibleSessions: ComputedRef<SessionRow[]>
  serverDays?: ComputedRef<UsageDay[] | null>
  chartMode: Ref<'tokens' | 'cost'>
  rowVal: (row: Record<string, unknown>, ...keys: string[]) => unknown
  fmtCost: (
    usd: number | null | undefined,
    opts?: { decimals?: number; source?: object },
  ) => string
  fmtNum: (value: number | null | undefined) => string
}) {
  const chartCaption = computed(() => {
    const days = options.serverDays?.value
    if (days) {
      const shown = Math.min(30, days.length)
      const suffix = days.length > shown
        ? ` · ${t('usageLogs.chart.showingOf', { shown, total: days.length })}`
        : ''
      return t('usageLogs.chart.daily') + suffix
    }
    const pool = options.visibleSessions.value.filter(r => {
      const inp = Number(options.rowVal(r, 'input_tokens', 'inputTokens') || 0)
      const out = Number(options.rowVal(r, 'output_tokens', 'outputTokens') || 0)
      return (inp + out) > 0
    })
    const shown = Math.min(20, pool.length)
    const suffix = pool.length > shown ? ` · ${t('usageLogs.chart.showingOf', { shown, total: pool.length })}` : ''
    return (options.chartMode.value === 'cost'
      ? t('usageLogs.chart.topByCost')
      : t('usageLogs.chart.topByTokens')) + suffix
  })

  const chartRows = computed((): ChartRow[] => {
    const days = options.serverDays?.value
    if (days) {
      const visibleDays = days.slice(-30)
      if (visibleDays.length === 0) return []
      let maxValue = Math.max(...visibleDays.map(day => (
        options.chartMode.value === 'cost'
          ? day.totals.cost
          : day.totals.input + day.totals.output
      )))
      if (maxValue === 0) maxValue = 1
      return visibleDays.map(day => {
        if (options.chartMode.value === 'cost') {
          const percent = (day.totals.cost / maxValue) * 100
          return {
            sessionKey: null,
            label: day.date,
            inputPct: percent,
            outputPct: 0,
            totalPct: percent,
            valueLabel: options.fmtCost(day.totals.cost, { source: day.totals }),
          }
        }
        const inputPct = (day.totals.input / maxValue) * 100
        const outputPct = (day.totals.output / maxValue) * 100
        return {
          sessionKey: null,
          label: day.date,
          inputPct,
          outputPct,
          totalPct: inputPct + outputPct,
          valueLabel: options.fmtNum(day.totals.input + day.totals.output),
        }
      })
    }
    const sorted = [...options.visibleSessions.value].filter(r => {
      const inp = Number(options.rowVal(r, 'input_tokens', 'inputTokens') || 0)
      const out = Number(options.rowVal(r, 'output_tokens', 'outputTokens') || 0)
      return (inp + out) > 0
    }).sort((a, b) => {
      if (options.chartMode.value === 'cost') {
        return (Number(options.rowVal(b, 'cost_usd', 'costUsd') || 0)) - (Number(options.rowVal(a, 'cost_usd', 'costUsd') || 0))
      }
      const totalA = Number(options.rowVal(a, 'input_tokens', 'inputTokens') || 0) + Number(options.rowVal(a, 'output_tokens', 'outputTokens') || 0)
      const totalB = Number(options.rowVal(b, 'input_tokens', 'inputTokens') || 0) + Number(options.rowVal(b, 'output_tokens', 'outputTokens') || 0)
      return totalB - totalA
    }).slice(0, 20)

    if (sorted.length === 0) return []

    let maxVal = 0
    if (options.chartMode.value === 'cost') {
      maxVal = Math.max(...sorted.map(r => Number(options.rowVal(r, 'cost_usd', 'costUsd') || 0)))
    } else {
      maxVal = Math.max(...sorted.map(r =>
        Number(options.rowVal(r, 'input_tokens', 'inputTokens') || 0) + Number(options.rowVal(r, 'output_tokens', 'outputTokens') || 0)
      ))
    }
    if (maxVal === 0) maxVal = 1

    return sorted.map(row => {
      const fullLabel = (options.rowVal(row, 'session', 'sessionKey', 'key') || '-') as string
      const label = fullLabel.length > 26 ? fullLabel.slice(0, 24) + '...' : fullLabel
      if (options.chartMode.value === 'cost') {
        const cost = Number(options.rowVal(row, 'cost_usd', 'costUsd') || 0)
        const pct = (cost / maxVal) * 100
        return {
          sessionKey: fullLabel,
          label,
          inputPct: pct,
          outputPct: 0,
          totalPct: pct,
          valueLabel: options.fmtCost(cost, { source: row }),
        }
      }

      const inp = Number(options.rowVal(row, 'input_tokens', 'inputTokens') || 0)
      const out = Number(options.rowVal(row, 'output_tokens', 'outputTokens') || 0)
      const total = inp + out
      const inputPct = (inp / maxVal) * 100
      const outputPct = (out / maxVal) * 100
      return {
        sessionKey: fullLabel,
        label,
        inputPct,
        outputPct,
        totalPct: inputPct + outputPct,
        valueLabel: options.fmtNum(total),
      }
    })
  })

  return { chartCaption, chartRows }
}
