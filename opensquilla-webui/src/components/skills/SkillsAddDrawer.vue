<template>
  <Teleport to="body">
    <Transition name="sk-add-drawer">
      <div v-if="open" class="sk-add-overlay" data-testid="skills-add-overlay">
        <div class="sk-add-overlay__scrim" data-testid="skills-add-scrim" @click="emit('close')" />
        <aside
          id="skills-add-drawer"
          ref="drawerRef"
          class="sk-add-drawer"
          role="dialog"
          aria-modal="true"
          aria-labelledby="skills-add-title"
        >
          <header class="sk-add-drawer__head">
            <div>
              <h2 id="skills-add-title">{{ t('cronSkills.registry.drawerTitle') }}</h2>
              <p>{{ t('cronSkills.registry.drawerSubtitle') }}</p>
            </div>
            <button
              ref="closeButtonRef"
              class="btn btn--ghost sk-add-drawer__close"
              type="button"
              :aria-label="t('common.close')"
              @click="emit('close')"
            >
              <Icon name="x" :size="18" />
            </button>
          </header>

          <div class="sk-add-drawer__body">
            <div class="sk-add-source-tabs" role="group" :aria-label="t('cronSkills.registry.sourceLabel')">
              <button
                id="skills-add-tab-clawhub"
                class="sk-add-source-tab"
                :class="{ 'is-active': sourceMode === 'clawhub' }"
                type="button"
                :aria-pressed="sourceMode === 'clawhub'"
                @click="sourceMode = 'clawhub'"
              >
                <Icon name="download" :size="16" />
                <span>{{ t('cronSkills.registry.sourceClawHub') }}</span>
                <span
                  v-if="runningSource === 'clawhub' && sourceMode !== 'clawhub'"
                  class="sk-add-source-status"
                >
                  <span class="sk-spinner" aria-hidden="true" />
                  <span class="sk-add-sr-only">{{ sourceRunningLabel('clawhub') }}</span>
                </span>
                <span
                  v-else-if="sourceAttentionCount('clawhub')"
                  class="sk-add-source-failures"
                  :aria-label="sourceAttentionLabel('clawhub')"
                >{{ sourceAttentionCount('clawhub') }}</span>
              </button>
              <button
                id="skills-add-tab-github"
                class="sk-add-source-tab"
                :class="{ 'is-active': sourceMode === 'github' }"
                type="button"
                :aria-pressed="sourceMode === 'github'"
                @click="sourceMode = 'github'"
              >
                <Icon name="share" :size="16" />
                <span>{{ t('cronSkills.registry.sourceGitHub') }}</span>
                <span
                  v-if="runningSource === 'github' && sourceMode !== 'github'"
                  class="sk-add-source-status"
                >
                  <span class="sk-spinner" aria-hidden="true" />
                  <span class="sk-add-sr-only">{{ sourceRunningLabel('github') }}</span>
                </span>
                <span
                  v-else-if="sourceAttentionCount('github')"
                  class="sk-add-source-failures"
                  :aria-label="sourceAttentionLabel('github')"
                >{{ sourceAttentionCount('github') }}</span>
              </button>
            </div>

            <p
              class="sk-add-sr-only sk-add-install-announcement"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >{{ installAnnouncement }}</p>

            <section
              v-if="queueRows.length"
              ref="queueRef"
              class="sk-add-queue"
              :data-source="sourceMode"
            >
              <div class="sk-add-section-title">
                <div>
                  <h3>
                    <span
                      v-if="currentActivityPhase === 'refreshing'"
                      class="sk-spinner"
                      aria-hidden="true"
                    />
                    {{ t('cronSkills.registry.queueTitle') }}
                  </h3>
                  <span>{{ queueSummary }}</span>
                </div>
                <div class="sk-add-section-title__actions">
                  <button
                    v-if="cancellableInstallSource === sourceMode"
                    class="btn btn--ghost btn--sm"
                    data-testid="skills-cancel-install"
                    type="button"
                    :disabled="cancellingSource === sourceMode"
                    @click="emit('cancelInstall', sourceMode)"
                  >{{ cancellingSource === sourceMode
                    ? t('cronSkills.registry.cancellingInstall')
                    : t('cronSkills.registry.cancelInstall') }}</button>
                  <button
                    class="btn btn--ghost btn--sm"
                    type="button"
                    :disabled="installControlsBlocked"
                    @click="emit('clearActivity', sourceMode)"
                  >{{ t('cronSkills.registry.clearActivity') }}</button>
                  <button
                    class="btn btn--ghost btn--sm sk-add-activity-toggle"
                    type="button"
                    :disabled="currentQueueRunning"
                    :aria-expanded="activityExpanded[sourceMode]"
                    :aria-label="activityExpanded[sourceMode]
                      ? t('cronSkills.registry.collapseActivity')
                      : t('cronSkills.registry.expandActivity')"
                    @click="activityExpanded[sourceMode] = !activityExpanded[sourceMode]"
                  >
                    <Icon name="chevronDown" :size="14" />
                  </button>
                </div>
              </div>
              <div v-show="activityExpanded[sourceMode]" class="sk-add-activity-body">
                <div v-if="currentRefreshWarning" class="sk-add-callout sk-add-callout--warning" role="status">
                  {{ currentRefreshWarning }}
                </div>
                <article
                  v-for="item in queueRows"
                  :key="item.id"
                  class="sk-add-queue-item"
                  :data-status="item.status"
                >
                  <span class="sk-add-queue-item__icon" aria-hidden="true">
                    <span v-if="item.status === 'installing' || item.status === 'cancelling'" class="sk-spinner" aria-hidden="true" />
                    <Icon v-else-if="item.status === 'installed' || item.status === 'unchanged'" name="check" :size="18" />
                    <Icon v-else-if="item.status === 'failed' || item.status === 'unknown'" name="info" :size="18" />
                    <Icon v-else name="clock" :size="18" />
                  </span>
                  <div class="sk-add-queue-item__body">
                    <div class="sk-add-queue-item__head">
                      <strong>{{ item.displayName }}</strong>
                      <span>{{ item.status === 'unknown'
                        ? t('cronSkills.registry.installResultUnknown')
                        : t(`cronSkills.registry.queueStatus.${item.status}`) }}</span>
                    </div>
                    <code :title="item.identifier">{{ item.identifier }}</code>
                    <p v-if="item.error" class="sk-add-queue-item__error">{{ item.error }}</p>
                    <p v-if="item.status === 'deferred'" class="sk-add-queue-item__note">
                      {{ t('cronSkills.registry.rateLimitDeferred') }}
                    </p>
                    <div v-if="item.resultMeta.length" class="sk-add-queue-item__meta">
                      <span v-for="meta in item.resultMeta" :key="meta">{{ meta }}</span>
                    </div>
                    <span
                      v-if="item.operationLabel"
                      class="sk-add-lifecycle"
                      :data-tone="item.operationTone"
                    >{{ item.operationLabel }}</span>
                    <span
                      v-if="item.lifecycleLabel"
                      class="sk-add-lifecycle"
                      :data-tone="item.lifecycleTone"
                    >{{ item.lifecycleLabel }}</span>
                    <details v-if="item.diagnostics.length" class="sk-add-diagnostics">
                      <summary>{{ t('cronSkills.registry.diagnostics', { count: item.diagnostics.length }) }}</summary>
                      <div v-for="diagnostic in item.diagnostics" :key="`${diagnostic.phase}:${diagnostic.code}`">
                        <strong>{{ diagnostic.code }}</strong>
                        <p>{{ diagnostic.message }}</p>
                        <p v-if="diagnostic.hint">{{ diagnostic.hint }}</p>
                        <pre v-if="hasDiagnosticDetails(diagnostic.details)">{{ diagnosticDetails(diagnostic.details || {}) }}</pre>
                      </div>
                    </details>
                    <button
                      v-if="item.status === 'failed' || item.status === 'cancelled'"
                      class="btn btn--sm sk-add-retry"
                      :class="item.requiresRiskAcknowledgement ? 'btn--primary' : 'btn--ghost'"
                      type="button"
                      :disabled="installControlsBlocked"
                      :data-testid="item.requiresRiskAcknowledgement
                        ? 'skills-install-acknowledge-risk'
                        : undefined"
                      @click="emit('retry', item.id, item.requiresRiskAcknowledgement)"
                    >
                      <Icon name="refresh" :size="14" />
                      <span>{{ item.requiresRiskAcknowledgement
                        ? t('cronSkills.registry.installAnyway')
                        : t('cronSkills.registry.retry') }}</span>
                    </button>
                  </div>
                </article>
              </div>
            </section>

            <section
              v-if="sourceMode === 'github'"
              id="skills-add-panel-github"
              class="sk-add-source-panel"
            >
              <label class="sk-add-field-label" for="skills-add-github-input">
                {{ t('cronSkills.registry.githubReferencesLabel') }}
              </label>
              <textarea
                id="skills-add-github-input"
                :value="githubUrl"
                class="sk-add-textarea"
                rows="7"
                spellcheck="false"
                autocomplete="off"
                :aria-describedby="githubReferencesDescribedBy"
                :aria-invalid="githubBatchOverLimit ? 'true' : undefined"
                :disabled="installControlsBlocked"
                :placeholder="t('cronSkills.registry.githubReferencesPlaceholder')"
                @input="emit('update:githubUrl', ($event.target as HTMLTextAreaElement).value)"
              />
              <p id="skills-add-github-format-hint" class="sk-add-help">
                {{ t('cronSkills.registry.githubReferencesHint') }}
              </p>
              <p id="skills-add-github-batch-hint" class="sk-add-help">
                {{ t('cronSkills.registry.githubBatchBehavior', {
                  count: githubReferenceCount,
                  max: GITHUB_BATCH_MAX_REFERENCES,
                }) }}
              </p>
              <div
                v-if="githubBatchOverLimit"
                id="skills-add-github-limit-hint"
                class="sk-add-callout sk-add-callout--danger"
                role="alert"
              >
                {{ t('cronSkills.registry.githubBatchLimitExceeded', {
                  max: GITHUB_BATCH_MAX_REFERENCES,
                }) }}
              </div>
              <button
                class="btn btn--primary sk-add-primary"
                data-testid="skills-install-github"
                type="button"
                :disabled="installControlsBlocked || githubReferenceCount === 0 || githubBatchOverLimit"
                :aria-busy="currentQueueRunning"
                @click="emit('installGithub')"
              >
                <Icon v-if="!currentQueueRunning" name="download" :size="16" />
                <span>{{ primaryActionLabel }}</span>
              </button>
              <p
                v-if="githubDuplicateCount > 0"
                id="skills-add-github-duplicates-hint"
                class="sk-add-help sk-add-help--center"
              >
                {{ t('cronSkills.registry.duplicatesSkipped', { count: githubDuplicateCount }) }}
              </p>
            </section>

            <section
              v-if="sourceMode === 'clawhub'"
              id="skills-add-panel-clawhub"
              class="sk-add-source-panel"
            >
              <label class="sk-add-field-label" for="skills-add-clawhub-query">
                {{ t('cronSkills.registry.searchLabel') }}
              </label>
              <div class="sk-add-search-row">
                <div class="sk-add-input-wrap">
                  <Icon name="search" :size="16" />
                  <input
                    id="skills-add-clawhub-query"
                    :value="registryQuery"
                    type="search"
                    autocomplete="off"
                    :placeholder="t('cronSkills.registry.searchPlaceholder')"
                    @input="emit('update:registryQuery', ($event.target as HTMLInputElement).value)"
                    @keydown.enter="emit('search')"
                  />
                </div>
                <button
                  class="btn btn--primary"
                  type="button"
                  :disabled="loading || !registryQuery.trim()"
                  :aria-busy="loading"
                  @click="emit('search')"
                >
                  {{ loading ? t('cronSkills.registry.searchingShort') : t('cronSkills.registry.search') }}
                </button>
              </div>

              <div v-if="registrySearchError" class="sk-add-callout sk-add-callout--danger" role="alert">
                {{ registrySearchError }}
              </div>
              <div
                v-for="diagnostic in registryDiagnostics"
                :key="`${diagnostic.phase}:${diagnostic.code}:${diagnostic.message}`"
                class="sk-add-callout"
                :class="diagnostic.blocking ? 'sk-add-callout--danger' : 'sk-add-callout--warning'"
              >
                <strong>{{ diagnostic.code }}</strong>
                <span>{{ diagnostic.message }}</span>
                <small v-if="diagnostic.hint">{{ diagnostic.hint }}</small>
                <small v-if="searchDiagnosticRetryAfter(diagnostic.details)">
                  {{ t('cronSkills.registry.retryAfter', {
                    value: searchDiagnosticRetryAfter(diagnostic.details),
                  }) }}
                </small>
              </div>

              <div v-if="loading" class="sk-add-empty" role="status">
                <span class="sk-spinner" aria-hidden="true" />
                <span>{{ t('cronSkills.registry.searching') }}</span>
              </div>
              <div v-else-if="results.length" class="sk-add-results">
                <article
                  v-for="row in resultRows"
                  :key="row.operationKey"
                  class="sk-add-result"
                  :data-status="row.queueStatus || undefined"
                >
                  <div class="sk-add-result__body">
                    <strong>{{ row.name }}</strong>
                    <p>{{ row.description }}</p>
                    <div class="sk-add-result__meta">
                      <span v-if="row.author">{{ row.author }}</span>
                      <span v-if="row.version">{{ row.version }}</span>
                      <span>{{ row.source }}</span>
                      <span>{{ row.trustLevel }}</span>
                      <span v-if="row.operationLabel" :data-tone="row.operationTone">
                        {{ row.operationLabel }}
                      </span>
                      <span v-if="row.lifecycleLabel" :data-tone="row.lifecycleTone">
                        {{ row.lifecycleLabel }}
                      </span>
                    </div>
                  </div>
                  <button
                    class="btn btn--sm"
                    :class="[
                      row.installed || row.queueStatus === 'failed' || row.queueStatus === 'unknown'
                        ? 'btn--ghost'
                        : 'btn--primary',
                    ]"
                    type="button"
                    :disabled="resultActionDisabled(row)"
                    :aria-busy="row.queueStatus === 'installing'"
                    @click="handleResultAction(row)"
                  >
                    <span>{{ resultActionLabel(row) }}</span>
                  </button>
                </article>
              </div>
              <div v-else class="sk-add-empty">
                <Icon name="skills" :size="30" />
                <span>{{ t('cronSkills.registry.hintBrowse') }}</span>
              </div>
            </section>

          </div>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, toRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import type {
  SkillInstallActivities,
  SkillInstallSource,
} from '@/composables/skills/useSkillRegistry'
import {
  GITHUB_BATCH_MAX_REFERENCES,
  skillInstallRequiresRiskAcknowledgement,
  skillRegistryOperationKey,
} from '@/composables/skills/useSkillRegistry'
import { skillLifecyclePresentation } from '@/composables/skills/useSkillsCatalog'
import type { RegistryResult, SkillDiagnostic } from '@/types/skills'

const props = defineProps<{
  open: boolean
  registryQuery: string
  githubUrl: string
  results: RegistryResult[]
  loading: boolean
  registryDiagnostics: SkillDiagnostic[]
  registrySearchError: string
  activities: SkillInstallActivities
  runningSource: SkillInstallSource | null
  cancellableInstallSource: SkillInstallSource | null
  cancellingSource: SkillInstallSource | null
  mutationBlocked?: boolean
}>()

const emit = defineEmits<{
  close: []
  'update:registryQuery': [value: string]
  'update:githubUrl': [value: string]
  search: []
  installGithub: []
  install: [identifier: string, source: string, displayName: string]
  retry: [id: string, acknowledgeRisk?: boolean]
  cancelInstall: [source: SkillInstallSource]
  clearActivity: [source: SkillInstallSource]
}>()

const { t } = useI18n()
const sourceMode = ref<'github' | 'clawhub'>('github')
const activityExpanded = ref<Record<SkillInstallSource, boolean>>({
  clawhub: false,
  github: false,
})
const drawerRef = ref<HTMLElement | null>(null)
const closeButtonRef = ref<HTMLButtonElement | null>(null)
const queueRef = ref<HTMLElement | null>(null)
useDialogA11y(drawerRef, toRef(props, 'open'), () => emit('close'), {
  initialFocus: closeButtonRef,
})

watch([() => props.runningSource, sourceMode], ([running, source]) => {
  if (!running || running !== source) return
  void nextTick(() => queueRef.value?.scrollIntoView({ block: 'nearest' }))
})

for (const source of ['clawhub', 'github'] as const) {
  watch(
    [
      () => props.activities[source].items.map(item => item.status).join('|'),
      () => props.runningSource,
    ],
    () => settleActivityExpansion(source),
    { immediate: true },
  )
}

function settleActivityExpansion(source: SkillInstallSource) {
  const items = props.activities[source].items
  if (props.runningSource === source
    || items.some(item => item.status === 'queued'
      || item.status === 'installing'
      || item.status === 'cancelling')) {
    activityExpanded.value[source] = true
    return
  }
  activityExpanded.value[source] = items.some(item =>
    item.status === 'failed' || item.status === 'unknown')
}

const currentActivity = computed(() => props.activities[sourceMode.value])
const currentItems = computed(() => currentActivity.value.items)
const currentRefreshWarning = computed(() => currentActivity.value.refreshWarning)
const currentQueueRunning = computed(() => props.runningSource === sourceMode.value)
const anyQueueRunning = computed(() => props.runningSource !== null)
const installControlsBlocked = computed(() => anyQueueRunning.value || Boolean(props.mutationBlocked))

function activityPhase(source: SkillInstallSource) {
  const activity = props.activities[source]
  if (activity.phase) return activity.phase
  if (props.runningSource !== source) return 'terminal'
  return activity.items.some(item => item.status === 'queued'
    || item.status === 'installing'
    || item.status === 'cancelling')
    ? 'installing'
    : 'refreshing'
}

const currentActivityPhase = computed(() => activityPhase(sourceMode.value))

const githubReferences = computed(() => props.githubUrl
  .split(/\r?\n/)
  .map(value => value.trim())
  .filter(Boolean))
const githubReferenceCount = computed(() => new Set(githubReferences.value).size)
const githubDuplicateCount = computed(() => githubReferences.value.length - githubReferenceCount.value)
const githubBatchOverLimit = computed(() =>
  githubReferenceCount.value > GITHUB_BATCH_MAX_REFERENCES)
const githubReferencesDescribedBy = computed(() => [
  'skills-add-github-format-hint',
  'skills-add-github-batch-hint',
  githubBatchOverLimit.value ? 'skills-add-github-limit-hint' : '',
  githubDuplicateCount.value > 0 ? 'skills-add-github-duplicates-hint' : '',
].filter(Boolean).join(' '))
const completedCount = computed(() => currentItems.value.filter(item =>
  item.status === 'installed'
    || item.status === 'unchanged'
    || item.status === 'failed'
    || item.status === 'unknown'
    || item.status === 'cancelled').length)
const currentIndex = computed(() => {
  const installing = currentItems.value.findIndex(item =>
    item.status === 'installing' || item.status === 'cancelling')
  return installing >= 0
    ? installing + 1
    : Math.min(completedCount.value + 1, currentItems.value.length)
})
const primaryActionLabel = computed(() => {
  if (currentQueueRunning.value) {
    if (props.cancellingSource === sourceMode.value) {
      return t('cronSkills.registry.cancellingInstall')
    }
    if (currentActivityPhase.value === 'refreshing') {
      return t('cronSkills.skillsView.refreshing')
    }
    return t('cronSkills.registry.installingProgress', {
      current: currentIndex.value,
      total: currentItems.value.length,
    })
  }
  return t('cronSkills.registry.installCount', { count: githubReferenceCount.value })
})
const queueSummary = computed(() => {
  if (currentQueueRunning.value) {
    if (currentActivityPhase.value === 'refreshing') {
      return t('cronSkills.skillsView.refreshing')
    }
    return `${t('cronSkills.registry.sourceInstalling', {
      name: runningItemName(sourceMode.value),
    })} · ${currentIndex.value} / ${currentItems.value.length}`
  }
  const installed = currentItems.value.filter(item => item.status === 'installed').length
  const unchanged = currentItems.value.filter(item => item.status === 'unchanged').length
  const failed = currentItems.value.filter(item => item.status === 'failed').length
  const deferred = currentItems.value.filter(item => item.status === 'deferred').length
  const unknown = currentItems.value.filter(item => item.status === 'unknown').length
  const cancelled = currentItems.value.filter(item => item.status === 'cancelled').length
  return [
    t('cronSkills.registry.queueProcessed', {
      processed: completedCount.value,
      total: currentItems.value.length,
    }),
    ...(installed ? [t('cronSkills.registry.queueInstalled', { count: installed })] : []),
    ...(unchanged ? [t('cronSkills.registry.queueUnchanged', { count: unchanged })] : []),
    ...(failed ? [t('cronSkills.registry.queueFailed', { count: failed })] : []),
    ...(deferred ? [t('cronSkills.registry.queueDeferred', { count: deferred })] : []),
    ...(unknown ? [t('cronSkills.registry.queueUnknown', { count: unknown })] : []),
    ...(cancelled ? [t('cronSkills.registry.queueCancelled', { count: cancelled })] : []),
  ].join(' · ')
})

function sourceRunningLabel(source: SkillInstallSource): string {
  if (props.cancellingSource === source) return t('cronSkills.registry.cancellingInstall')
  if (activityPhase(source) === 'refreshing') return t('cronSkills.skillsView.refreshing')
  return t('cronSkills.registry.sourceInstalling', { name: runningItemName(source) })
}

const installAnnouncement = computed(() => {
  if (props.runningSource) return sourceRunningLabel(props.runningSource)
  if (currentItems.value.length) return queueSummary.value
  return ''
})

function sourceAttentionCount(source: SkillInstallSource): number {
  return props.activities[source].items.filter(item =>
    item.status === 'failed' || item.status === 'unknown').length
}

function sourceAttentionLabel(source: SkillInstallSource): string {
  const items = props.activities[source].items
  const failed = items.filter(item => item.status === 'failed').length
  const unknown = items.filter(item => item.status === 'unknown').length
  return [
    ...(failed ? [t('cronSkills.registry.sourceFailures', { count: failed })] : []),
    ...(unknown ? [t('cronSkills.registry.queueUnknown', { count: unknown })] : []),
  ].join(', ')
}

function runningItemName(source: SkillInstallSource): string {
  const activity = props.activities[source]
  return activity.items.find(item => item.status === 'installing')?.displayName
    || activity.items.find(item => item.status === 'cancelling')?.displayName
    || activity.items.find(item => item.status === 'queued')?.displayName
    || t(`cronSkills.registry.source${source === 'github' ? 'GitHub' : 'ClawHub'}`)
}

function activityForSource(source: string) {
  return props.activities[source === 'github' ? 'github' : 'clawhub']
}

const resultRows = computed(() => props.results.map((result) => {
  const lifecycle = result.lifecycle
  const showLifecycleWithoutInstall = lifecycle
    && (
      lifecycle.load_state === 'rejected'
      || lifecycle.load_state === 'serving_previous'
      || lifecycle.load_state === 'validated_offline'
      || lifecycle.install_state === 'missing'
      || lifecycle.install_state === 'drifted'
      || lifecycle.compatibility_state === 'unsupported'
    )
  const installSource = result.source || 'clawhub'
  const installId = result.installReference
    || result.install_reference
    || result.identifier
    || result.name
  const operationKey = skillRegistryOperationKey(installId, installSource)
  const queueItem = activityForSource(installSource).items.find(item => item.id === operationKey)
  const operationFailed = queueItem?.status === 'failed'
  const operationUnknown = queueItem?.status === 'unknown'
  const operationPreserved = operationFailed && Boolean(queueItem?.result?.installed)
  const presentation = lifecycle && (
    operationPreserved
    || (!operationFailed && (result.installed || showLifecycleWithoutInstall))
  )
    ? skillLifecyclePresentation({ name: result.name, lifecycle }, 'registry')
    : null
  const operationLabel = operationUnknown
    ? t('cronSkills.registry.installResultUnknown')
    : operationFailed
      ? t('cronSkills.registry.queueStatus.failed')
      : ''
  const operationTone = operationUnknown ? 'warning' : 'danger'
  return {
    name: result.name,
    description: (result.description || '').slice(0, 180),
    author: result.author || '',
    version: result.version || '',
    source: installSource,
    trustLevel: result.trust_level || t('cronSkills.registry.community'),
    installed: Boolean(result.installed),
    operationLabel,
    operationTone,
    lifecycleLabel: presentation?.label || '',
    lifecycleTone: presentation?.tone || 'neutral',
    installId,
    installSource,
    operationKey,
    queueStatus: queueItem?.status,
  }
}))

type ResultRow = (typeof resultRows.value)[number]

function resultActionLabel(row: ResultRow): string {
  if (row.queueStatus === 'queued'
    || row.queueStatus === 'installing'
    || row.queueStatus === 'cancelling') {
    return t(`cronSkills.registry.queueStatus.${row.queueStatus}`)
  }
  if (row.queueStatus === 'cancelled') return t('cronSkills.registry.retry')
  if (row.queueStatus === 'failed' || row.queueStatus === 'unknown') {
    return t('cronSkills.registry.viewDetails')
  }
  if (row.installed) return t('cronSkills.registry.installed')
  if (row.queueStatus) return t(`cronSkills.registry.queueStatus.${row.queueStatus}`)
  return t('cronSkills.registry.install')
}

function handleResultAction(row: ResultRow) {
  if (row.queueStatus === 'failed' || row.queueStatus === 'unknown') {
    const source = row.installSource === 'github' ? 'github' : 'clawhub'
    sourceMode.value = source
    activityExpanded.value[source] = true
    void nextTick(() => queueRef.value?.scrollIntoView({ block: 'nearest' }))
    return
  }
  emit('install', row.installId, row.installSource, row.name)
}

function resultActionDisabled(row: ResultRow): boolean {
  if (row.queueStatus === 'failed' || row.queueStatus === 'unknown') return false
  return row.installed || installControlsBlocked.value || row.queueStatus === 'queued'
}

const queueRows = computed(() => currentItems.value.map((item) => {
  const lifecycle = item.result?.lifecycle
  const operationFailed = item.status === 'failed'
  const operationPreserved = operationFailed && Boolean(item.result?.installed)
  const presentation = lifecycle && (!operationFailed || operationPreserved)
    ? skillLifecyclePresentation({ name: item.displayName, lifecycle }, 'registry')
    : null
  const resolution = item.result?.resolution
  const revision = resolution?.immutableRevision || ''
  const revisionLabel = revision && revision !== resolution?.version
    ? (/^[0-9a-f]{40}$/i.test(revision) ? revision.slice(0, 10) : revision)
    : ''
  const resultMeta = [...new Set([
    item.source,
    resolution?.publisher,
    resolution?.version,
    revisionLabel,
    item.result?.success ? effectiveFromLabel(item.result.effectiveFrom) : '',
  ].filter((value): value is string => Boolean(value)))]
  return {
    ...item,
    requiresRiskAcknowledgement: skillInstallRequiresRiskAcknowledgement(item.result),
    operationLabel: operationFailed
      ? t(item.result?.installed
        ? 'cronSkills.registry.existingInstallPreserved'
        : 'cronSkills.registry.notInstalled')
      : '',
    operationTone: operationFailed && item.result?.installed
      ? 'warning'
      : 'danger',
    lifecycleLabel: presentation?.label || '',
    lifecycleTone: presentation?.tone || 'neutral',
    diagnostics: item.result?.diagnostics || [],
    resultMeta,
  }
}))

function diagnosticDetails(details: Record<string, unknown>): string {
  try {
    return JSON.stringify(details, null, 2)
  } catch {
    return String(details)
  }
}

function searchDiagnosticRetryAfter(
  details: Record<string, unknown> | undefined,
): string {
  const value = details?.retryAfter
  if (typeof value === 'number') {
    return Number.isFinite(value) && value >= 0 ? String(value) : ''
  }
  if (typeof value !== 'string') return ''
  return value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, 80)
}

function hasDiagnosticDetails(details: Record<string, unknown> | undefined): boolean {
  return Boolean(details && Object.keys(details).length)
}

function effectiveFromLabel(value: string | undefined): string {
  if (!value) return ''
  if (value === 'next_turn') return t('cronSkills.registry.effectiveNextTurn')
  if (value === 'next_start') return t('cronSkills.registry.effectiveNextStart')
  return t('cronSkills.registry.effectiveFrom', { value })
}
</script>

<style scoped>
.sk-add-overlay {
  --sk-add-scrim: color-mix(in srgb, var(--scrim) 45%, transparent);

  inset: 0;
  position: fixed;
  z-index: 1200;
}

.sk-add-overlay__scrim {
  background: var(--sk-add-scrim);
  inset: 0;
  position: absolute;
}

.sk-add-drawer {
  background: var(--bg-surface);
  border-left: 1px solid var(--border);
  bottom: 0;
  box-shadow: var(--elev-3);
  color: var(--text);
  display: flex;
  flex-direction: column;
  max-width: 100vw;
  position: absolute;
  right: 0;
  top: 0;
  width: 460px;
}

.sk-add-drawer__head {
  align-items: flex-start;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: 24px;
}

.sk-add-drawer__head h2 {
  font-size: 1.25rem;
  margin: 0;
}

.sk-add-drawer__head p {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: 6px 0 0;
}

.sk-add-drawer__close {
  flex: 0 0 auto;
  padding: 7px;
}

.sk-add-drawer__body {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: var(--sp-4);
  min-height: 0;
  overflow-y: auto;
  padding: 20px 24px 32px;
}

.sk-add-source-tabs {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: grid;
  grid-template-columns: 1fr 1fr;
  padding: 3px;
}

.sk-add-source-status {
  align-items: center;
  display: inline-flex;
}

.sk-add-source-status .sk-spinner {
  height: 13px;
  width: 13px;
}

.sk-add-source-failures {
  align-items: center;
  background: color-mix(in srgb, var(--danger) 12%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--danger) 38%, var(--border));
  border-radius: 999px;
  color: var(--danger);
  display: inline-flex;
  font-size: 10px;
  justify-content: center;
  line-height: 1;
  min-height: 18px;
  min-width: 18px;
  padding: 2px 5px;
}

.sk-add-sr-only {
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}

.sk-add-source-tab {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 650;
  gap: 7px;
  justify-content: center;
  min-height: 36px;
  padding: 7px 10px;
}

.sk-add-source-tab.is-active {
  background: var(--bg-surface);
  border-color: var(--accent);
  color: var(--accent);
}

.sk-add-source-tab:focus-visible,
.sk-add-textarea:focus,
.sk-add-input-wrap:focus-within {
  box-shadow: var(--focus-ring);
  outline: 0;
}

.sk-add-source-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.sk-add-field-label {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 650;
}

.sk-add-textarea {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.55;
  min-height: 170px;
  padding: 12px;
  resize: vertical;
  width: 100%;
}

.sk-add-textarea:disabled {
  opacity: .65;
}

.sk-add-help {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  margin: 0;
}

.sk-add-help--center {
  text-align: center;
}

.sk-add-primary {
  justify-content: center;
  margin-top: var(--sp-2);
  width: 100%;
}

.sk-add-search-row {
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: 1fr auto;
}

.sk-add-input-wrap {
  align-items: center;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-dim);
  display: flex;
  gap: 8px;
  padding: 0 10px;
}

.sk-add-input-wrap input:not([type="radio"]):not([type="checkbox"]) {
  background: transparent;
  border: 0;
  box-shadow: none;
  color: var(--text);
  min-width: 0;
  outline: 0;
  padding: 8px 0;
  width: 100%;
}

.sk-add-callout {
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--warn) 35%, var(--border));
  border-radius: var(--radius-md);
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  font-size: var(--fs-xs);
  gap: 3px;
  padding: 10px 12px;
}

.sk-add-callout--danger {
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 38%, var(--border));
}

.sk-add-callout strong {
  color: var(--text);
}

.sk-add-empty {
  align-items: center;
  border: 1px dashed var(--border);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  justify-content: center;
  min-height: 120px;
  padding: var(--sp-4);
  text-align: center;
}

.sk-add-results,
.sk-add-queue {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.sk-add-queue {
  max-height: min(46dvh, 520px);
  overflow-y: auto;
  overscroll-behavior: contain;
}

.sk-add-queue .sk-add-section-title {
  background: var(--bg-surface);
  position: sticky;
  top: 0;
  z-index: 1;
}

.sk-add-result {
  align-items: center;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: 12px;
}

.sk-add-result__body {
  min-width: 0;
}

.sk-add-result__body strong {
  display: block;
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}

.sk-add-result__body p {
  color: var(--text-muted);
  display: -webkit-box;
  font-size: var(--fs-xs);
  line-height: 1.45;
  margin: 4px 0;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.sk-add-result[data-status="installing"] {
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
}

.sk-add-result[data-status="failed"] {
  background: color-mix(in srgb, var(--danger) 5%, var(--bg));
  border-color: color-mix(in srgb, var(--danger) 28%, var(--border));
}

.sk-add-result__meta,
.sk-add-queue-item__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.sk-add-result__meta span,
.sk-add-queue-item__meta span,
.sk-add-lifecycle {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 1px 5px;
}

.sk-add-section-title {
  align-items: flex-start;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  margin-top: var(--sp-2);
  padding-bottom: 8px;
}

.sk-add-section-title > div:first-child {
  min-width: 0;
}

.sk-add-section-title__actions {
  align-items: center;
  display: flex;
  flex: 0 0 auto;
  gap: 2px;
}

.sk-add-section-title__actions .btn {
  min-height: 28px;
  padding: 4px 6px;
}

.sk-add-activity-toggle svg {
  transition: transform var(--dur-fast) var(--ease-standard);
}

.sk-add-activity-toggle[aria-expanded="false"] svg {
  transform: rotate(-90deg);
}

.sk-add-activity-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.sk-add-section-title h3 {
  align-items: center;
  display: inline-flex;
  font-size: var(--fs-sm);
  gap: 6px;
  margin: 0;
}

.sk-add-section-title h3 .sk-spinner {
  height: 13px;
  width: 13px;
}

.sk-add-section-title span {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}

.sk-add-queue-item {
  align-items: flex-start;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: grid;
  gap: var(--sp-3);
  grid-template-columns: auto 1fr;
  padding: 13px;
}

.sk-add-queue-item[data-status="failed"] {
  background: color-mix(in srgb, var(--danger) 6%, var(--bg));
  border-color: color-mix(in srgb, var(--danger) 32%, var(--border));
}

.sk-add-queue-item[data-status="cancelled"] {
  background: color-mix(in srgb, var(--text-muted) 5%, var(--bg));
}

.sk-add-queue-item[data-status="installed"] .sk-add-queue-item__icon,
.sk-add-queue-item[data-status="unchanged"] .sk-add-queue-item__icon {
  color: var(--ok);
}

.sk-add-queue-item[data-status="failed"] .sk-add-queue-item__icon,
.sk-add-queue-item[data-status="failed"] .sk-add-queue-item__head span,
.sk-add-queue-item__error {
  color: var(--danger);
}

.sk-add-queue-item__icon {
  align-items: center;
  color: var(--text-dim);
  display: inline-flex;
  height: 24px;
  justify-content: center;
  width: 24px;
}

.sk-add-queue-item__body {
  min-width: 0;
}

.sk-add-queue-item__head {
  align-items: baseline;
  display: flex;
  gap: var(--sp-2);
  justify-content: space-between;
}

.sk-add-queue-item__head strong {
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}

.sk-add-queue-item__head span {
  color: var(--text-muted);
  flex: 0 0 auto;
  font-size: var(--fs-xs);
}

.sk-add-queue-item__body > code {
  color: var(--text-dim);
  display: block;
  font-size: 10px;
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sk-add-queue-item__error {
  font-size: var(--fs-xs);
  margin: 7px 0;
}

.sk-add-queue-item__note {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: 7px 0;
}

.sk-add-queue-item__meta,
.sk-add-lifecycle {
  margin-top: 7px;
}

.sk-add-lifecycle[data-tone="success"],
.sk-add-result__meta span[data-tone="success"] { border-color: color-mix(in srgb, var(--ok) 45%, var(--border)); color: var(--ok); }
.sk-add-lifecycle[data-tone="info"],
.sk-add-result__meta span[data-tone="info"] { border-color: color-mix(in srgb, var(--info) 45%, var(--border)); color: var(--info); }
.sk-add-lifecycle[data-tone="warning"],
.sk-add-result__meta span[data-tone="warning"] { border-color: color-mix(in srgb, var(--warn) 45%, var(--border)); color: var(--warn); }
.sk-add-lifecycle[data-tone="danger"],
.sk-add-result__meta span[data-tone="danger"] { border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); color: var(--danger); }

.sk-add-diagnostics {
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin-top: 9px;
  padding-top: 7px;
}

.sk-add-diagnostics summary {
  color: var(--text);
  cursor: pointer;
}

.sk-add-diagnostics div {
  margin-top: 8px;
}

.sk-add-diagnostics p {
  margin: 3px 0;
}

.sk-add-diagnostics pre {
  background: var(--bg-elevated);
  border-radius: var(--radius-sm);
  font-size: 10px;
  margin: 5px 0 0;
  max-height: 160px;
  overflow: auto;
  padding: 7px;
  white-space: pre-wrap;
}

.sk-add-retry {
  margin-top: 8px;
}

.sk-add-drawer-enter-active,
.sk-add-drawer-leave-active {
  transition: opacity var(--dur-base) var(--ease-standard);
}

.sk-add-drawer-enter-active .sk-add-drawer,
.sk-add-drawer-leave-active .sk-add-drawer {
  transition: transform var(--dur-base) var(--ease-standard);
}

.sk-add-drawer-enter-from,
.sk-add-drawer-leave-to {
  opacity: 0;
}

.sk-add-drawer-enter-from .sk-add-drawer,
.sk-add-drawer-leave-to .sk-add-drawer {
  transform: translateX(100%);
}

@media (max-width: 720px) {
  .sk-add-drawer {
    border-left: 0;
    height: 100dvh;
    width: 100vw;
  }

  .sk-add-drawer__head {
    padding: 18px;
  }

  .sk-add-drawer__body {
    padding: 16px 18px 28px;
  }
}
</style>
