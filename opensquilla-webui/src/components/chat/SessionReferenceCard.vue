<template>
  <button
    class="session-reference-card"
    type="button"
    data-testid="session-reference-card"
    :data-session-key="reference.scope.sessionKey"
    :data-session-state="availability"
    :data-reference-unavailable-reason="failureReason || undefined"
    :disabled="availability !== 'available'"
    :aria-label="`${label} — ${statusLabel}`"
    @click="openSession"
  >
    <span class="session-reference-card__identity">
      <Icon name="chat" :size="17" />
      <span :title="label">{{ label }}</span>
    </span>
    <span class="session-reference-card__status" :data-status="availability">
      {{ statusLabel }}
      <Icon v-if="availability === 'available'" name="chevronRight" :size="15" />
    </span>
  </button>
</template>

<script setup lang="ts">
import { computed, inject, onScopeDispose, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { SessionReferenceV1 } from '@/types/references'
import { SESSION_DIRECTORY_KEY, SessionDirectoryError, type ResolvedSession } from '@/modules/sessionDirectory'
import { SESSION_DIRECTORY_CHANGES_KEY } from '@/modules/sessionDirectoryChanges'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'

const props = defineProps<{
  reference: SessionReferenceV1
  resolveSessionAvailability?: (sessionKey: string) => Promise<boolean>
}>()

const emit = defineEmits<{ open: [sessionKey: string] }>()
const { t } = useI18n()
const directory = inject(SESSION_DIRECTORY_KEY, null)
const changes = inject(SESSION_DIRECTORY_CHANGES_KEY, null)
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const referenceGatewayEndpoint = gateway?.loadConnectionEndpoint() ?? null
const label = ref(props.reference.label)
const runStatus = ref<string | null>(props.reference.state.runStatus)
const failureReason = ref('')
const availability = ref<'available' | 'checking' | 'missing'>(
  props.reference.state.available && props.reference.capabilities.open !== false
    ? 'available'
    : 'missing',
)
const statusLabel = computed(() => {
  if (availability.value === 'missing') return t('chat.sessionReference.status.missing')
  if (availability.value === 'checking') return t('chat.sessionReference.status.checking')
  switch (runStatus.value) {
    case 'queued': return t('chat.sessionReference.status.queued')
    case 'running': return t('chat.sessionReference.status.running')
    case 'failed':
    case 'timeout':
    case 'cancelled':
    case 'interrupted': return t('chat.sessionReference.status.failed')
    case 'missing': return t('chat.sessionReference.status.missing')
    default: return t('chat.sessionReference.status.idle')
  }
})

let generation = 0
let disposed = false
let activeRequest: AbortController | null = null

function gatewayContext() {
  return gateway ? {
    endpoint: gateway.loadConnectionEndpoint(),
    epoch: gateway.subscriptionEpoch,
    available: gateway.isAvailable,
  } : null
}

function matchesGateway(context: ReturnType<typeof gatewayContext>): boolean {
  const current = gatewayContext()
  return context === null ? current === null : current !== null
    && current.available
    && current.endpoint === context.endpoint
    && current.epoch === context.epoch
}

function unavailableReason(): string {
  if (!props.reference.scope.sessionKey) return 'invalid-key'
  if (!props.reference.state.available || props.reference.capabilities.open === false) return 'capability'
  // The current directory has no instance-ID attestation API. A scoped
  // reference must stay unavailable until its instance can be verified.
  if (props.reference.scope.gatewayInstanceId) return 'instance-unverified'
  if (!directory && !props.resolveSessionAvailability) return 'resolver-unavailable'
  if (gateway && !gateway.isAvailable) return 'gateway-unavailable'
  if (gateway && gateway.loadConnectionEndpoint() !== referenceGatewayEndpoint) return 'gateway-changed'
  return ''
}

function canResolve(): boolean {
  return !unavailableReason()
}

function invalidate() {
  generation += 1
  activeRequest?.abort()
  activeRequest = null
}

async function refresh(): Promise<boolean> {
  invalidate()
  const current = generation
  const key = props.reference.scope.sessionKey
  failureReason.value = unavailableReason()
  if (failureReason.value) {
    availability.value = 'missing'
    return false
  }
  const request = new AbortController()
  activeRequest = request
  const context = gatewayContext()
  try {
    let available = false
    let resolved: ResolvedSession | null = null
    if (directory) {
      resolved = await directory.resolve({ key, signal: request.signal })
      // A structured reference is an exact identity, never a fuzzy lookup.
      available = resolved.key === key && Boolean(resolved.id)
    } else if (props.resolveSessionAvailability) {
      available = await props.resolveSessionAvailability(key)
    }
    if (disposed || current !== generation) return false
    failureReason.value = !available ? 'identity-mismatch'
      : unavailableReason() || (!matchesGateway(context) ? 'gateway-changed' : '')
    available = available && !failureReason.value
    if (available && resolved) {
      label.value = resolved.title?.trim() || props.reference.label
      runStatus.value = resolved.runStatus || props.reference.state.runStatus
    }
    availability.value = available ? 'available' : 'missing'
    return available
  } catch (error) {
    if (!disposed && current === generation) {
      failureReason.value = `resolve-error:${error instanceof SessionDirectoryError ? error.code : 'unknown'}`
      availability.value = 'missing'
    }
    return false
  }
}

// Watch primitive identity/metadata values. Streaming reparses tool results
// into new objects on every token; equivalent references must not issue RPCs.
watch([
  () => props.reference.id,
  () => props.reference.scope.sessionKey,
  () => props.reference.scope.gatewayInstanceId,
  () => props.reference.label,
  () => props.reference.state.available,
  () => props.reference.state.runStatus,
  () => props.reference.state.revision,
  () => props.reference.capabilities.open,
  () => props.resolveSessionAvailability,
  () => gateway?.loadConnectionEndpoint(),
  () => gateway?.subscriptionEpoch,
  () => gateway?.isAvailable,
], () => {
  label.value = props.reference.label
  runStatus.value = props.reference.state.runStatus
  availability.value = canResolve() ? 'checking' : 'missing'
  void refresh()
}, { immediate: true, flush: 'sync' })

const subscription = changes?.subscribe(change => {
  if (change.key !== props.reference.scope.sessionKey || !canResolve()) return
  if (change.reason === 'deleted') {
    invalidate()
    availability.value = 'missing'
    failureReason.value = 'deleted'
    return
  }
  if (change.runStatus) runStatus.value = change.runStatus
  void refresh()
})

onScopeDispose(() => {
  disposed = true
  invalidate()
  subscription?.close()
})

async function openSession() {
  if (availability.value !== 'available' || !canResolve()) return
  const key = props.reference.scope.sessionKey
  const context = gatewayContext()
  availability.value = 'checking'
  const opening = refresh()
  const current = generation
  if (await opening && current === generation && !disposed && matchesGateway(context)) emit('open', key)
}
</script>

<style scoped>
.session-reference-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  gap: var(--sp-3);
  margin: var(--sp-2) 0;
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  text-align: left;
  font: inherit;
  cursor: pointer;
}

.session-reference-card__identity,
.session-reference-card__status { display: inline-flex; align-items: center; }
.session-reference-card__identity { min-width: 0; gap: var(--sp-2); font-size: var(--fs-sm); font-weight: 600; }
.session-reference-card__identity :deep(.icon) { color: var(--text-muted); }
.session-reference-card__identity > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-reference-card__status { flex: 0 0 auto; gap: var(--sp-1); color: var(--text-muted); font-size: var(--fs-sm); }
.session-reference-card__status[data-status='missing'] { color: var(--danger); }
.session-reference-card:hover { background: var(--bg-hover); }
.session-reference-card:disabled { cursor: default; opacity: var(--state-disabled-opacity); }
.session-reference-card:disabled:hover { background: var(--bg-surface); }
.session-reference-card:focus-visible { outline: 2px solid var(--border-focus); outline-offset: 2px; }
@media (max-width: 540px) { .session-reference-card { padding: var(--sp-3); } }
</style>
