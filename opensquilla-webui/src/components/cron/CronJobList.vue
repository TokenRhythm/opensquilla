<template>
  <section class="cron-jobs">
    <div class="cron-jobs__head">
      <h3 class="cron-jobs__title">
        <template v-if="searchText">{{ t('cronSkills.list.matchingSchedules') }} <span class="cron-jobs__count">{{ t('cronSkills.list.countOf', { n: jobs.length, total: totalJobs }) }}</span></template>
        <template v-else>{{ t('cronSkills.list.allSchedules') }} <span class="cron-jobs__count">{{ jobs.length }}</span></template>
      </h3>
      <div class="control-segmented" role="tablist" :aria-label="t('cronSkills.list.viewMode')">
        <button class="control-segmented__btn" :class="{ 'is-active': viewMode === 'cards' }" role="tab" @click="emit('update:viewMode', 'cards')">{{ t('cronSkills.list.cards') }}</button>
        <button class="control-segmented__btn" :class="{ 'is-active': viewMode === 'table' }" role="tab" @click="emit('update:viewMode', 'table')">{{ t('cronSkills.list.table') }}</button>
      </div>
    </div>

    <div v-if="jobs.length === 0 && totalJobs > 0" class="state">
      <div class="state-icon"><Icon name="search" :size="48" /></div>
      <div class="state-title">{{ t('cronSkills.list.noMatchesTitle') }}</div>
      <p class="state-text">{{ t('cronSkills.list.noMatchesText') }}</p>
    </div>

    <div v-else-if="viewMode === 'cards'" class="cron-card-grid control-card-grid" style="--control-card-min: 300px">
      <article
        v-for="(job, i) in jobs"
        :key="job.id"
        class="cron-card control-card control-card--interactive"
        :class="{ 'is-selected control-card--selected': selectedId === job.id, 'is-imminent': isImminent(job, now) }"
        :style="{ '--stagger': i }"
        :data-cron-row="job.id"
      >
        <header class="cron-card__head">
          <input
            v-if="bulkMode"
            class="cron-bulk-check"
            type="checkbox"
            :checked="selectedJobIds.has(job.id)"
            :aria-label="t('cronSkills.list.selectJob', { name: displayJobName(job) })"
            @change="emit('toggle-selection', job.id)"
          >
          <span class="cron-card__dot" :class="dotClass(job)" />
          <button type="button" class="cron-card__name" :title="t('cronSkills.list.showRunHistory')" @click="emit('select', job.id)">
            {{ displayJobName(job) }}
          </button>
        </header>
        <div class="cron-card__timing">
          <span class="cron-card__schedule-text">
            <Icon name="clock" :size="14" />
            {{ explainCron(job.expression || '') || job.expression || job.schedule || '—' }}
          </span>
          <span class="cron-card__next" :class="{ 'is-paused': !job.enabled }">
            {{ job.enabled ? nextRunText(job, now) : t('cronSkills.list.paused') }}
          </span>
        </div>
        <div class="cron-card__workspace" :class="{ 'is-unavailable': workspaceUnavailable(job) }">
          <Icon name="folder" :size="14" />
          <span>{{ workspaceLabel(job) }}</span>
          <button v-if="workspaceUnavailable(job)" class="cron-workspace-rebind" type="button" @click="emit('edit', job)">
            {{ t('cronSkills.list.rebindWorkspace') }}
          </button>
        </div>
        <footer class="cron-card__actions">
          <button class="cron-iconbtn" :aria-label="t('cronSkills.list.showRunHistory')" :title="t('cronSkills.list.showRunHistory')" @click="emit('select', job.id)">
            <Icon name="clock" :size="15" />
          </button>
          <button class="cron-iconbtn cron-iconbtn--accent" :aria-label="runActionLabel(job)" :title="runActionLabel(job)" :disabled="runningJobIds.has(job.id) || workspaceUnavailable(job)" @click="emit('run', job.id)">
            <span v-if="runningJobIds.has(job.id)" class="cron-spinner" aria-hidden="true"></span>
            <Icon v-else :name="isJobFailed(job) ? 'refresh' : 'send'" :size="15" />

          </button>
          <button class="cron-iconbtn" :aria-label="t('cronSkills.list.edit')" :title="t('cronSkills.list.edit')" @click="emit('edit', job)">
            <Icon name="edit" :size="15" />
          </button>
          <button class="cron-iconbtn cron-iconbtn--sm" :aria-label="job.enabled ? t('cronSkills.list.pause') : t('cronSkills.list.resume')" :title="job.enabled ? t('cronSkills.list.pause') : t('cronSkills.list.resume')" @click="emit('toggle', job)">
            <Icon :name="job.enabled ? 'pause' : 'play'" :size="14" />
          </button>
          <button class="cron-iconbtn cron-iconbtn--sm cron-iconbtn--danger" :aria-label="t('cronSkills.list.delete')" :title="t('cronSkills.list.delete')" @click="emit('delete', job)">
            <Icon name="trash" :size="14" />
          </button>
        </footer>
      </article>
    </div>

    <div v-else class="cron-table-wrap">
      <table class="cron-table cron-table--tasks">
        <thead>
          <tr>
            <th v-if="bulkMode" class="cron-table__check"></th>
            <th v-for="col in tableCols" :key="col.key" :class="{ 'cron-th-sort': sortableCols.includes(col.key), 'cron-table__actions-head': col.key === '_actions' }" @click="sortableCols.includes(col.key) ? emit('sort', col.key) : undefined">
              {{ col.label }}
              <span v-if="sortableCols.includes(col.key) && sortCol === col.key" class="cron-table__arrow">{{ sortAsc ? ' ▲' : ' ▼' }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="job in jobs" :key="job.id" :class="{ 'is-selected': selectedId === job.id, 'is-imminent': isImminent(job, now) }" :data-cron-row="job.id">
            <td v-if="bulkMode" class="cron-table__check"><input class="cron-bulk-check" type="checkbox" :checked="selectedJobIds.has(job.id)" :aria-label="t('cronSkills.list.selectJob', { name: displayJobName(job) })" @change="emit('toggle-selection', job.id)"></td>
            <td class="cron-table__task"><div class="cron-table__task-content"><span class="cron-card__dot" :class="dotClass(job)" /><button class="cron-link" @click="emit('select', job.id)">{{ displayJobName(job) }}</button></div></td>
            <td class="cron-table__schedule"><div class="cron-table__schedule-content"><Icon name="clock" :size="14" /><span>{{ explainCron(job.expression || '') || job.expression || job.schedule || '—' }}</span></div></td>
            <td class="cron-table__workspace" :class="{ 'is-unavailable': workspaceUnavailable(job) }">
              <Icon name="folder" :size="14" />
              <span>{{ workspaceLabel(job) }}</span>
              <button v-if="workspaceUnavailable(job)" class="cron-workspace-rebind" type="button" @click="emit('edit', job)">{{ t('cronSkills.list.rebindWorkspace') }}</button>
            </td>
            <td class="cron-table__time cron-table__next">{{ job.enabled ? nextRunText(job, now) : t('cronSkills.list.paused') }}</td>
            <td class="cron-table__actions"><div class="cron-table__actions-content">
              <button class="cron-iconbtn cron-iconbtn--sm" :aria-label="t('cronSkills.list.showRunHistory')" :title="t('cronSkills.list.showRunHistory')" @click="emit('select', job.id)"><Icon name="clock" :size="14" /></button>
              <button class="cron-iconbtn cron-iconbtn--sm cron-iconbtn--accent" :aria-label="runActionLabel(job)" :title="runActionLabel(job)" :disabled="runningJobIds.has(job.id) || workspaceUnavailable(job)" @click="emit('run', job.id)"><span v-if="runningJobIds.has(job.id)" class="cron-spinner" aria-hidden="true"></span><Icon v-else :name="isJobFailed(job) ? 'refresh' : 'send'" :size="14" /></button>
              <button class="cron-iconbtn cron-iconbtn--sm" :aria-label="job.enabled ? t('cronSkills.list.pause') : t('cronSkills.list.resume')" :title="job.enabled ? t('cronSkills.list.pause') : t('cronSkills.list.resume')" @click="emit('toggle', job)"><Icon :name="job.enabled ? 'pause' : 'play'" :size="14" /></button>
              <button class="cron-iconbtn cron-iconbtn--sm" :aria-label="t('cronSkills.list.edit')" :title="t('cronSkills.list.edit')" @click="emit('edit', job)"><Icon name="edit" :size="14" /></button>
              <button class="cron-iconbtn cron-iconbtn--sm cron-iconbtn--danger" :aria-label="t('cronSkills.list.delete')" :title="t('cronSkills.list.delete')" @click="emit('delete', job)"><Icon name="trash" :size="14" /></button>
            </div></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { localizedCronJobName } from '@/utils/cron/templateNames'
import Icon from '@/components/Icon.vue'
import type { CronJob, CronPanelTemplate } from '@/types/cron'
import type { ProjectWorkspaceItem } from '@/composables/useProjectWorkspaces'
import { explainCron } from '@/utils/cron/schedule'
import { dotClass, isImminent, isJobFailed, nextRunText } from '@/composables/cron/useCronJobs'

const { t } = useI18n()
const displayJobName = (job: CronJob) => localizedCronJobName(job.name, job.id, t)

const props = defineProps<{
  jobs: CronJob[]
  totalJobs: number
  searchText: string
  viewMode: 'cards' | 'table'
  selectedId: string | null
  sortCol: string
  sortAsc: boolean
  now: number
  runningJobIds: Set<string>
  bulkMode: boolean
  selectedJobIds: Set<string>
  projectWorkspaces: ProjectWorkspaceItem[]
  projectWorkspacesLoaded: boolean
}>()

const emit = defineEmits<{
  'update:viewMode': [mode: 'cards' | 'table']
  'toggle-selection': [id: string]
  create: []
  preset: [template: CronPanelTemplate]
  select: [id: string]
  run: [id: string]
  toggle: [job: CronJob]
  edit: [job: CronJob]
  delete: [job: CronJob]
  sort: [key: string]
}>()

const tableCols = computed(() => [
  { key: 'name', label: t('cronSkills.list.colName') },
  { key: 'expression', label: t('cronSkills.list.colSchedule') },
  { key: 'workspace', label: t('cronSkills.list.colWorkspace') },
  { key: 'next_run', label: t('cronSkills.list.colNextRun') },
  { key: '_actions', label: '' },
])
const sortableCols = ['name', 'expression']

function workspaceUnavailable(job: CronJob): boolean {
  if (!job.workspaceId || !props.projectWorkspacesLoaded) return false
  const workspace = props.projectWorkspaces.find(item => item.id === job.workspaceId)
  return !workspace || !workspace.available
}

function workspaceLabel(job: CronJob): string {
  const name = job.workspaceName || t('cronSkills.list.noWorkspace')
  return workspaceUnavailable(job)
    ? t('cronSkills.list.workspaceUnavailable', { name })
    : name
}

function runActionLabel(job: CronJob): string {
  if (workspaceUnavailable(job)) return t('cronSkills.list.rebindBeforeRun')
  if (props.runningJobIds.has(job.id)) return t('cronSkills.list.running')
  return isJobFailed(job) ? t('cronSkills.list.retry') : t('cronSkills.list.runNow')
}

</script>
