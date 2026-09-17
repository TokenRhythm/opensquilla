<template>
  <section
    ref="cardElement"
    class="meta-setup-card"
    :data-phase="state.phase"
    data-testid="meta-setup-card"
    role="region"
    :aria-labelledby="titleId"
    :aria-describedby="descriptionId"
    :aria-busy="busy ? 'true' : 'false'"
    tabindex="-1"
  >
    <header class="meta-setup-card__header">
      <h3 :id="titleId" class="meta-setup-card__title">
        {{ t('chat.metaSetup.title', { skill: state.name }) }}
      </h3>
      <span class="meta-setup-card__badge" :class="`is-${state.phase}`">
        {{ phaseLabel }}
      </span>
    </header>

    <div class="meta-setup-card__body">
      <p :id="descriptionId" class="meta-setup-card__intro">
        {{ t('chat.metaSetup.intro') }}
      </p>

      <section v-if="missingDependencies.length" class="meta-setup-card__section">
        <h4>{{ t('chat.metaSetup.missingDependencies') }}</h4>
        <ul class="meta-setup-card__chips" data-testid="meta-setup-missing">
          <li v-for="dependency in missingDependencies" :key="dependency">
            <code>{{ dependency }}</code>
          </li>
        </ul>
      </section>

      <section v-if="actions.length" class="meta-setup-card__section">
        <h4>{{ t('chat.metaSetup.installActions') }}</h4>
        <ul class="meta-setup-card__actions-list">
          <li v-for="action in actions" :key="action.id" class="meta-setup-card__action">
            <div class="meta-setup-card__action-head">
              <strong>{{ actionDisplayLabel(action) }}</strong>
              <span v-if="action.version" class="meta-setup-card__version">
                {{ t('chat.metaSetup.version', { version: action.version }) }}
              </span>
            </div>
            <p v-if="action.bins?.length" class="meta-setup-card__action-bins">
              {{ action.bins.join(', ') }}
            </p>
            <dl v-if="action.source || action.license || action.download_size_bytes" class="meta-setup-card__meta">
              <template v-if="action.source">
                <dt>{{ t('chat.metaSetup.source') }}</dt>
                <dd>
                  <a
                    v-if="isHttpsUrl(action.source)"
                    :href="action.source"
                    target="_blank"
                    rel="noopener"
                  >{{ action.source }}</a>
                  <span v-else>{{ action.source }}</span>
                </dd>
              </template>
              <template v-if="action.license">
                <dt>{{ t('chat.metaSetup.license') }}</dt>
                <dd>{{ action.license }}</dd>
              </template>
              <template v-if="action.download_size_bytes">
                <dt>{{ t('chat.metaSetup.downloadSize') }}</dt>
                <dd>{{ action.download_size_is_minimum ? '≥ ' : '' }}{{ formatBytes(action.download_size_bytes) }}</dd>
              </template>
            </dl>
            <p
              v-if="action.reason"
              :class="action.available === false
                ? 'meta-setup-card__unavailable'
                : 'meta-setup-card__action-reason'"
            >
              {{ actionDisplayReason(action) }}
            </p>
            <p v-if="action.requires_admin" class="meta-setup-card__admin-note">
              {{ t('chat.metaSetup.requiresAdmin') }}
            </p>
          </li>
        </ul>
      </section>

      <p v-if="state.phase === 'confirm' && state.actionIds.length" class="meta-setup-card__hint">
        {{ t('chat.metaSetup.confirmHint') }}
      </p>
      <p v-if="state.phase === 'confirm' && usesHomebrew" class="meta-setup-card__hint">
        {{ t('chat.metaSetup.homebrewHint') }}
      </p>
      <section
        v-if="providerActions.length"
        class="meta-setup-card__provider-list"
        data-testid="meta-setup-providers"
      >
        <div
          v-for="action in providerActions"
          :key="action.id"
          class="meta-setup-card__credential"
          data-testid="meta-setup-provider"
          :data-provider-id="action.provider_id"
        >
          <strong>{{ t('chat.metaSetup.providerTitle', {
            provider: providerDisplayLabel(action),
          }) }}</strong>
          <p v-if="action.capability_ids?.length">
            {{ t('chat.metaSetup.providerCapabilities', {
              capabilities: action.capability_ids.join(', '),
            }) }}
          </p>
          <p>{{ providerReason(action) }}</p>
          <p>{{ t('chat.metaSetup.providerNoChargeHint') }}</p>
        </div>
      </section>

      <div
        v-if="busy"
        class="meta-setup-card__status"
        data-testid="meta-setup-status"
      >
        <LoadingSpinner aria-hidden="true" />
        <div>
          <strong
            data-testid="meta-setup-phase-status"
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >{{ statusText }}</strong>
          <p v-if="currentActionLabel">{{ currentActionLabel }}</p>
          <p v-if="actionTotal > 0">
            {{ t('chat.metaSetup.completedActions', {
              completed: state.completedActions.length,
              total: actionTotal,
            }) }}
          </p>
          <div
            v-if="showDownloadProgress"
            class="meta-setup-card__download"
            data-testid="meta-setup-download"
          >
            <div class="meta-setup-card__download-copy">
              <span>{{ t('chat.metaSetup.downloadProgress') }}</span>
              <span>{{ downloadProgressText }}</span>
            </div>
            <div
              class="meta-setup-card__progress"
              role="progressbar"
              :aria-label="t('chat.metaSetup.downloadProgress')"
              :aria-valuemin="0"
              :aria-valuemax="state.downloadTotalBytes"
              :aria-valuenow="clampedDownloadedBytes"
              :aria-valuetext="downloadProgressText"
            >
              <span :style="{ width: `${downloadPercent}%` }" />
            </div>
          </div>
        </div>
      </div>

      <div
        v-if="state.phase === 'failed'"
        class="meta-setup-card__problem is-failed"
        role="alert"
        data-testid="meta-setup-error"
      >
        <strong>{{ t('chat.metaSetup.failedTitle') }}</strong>
        <p>{{ state.error || state.message || t('chat.metaSetup.launchFailed') }}</p>
      </div>

      <div
        v-if="state.phase === 'blocked'"
        class="meta-setup-card__problem is-blocked"
        role="alert"
        data-testid="meta-setup-blocked"
      >
        <strong>{{ t('chat.metaSetup.blockedTitle') }}</strong>
        <p>{{ blockedText }}</p>
      </div>
    </div>

    <footer class="meta-setup-card__footer">
      <template v-if="state.phase === 'confirm'">
        <button
          type="button"
          class="btn btn--ghost"
          data-testid="meta-setup-cancel"
          @click="emit('cancel')"
        >
          {{ t('chat.metaSetup.notNow') }}
        </button>
        <button
          v-for="(action, index) in providerActions"
          :key="action.id"
          type="button"
          class="btn"
          :class="state.actionIds.length === 0 && index === 0 ? 'btn--primary' : 'btn--ghost'"
          data-testid="meta-setup-configure-provider"
          :data-provider-id="action.provider_id"
          :disabled="providerNavigationPending"
          @click="emit('configure', action.provider_id || '')"
        >
          {{ t('chat.metaSetup.configureProvider', {
            provider: providerDisplayLabel(action),
          }) }}
        </button>
        <button
          v-if="state.retryMode === 'readiness'"
          type="button"
          class="btn btn--ghost"
          data-testid="meta-setup-retry"
          :disabled="providerNavigationPending"
          @click="emit('retry')"
        >
          {{ t('chat.metaSetup.checkAgain') }}
        </button>
        <button
          v-if="state.actionIds.length"
          type="button"
          class="btn btn--primary"
          data-testid="meta-setup-confirm"
          @click="emit('confirm')"
        >
          {{ t('chat.metaSetup.installAndContinue') }}
        </button>
      </template>

      <template v-else-if="busy">
        <p class="meta-setup-card__footer-note">
          {{ t('chat.metaSetup.backgroundHint') }}
        </p>
        <button
          type="button"
          class="btn btn--ghost"
          data-testid="meta-setup-cancel"
          @click="emit('cancel')"
        >
          {{ t('chat.metaSetup.hide') }}
        </button>
      </template>

      <template v-else>
        <button
          type="button"
          class="btn btn--ghost"
          data-testid="meta-setup-cancel"
          @click="emit('cancel')"
        >
          {{ t('chat.metaSetup.close') }}
        </button>
        <button
          v-for="action in providerActions"
          :key="action.id"
          type="button"
          class="btn btn--ghost"
          data-testid="meta-setup-configure-provider"
          :data-provider-id="action.provider_id"
          :disabled="providerNavigationPending"
          @click="emit('configure', action.provider_id || '')"
        >
          {{ t('chat.metaSetup.configureProvider', {
            provider: providerDisplayLabel(action),
          }) }}
        </button>
        <button
          v-if="state.retryMode"
          type="button"
          class="btn btn--primary"
          data-testid="meta-setup-retry"
          @click="emit('retry')"
        >
          {{ state.retryMode === 'readiness'
            ? t('chat.metaSetup.checkAgain')
            : t('chat.metaSetup.retry') }}
        </button>
      </template>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUpdated, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import LoadingSpinner from '@/components/LoadingSpinner.vue'
import type {
  MetaSetupAction,
  MetaSetupManualAction,
  MetaSetupState,
} from '@/types/metaSetup'

let nextCardId = 0
const instanceId = ++nextCardId
const titleId = `meta-setup-title-${instanceId}`
const descriptionId = `meta-setup-description-${instanceId}`

const props = withDefaults(defineProps<{
  state: MetaSetupState
  providerNavigationPending?: boolean
}>(), {
  providerNavigationPending: false,
})

const emit = defineEmits<{
  confirm: []
  cancel: []
  retry: []
  configure: [providerId: string]
}>()

const { t, locale } = useI18n()
const cardElement = ref<HTMLElement | null>(null)
type PendingFocusTarget = 'card' | 'close' | 'retry-or-close'
let pendingFocusTarget: PendingFocusTarget | null = null

watch(
  () => props.state.phase,
  (phase, previousPhase) => {
    const card = cardElement.value
    const activeElement = document.activeElement
    const focusWasWithinCard = Boolean(
      card
      && activeElement instanceof Element
      && card.contains(activeElement),
    )

    if (previousPhase !== undefined && !focusWasWithinCard) return
    pendingFocusTarget = phase === 'failed' || phase === 'blocked'
      ? 'retry-or-close'
      : 'card'
  },
  { immediate: true, flush: 'sync' },
)

function restorePendingFocus(): void {
  const target = pendingFocusTarget
  if (!target) return
  pendingFocusTarget = null

  const card = cardElement.value
  if (!card) return
  const closeButton = card.querySelector<HTMLButtonElement>('[data-testid="meta-setup-cancel"]')
  const retryButton = card.querySelector<HTMLButtonElement>('[data-testid="meta-setup-retry"]')
  const element = target === 'retry-or-close'
    ? retryButton || closeButton || card
    : target === 'close'
      ? closeButton || card
      : card
  element.focus({ preventScroll: true })
}

onMounted(restorePendingFocus)
onUpdated(restorePendingFocus)

const busy = computed(() => props.state.phase === 'installing' || props.state.phase === 'verifying')

const actions = computed<MetaSetupAction[]>(() => props.state.readiness.setup_actions || [])

const usesHomebrew = computed(() => actions.value.some(action => (
  /formulae\.brew\.sh/i.test(action.source || '')
)))

const providerActions = computed<MetaSetupManualAction[]>(() => (
  (props.state.readiness.manual_setup_actions || [])
    .filter(action => (
      action.kind === 'provider_connection'
      && action.available !== false
      && Boolean(action.provider_id?.trim())
    ))
    .sort((left, right) => Number(Boolean(right.recommended)) - Number(Boolean(left.recommended)))
))

const missingDependencies = computed(() => {
  const readiness = props.state.readiness
  const items = [
    ...(readiness.missing_bins || []),
    ...(readiness.missing_env || []),
    ...(readiness.missing_env_any || []).map(group => group.join(' / ')),
    ...(readiness.missing_skills || []),
    ...(readiness.missing_capabilities || []),
    ...(readiness.missing_provider_capabilities || []),
  ]
  return [...new Set(items.filter(Boolean))]
})

const phaseLabel = computed(() => {
  const keys = {
    confirm: 'chat.metaSetup.badgeConfirm',
    installing: 'chat.metaSetup.badgeInstalling',
    verifying: 'chat.metaSetup.badgeVerifying',
    failed: 'chat.metaSetup.badgeFailed',
    blocked: 'chat.metaSetup.badgeBlocked',
  } as const
  return t(keys[props.state.phase])
})

const statusText = computed(() => {
  if (props.state.phase === 'verifying') return t('chat.metaSetup.verifyingStatus')
  const total = Number(props.state.downloadTotalBytes) || 0
  const downloaded = Number(props.state.downloadedBytes) || 0
  if (total > 0 && downloaded >= total) return t('chat.metaSetup.finishingStatus')
  return t('chat.metaSetup.installingStatus')
})

const actionTotal = computed(() => {
  const ids = new Set([...props.state.actionIds, ...props.state.completedActions])
  return ids.size
})

const currentActionLabel = computed(() => {
  if (!props.state.currentAction) return ''
  const action = actions.value.find(candidate => candidate.id === props.state.currentAction)
  return action ? actionDisplayLabel(action) : props.state.currentAction
})

const showDownloadProgress = computed(() => (
  props.state.phase === 'installing'
  && Number(props.state.downloadTotalBytes) > 0
))

const clampedDownloadedBytes = computed(() => Math.min(
  Math.max(0, Number(props.state.downloadedBytes) || 0),
  Math.max(0, Number(props.state.downloadTotalBytes) || 0),
))

const downloadPercent = computed(() => {
  const total = Number(props.state.downloadTotalBytes) || 0
  return total > 0 ? Math.min(100, (clampedDownloadedBytes.value / total) * 100) : 0
})

const downloadProgressText = computed(() => (
  `${formatBytes(clampedDownloadedBytes.value)} / ${formatBytes(Number(props.state.downloadTotalBytes))}`
))

const blockedText = computed(() => {
  if (props.state.blockedReason === 'session_changed') {
    return t('chat.metaSetup.sessionChanged')
  }
  if (props.state.blockedReason === 'no_actions') {
    return props.state.error || t('chat.metaSetup.noAutomaticSetup')
  }
  return props.state.error || props.state.message || t('chat.metaSetup.noAutomaticSetup')
})

function isHttpsUrl(value: string): boolean {
  return /^https:\/\//i.test(value)
}

function actionDisplayLabel(action: MetaSetupAction): string {
  if (action.install_id === 'paper-tex') return t('chat.metaSetup.paperToolchain')
  if (action.install_id === 'media-ffmpeg') return t('chat.metaSetup.mediaToolchain')
  return action.label || action.id
}

function actionDisplayReason(action: MetaSetupAction): string {
  const reason = action.reason || ''
  const missing = /^Missing runtime capabilities:\s*(.+)$/i.exec(reason)
  if (missing) {
    return t('chat.metaSetup.missingRuntimeCapabilities', { capabilities: missing[1] })
  }
  return reason
}

function providerDisplayLabel(action: MetaSetupManualAction): string {
  if (action.label?.trim()) return action.label.trim()
  return String(action.provider_id || '')
    .split(/[-_.]+/)
    .filter(Boolean)
    .map(part => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    .join(' ')
}

function providerReason(action: MetaSetupManualAction): string {
  const keys: Record<string, string> = {
    missing_credential: 'chat.metaSetup.providerReasonMissingCredential',
    credential_pool_exhausted: 'chat.metaSetup.providerReasonPoolExhausted',
    credential_endpoint_mismatch: 'chat.metaSetup.providerReasonEndpointMismatch',
    missing_base_url: 'chat.metaSetup.providerReasonMissingEndpoint',
    invalid_endpoint: 'chat.metaSetup.providerReasonInvalidEndpoint',
    runtime_unsupported: 'chat.metaSetup.providerReasonRuntimeUnsupported',
    unknown_provider: 'chat.metaSetup.providerReasonUnknownProvider',
  }
  return t(keys[String(action.reason_code || '')]
    || 'chat.metaSetup.providerReasonConnectionRequired')
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let amount = value
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  const maximumFractionDigits = amount >= 10 || unit === 0 ? 0 : 1
  return `${new Intl.NumberFormat(locale.value, { maximumFractionDigits }).format(amount)} ${units[unit]}`
}
</script>

<style scoped>
.meta-setup-card {
  display: flex;
  flex-direction: column;
  width: min(100%, var(--chat-col));
  max-height: min(42rem, calc(100dvh - 8rem));
  margin: var(--sp-3) auto;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
}

.meta-setup-card__header,
.meta-setup-card__footer {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
}

.meta-setup-card__header {
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
}

.meta-setup-card__title {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  font-size: var(--fs-md);
  font-weight: 650;
}

.meta-setup-card__badge {
  flex: 0 0 auto;
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-pill);
  color: var(--text-muted);
  background: var(--bg-elevated);
  font-size: var(--fs-xs);
  font-weight: 650;
}

.meta-setup-card__badge.is-failed,
.meta-setup-card__problem.is-failed {
  border-color: color-mix(in srgb, var(--danger) 45%, var(--border));
  color: var(--danger);
}

.meta-setup-card__badge.is-blocked,
.meta-setup-card__problem.is-blocked {
  border-color: color-mix(in srgb, var(--warn) 45%, var(--border));
  color: var(--warn);
}

.meta-setup-card__body {
  display: grid;
  flex: 1 1 auto;
  gap: var(--sp-4);
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--sp-4);
}

.meta-setup-card__intro,
.meta-setup-card__hint,
.meta-setup-card__credential p,
.meta-setup-card__action p,
.meta-setup-card__problem p,
.meta-setup-card__status p {
  margin: 0;
}

.meta-setup-card__intro,
.meta-setup-card__hint,
.meta-setup-card__credential p,
.meta-setup-card__action-bins,
.meta-setup-card__status p {
  color: var(--text-muted);
}

.meta-setup-card__section {
  min-width: 0;
}

.meta-setup-card__credential {
  padding: var(--sp-3);
  border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--accent) 6%, var(--bg-elevated));
}

.meta-setup-card__provider-list {
  display: grid;
  gap: var(--sp-2);
}

.meta-setup-card__credential p {
  margin-top: var(--sp-1) !important;
}

.meta-setup-card__section h4 {
  margin: 0 0 var(--sp-2);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 650;
  letter-spacing: 0.03em;
  text-transform: uppercase;
}

.meta-setup-card__chips,
.meta-setup-card__actions-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.meta-setup-card__chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.meta-setup-card__chips li {
  max-width: 100%;
  padding: var(--sp-1) var(--sp-2);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  overflow-wrap: anywhere;
}

.meta-setup-card__actions-list {
  display: grid;
  gap: var(--sp-2);
}

.meta-setup-card__action {
  min-width: 0;
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
}

.meta-setup-card__action-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
}

.meta-setup-card__action-head strong,
.meta-setup-card__action-head span,
.meta-setup-card__meta dd {
  min-width: 0;
  overflow-wrap: anywhere;
}

.meta-setup-card__version,
.meta-setup-card__meta {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.meta-setup-card__action-bins {
  margin-top: var(--sp-1) !important;
  overflow-wrap: anywhere;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}

.meta-setup-card__meta {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: var(--sp-1) var(--sp-2);
  margin: var(--sp-2) 0 0;
}

.meta-setup-card__meta dt,
.meta-setup-card__meta dd {
  margin: 0;
}

.meta-setup-card__meta a {
  color: var(--accent);
}

.meta-setup-card__unavailable {
  margin-top: var(--sp-2) !important;
  color: var(--warn);
  font-size: var(--fs-xs);
}

.meta-setup-card__action-reason {
  margin-top: var(--sp-2) !important;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.meta-setup-card__admin-note {
  margin-top: var(--sp-2) !important;
  color: var(--warn);
  font-size: var(--fs-xs);
}

.meta-setup-card__status,
.meta-setup-card__problem {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
}

.meta-setup-card__status > div,
.meta-setup-card__problem {
  min-width: 0;
}

.meta-setup-card__status p,
.meta-setup-card__problem p {
  margin-top: var(--sp-1);
  overflow-wrap: anywhere;
  font-size: var(--fs-sm);
}

.meta-setup-card__download {
  display: grid;
  gap: var(--sp-1);
  width: min(24rem, 100%);
  margin-top: var(--sp-2);
}

.meta-setup-card__download-copy {
  display: flex;
  justify-content: space-between;
  gap: var(--sp-3);
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.meta-setup-card__progress {
  height: 0.375rem;
  overflow: hidden;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--border) 72%, transparent);
}

.meta-setup-card__progress > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  transition: width var(--dur-fast) var(--ease-out);
}

@media (prefers-reduced-motion: reduce) {
  .meta-setup-card__progress > span {
    transition: none;
  }
}

.meta-setup-card__problem {
  display: block;
}

.meta-setup-card__footer {
  flex-wrap: wrap;
  justify-content: flex-end;
  border-top: 1px solid var(--border);
  background: var(--bg-elevated);
}

.meta-setup-card__footer .btn {
  min-height: 40px;
}

.meta-setup-card__footer-note {
  flex: 1 1 18rem;
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

@media (max-width: 560px) {
  .meta-setup-card {
    max-height: calc(100dvh - 6rem);
  }

  .meta-setup-card__header,
  .meta-setup-card__action-head {
    align-items: flex-start;
  }

  .meta-setup-card__action-head {
    flex-direction: column;
    gap: var(--sp-1);
  }

  .meta-setup-card__footer {
    display: grid;
    grid-template-columns: 1fr;
  }

  .meta-setup-card__footer .btn {
    width: 100%;
  }

  .meta-setup-card__footer-note {
    order: 2;
  }
}

@media (max-height: 560px) {
  .meta-setup-card {
    max-height: calc(100dvh - 2rem);
  }
}
</style>
