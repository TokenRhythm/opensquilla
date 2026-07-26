<template>
  <div class="hub control-stage">
    <header class="control-stage__header">
      <div class="control-stage__title-block">
        <h1 class="control-stage__title">{{ t('sessions.title') }}</h1>
        <p class="control-stage__subtitle">
          {{ t('sessions.subtitle') }}
        </p>
      </div>
      <div class="control-stage__actions">
        <button class="btn btn--ghost" :title="t('sessions.refresh')" :disabled="refreshing" @click="refresh">
          <Icon name="refresh" :size="16" />
          <span>{{ refreshing ? t('sessions.refreshing') : t('sessions.refresh') }}</span>
        </button>
      </div>
    </header>

    <SessionsTaskInput @submit="startTask" />

    <SessionsAttentionStrip
      :approvals-count="pendingApprovals.length"
      :running-count="runningCount"
      :queued-count="queuedCount"
      :cost-usd="costUsd"
      :cost-period="costPeriod"
      @open-approvals="openBlockedSession"
      @open-usage="router.push('/usage')"
    />

    <section class="hub-list">
      <div v-if="allSessions.length > 0" class="hub-list__head">
        <div class="hub-filters control-segmented" role="group" :aria-label="t('sessions.filter.ariaLabel')">
          <button
            v-for="chip in FILTER_CHIPS"
            :key="chip.id"
            type="button"
            class="hub-filter control-segmented__btn"
            :class="{ 'is-active': filter === chip.id }"
            :aria-pressed="filter === chip.id"
            @click="filter = chip.id"
          >
            {{ t(chip.labelKey) }}
          </button>
        </div>
        <div class="hub-search">
          <span class="hub-search__icon" aria-hidden="true">
            <Icon name="search" :size="14" />
          </span>
          <input
            v-model="search"
            type="text"
            class="hub-search__input"
            :placeholder="t('sessions.search.placeholder')"
            :aria-label="t('sessions.search.ariaLabel')"
            autocomplete="off"
          />
        </div>
      </div>

      <ErrorState
        v-if="sessionListError"
        :message="t('sessions.error.load')"
        :on-retry="loadAll"
      />

      <div v-else-if="isLoading && allSessions.length === 0" class="hub-state control-empty">
        <LoadingSpinner />
        <p class="control-empty__hint">{{ t('sessions.loading') }}</p>
      </div>

      <div v-else-if="allSessions.length === 0" class="hub-state control-empty">
        <Icon name="sessions" :size="32" class="control-empty__icon" aria-hidden="true" />
        <div class="control-empty__title">{{ t('sessions.empty.title') }}</div>
        <p class="control-empty__hint">{{ t('sessions.empty.body') }}</p>
      </div>

      <div v-else-if="ledgerEntries.length === 0" class="hub-state control-empty">
        <Icon name="search" :size="32" class="control-empty__icon" aria-hidden="true" />
        <div class="control-empty__title">{{ t('sessions.noMatches.title') }}</div>
        <p class="control-empty__hint">{{ t('sessions.noMatches.body') }}</p>
        <button class="btn btn--ghost" @click="clearFilters">{{ t('sessions.noMatches.clear') }}</button>
      </div>

      <SessionsLedger
        v-else
        :entries="ledgerEntries"
        :agent-names="agentNames"
        :agents-loaded="agentsLoaded"
        :needs-input-keys="needsInputKeys"
        @open="openSession"
        @remove="removeSession"
      />
    </section>

    <SessionInspectDrawer
      :open="inspectItem !== null"
      :item="inspectItem"
      :agent-name="inspectAgentName"
      :parent-item="inspectParent"
      :needs-input="inspectItem ? needsInputKeys.has(inspectItem.key) : false"
      @close="closeInspect"
      @open-chat="openInChat"
      @aborted="onInspectAborted"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { useRpcStore } from '@/stores/rpc'
import Icon from '@/components/Icon.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import { useConfirm } from '@/composables/useConfirm'
import { requestUsageSnapshot } from '@/composables/usage/useUsageQuery'
import type { UsageSnapshot } from '@/types/usage'
import SessionsTaskInput from '@/components/sessions/SessionsTaskInput.vue'
import SessionsAttentionStrip from '@/components/sessions/SessionsAttentionStrip.vue'
import SessionsLedger from '@/components/sessions/SessionsLedger.vue'
import SessionInspectDrawer from '@/components/sessions/SessionInspectDrawer.vue'
import {
  arrangeSessionLedger,
  itemKey,
  sessionMatches,
  sessionParentKey,
  useSessions,
  type SessionItem,
} from '@/composables/useSessions'
import {
  dispatchLocalSessionsDeleted,
  localSessionsDeletedDetail,
  LOCAL_SESSIONS_DELETED_EVENT,
} from '@/utils/sessionSync'
import { sessionAgentIdentity } from '@/components/sessions/sessionDisplay'

type FilterId = 'all' | 'chats' | 'automations' | 'channels'

interface AgentsListResponse {
  agents?: Array<{ id?: string; name?: string }>
}

interface DeleteResponse {
  deleted?: string[]
  errors?: unknown[]
}

const FILTER_CHIPS: Array<{ id: FilterId; labelKey: string }> = [
  { id: 'all', labelKey: 'sessions.filter.all' },
  { id: 'chats', labelKey: 'sessions.filter.chats' },
  { id: 'automations', labelKey: 'sessions.filter.automations' },
  { id: 'channels', labelKey: 'sessions.filter.channels' },
]

const FILTER_KINDS: Record<Exclude<FilterId, 'all'>, string> = {
  chats: 'chat',
  automations: 'cron',
  channels: 'channel',
}

const REFRESH_DEBOUNCE_MS = 150
const FALLBACK_POLL_MS = 30000
const SESSIONS_VIEW_SYNC_SOURCE = 'sessions-view'

const { t } = useI18n()
const router = useRouter()
const rpc = useRpcStore()
const { confirm } = useConfirm()
const { sessionsList, allSessions, isLoading, sessionListError, loadSessions } = useSessions()

const filter = ref<FilterId>('all')
const search = ref('')
const agentNames = ref<Map<string, string>>(new Map())
const agentsLoaded = ref(false)
let agentsRequestGeneration = 0
const pendingApprovals = ref<string[]>([])
const costUsd = ref<number | null>(null)
const costPeriod = ref<'today' | 'total'>('total')

let refreshTimer: ReturnType<typeof setTimeout> | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null
let unsubs: Array<() => void> = []
let lastCostSnapshot: UsageSnapshot | null = null

// ---------------------------------------------------------------------------
// Derivations
// ---------------------------------------------------------------------------

const runningCount = computed(() => allSessions.value.filter(s => s.runStatus === 'running').length)
const queuedCount = computed(() => allSessions.value.filter(s => s.runStatus === 'queued').length)
const needsInputKeys = computed(() => new Set(pendingApprovals.value))

function matchesFilter(item: SessionItem, byKey: Map<string, SessionItem>): boolean {
  if (filter.value === 'all') return true
  const kind = FILTER_KINDS[filter.value]
  // Subagent rows follow their parent through the filter.
  let current: SessionItem | undefined = item
  for (let hop = 0; current && hop < 4; hop++) {
    if (current.sessionKind === kind) return true
    const parentKey = sessionParentKey(current)
    current = parentKey ? byKey.get(parentKey) : undefined
  }
  return false
}

const ledgerEntries = computed(() => {
  const query = search.value.trim().toLowerCase()
  const byKey = new Map(allSessions.value.map(item => [item.key, item]))
  const visible = allSessions.value.filter(item =>
    matchesFilter(item, byKey) && (!query || sessionMatches(item, query)))
  return arrangeSessionLedger(visible)
})

// The inspected row tracks the live session list by key so status flips keep
// rendering; the click-time snapshot covers rows that drop out of the list.
const inspectKey = ref('')
const inspectFallback = ref<SessionItem | null>(null)

const inspectItem = computed(() => {
  if (!inspectKey.value) return null
  return allSessions.value.find(item => item.key === inspectKey.value) || inspectFallback.value
})

const inspectParent = computed(() => {
  const item = inspectItem.value
  if (!item) return null
  const parentKey = sessionParentKey(item)
  return parentKey ? allSessions.value.find(candidate => candidate.key === parentKey) || null : null
})

const inspectAgentName = computed(() => {
  const identity = sessionAgentIdentity(
    inspectItem.value?.effectiveAgentId,
    agentNames.value,
    agentsLoaded.value,
  )
  if (identity.kind === 'unknown') return t('sessions.unknownAgent')
  if (identity.kind === 'deleted') return t('sessions.deletedAgent', { id: identity.value })
  return identity.value
})

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function loadAgents() {
  const generation = ++agentsRequestGeneration
  try {
    const data = await rpc.call<AgentsListResponse>('agents.list')
    if (generation !== agentsRequestGeneration) return
    agentNames.value = new Map(
      (data?.agents || [])
        .filter(agent => agent.id)
        .map(agent => [String(agent.id), String(agent.name || agent.id)]))
    agentsLoaded.value = true
  } catch {
    if (generation === agentsRequestGeneration) {
      // A stale directory must not produce false “Deleted agent” labels after
      // the current authoritative lookup failed.
      agentsLoaded.value = false
    }
  }
}

function approvalAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  try {
    const token = sessionStorage.getItem('opensquilla.wsToken') || ''
    if (token) headers['Authorization'] = `Bearer ${token}`
  } catch { /* ignore */ }
  return headers
}

async function refreshApprovals() {
  try {
    const res = await fetch('/api/approvals', { headers: approvalAuthHeaders() })
    if (!res.ok) return
    const data = await res.json() as { pending?: Array<{ sessionKey?: string }> }
    pendingApprovals.value = (data.pending || [])
      .map(item => String(item.sessionKey || '').trim())
      .filter(Boolean)
  } catch {
    // Strip keeps the last known count.
  }
}

async function refreshCost() {
  try {
    const snapshot = await requestUsageSnapshot(rpc, 'today', {
      days: false,
      models: false,
      sessions: false,
      // Old gateways only expose lifetime totals. Showing that value is safe
      // as long as the label says total rather than incorrectly claiming today.
      fallbackRange: 'all',
      cachedSnapshot: lastCostSnapshot,
    })
    lastCostSnapshot = snapshot
    costUsd.value = snapshot.totals.cost
    costPeriod.value = snapshot.source === 'usage_ledger' ? 'today' : 'total'
  } catch {
    costUsd.value = null
    costPeriod.value = 'total'
  }
}

function loadAll() {
  void loadSessions()
  void loadAgents()
  void refreshApprovals()
  void refreshCost()
}

const refreshing = ref(false)

// Manual refresh shows a busy state; the fallback poll keeps calling loadAll so
// the button reacts only to user clicks, not background refreshes.
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await Promise.all([loadSessions(), loadAgents(), refreshApprovals(), refreshCost()])
  } finally {
    refreshing.value = false
  }
}

function scheduleSessionRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    void loadSessions()
  }, REFRESH_DEBOUNCE_MS)
}

function applyLocalDeletedSessions(keys: Set<string>) {
  if (keys.size === 0) return
  sessionsList.value = sessionsList.value.filter(item => !keys.has(itemKey(item)))
  if (inspectKey.value && keys.has(inspectKey.value)) closeInspect()
}

function handleLocalSessionsDeleted(event: Event) {
  const detail = localSessionsDeletedDetail(event)
  if (!detail || detail.source === SESSIONS_VIEW_SYNC_SOURCE) return
  applyLocalDeletedSessions(new Set(detail.keys))
  scheduleSessionRefresh()
}

function handleApprovalPush() {
  void refreshApprovals()
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

function startTask(text: string) {
  router.push({
    path: '/chat/new',
    query: { agent: 'main' },
    // autosend asks the draft to fire the prefill in one step, so "Start task"
    // actually starts the task instead of dropping the operator at the composer.
    state: { prefill: text, autosend: true },
  }).catch(() => {})
}

// Row click opens the inspect drawer; navigation moved to the drawer's
// explicit "Open in chat" action.
function openSession(item: SessionItem) {
  inspectKey.value = item.key
  inspectFallback.value = item
}

function closeInspect() {
  inspectKey.value = ''
  inspectFallback.value = null
}

function openInChat(item: SessionItem) {
  closeInspect()
  router.push({ path: '/chat', query: { session: item.key } })
}

function onInspectAborted() {
  void loadSessions()
}

function openBlockedSession() {
  const key = pendingApprovals.value.find(Boolean)
  if (key) {
    router.push({ path: '/chat', query: { session: key } })
  }
  // No session-attached pending approval: nothing to open. Approvals resolve
  // inline in chat, so there is no standalone queue page to fall back to.
}

function clearFilters() {
  filter.value = 'all'
  search.value = ''
}

async function removeSession(item: SessionItem) {
  const ok = await confirm({
    title: t('sessions.delete.title'),
    body: t('sessions.delete.body', { title: item.title }),
    primaryLabel: t('sessions.delete.confirm'),
  })
  if (!ok) {
    return
  }
  let result: DeleteResponse | null = null
  try {
    result = await rpc.call<DeleteResponse>('sessions.delete', { keys: [item.key] })
  } catch (err) {
    console.warn('Delete failed: ' + (err instanceof Error ? err.message : String(err)))
    return
  }
  const deleted = new Set(result?.deleted || [])
  if (!deleted.has(item.key)) {
    console.warn('Delete failed: session was not reported deleted', result?.errors)
    void loadSessions()
    return
  }
  applyLocalDeletedSessions(deleted)
  dispatchLocalSessionsDeleted(deleted, SESSIONS_VIEW_SYNC_SOURCE)
  void loadSessions()
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

// This view is kept-alive (route meta.keepAlive), so live subscriptions and the
// fallback poll are bound on activation and released on deactivation — they must
// not keep firing while the view is cached and off-screen. onActivated also runs
// on first display, covering the initial load. onUnmounted is a final safety net
// for the rare case the KeepAlive cache evicts this instance.
function teardownLive() {
  unsubs.forEach(unsub => unsub())
  unsubs = []
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  window.removeEventListener(LOCAL_SESSIONS_DELETED_EVENT, handleLocalSessionsDeleted)
}

onActivated(() => {
  loadAll()
  window.removeEventListener(LOCAL_SESSIONS_DELETED_EVENT, handleLocalSessionsDeleted)
  window.addEventListener(LOCAL_SESSIONS_DELETED_EVENT, handleLocalSessionsDeleted)
  unsubs = [
    rpc.on('sessions.changed', scheduleSessionRefresh),
    rpc.on('exec.approval.requested', handleApprovalPush),
    rpc.on('exec.approval.resolved', handleApprovalPush),
    rpc.on('plugin.approval.requested', handleApprovalPush),
    rpc.on('plugin.approval.resolved', handleApprovalPush),
  ]
  pollTimer = setInterval(loadAll, FALLBACK_POLL_MS)
})

onDeactivated(teardownLive)
onUnmounted(teardownLive)
</script>

<style scoped>
.hub-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

.hub-state {
  min-height: 40vh;
}

.hub-list__head {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  justify-content: space-between;
}

.hub-filter:focus-visible {
  box-shadow: var(--focus-ring);
  outline: none;
}

.hub-search {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  gap: var(--sp-2);
  min-width: 200px;
  padding: 0 var(--sp-3);
}

.hub-search__icon {
  color: var(--text-dim);
  display: inline-flex;
  flex-shrink: 0;
}

.hub-search__input {
  background: transparent;
  border: none;
  color: var(--text);
  font-size: var(--fs-sm);
  outline: none;
  padding: var(--sp-2) 0;
  width: 100%;
}

.hub-search__input::placeholder {
  color: var(--text-dim);
}


@media (max-width: 760px) {
  .hub-list__head {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
