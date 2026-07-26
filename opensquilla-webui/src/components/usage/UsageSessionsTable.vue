<template>
  <section class="usage-sessions">
    <div class="usage-section-head">
      <h3 class="usage-section-title">{{ t('usageLogs.sessions.title') }}</h3>
      <span class="usage-section-meta">{{ sessionsMeta }}</span>
    </div>
    <div class="usage-table-wrap">
      <table class="usage-table">
        <thead>
          <tr>
            <th
              v-for="col in tableColumns"
              :key="col.key"
              :class="{ 'usage-th-sort': sortableCols.includes(col.key) }"
              @click="sortableCols.includes(col.key) ? emit('sort', col.key) : undefined"
            >
              {{ col.label }}
              <span v-if="sortCol === col.key" class="usage-table__arrow">{{ sortAsc ? ' ▲' : ' ▼' }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <template v-if="sortedRows.length === 0">
            <tr>
              <td :colspan="tableColumns.length" class="usage-empty-row">
                <div class="state">
                  <div class="state-icon">
                    <Icon name="usage" :size="36" />
                  </div>
                  <div class="state-title">{{ t('usageLogs.sessions.emptyTitle') }}</div>
                  <p class="state-text">{{ t('usageLogs.sessions.emptyText') }}</p>
                </div>
              </td>
            </tr>
          </template>
          <template v-for="row in sortedRows" :key="rowKey(row.raw)">
            <tr>
              <td data-label="Session">
                <a
                  v-if="row.sessionKey"
                  href="#"
                  class="usage-sess-link"
                  :title="t('usageLogs.sessions.openChat', { session: row.sessionKey })"
                  @click.prevent="emit('openSession', row.sessionKey)"
                >{{ row.sessionLabel }}</a>
                <span v-else>{{ row.sessionLabel }}</span>
              </td>
              <td data-label="Modified" class="usage-mono usage-dim">{{ row.modified }}</td>
              <td data-label="Input" class="usage-mono">{{ row.inputTokens != null ? row.inputTokens.toLocaleString() : '-' }}</td>
              <td data-label="Output" class="usage-mono">{{ row.outputTokens != null ? row.outputTokens.toLocaleString() : '-' }}</td>
              <td data-label="Cache R" class="usage-mono usage-dim">{{ row.cacheReadTokens != null ? row.cacheReadTokens.toLocaleString() : '-' }}</td>
              <td data-label="Cache W" class="usage-mono usage-dim">{{ row.cacheWriteTokens != null ? row.cacheWriteTokens.toLocaleString() : '-' }}</td>
              <td data-label="Cost" class="usage-mono usage-cost">{{ fmtCost(row.cost, { source: row.raw }) }}</td>
              <td data-label="Source">
                <span
                  class="usage-source"
                  :class="costSourceClasses(row.raw)"
                  :title="costSourceTooltip(row.raw)"
                >{{ costSourceLabel(row.raw) }}</span>
              </td>
              <td data-label="Model">
                <button
                  v-if="row.hasModelBreakdown"
                  class="usage-model-toggle"
                  :class="{ open: expandedSessions.has(row.rowIdentity) }"
                  @click="emit('toggleModelExpand', row)"
                >
                  <span>{{ modelDisplayLabel(row.raw) }}</span><span class="usage-model-caret">▾</span>
                </button>
                <span v-else class="usage-model-text">{{ modelDisplayLabel(row.raw) }}</span>
              </td>
            </tr>
            <tr v-if="expandedSessions.has(row.rowIdentity)" class="usage-expand-row">
              <td class="usage-expand-cell" :colspan="tableColumns.length">
                <UsageModelBreakdown
                  :rows="rowBreakdown(row.raw)"
                  :total-tokens="rowBreakdownTotalTokens(row.raw)"
                  :total-cost="rowBreakdownTotalCost(row.raw)"
                  :native-source="row.raw"
                  :any-prorated="rowBreakdownAnyProrated(row.raw)"
                  :fmt-cost="fmtCost"
                  :cost-source-classes="costSourceClassesForBreakdown"
                  :cost-source-label="costSourceLabelForBreakdown"
                  :cost-source-tooltip="costSourceTooltipForBreakdown"
                />
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import UsageModelBreakdown from '@/components/usage/UsageModelBreakdown.vue'

const { t } = useI18n()
import type { BreakdownRow, SessionRow, SortedRow, TableColumn } from '@/types/usage'

defineProps<{
  tableColumns: TableColumn[]
  sortableCols: string[]
  sortCol: string
  sortAsc: boolean
  sortedRows: SortedRow[]
  sessionsMeta: string
  expandedSessions: Set<string>
  fmtCost: (
    cost: number | null | undefined,
    opts?: { decimals?: number; source?: object },
  ) => string
  costSourceLabel: (row: SessionRow) => string
  costSourceTooltip: (row: SessionRow) => string
  costSourceClasses: (row: SessionRow) => Record<string, boolean>
  costSourceClassesForBreakdown: (row: BreakdownRow) => Record<string, boolean>
  costSourceLabelForBreakdown: (row: BreakdownRow) => string
  costSourceTooltipForBreakdown: (row: BreakdownRow) => string
  modelDisplayLabel: (row: SessionRow) => string
  rowKey: (row: SessionRow) => string
  rowBreakdown: (row: SessionRow) => BreakdownRow[]
  rowBreakdownTotalTokens: (row: SessionRow) => number
  rowBreakdownTotalCost: (row: SessionRow) => number
  rowBreakdownAnyProrated: (row: SessionRow) => boolean
}>()

const emit = defineEmits<{
  sort: [column: string]
  openSession: [sessionKey: string]
  toggleModelExpand: [row: SortedRow]
}>()
</script>
