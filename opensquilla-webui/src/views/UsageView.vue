<template>
  <div class="usage-stage control-stage control-stage--spacious">
    <header class="usage-stage__header control-stage__header">
      <div class="usage-stage__title-block control-stage__title-block">
        <h1 class="usage-stage__title control-stage__title">{{ t('usageLogs.usage.title') }}</h1>
        <p class="usage-stage__subtitle control-stage__subtitle">{{ t('usageLogs.usage.subtitle') }}</p>
        <small class="usage-range-notice" aria-live="polite">{{ rangeHiddenHint }}</small>
      </div>
      <div class="usage-stage__actions control-stage__actions mobile-action-strip">
        <div class="control-segmented mobile-action-strip__item" role="group" :aria-label="t('usageLogs.usage.currency')">
          <button
            class="control-segmented__btn"
            :class="{ 'is-active': currency === 'USD' }"
            :title="t('usageLogs.usage.usDollar')"
            @click="setCurrency('USD')"
          >$ USD</button>
          <button
            class="control-segmented__btn"
            :class="{ 'is-active': currency === 'CNY' }"
            :title="t('usageLogs.usage.chineseYuan')"
            @click="setCurrency('CNY')"
          >¥ CNY</button>
        </div>
        <button class="btn btn--ghost mobile-action-strip__button" :title="t('usageLogs.usage.downloadCsv')" @click="exportCsv">
          <Icon name="download" :size="16" /><span class="mobile-action-strip__label">{{ t('usageLogs.usage.export') }}</span>
        </button>
        <button class="btn btn--ghost mobile-action-strip__button" :title="t('usageLogs.usage.refresh')" :disabled="refreshing" @click="refresh">
          <Icon name="refresh" :size="16" /><span class="mobile-action-strip__label">{{ refreshing ? t('usageLogs.usage.refreshing') : t('usageLogs.usage.refresh') }}</span>
        </button>
      </div>
    </header>

    <UsageSummaryStats
      :total-tokens="totalTokensDisplay"
      :token-parts="tokensBreakdownParts"
      :total-cost="totalCostDisplay"
      :cost-hint-text="costHintText"
      :cost-hint-title="costHintTitle"
      :session-count="sessionCountDisplay"
      :avg-cost="avgCostDisplay"
    />

    <UsageChart
      v-model:chart-mode="chartMode"
      :range="range"
      :caption="chartCaption"
      :rows="chartRows"
      @set-range="setRange"
      @open-session="openSession"
    />

    <UsageModelCards
      :model-cards="modelCards"
      :models-meta="modelsMeta"
      :fmt-cost="fmtCost"
      :cost-source-classes-for-model-card="costSourceClassesForModelCard"
      :cost-source-label-for-model-card="costSourceLabelForModelCard"
      :cost-source-tooltip-for-model-card="costSourceTooltipForModelCard"
    />

    <div v-if="usageLoading && sortedRows.length === 0" class="state">
      <LoadingSpinner />
    </div>

    <ErrorState v-else-if="usageError" :message="usageError" :on-retry="loadData" />

    <UsageSessionsTable
      v-else
      :table-columns="tableColumns"
      :sortable-cols="sortableCols"
      :sort-col="sortCol"
      :sort-asc="sortAsc"
      :sorted-rows="sortedRows"
      :sessions-meta="sessionsMeta"
      :expanded-sessions="expandedSessions"
      :fmt-cost="fmtCost"
      :cost-source-label="costSourceLabel"
      :cost-source-tooltip="costSourceTooltip"
      :cost-source-classes="costSourceClasses"
      :cost-source-classes-for-breakdown="costSourceClassesForBreakdown"
      :cost-source-label-for-breakdown="costSourceLabelForBreakdown"
      :cost-source-tooltip-for-breakdown="costSourceTooltipForBreakdown"
      :model-display-label="modelDisplayLabel"
      :row-key="rowKey"
      :row-breakdown="rowBreakdown"
      :row-breakdown-total-tokens="rowBreakdownTotalTokens"
      :row-breakdown-total-cost="rowBreakdownTotalCost"
      :row-breakdown-any-prorated="rowBreakdownAnyProrated"
      @sort="setSort"
      @open-session="openSession"
      @toggle-model-expand="toggleModelExpand"
    />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import UsageSummaryStats from '@/components/usage/UsageSummaryStats.vue'
import UsageChart from '@/components/usage/UsageChart.vue'
import UsageModelCards from '@/components/usage/UsageModelCards.vue'
import UsageSessionsTable from '@/components/usage/UsageSessionsTable.vue'
import Icon from '@/components/Icon.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import { useUsageData } from '@/composables/usage/useUsageData'

const { t } = useI18n()

const {
  currency,
  sortCol,
  sortAsc,
  chartMode,
  range,
  usageLoading,
  usageError,
  expandedSessions,
  tableColumns,
  sortableCols,
  rangeHiddenHint,
  totalTokensDisplay,
  tokensBreakdownParts,
  totalCostDisplay,
  costHintText,
  costHintTitle,
  sessionCountDisplay,
  avgCostDisplay,
  chartCaption,
  chartRows,
  modelCards,
  modelsMeta,
  sortedRows,
  sessionsMeta,
  setCurrency,
  setRange,
  setSort,
  openSession,
  toggleModelExpand,
  loadData,
  exportCsv,
  fmtCost,
  costSourceLabel,
  costSourceTooltip,
  costSourceClasses,
  costSourceClassesForBreakdown,
  costSourceLabelForBreakdown,
  costSourceTooltipForBreakdown,
  costSourceClassesForModelCard,
  costSourceLabelForModelCard,
  costSourceTooltipForModelCard,
  modelDisplayLabel,
  rowKey,
  rowBreakdown,
  rowBreakdownTotalTokens,
  rowBreakdownTotalCost,
  rowBreakdownAnyProrated,
} = useUsageData()

// Manual refresh shows a busy state; loadData (also the poll/mount handler) now
// returns the refresh promise, so a local flag spans just the user-driven load.
const refreshing = ref(false)
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await loadData()
  } finally {
    refreshing.value = false
  }
}
</script>

<style>
.usage-range-notice {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  margin-top: 4px;
  min-height: 1.2em;
}

/* Chart */
.usage-chart {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--sp-4) var(--sp-5);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.usage-chart__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.usage-chart__legend {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  font-size: var(--fs-xs);
  color: var(--text-muted);
  flex-wrap: wrap;
}
.usage-chart__legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.usage-chart__swatch {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-radius: var(--radius-sm);
}
.usage-chart__swatch--input {
  background: var(--accent);
}
.usage-chart__swatch--output {
  background: var(--chart-2);
}
.usage-chart__legend-spacer {
  flex: 1;
}
.usage-chart__caption {
  font-style: italic;
  color: var(--text-dim);
}

/* Bars */
.usage-bars {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.usage-bars__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: var(--sp-6);
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.usage-bars__empty-icon {
  color: var(--text-dim);
}
.usage-bar-row {
  display: grid;
  grid-template-columns: minmax(0, 180px) 1fr auto;
  align-items: center;
  gap: 10px;
  padding: 5px 0;
  background: transparent;
  border: 0;
  cursor: pointer;
  text-align: left;
  font: inherit;
  color: inherit;
  transition: opacity var(--transition);
  animation: usage-fade-in var(--dur-enter) var(--ease-out) both;
  animation-delay: calc(var(--i) * 30ms);
}
.usage-bar-row:hover {
  opacity: 0.85;
}
.usage-bar-row.is-static {
  cursor: default;
}
.usage-bar-row.is-static:hover {
  opacity: 1;
}
.usage-bar-row__label {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: right;
  padding-right: 4px;
}
.usage-bar-row__track {
  position: relative;
  height: 18px;
  background: var(--bg-elevated);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.usage-bar-row__fill {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  border-radius: var(--radius-sm);
}
.usage-bar-row__fill--input {
  background: var(--accent);
}
.usage-bar-row__fill--output {
  background: var(--chart-2);
}
.usage-bar-row__cap {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--text);
  opacity: 0.3;
}
.usage-bar-row__value {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  min-width: 60px;
  text-align: right;
}

/* Models section */
.usage-models,
.usage-sessions {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.usage-section-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--sp-2);
}
.usage-section-title {
  font-size: var(--fs-lg);
  font-weight: 600;
  margin: 0;
}
.usage-section-meta {
  font-size: var(--fs-xs);
  color: var(--text-dim);
}

.usage-models__empty {
  padding: var(--sp-5);
  text-align: center;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.usage-model-card {
  gap: var(--sp-2);
}
.usage-model-card__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--sp-2);
}
.usage-model-card__id {
  display: flex;
  align-items: center;
  gap: 2px;
  min-width: 0;
}
.usage-model-card__provider {
  font-size: var(--fs-xs);
  color: var(--text-dim);
}
.usage-model-card__name {
  font-weight: 600;
  font-size: var(--fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.usage-model-card__share {
  font-size: var(--fs-xs);
  font-weight: 700;
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}
.usage-model-card__share-bar {
  height: 4px;
  background: var(--bg-elevated);
  border-radius: var(--radius-xs);
  overflow: hidden;
}
.usage-model-card__share-fill {
  display: block;
  height: 100%;
  background: var(--accent);
  border-radius: var(--radius-xs);
  transition: width var(--dur-enter) var(--ease-standard);
}
.usage-model-card__rows {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;
  margin: 0;
  font-size: var(--fs-xs);
}
.usage-model-card__rows > div {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 4px;
}
.usage-model-card__rows dt {
  color: var(--text-dim);
  font-weight: 500;
}
.usage-model-card__rows dd {
  margin: 0;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.usage-model-card__cost-row {
  grid-column: 1 / -1;
  border-top: 1px solid var(--border);
  padding-top: 4px;
  margin-top: 2px;
}

/* Table */
.usage-table-wrap {
  overflow-x: auto;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}
.usage-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm);
}
.usage-table th {
  text-align: left;
  padding: 10px 12px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--text-dim);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.usage-table td {
  padding: 10px 12px;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
  vertical-align: middle;
}
.usage-table tr:last-child td {
  border-bottom: 0;
}
.usage-th-sort {
  cursor: pointer;
  user-select: none;
  transition: color var(--transition);
}
.usage-th-sort:hover {
  color: var(--accent);
}
.usage-table__arrow {
  margin-left: 4px;
  color: var(--accent);
}
.usage-empty-row {
  text-align: center;
  padding: var(--sp-6);
}
.usage-sess-link {
  color: var(--accent);
  text-decoration: none;
  font-weight: 500;
}
.usage-sess-link:hover {
  text-decoration: underline;
}
.usage-mono {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}
.usage-dim {
  color: var(--text-dim);
}
.usage-cost {
  font-weight: 600;
}

/* Model toggle in table */
.usage-model-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: 0;
  padding: 0;
  cursor: pointer;
  font: inherit;
  color: var(--accent);
}
.usage-model-toggle:hover {
  text-decoration: underline;
}
.usage-model-caret {
  font-size: 10px;
  transition: transform var(--dur-base) var(--ease-standard);
}
.usage-model-toggle.open .usage-model-caret {
  transform: rotate(180deg);
}
.usage-model-text {
  color: var(--text-muted);
}

/* Expand row */
.usage-expand-row {
  background: var(--bg-elevated);
}
.usage-expand-cell {
  padding: 0 !important;
}
.usage-expand {
  padding: var(--sp-3) var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.usage-expand__head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-xs);
}
.usage-expand__connector {
  width: 2px;
  height: 16px;
  background: var(--accent);
  border-radius: 1px; /* radius-allow: hairline capsule = half the 2px connector width */
}
.usage-expand__eyebrow {
  font-weight: 700;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.1em;
}
.usage-expand__count {
  color: var(--text-muted);
}
.usage-expand__spacer {
  flex: 1;
}
.usage-expand__total {
  font-family: var(--font-mono);
  color: var(--text-muted);
}
.usage-expand__notice {
  font-size: var(--fs-xs);
  color: var(--warn);
  padding: 4px 8px;
  background: color-mix(in srgb, var(--warn) 8%, transparent);
  border-radius: var(--radius-sm);
}
.usage-expand__list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.usage-expand__row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 120px 80px 80px 100px;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
  animation: usage-fade-in var(--dur-base) var(--ease-out) both;
  animation-delay: calc(var(--i) * 30ms);
}
.usage-expand__model {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.usage-expand__provider {
  color: var(--text-dim);
}
.usage-expand__share {
  display: flex;
  align-items: center;
  gap: 6px;
}
.usage-expand__share-track {
  flex: 1;
  height: 4px;
  background: var(--bg-elevated);
  border-radius: var(--radius-xs);
  overflow: hidden;
}
.usage-expand__share-fill {
  display: block;
  height: 100%;
  background: var(--accent);
  border-radius: var(--radius-xs);
}
.usage-expand__share-pct {
  font-family: var(--font-mono);
  color: var(--text-dim);
  min-width: 36px;
  text-align: right;
}
.usage-expand__tokens,
.usage-expand__cost {
  font-family: var(--font-mono);
  text-align: right;
}

/* Cost source badges */
.usage-source {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: 10.5px;
  font-weight: 600;
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  color: var(--text-muted);
}
.usage-source--provider_billed {
  border-color: color-mix(in srgb, var(--ok) 50%, var(--border));
  color: var(--ok);
}
.usage-source--provider_billed_prorated {
  border-style: dashed;
  border-color: color-mix(in srgb, var(--ok) 50%, var(--border));
  color: var(--ok);
}
.usage-source--opensquilla_estimate {
  border-color: color-mix(in srgb, var(--warn) 50%, var(--border));
  color: var(--warn);
}
.usage-source--mixed {
  border-color: color-mix(in srgb, var(--accent) 50%, var(--border));
  color: var(--accent);
}
.usage-source--unavailable {
  border-color: color-mix(in srgb, var(--danger) 50%, var(--border));
  color: var(--danger);
}
.usage-source--ephemeral {
  opacity: 0.7;
  font-style: italic;
}

/* Empty state */
.state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: var(--sp-5);
  color: var(--text-muted);
}
.state-icon {
  color: var(--text-dim);
}
.state-title {
  font-weight: 600;
  color: var(--text);
}
.state-text {
  margin: 0;
  font-size: var(--fs-sm);
}

/* Animations */
@keyframes usage-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* Responsive */
@media (max-width: 720px) {
  .usage-stage__header {
    flex-direction: column;
    align-items: stretch;
  }
  .usage-stage__actions {
    width: 100%;
  }
  .usage-bar-row {
    grid-template-columns: minmax(0, 100px) 1fr auto;
  }
  .usage-expand__row {
    grid-template-columns: 1fr 80px;
    gap: 4px;
  }
  .usage-expand__share,
  .usage-expand__tokens,
  .usage-expand__cost,
  .usage-expand__source {
    display: none;
  }
}
</style>
