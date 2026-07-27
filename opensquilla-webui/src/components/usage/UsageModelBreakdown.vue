<template>
  <div class="usage-expand">
    <div class="usage-expand__head">
      <span class="usage-expand__connector" aria-hidden="true" />
      <span class="usage-expand__eyebrow">{{ t('usageLogs.breakdown.title') }}</span>
      <span class="usage-expand__count">{{ t('usageLogs.breakdown.modelCount', { count: rows.length }) }}</span>
      <span class="usage-expand__spacer" />
      <span class="usage-expand__total">{{ t('usageLogs.breakdown.total', { tokens: totalTokens.toLocaleString(), cost: fmtCost(totalCost, { source: nativeSource }) }) }}</span>
    </div>
    <div v-if="anyProrated" class="usage-expand__notice" role="note">
      {{ t('usageLogs.breakdown.proratedNotice') }}
    </div>
    <div class="usage-expand__list" role="table" :aria-label="t('usageLogs.breakdown.title')">
      <div
        v-for="(m, mi) in rows"
        :key="mi"
        class="usage-expand__row"
        :style="`--i:${mi}`"
        role="row"
      >
        <div class="usage-expand__model" role="cell" :title="m.model">
          <span v-if="m.provider" class="usage-expand__provider">{{ m.provider }}/</span><span class="usage-expand__name">{{ m.name }}</span>
        </div>
        <div class="usage-expand__share" role="cell">
          <span class="usage-expand__share-track">
            <span class="usage-expand__share-fill" :style="`width:${m.share.toFixed(2)}%`" />
          </span>
          <span class="usage-expand__share-pct">{{ m.share.toFixed(1) }}%</span>
        </div>
        <div class="usage-expand__tokens" role="cell">{{ m.tokens.toLocaleString() }}</div>
        <div class="usage-expand__cost" role="cell">{{ fmtCost(m.cost, { source: m }) }}</div>
        <div class="usage-expand__source" role="cell">
          <span
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
import type { BreakdownRow } from '@/types/usage'

const { t } = useI18n()

defineProps<{
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
</script>
