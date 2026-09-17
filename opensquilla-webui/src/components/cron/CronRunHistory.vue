<template>
  <div class="cron-detail">
    <div class="cron-detail__head">
      <div>
        <span class="cron-detail__eyebrow">{{ t('cronSkills.runHistory.eyebrow') }}</span>
        <strong class="cron-detail__name">{{ localizedCronJobName(job.name, job.id, t) }}</strong>
      </div>
      <button type="button" class="cron-iconbtn" :aria-label="t('common.close')" @click="emit('close')">
        <Icon name="x" :size="16" />
      </button>
    </div>
    <div class="cron-detail__runs">
      <p v-if="loading" class="cron-muted">{{ t('cronSkills.runHistory.loading') }}</p>
      <p v-else-if="runs.length === 0" class="cron-muted">{{ t('cronSkills.runHistory.empty') }}</p>
      <DataTable
        v-else
        class="cron-runs-table"
        :columns="runColumns"
        :rows="runRows"
        :empty-text="t('cronSkills.runHistory.empty')"
      >
        <template #status="{ row }">
          <span :class="`status status--${row.status === 'ok' ? 'ok' : 'err'}`">{{ row.statusLabel }}</span>
        </template>
        <template #reply="{ row }">
          <span class="cron-runs__reply">{{ row.reply }}</span>
        </template>
        <template #_chat="{ row }">
          <button v-if="row.sessionKey" type="button" class="cron-link cron-run-chat-link" @click="emit('openChat', String(row.sessionKey))">
            {{ t('cronSkills.runHistory.openChat') }}
          </button>
        </template>
      </DataTable>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { localizedCronJobName } from '@/utils/cron/templateNames'
import DataTable from '@/components/DataTable.vue'
import Icon from '@/components/Icon.vue'
import { relTime } from '@/utils/cron/time'

const { t } = useI18n()

interface CronRunHistoryJob {
  id: string
  name?: string
}

interface CronRunHistoryRun {
  started_at?: string
  status?: string
  duration_ms?: number
  deliveryStatus?: Record<string, unknown> | string
  delivery_status?: Record<string, unknown> | string
  summary?: string
  sessionKey?: string
}

const props = defineProps<{
  job: CronRunHistoryJob
  runs: CronRunHistoryRun[]
  loading: boolean
}>()

const emit = defineEmits<{
  close: []
  openChat: [sessionKey: string]
}>()

const runColumns = computed(() => [
  { key: 'time', label: t('cronSkills.runHistory.colTime') },
  { key: 'status', label: t('cronSkills.runHistory.colStatus') },
  { key: 'duration', label: t('cronSkills.runHistory.colDuration') },
  { key: 'delivery', label: t('cronSkills.runHistory.colDelivery') },
  { key: 'reply', label: t('cronSkills.runHistory.colReply') },
  { key: '_chat', label: '' },
])

const runRows = computed(() =>
  props.runs.map(run => ({
    time: run.started_at ? relTime(run.started_at) : '—',
    status: run.status || 'unknown',
    statusLabel: run.status || t('cronSkills.runHistory.unknown'),
    duration: run.duration_ms != null ? run.duration_ms + 'ms' : '—',
    delivery: deliveryStatusText(run),
    reply: run.summary ? run.summary.substring(0, 120) : '—',
    sessionKey: run.sessionKey ?? null,
  })),
)

function deliveryStatusText(run: CronRunHistoryRun): string {
  const status = run.deliveryStatus || run.delivery_status
  if (!status) return '—'
  if (typeof status === 'string') return status
  return `ch: ${stringField(status, 'channel') || '-'}, ws: ${stringField(status, 'ws') || '-'}`
}

function stringField(source: Record<string, unknown>, key: string): string {
  const value = source[key]
  return typeof value === 'string' ? value : ''
}

</script>

<style scoped>
.cron-detail {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  margin-top: var(--sp-3);
  overflow: hidden;
}

.cron-detail__head {
  align-items: center;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: var(--sp-4);
}

.cron-detail__eyebrow {
  color: var(--text-dim);
  display: block;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.cron-detail__name {
  color: var(--text);
  font-size: var(--fs-md);
}

.cron-detail__runs {
  padding: var(--sp-4);
}

.cron-muted {
  color: var(--text-dim);
}

.cron-mono {
  font-family: var(--font-mono);
}

.cron-iconbtn {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  gap: 4px;
  padding: 4px 8px;
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}

.cron-iconbtn:hover {
  background: var(--bg-elevated);
  border-color: var(--border);
  color: var(--text);
}

.cron-runs__reply {
  color: var(--text-muted);
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cron-link {
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  padding: 0;
  text-decoration: underline;
}

.cron-link:hover {
  color: var(--text);
}

.status {
  border-radius: var(--radius-sm);
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  text-transform: uppercase;
}

.status--ok {
  background: color-mix(in srgb, var(--ok) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--ok) 40%, var(--border));
  color: var(--ok);
}

.status--err {
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--border));
  color: var(--danger);
}
</style>
