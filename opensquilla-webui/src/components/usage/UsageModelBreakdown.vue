<template>
  <div class="usage-expand">
    <div class="usage-expand__head">
      <span class="usage-expand__connector" aria-hidden="true" />
      <span class="usage-expand__eyebrow">{{ t(hasCapabilities ? 'usageLogs.breakdown.callTitle' : 'usageLogs.breakdown.title') }}</span>
      <span v-if="!hasCapabilities" class="usage-expand__count">{{ t('usageLogs.breakdown.modelCount', { count: rows.length }) }}</span>
      <span class="usage-expand__spacer" />
      <span class="usage-expand__total">{{ hasCapabilities ? fmtCost(totalCost, { source: nativeSource }) : t('usageLogs.breakdown.total', { tokens: totalTokens.toLocaleString(), cost: fmtCost(totalCost, { source: nativeSource }) }) }}</span>
    </div>
    <div v-if="anyProrated" class="usage-expand__notice" role="note">
      {{ t('usageLogs.breakdown.proratedNotice') }}
    </div>
    <div class="usage-expand__list" role="table" :aria-label="t(hasCapabilities ? 'usageLogs.breakdown.callTitle' : 'usageLogs.breakdown.title')">
      <div
        v-for="(m, mi) in rows"
        :key="mi"
        class="usage-expand__row"
        :style="`--i:${mi}`"
        role="row"
      >
        <div class="usage-expand__model" role="cell" :title="m.model">
          <span class="usage-expand__identity"><span v-if="m.provider" class="usage-expand__provider">{{ m.provider }}/</span><span class="usage-expand__name">{{ m.name }}</span></span>
        </div>
        <div class="usage-expand__share" role="cell">
          <span v-if="totalCost > 0 && !unknownZero(m)" class="usage-expand__share-track">
            <span class="usage-expand__share-fill" :style="`width:${m.share.toFixed(2)}%`" />
          </span>
          <span class="usage-expand__share-pct">{{ totalCost > 0 && !unknownZero(m) ? `${m.share.toFixed(1)}%` : '—' }}</span>
        </div>
        <div class="usage-expand__tokens" role="cell">
          <template v-if="isCapability(m)">
            <span v-for="(line, index) in meterLines(m)" :key="index" class="usage-expand__meter">{{ line }}</span>
          </template>
          <template v-else>
            <span class="usage-expand__metric-label">{{ t('usageLogs.metrics.tokens') }}</span>
            <span>{{ m.tokenUnknownItemCount ? '—' : m.tokens.toLocaleString() }}</span>
          </template>
        </div>
        <div class="usage-expand__cost" :class="{ 'usage-expand__cost--unpriced': m.cost === 0 && hasUnpricedUsage(m) }" role="cell">
          <span class="usage-expand__metric-label">{{ t('usageLogs.metrics.cost') }}</span>
          <span>{{ m.costSource === 'none' && m.cost === 0 ? '—' : fmtCost(m.cost, { source: m }) }}</span>
        </div>
        <div class="usage-expand__source" role="cell">
          <span
            v-if="m.costSource !== 'unavailable'"
            class="usage-source"
            :class="costSourceClasses(m)"
            :title="costSourceTooltip(m)"
          >{{ costSourceLabel(m) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { computed } from 'vue'
import { hasUnpricedUsage } from '@/composables/usage/nativeBilling'
import type { BreakdownRow } from '@/types/usage'

const { t, te } = useI18n()

const props = defineProps<{
  rows: BreakdownRow[]
  totalTokens: number
  totalCost: number
  nativeSource: object
  anyProrated: boolean
  fmtCost: (
    cost: number | null | undefined,
    opts?: { decimals?: number; source?: object },
  ) => string
  costSourceClasses: (row: BreakdownRow) => Record<string, boolean>
  costSourceLabel: (row: BreakdownRow) => string
  costSourceTooltip: (row: BreakdownRow) => string
}>()

function isCapability(row: BreakdownRow): boolean {
  return Boolean(row.capability && !['llm', 'unknown'].includes(row.capability))
    || Boolean(row.meters?.some(meter => meter.unit !== 'token'))
}

const hasCapabilities = computed(() => props.rows.some(isCapability))

function unknownZero(row: BreakdownRow): boolean {
  return row.cost === 0 && (row.costSource === 'none' || hasUnpricedUsage(row))
}

function meterLines(row: BreakdownRow): string[] {
  const meters = (row.meters || []).filter(meter =>
    meter.unknownCount > 0 || !/^0(?:\.0+)?$/.test(meter.quantity),
  )
  return meters.map(meter => {
    const metricKey = `usageLogs.capabilities.metrics.${meter.metric}`
    const unitKey = `usageLogs.capabilities.units.${meter.unit.replace(/-/g, '_')}`
    const label = te(metricKey) ? t(metricKey) : meter.metric.replace(/_/g, ' ')
    const unit = te(unitKey) ? t(unitKey) : meter.unit
    const unknown = meter.unknownCount > 0 && meter.unknownCount >= meter.measurementCount
    const value = unknown ? t('usageLogs.capabilities.unknown') : `${meter.quantity} ${unit}`
    const text = `${label} ${value}`
    const dimensions = Object.entries(meter.dimensions)
      .filter(([key]) => !['operation', 'provider', 'model', 'mode', 'billing_component'].includes(key))
      .map(([, value]) => value).join(' · ')
    const result = !unknown && meter.unknownCount > 0
      ? t('usageLogs.capabilities.partialMeter', { value: text, count: meter.unknownCount }) : text
    return dimensions ? `${result} · ${dimensions}` : result
  })
}
</script>

<style scoped>
.usage-expand__identity { min-width: 0; overflow-wrap: anywhere; }
.usage-expand__meter { display: block; flex-basis: 100%; white-space: normal; overflow-wrap: anywhere; }
</style>
