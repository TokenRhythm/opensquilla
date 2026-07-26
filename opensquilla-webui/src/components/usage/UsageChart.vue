<template>
  <section class="usage-chart">
    <div class="usage-chart__head">
      <div class="control-segmented" role="tablist" :aria-label="t('usageLogs.chart.metric')">
        <button
          class="control-segmented__btn"
          :class="{ 'is-active': chartMode === 'tokens' }"
          role="tab"
          @click="emit('update:chartMode', 'tokens')"
        >{{ t('usageLogs.chart.tokens') }}</button>
        <button
          class="control-segmented__btn"
          :class="{ 'is-active': chartMode === 'cost' }"
          role="tab"
          @click="emit('update:chartMode', 'cost')"
        >{{ t('usageLogs.chart.cost') }}</button>
      </div>
      <div class="control-segmented" role="tablist" :aria-label="t('usageLogs.chart.dateRange')">
        <button
          v-for="r in ['all', '7', '14', '30']"
          :key="r"
          class="control-segmented__btn"
          :class="{ 'is-active': range === r }"
          role="tab"
          @click="emit('setRange', r)"
        >{{ r === 'all' ? t('usageLogs.chart.rangeAll') : t('usageLogs.chart.rangeDays', { days: r }) }}</button>
      </div>
    </div>
    <div class="usage-chart__legend">
      <span class="usage-chart__legend-item"><span class="usage-chart__swatch usage-chart__swatch--input" />{{ t('usageLogs.chart.input') }}</span>
      <span v-show="chartMode === 'tokens'" class="usage-chart__legend-item"><span class="usage-chart__swatch usage-chart__swatch--output" />{{ t('usageLogs.chart.output') }}</span>
      <span class="usage-chart__legend-spacer" />
      <span class="usage-chart__caption">{{ caption }}</span>
    </div>
    <div class="usage-bars">
      <template v-if="rows.length === 0">
        <div class="usage-bars__empty">
          <div class="usage-bars__empty-icon">
            <Icon name="usage" :size="36" />
          </div>
          <div>{{ t('usageLogs.chart.emptyWindow') }}</div>
        </div>
      </template>
      <component
        v-for="(row, i) in rows"
        :key="i"
        :is="row.sessionKey ? 'button' : 'div'"
        class="usage-bar-row"
        :class="{ 'is-static': !row.sessionKey }"
        :type="row.sessionKey ? 'button' : undefined"
        :title="row.sessionKey ? t('usageLogs.chart.openSession', { session: row.sessionKey }) : row.label"
        :style="`--i:${i}`"
        @click="row.sessionKey && emit('openSession', row.sessionKey)"
      >
        <span class="usage-bar-row__label">{{ row.label }}</span>
        <span class="usage-bar-row__track">
          <span class="usage-bar-row__fill usage-bar-row__fill--input" :style="`width:${row.inputPct.toFixed(1)}%`" />
          <span
            v-if="row.outputPct > 0"
            class="usage-bar-row__fill usage-bar-row__fill--output"
            :style="`width:${row.outputPct.toFixed(1)}%`"
          />
          <span class="usage-bar-row__cap" :style="`left:${Math.min(100, row.totalPct).toFixed(1)}%`" />
        </span>
        <span class="usage-bar-row__value usage-mono">{{ row.valueLabel }}</span>
      </component>
    </div>
  </section>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ChartRow } from '@/types/usage'

const { t } = useI18n()

defineProps<{
  chartMode: 'tokens' | 'cost'
  range: string
  caption: string
  rows: ChartRow[]
}>()

const emit = defineEmits<{
  'update:chartMode': [mode: 'tokens' | 'cost']
  setRange: [range: string]
  openSession: [sessionKey: string]
}>()
</script>
