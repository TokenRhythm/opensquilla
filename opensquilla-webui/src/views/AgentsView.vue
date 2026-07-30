<template>
  <div class="ag-stage control-stage">
    <header class="ag-stage__header control-stage__header">
      <div class="ag-stage__title-block control-stage__title-block">
        <h1
          id="agents-page-title"
          class="ag-stage__title control-stage__title"
          tabindex="-1"
        >{{ t('console.agents.title') }}</h1>
        <p class="ag-stage__subtitle control-stage__subtitle">{{ t('console.agents.subtitle') }}</p>
      </div>
      <div class="ag-stage__actions control-stage__actions">
        <button
          class="btn btn--ghost"
          type="button"
          :title="t('console.agents.settingsHint')"
          @click="openSettingsSurface"
        >
          <Icon name="settings" :size="16" aria-hidden="true" />
          <span>{{ t('console.agents.openSettings') }}</span>
        </button>
        <button class="btn btn--ghost" @click="loadData">
          <Icon name="refresh" :size="16" />
          <span>{{ t('console.common.refresh') }}</span>
        </button>
      </div>
    </header>

    <section class="stat-row control-stat-grid control-stat-grid--fixed" style="--control-stat-columns: 3">
      <div class="stat stat--hero control-stat control-stat--hero">
        <div class="stat-label control-stat__label">{{ t('console.agents.totalAgents') }}</div>
        <div class="stat-value control-stat__value">{{ total }}</div>
        <div class="stat-hint control-stat__hint">
          {{ builtins ? t('console.agents.builtinCount', { n: builtins }) : '' }}
          {{ builtins && customs ? ' &middot; ' : '' }}
          {{ customs ? t('console.agents.customCount', { n: customs }) : '' }}
        </div>
      </div>
      <div class="stat control-stat">
        <div class="stat-label control-stat__label">{{ t('console.agents.modelsInUse') }}</div>
        <div class="stat-value mono control-stat__value control-stat__value--mono">{{ models.size || '—' }}</div>
        <div class="stat-hint control-stat__hint">{{ models.size ? t('console.agents.distinctModels') : t('console.agents.unset') }}</div>
      </div>
      <div class="stat control-stat">
        <div class="stat-label control-stat__label">{{ t('console.agents.toolsWired') }}</div>
        <div class="stat-value control-stat__value">{{ toolsCount }}</div>
        <div class="stat-hint control-stat__hint">{{ t('console.agents.acrossAllAgents') }}</div>
      </div>
    </section>

    <section class="ag-create">
      <form class="ag-create__form" @submit.prevent="onInlineAdd">
        <label class="ag-field">
          <span>{{ t('console.agents.agentId') }}</span>
          <input v-model="newId" class="ag-input" name="id" autocomplete="off" required placeholder="e.g. data-analyst" />
        </label>
        <label class="ag-field">
          <span>{{ t('console.agents.displayName') }} <span class="ag-field__optional">{{ t('console.agents.optional') }}</span></span>
          <input v-model="newName" class="ag-input" name="name" autocomplete="off" :placeholder="t('console.agents.defaultsToId')" />
        </label>
        <button class="btn btn--primary" type="submit">
          <Icon name="plus" :size="16" />
          <span>{{ t('console.agents.add') }}</span>
        </button>
      </form>
      <p class="ag-create__hint">{{ t('console.agents.createHint') }}</p>
    </section>

    <section class="ag-list">
      <div class="ag-list__head">
        <h3 class="ag-list__title">
          {{ t('console.agents.configuredAgents') }}
          <span v-if="agents.length > 0" class="ag-list__count">{{ agents.length }}</span>
        </h3>
      </div>

      <div v-if="loading && agents.length === 0" class="state">
        <LoadingSpinner />
      </div>

      <ErrorState v-else-if="error" :message="error" :on-retry="loadData" />

      <div v-else-if="agents.length === 0" class="control-empty">
        <Icon name="agents" :size="32" class="control-empty__icon" aria-hidden="true" />
        <div class="control-empty__title">{{ t('console.agents.emptyTitle') }}</div>
        <p class="control-empty__hint">{{ t('console.agents.emptyTextBefore') }} <code>main</code> {{ t('console.agents.emptyTextAfter') }}</p>
      </div>

      <div v-else class="ag-cards control-card-grid" style="--control-card-min: 320px">
        <article
          v-for="(a, i) in agents"
          :key="a.id || a.name || i"
          class="ag-card control-card"
          :class="{ 'is-builtin control-card--accent': isAgentBuiltin(a) }"
          :style="{ '--i': i }"
        >
          <header class="ag-card__head">
            <div class="ag-card__id-block">
              <button
                type="button"
                class="ag-card__id ag-card__id-btn"
                @click="openDrawer('view', a.id || a.name || '')"
              >{{ a.id || a.name || '—' }}</button>
              <span :class="['chip', isAgentBuiltin(a) ? 'chip-ok' : 'chip-info']">{{ a.type || (a.isBuiltin ? t('console.agents.builtin') : t('console.agents.custom')) }}</span>
            </div>
            <div class="ag-card__actions">
              <button class="ag-iconbtn" :title="t('console.agents.openChat')" @click.stop="openChat(a.id)">
                <Icon name="chat" :size="16" />
                <span>{{ t('console.agents.chat') }}</span>
              </button>
              <button
                v-if="isAgentBuiltin(a)"
                class="ag-iconbtn"
                :title="t('console.agents.customizeHint')"
                @click.stop="customizeFromBuiltin(a.id)"
              >
                <Icon name="plus" :size="16" />
                <span>{{ t('console.agents.customize') }}&hellip;</span>
              </button>
              <button
                v-else
                class="ag-iconbtn"
                :title="t('console.common.edit')"
                @click.stop="openDrawer('edit', a.id)"
              >
                <Icon name="edit" :size="16" />
                <span>{{ t('console.common.edit') }}</span>
              </button>
              <button
                v-if="!isAgentBuiltin(a)"
                class="ag-iconbtn ag-iconbtn--danger"
                :title="t('console.common.delete')"
                @click.stop="deleteAgent(a.id)"
              >
                <Icon name="trash" :size="16" />
                <span>{{ t('console.common.delete') }}</span>
              </button>
            </div>
          </header>
          <div class="ag-card__name">{{ a.name || a.id || '—' }}</div>
          <p v-if="a.description" class="ag-card__desc">{{ a.description }}</p>
          <dl class="ag-card__meta">
            <div v-if="a.model">
              <dt>{{ t('console.agents.model') }}</dt>
              <dd class="ag-mono">{{ a.model }}</dd>
            </div>
            <div v-if="agentTools(a).length">
              <dt>{{ t('console.agents.tools') }}</dt>
              <dd>{{ agentTools(a).length }}</dd>
            </div>
            <div v-if="agentSkills(a).length">
              <dt>{{ t('console.agents.skills') }}</dt>
              <dd>{{ agentSkills(a).length }}</dd>
            </div>
          </dl>
          <div v-if="agentTools(a).length" class="ag-card__chips">
            <span class="ag-chips-label">{{ t('console.agents.tools') }}</span>
            <span v-for="t in agentTools(a).slice(0, 8)" :key="t" class="ag-chip">{{ t }}</span>
            <span v-if="agentTools(a).length > 8" class="ag-chip ag-chip--dim">+{{ agentTools(a).length - 8 }}</span>
          </div>
        </article>
      </div>
    </section>

    <!-- Drawer -->
    <Teleport to="body">
      <Transition name="drawer">
        <div v-if="drawerOpen" class="drawer-overlay" @click="requestCloseDrawer">
          <div
            ref="drawerRef"
            class="drawer"
            :class="{ 'drawer--wide': true }"
            role="dialog"
            aria-modal="true"
            aria-labelledby="agents-drawer-title"
            @click.stop
          >
            <div class="drawer__header">
              <h3 id="agents-drawer-title" class="drawer__title">{{ drawerTitle }}</h3>
              <button class="drawer__close" :aria-label="t('common.close')" @click="requestCloseDrawer">
                <Icon name="x" :size="20" />
              </button>
            </div>
            <div class="drawer__body">
              <div class="ag-drawer__sections">
                <fieldset class="ag-drawer__section">
                  <legend>{{ t('console.agents.identity') }}</legend>
                  <label class="ag-field">
                    <span>{{ t('console.agents.agentId') }}</span>
                    <input v-model="form.id" class="ag-input" type="text" autocomplete="off" disabled />
                  </label>
                  <label class="ag-field">
                    <span>{{ t('console.agents.displayName') }}</span>
                    <input v-model="form.name" class="ag-input" type="text" autocomplete="off" :disabled="drawerMode === 'view'" :placeholder="t('console.agents.defaultsToId')" />
                  </label>
                  <label class="ag-field">
                    <span>{{ t('console.agents.description') }}</span>
                    <input v-model="form.description" class="ag-input" type="text" autocomplete="off" :disabled="drawerMode === 'view'" :placeholder="t('console.agents.descriptionPlaceholder')" />
                  </label>
                </fieldset>

                <details class="ag-drawer__section ag-drawer__section--advanced" :open="advancedOpen">
                  <summary>{{ t('console.agents.capabilitiesAdvanced') }}</summary>
                  <label class="ag-field">
                    <span>{{ t('console.agents.toolsCommaSeparated') }}</span>
                    <input v-model="toolsInput" class="ag-input" type="text" autocomplete="off" :disabled="drawerMode === 'view'" :placeholder="t('console.agents.toolsPlaceholder')" />
                  </label>
                  <label class="ag-field">
                    <span>{{ t('console.agents.workspace') }}</span>
                    <input v-model="form.workspace" class="ag-input" type="text" autocomplete="off" :disabled="drawerMode === 'view'" :placeholder="t('console.agents.workspacePlaceholder')" />
                  </label>
                  <label class="ag-field">
                    <span>{{ t('console.agents.agentDir') }}</span>
                    <input v-model="form.agentDir" class="ag-input" type="text" autocomplete="off" :disabled="drawerMode === 'view'" :placeholder="t('console.agents.optionalPlaceholder')" />
                  </label>
                  <label class="ag-field ag-field--inline">
                    <input v-model="form.enabled" type="checkbox" :disabled="drawerMode === 'view'" />
                    <span>{{ t('console.agents.enabled') }}</span>
                  </label>
                </details>

                <div v-if="drawerModel || systemPromptHint" class="ag-drawer__readonly-meta">
                  <div v-if="drawerModel">
                    <dt>{{ t('console.agents.inheritedModel') }}</dt>
                    <dd class="ag-mono">{{ drawerModel }}</dd>
                  </div>
                  <div v-if="systemPromptHint">
                    <dt>{{ t('console.agents.systemPrompt') }}</dt>
                    <dd class="ag-dim">{{ t('console.agents.systemPromptHint') }}</dd>
                  </div>
                </div>
              </div>
            </div>
            <div class="drawer__footer">
              <template v-if="drawerMode === 'view'">
                <button class="btn btn--ghost" @click="requestCloseDrawer">{{ t('common.close') }}</button>
                <button v-if="drawerIsBuiltin" class="btn btn--primary" @click="customizeFromBuiltin(drawerAgentId)">
                  <Icon name="plus" :size="16" />
                  <span>{{ t('console.agents.customize') }}&hellip;</span>
                </button>
                <button v-else class="btn btn--primary" @click="enterEditMode">{{ t('console.common.edit') }}</button>
              </template>
              <template v-else>
                <button class="btn btn--ghost" @click="onCancelEdit">{{ t('common.cancel') }}</button>
                <button class="btn btn--primary" :disabled="!isDirty || saving" @click="onSave">
                  {{ t('console.agents.saveChanges') }}{{ isDirty ? ' &bull;' : '' }}
                </button>
              </template>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Confirm modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="confirmOpen" class="modal-overlay" @click="cancelConfirm">
          <div
            ref="confirmRef"
            class="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="agents-confirm-title"
            @click.stop
          >
            <h3 id="agents-confirm-title" class="modal__title">{{ confirmTitle }}</h3>
            <div class="modal__body">
              <p>{{ confirmBody }}</p>
            </div>
            <div class="modal__footer">
              <button :class="['btn', confirmPrimaryClass]" @click="onConfirmPrimary">{{ confirmPrimaryLabel }}</button>
              <button ref="confirmCancelBtn" class="btn btn--ghost" @click="cancelConfirm">{{ t('common.cancel') }}</button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { useRpcStore } from '@/stores/rpc'
import Icon from '@/components/Icon.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import { useAgentsData } from '@/composables/agents/useAgentsData'
import { isAgentBuiltin, useAgentDrawer } from '@/composables/agents/useAgentDrawer'
import { useDialogA11y } from '@/composables/useDialogA11y'
import type { Agent } from '@/types/agents'
import { useToasts } from '@/composables/useToasts'

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const { t } = useI18n()
const rpc = useRpcStore()
const { pushToast } = useToasts()
const router = useRouter()

const { agents, loadData, loading, error } = useAgentsData()
const newId = ref('')
const newName = ref('')

const confirmOpen = ref(false)
const confirmTitle = ref('')
const confirmBody = ref('')
const confirmPrimaryLabel = ref(t('console.common.confirm'))
const confirmPrimaryClass = ref('btn--danger')
let confirmResolve: ((value: boolean) => void) | null = null

const drawerRef = ref<HTMLElement | null>(null)
const confirmRef = ref<HTMLElement | null>(null)
const confirmCancelBtn = ref<HTMLElement | null>(null)

const {
  drawerOpen,
  drawerMode,
  drawerAgentId,
  drawerIsBuiltin,
  drawerModel,
  systemPromptHint,
  saving,
  form,
  drawerTitle,
  isDirty,
  toolsInput,
  advancedOpen,
  openDrawer,
  requestCloseDrawer,
  enterEditMode,
  onCancelEdit,
  buildSavePayload,
  applyUpdatedAgent,
} = useAgentDrawer(agents, confirmDiscard)

useDialogA11y(drawerRef, drawerOpen, () => { void requestCloseDrawer() })
useDialogA11y(confirmRef, confirmOpen, cancelConfirm, { initialFocus: confirmCancelBtn })

// ---------------------------------------------------------------------------
// Computed
// ---------------------------------------------------------------------------

const total = computed(() => agents.value.length)
const builtins = computed(() => agents.value.filter(a => a.type === 'builtin' || a.isBuiltin).length)
const customs = computed(() => total.value - builtins.value)
const toolsCount = computed(() =>
  agents.value.reduce((acc, a) => acc + (Array.isArray(a.tools) ? a.tools.length : 0), 0)
)
const models = computed(() => {
  const set = new Set<string>()
  agents.value.forEach(a => { if (a.model) set.add(a.model) })
  return set
})

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

function agentTools(a: Agent): string[] {
  return Array.isArray(a.tools) ? a.tools : []
}

function agentSkills(a: Agent): string[] {
  return Array.isArray(a.skills) ? a.skills : []
}

// The button's hint promises provider/model defaults, so deep-link straight to
// that Settings section rather than the dialog's default landing.
function openSettingsSurface() {
  router.push('/settings/provider')
}

function openChat(id?: string) {
  if (!id) return
  const agentId = String(id || '').trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-').replace(/^-+|-+$/g, '') || 'main'
  const suffix = Math.random().toString(36).slice(2, 10)
  router.push({ path: '/chat', query: { session: `agent:${agentId}:webchat:${suffix}` } })
}

// ---------------------------------------------------------------------------
// Inline create
// ---------------------------------------------------------------------------

async function onInlineAdd() {
  const id = newId.value.trim()
  const name = newName.value.trim()
  if (!id) return
  const payload: Record<string, unknown> = { id }
  if (name) payload.name = name
  try {
    await rpc.call('agents.create', payload)
    pushToast(t('console.agents.toastCreated', { id }), { tone: 'ok' })
    newId.value = ''
    newName.value = ''
    await loadData()
  } catch (err: unknown) {
    const code = rpcErrorCode(err)
    if (code === 'agent.exists') pushToast(t('console.agents.toastExists', { id }), { tone: 'danger' })
    else pushToast(t('console.agents.toastCreateFailed', { msg: errorMessage(err) }), { tone: 'danger' })
  }
}

function customizeFromBuiltin(builtinId?: string) {
  const seedId = (builtinId || 'main') + '-copy'
  newId.value = seedId
  newName.value = (builtinId || 'main') + ' (copy)'
  nextTick(() => {
    const input = document.querySelector('.ag-create__form input[name="id"]') as HTMLInputElement | null
    if (input) {
      input.focus()
      input.select()
    }
    document.querySelector('.ag-create')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
  pushToast(t('console.agents.toastTweakId'))
}

async function onSave() {
  if (saving.value) return
  saving.value = true
  try {
    const payload = buildSavePayload()
    if (Object.keys(payload).length <= 1) {
      pushToast(t('console.agents.toastNothingToSave'))
      saving.value = false
      return
    }
    await rpc.call('agents.update', payload)
    pushToast(t('console.agents.toastUpdated', { id: drawerAgentId.value }), { tone: 'ok' })
    await loadData()
    const updated = agents.value.find(a => a.id === drawerAgentId.value)
    if (updated) {
      applyUpdatedAgent(updated)
    }
  } catch (err: unknown) {
    const code = rpcErrorCode(err)
    const msg = errorMessage(err)
    let friendly = t('console.agents.toastSaveFailed', { msg })
    if (code === 'agent.not_found') friendly = t('console.agents.toastNotFound', { id: drawerAgentId.value })
    if (code === 'agent.builtin_immutable') friendly = t('console.agents.toastBuiltinImmutable', { id: drawerAgentId.value })
    pushToast(friendly, { tone: 'danger' })
  } finally {
    saving.value = false
  }
}

// ---------------------------------------------------------------------------
// Delete
// ---------------------------------------------------------------------------

async function deleteAgent(id?: string) {
  if (!id) return
  const ok = await confirmModal(
    t('console.agents.deleteTitle'),
    t('console.agents.deleteBody', { id }),
    t('console.common.delete'),
    'btn--danger'
  )
  if (!ok) return
  try {
    await rpc.call('agents.delete', { id })
    pushToast(t('console.agents.toastDeleted', { id }), { tone: 'ok' })
    await loadData()
  } catch (err: unknown) {
    pushToast(t('console.agents.toastDeleteFailed', { msg: errorMessage(err) }), { tone: 'danger' })
  }
}

// ---------------------------------------------------------------------------
// Confirm helpers
// ---------------------------------------------------------------------------

function confirmModal(title: string, bodyText: string, primaryLabel = t('console.common.confirm'), primaryCls = 'btn--danger'): Promise<boolean> {
  return new Promise((resolve) => {
    confirmTitle.value = title
    confirmBody.value = bodyText
    confirmPrimaryLabel.value = primaryLabel
    confirmPrimaryClass.value = primaryCls
    confirmOpen.value = true
    confirmResolve = resolve
  })
}

function onConfirmPrimary() {
  confirmOpen.value = false
  if (confirmResolve) {
    confirmResolve(true)
    confirmResolve = null
  }
}

function cancelConfirm() {
  confirmOpen.value = false
  if (confirmResolve) {
    confirmResolve(false)
    confirmResolve = null
  }
}

function confirmDiscard(): Promise<boolean> {
  return confirmModal(
    t('console.agents.discardTitle'),
    t('console.agents.discardBody'),
    t('common.discard'),
    'btn--danger'
  )
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function rpcErrorCode(err: unknown): string {
  if (!err || typeof err !== 'object' || !('code' in err)) return ''
  const code = (err as { code?: unknown }).code
  return typeof code === 'string' ? code : ''
}
</script>

<style scoped>
.stat--hero {
  min-height: 116px;
}

.ag-create {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--sp-4);
}

.ag-create__form {
  align-items: flex-end;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
}

.ag-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 180px;
}

.ag-field span {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  font-weight: 500;
}

.ag-field__optional {
  color: var(--text-dim);
  font-weight: 400;
}

.ag-input {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  font-size: var(--fs-sm);
  padding: 8px 12px;
  width: 100%;
}

.ag-input:focus {
  border-color: var(--accent);
  outline: none;
}

.ag-input:disabled {
  opacity: 0.6;
}

.ag-create__hint {
  color: var(--text-dim);
  font-size: var(--fs-sm);
  margin: var(--sp-3) 0 0;
}

.ag-list__head {
  align-items: center;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
}

.ag-list__title {
  font-size: var(--fs-md);
  letter-spacing: 0;
  margin: 0;
}

.ag-list__count {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-variant-numeric: tabular-nums;
  margin-left: 6px;
  padding: 2px 8px;
}

.ag-card {
  outline: none;
}

.ag-card.is-builtin {
  border-left: 3px solid var(--ok);
}

.ag-card__head {
  align-items: flex-start;
  display: flex;
  gap: var(--sp-2);
  justify-content: space-between;
}

.ag-card__id-block {
  align-items: center;
  display: flex;
  gap: 8px;
  min-width: 0;
}

.ag-card__id {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ag-card__id-btn {
  background: transparent;
  border: 0;
  padding: 0;
  margin: 0;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.ag-card__id-btn:focus-visible {
  border-radius: var(--radius-sm);
  outline: none;
  box-shadow: var(--focus-ring);
}

.ag-card__actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

.ag-iconbtn {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  gap: 4px;
  padding: 4px 8px;
  font-size: 12px;
}

.ag-iconbtn:hover {
  background: var(--bg-elevated);
  border-color: var(--border);
  color: var(--text);
}

.ag-iconbtn--danger:hover {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  border-color: color-mix(in srgb, var(--danger) 40%, var(--border));
  color: var(--danger);
}

.ag-card__name {
  font-size: var(--fs-md);
  font-weight: 600;
}

.ag-card__desc {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin: 0;
}

.ag-card__meta {
  display: grid;
  gap: var(--sp-2);
  margin: 0;
}

.ag-card__meta > div {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
  justify-content: space-between;
}

.ag-card__meta dt {
  color: var(--text-dim);
  font-size: var(--fs-sm);
}

.ag-card__meta dd {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  margin: 0;
}

.ag-mono {
  font-family: var(--font-mono);
}

.ag-card__chips {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.ag-chips-label {
  color: var(--text-dim);
  font-size: 11px;
  font-weight: 600;
  margin-right: 4px;
  text-transform: uppercase;
}

.ag-chip {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 2px 8px;
}

.ag-chip--dim {
  opacity: 0.6;
}

.chip {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  display: inline-flex;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  padding: 3px 8px;
  text-transform: uppercase;
}

.chip-ok {
  background: color-mix(in srgb, var(--ok) 12%, transparent);
  border-color: color-mix(in srgb, var(--ok) 40%, var(--border));
  color: var(--ok);
}

.chip-info {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
  color: var(--accent);
}

.state {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  color: var(--text);
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  padding: var(--sp-8) var(--sp-4);
  text-align: center;
}

/* Drawer */
.drawer-overlay {
  align-items: flex-end;
  background: var(--scrim);
  bottom: 0;
  display: flex;
  justify-content: flex-end;
  left: 0;
  position: fixed;
  right: 0;
  top: 0;
  z-index: 1000;
}

.drawer {
  background: var(--bg-surface);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100%;
  max-width: 520px;
  width: 100%;
}

.drawer--wide {
  max-width: 520px;
}

.drawer__header {
  align-items: center;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: var(--sp-4);
}

.drawer__title {
  font-size: var(--fs-md);
  font-weight: 600;
  margin: 0;
}

.drawer__close {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  height: 32px;
  justify-content: center;
  width: 32px;
}

.drawer__close:hover {
  background: var(--bg-elevated);
  border-color: var(--border);
  color: var(--text);
}

.drawer__body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-4);
}

.drawer__footer {
  align-items: center;
  border-top: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: flex-end;
  padding: var(--sp-4);
}

.ag-drawer__sections {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}

.ag-drawer__section {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin: 0;
  padding: var(--sp-4);
}

.ag-drawer__section legend,
.ag-drawer__section summary {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  font-weight: 600;
}

.ag-drawer__section--advanced summary {
  cursor: pointer;
  user-select: none;
}

.ag-field--inline {
  align-items: center;
  flex-direction: row;
  gap: 8px;
}

.ag-drawer__readonly-meta {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  display: grid;
  gap: var(--sp-2);
  padding: var(--sp-4);
}

.ag-drawer__readonly-meta > div {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
  justify-content: space-between;
}

.ag-drawer__readonly-meta dt {
  color: var(--text-dim);
  font-size: var(--fs-sm);
}

.ag-drawer__readonly-meta dd {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  margin: 0;
}

.ag-dim {
  color: var(--text-dim);
  font-size: var(--fs-sm);
}

/* Modal */
.modal-overlay {
  align-items: center;
  background: var(--scrim);
  bottom: 0;
  display: flex;
  justify-content: center;
  left: 0;
  position: fixed;
  right: 0;
  top: 0;
  z-index: 1100;
}

.modal {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  max-width: 420px;
  padding: var(--sp-5);
  width: 90%;
}

.modal__title {
  font-size: var(--fs-md);
  font-weight: 600;
  margin: 0 0 var(--sp-3);
}

.modal__body {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin-bottom: var(--sp-4);
}

.modal__footer {
  display: flex;
  gap: var(--sp-3);
  justify-content: flex-end;
}

/* Transitions */
.drawer-enter-active,
.drawer-leave-active {
  transition: opacity var(--dur-base);
}

.drawer-enter-from,
.drawer-leave-to {
  opacity: 0;
}

.modal-enter-active,
.modal-leave-active {
  transition: opacity var(--dur-base);
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}

@media (max-width: 980px) {
  .stat-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .ag-stage__header {
    align-items: stretch;
    flex-direction: column;
  }

  .ag-stage__header .btn {
    align-self: flex-start;
    width: auto;
  }

  .ag-cards {
    grid-template-columns: 1fr;
  }

  .drawer {
    max-width: 100%;
  }
}

@media (max-width: 480px) {
  .stat-row {
    grid-template-columns: 1fr;
  }
}
</style>
