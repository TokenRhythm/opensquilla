import { computed, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'
import type { ChartRow, UsageDay, UsageSession, UsageTotals } from '@/types/usage'

const t = i18n.global.t

export function useUsageChartRows(options: {
  visibleSessions: ComputedRef<UsageSession[]>
  serverDays?: ComputedRef<UsageDay[] | null>
  chartMode: Ref<'tokens' | 'cost'>
  fmtCost: (
    usd: number | null | undefined,
    opts?: { decimals?: number; source?: UsageSession | UsageTotals },
  ) => string
  fmtNum: (value: number | null | undefined) => string
  taskName: (row: UsageSession) => string
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
      return (r.inputTokens ?? 0) + (r.outputTokens ?? 0) > 0
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
      const visibleDays = [...days]
        .sort((a, b) => b.date.localeCompare(a.date))
        .slice(0, 30)
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
      return (r.inputTokens ?? 0) + (r.outputTokens ?? 0) > 0
    }).sort((a, b) => {
      if (options.chartMode.value === 'cost') {
        return (b.costUsd ?? 0) - (a.costUsd ?? 0)
      }
      return ((b.inputTokens ?? 0) + (b.outputTokens ?? 0)) - ((a.inputTokens ?? 0) + (a.outputTokens ?? 0))
    }).slice(0, 20)

    if (sorted.length === 0) return []

    let maxVal = 0
    if (options.chartMode.value === 'cost') {
      maxVal = Math.max(...sorted.map(r => (r.costUsd ?? 0)))
    } else {
      maxVal = Math.max(...sorted.map(r => (r.inputTokens ?? 0) + (r.outputTokens ?? 0)))
    }
    if (maxVal === 0) maxVal = 1

    return sorted.map(row => {
      const sessionKey = row.sessionKey
      const label = options.taskName(row)
      if (options.chartMode.value === 'cost') {
        const cost = row.costUsd ?? 0
        const pct = (cost / maxVal) * 100
        return {
          sessionKey,
          label,
          inputPct: pct,
          outputPct: 0,
          totalPct: pct,
          valueLabel: options.fmtCost(cost, { source: row }),
        }
      }

      const inp = row.inputTokens ?? 0
      const out = row.outputTokens ?? 0
      const total = inp + out
      const inputPct = (inp / maxVal) * 100
      const outputPct = (out / maxVal) * 100
      return {
        sessionKey,
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
