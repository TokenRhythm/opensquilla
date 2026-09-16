import { serializeNativeBilling } from './nativeBilling'
import type { UsageSession, UsageSnapshot } from '@/types/usage'

const DEFAULT_CNY_RATE = 7.25

export function buildUsageCsv(
  snapshot: UsageSnapshot | null,
  visibleRows: UsageSession[],
  cnyRate = DEFAULT_CNY_RATE,
): string {
  const headers = [
    'row_type',
    'aggregation_mode',
    'coverage_status',
    'range_preset',
    'range_from_ms',
    'range_to_ms',
    'timezone',
    'session',
    'input_tokens',
    'output_tokens',
    'cache_read_tokens',
    'cache_write_tokens',
    'cost_usd',
    'cost_cny',
    'billed_cost_usd',
    'estimated_cost_usd',
    'estimated_event_count',
    'cost_source',
    'missing_cost_entries',
    'cost_ephemeral',
    'model',
    'native_billed_by_currency',
    'pending_billing_receipt_count',
    'native_billing_coverage_status',
    'native_billing_exact_from_ms',
    'native_billing_reason_codes',
    'native_billing_missing_confirmed_receipt_count',
    'native_billing_pending_receipt_count',
  ]
  const common = [
    snapshot?.mode || 'session_approximation',
    snapshot?.coverage.status || 'approximate',
    snapshot?.range.preset || '',
    snapshot?.range.fromMs ?? '',
    snapshot?.range.toMs ?? '',
    snapshot?.timezone || '',
  ]
  const totals = snapshot?.totals
  const summary = [
    'summary',
    ...common,
    '',
    totals?.input ?? '',
    totals?.output ?? '',
    totals?.cacheRead ?? '',
    totals?.cacheWrite ?? '',
    totals?.cost != null ? totals.cost.toFixed(9) : '',
    totals?.cost != null ? (totals.cost * cnyRate).toFixed(9) : '',
    totals?.billedCost != null ? totals.billedCost.toFixed(9) : '',
    totals?.estimatedCost != null ? totals.estimatedCost.toFixed(9) : '',
    totals?.estimatedEventCount ?? '',
    totals?.costSource || '',
    totals?.missingCostEntries ?? '',
    'false',
    '',
    serializeNativeBilling(totals),
    totals?.pendingBillingReceiptCount ?? '',
    snapshot?.coverage.nativeBilling.status || '',
    snapshot?.coverage.nativeBilling.exactFromMs ?? '',
    snapshot?.coverage.nativeBilling.reasonCodes.join('|') || '',
    snapshot?.coverage.nativeBilling.missingConfirmedReceiptCount ?? '',
    snapshot?.coverage.nativeBilling.pendingReceiptCount ?? '',
  ]
  const rows = visibleRows.map(row => {
    const costUsd = row.costUsd
    return [
      'session',
      ...common,
      row.session || row.sessionKey,
      row.inputTokens ?? '',
      row.outputTokens ?? '',
      row.cacheReadTokens ?? '',
      row.cacheWriteTokens ?? '',
      costUsd?.toFixed(6) ?? '',
      costUsd != null ? (costUsd * cnyRate).toFixed(6) : '',
      row.billedCostUsd?.toFixed(6) ?? '',
      row.estimatedCostUsd?.toFixed(6) ?? '',
      row.estimatedEventCount ?? '',
      row.costSource,
      row.missingCostEntries ?? '',
      row.costEphemeral ? 'true' : 'false',
      row.model || '',
      serializeNativeBilling(row),
      row.pendingBillingReceiptCount,
      snapshot?.coverage.nativeBilling.status || '',
      snapshot?.coverage.nativeBilling.exactFromMs ?? '',
      snapshot?.coverage.nativeBilling.reasonCodes.join('|') || '',
      snapshot?.coverage.nativeBilling.missingConfirmedReceiptCount ?? '',
      snapshot?.coverage.nativeBilling.pendingReceiptCount ?? '',
    ]
  })
  return [headers, summary, ...rows]
    .map(row => row.map(item => '"' + String(item).replace(/"/g, '""') + '"').join(','))
    .join('\n')
}
