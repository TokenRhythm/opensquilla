import { ref } from 'vue'
import type { UsageRangeSelection } from '@/types/usage'

const CURRENCY_KEY = 'opensquilla-currency'
const RANGE_KEY = 'opensquilla-usage-range'

export function parseUsageRange(value: unknown): UsageRangeSelection | undefined {
  return typeof value === 'string' && ['all', 'today', '7', '14', '30'].includes(value)
    ? value as UsageRangeSelection
    : undefined
}

export function normalizeUsageRange(range: string | null): UsageRangeSelection {
  return parseUsageRange(range) ?? '7'
}

export function useUsagePreferences() {
  const currency = ref(localStorage.getItem(CURRENCY_KEY) || 'USD')
  const range = ref(normalizeUsageRange(localStorage.getItem(RANGE_KEY)))

  function setCurrency(cur: string) {
    if (cur !== 'USD' && cur !== 'CNY') return
    currency.value = cur
    localStorage.setItem(CURRENCY_KEY, currency.value)
  }

  function setRange(nextRange: string) {
    range.value = normalizeUsageRange(nextRange)
    localStorage.setItem(RANGE_KEY, range.value)
  }

  return {
    currency,
    range,
    setCurrency,
    setRange,
  }
}
