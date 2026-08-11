<template>
  <div
    class="goal-ribbon"
    :data-status="goal.status"
    role="status"
    aria-live="polite"
  >
    <div
      class="goal-ribbon__main"
      :class="{
        'goal-ribbon__main--expanded': objectiveExpanded,
        'goal-ribbon__main--editing': editing,
      }"
    >
      <span class="goal-ribbon__icon" aria-hidden="true">
        <Icon name="target" :size="15" />
      </span>
      <div class="goal-ribbon__content">
        <div v-if="!editing" class="goal-ribbon__summary">
          <span class="goal-ribbon__title">{{ titleText }}</span>
          <button
            type="button"
            class="goal-ribbon__text"
            :class="{ 'goal-ribbon__text--expanded': objectiveExpanded }"
            :aria-expanded="objectiveExpanded"
            :title="objectiveExpanded
              ? t('chat.goal.collapseObjective')
              : t('chat.goal.expandObjective')"
            @click="objectiveExpanded = !objectiveExpanded"
          >
            <span class="goal-ribbon__text-copy">{{ goal.objective }}</span>
            <Icon
              class="goal-ribbon__text-toggle"
              name="chevronDown"
              :size="13"
              aria-hidden="true"
            />
          </button>
        </div>
        <form v-else class="goal-ribbon__edit" @submit.prevent="submitEdit">
          <label class="goal-ribbon__sr-only" :for="editInputId">
            {{ t('chat.goal.editObjective') }}
          </label>
          <textarea
            :id="editInputId"
            ref="editInput"
            v-model="editText"
            class="goal-ribbon__edit-input"
            rows="3"
            :disabled="busy || editSubmitting"
            @input="resizeEditInput"
            @keydown.esc.prevent="cancelEdit"
            @keydown.meta.enter.prevent="submitEdit"
            @keydown.ctrl.enter.prevent="submitEdit"
          />
          <button
            type="submit"
            class="goal-ribbon__edit-button"
            :disabled="busy || editSubmitting || !editText.trim()"
          >
            {{ t('chat.goal.saveEdit') }}
          </button>
          <button
            type="button"
            class="goal-ribbon__edit-button"
            :disabled="busy || editSubmitting"
            @click="cancelEdit"
          >
            {{ t('chat.goal.cancelEdit') }}
          </button>
        </form>
        <span
          v-if="metaText && !editing"
          class="goal-ribbon__meta"
          :class="{ 'goal-ribbon__meta--visible': showMetaInline }"
        >
          {{ metaText }}
        </span>
      </div>
      <span v-if="!editing" ref="actionsRef" class="goal-ribbon__actions">
        <span
          v-if="goal.status === 'complete'"
          class="goal-ribbon__finalizing"
          role="status"
        >
          <Icon name="check" :size="13" aria-hidden="true" />
          {{ t('chat.goal.finalizing') }}
        </span>
        <button
          v-else
          type="button"
          class="goal-ribbon__primary"
          :disabled="busy"
          :data-action="lifecycleAction"
          data-testid="goal-lifecycle-action"
          @click="invokeLifecycleAction"
        >
          <Icon :name="lifecycleIcon" :size="13" aria-hidden="true" />
          {{ lifecycleLabel }}
        </button>
        <button
          v-if="goal.status !== 'complete'"
          ref="menuTrigger"
          type="button"
          class="goal-ribbon__menu-trigger"
          :class="{ 'is-open': menuOpen }"
          :title="t('chat.goal.actions')"
          :aria-label="t('chat.goal.actions')"
          aria-haspopup="menu"
          :aria-expanded="menuOpen"
          :disabled="busy"
          @click.stop="toggleMenu"
          @keydown.down.prevent="openMenu('first')"
          @keydown.up.prevent="openMenu('last')"
        >
          <Icon name="moreHorizontal" :size="17" aria-hidden="true" />
        </button>
        <div
          v-if="menuOpen"
          ref="menu"
          class="goal-ribbon__menu"
          role="menu"
          :aria-label="t('chat.goal.actions')"
          @keydown="onMenuKeydown"
        >
          <button
            type="button"
            class="goal-ribbon__menu-item"
            role="menuitem"
            @click="beginEdit"
          >
            <Icon name="edit" :size="15" aria-hidden="true" />
            <span>{{ t('chat.goal.edit') }}</span>
          </button>
          <div class="goal-ribbon__menu-divider" role="separator" />
          <button
            type="button"
            class="goal-ribbon__menu-item goal-ribbon__menu-item--danger"
            role="menuitem"
            @click="requestClear"
          >
            <Icon name="trash" :size="15" aria-hidden="true" />
            <span>{{ t('chat.goal.remove') }}</span>
          </button>
        </div>
      </span>
    </div>

    <details v-if="goal.progress && goal.progress.steps.length" class="goal-ribbon__progress">
      <summary>{{ progressSummary }}</summary>
      <p v-if="goal.progress.explanation" class="goal-ribbon__explanation">
        {{ goal.progress.explanation }}
      </p>
      <ol class="goal-ribbon__steps">
        <li
          v-for="(step, index) in goal.progress.steps"
          :key="`${index}:${step.text}`"
          :data-status="step.status"
        >
          <span class="goal-ribbon__step-marker" aria-hidden="true">
            {{ step.status === 'completed' ? '✓' : step.status === 'in_progress' ? '●' : '○' }}
          </span>
          <span>{{ step.text }}</span>
        </li>
      </ol>
    </details>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { GoalSnapshot } from '@/composables/chat/useChatGoals'
import { useDocumentEvent } from '@/composables/useDocumentEvent'

const props = defineProps<{
  goal: GoalSnapshot
  elapsed: string
  busy?: boolean
  planModeActive?: boolean
  connectionTakeoverAvailable?: boolean
  reattaching?: boolean
}>()

const emit = defineEmits<{
  edit: [objective: string, settle: (accepted: boolean) => void]
  pause: []
  resume: []
  takeover: []
  clear: []
}>()

const { t } = useI18n()
const editing = ref(false)
const editSubmitting = ref(false)
const objectiveExpanded = ref(false)
const editText = ref(props.goal.objective)
const editInput = ref<HTMLTextAreaElement | null>(null)
const actionsRef = ref<HTMLElement | null>(null)
const menuTrigger = ref<HTMLButtonElement | null>(null)
const menu = ref<HTMLElement | null>(null)
const menuOpen = ref(false)
const editInputId = `goal-edit-${props.goal.goalId}`
const EDIT_INPUT_MIN_HEIGHT = 64
const EDIT_INPUT_MAX_HEIGHT = 180

type LifecycleAction = 'pause' | 'resume' | 'takeover'

const goalHasUnsettledTask = computed(() => (
  props.goal.activeTaskId !== null || props.goal.executionState !== 'idle'
))

const lifecycleAction = computed<LifecycleAction>(() => {
  if (props.connectionTakeoverAvailable) return 'takeover'
  return props.goal.status === 'active' ? 'pause' : 'resume'
})

const lifecycleLabel = computed(() => {
  if (lifecycleAction.value === 'takeover') return t('chat.goal.continue')
  if (props.goal.status === 'active') {
    return goalHasUnsettledTask.value
      ? t('chat.goal.pauseAfterTurn')
      : t('chat.goal.pause')
  }
  if (props.goal.status === 'paused') {
    return goalHasUnsettledTask.value
      ? t('chat.goal.resumeAutomatic')
      : t('chat.goal.resume')
  }
  if (props.goal.status === 'blocked') return t('chat.goal.continueBlocked')
  if (props.goal.status === 'usage_limited') return t('chat.goal.retry')
  return t('chat.goal.resume')
})

const lifecycleIcon = computed(() => (
  lifecycleAction.value === 'pause' ? 'pause' : 'play'
))

watch(() => props.goal.objective, objective => {
  if (!editing.value) editText.value = objective
})

const titleText = computed(() => {
  if (
    props.goal.status === 'active'
    && props.goal.continuationDeferredReason === 'owner_disconnected'
  ) return t('chat.goal.detachedTitle')
  switch (props.goal.status) {
    case 'active': return t('chat.goal.activeTitle')
    case 'paused': return t('chat.goal.pausedTitle')
    case 'blocked': return t('chat.goal.blockedTitle')
    case 'usage_limited': return t('chat.goal.usageLimitedTitle')
    case 'complete': return t('chat.goal.completeTitle')
    default: return t('chat.goal.activeTitle')
  }
})

const pauseReasonText = computed(() => {
  switch (props.goal.pauseReason) {
    case 'user':
    case 'user_paused': return t('chat.goal.pausedByUser')
    case 'turn_limit': return t('chat.goal.turnLimitReached')
    case 'runtime_limit': return t('chat.goal.runtimeLimitReached')
    case 'process_restart': return t('chat.goal.pausedAfterRestart')
    case 'lease_revoked': return t('chat.goal.leaseRevoked')
    case 'feature_disabled': return t('chat.goal.featureDisabled')
    case 'activation_failed':
    case 'persistence_error': return t('chat.goal.executionError')
    case 'goal_checkpoint_required': return t('chat.goal.checkpointRequired')
    case 'user_cancelled': return t('chat.goal.userCancelled')
    default: return ''
  }
})

const metaText = computed(() => {
  const parts: string[] = []
  // Keep the actionable lifecycle cause ahead of accounting metadata so it
  // remains visible when a long objective forces this one-line ribbon to elide.
  if (props.goal.status === 'paused' && pauseReasonText.value) {
    parts.push(pauseReasonText.value)
  } else if (props.goal.status === 'blocked' && props.goal.blockedReason) {
    parts.push(props.goal.blockedReason)
  } else if (props.goal.status === 'usage_limited') {
    parts.push(t('chat.goal.usageLimitReached'))
  }
  if (props.goal.continuationDeferredReason === 'owner_disconnected') {
    parts.push(props.reattaching
      ? t('chat.goal.reconnecting')
      : t('chat.goal.waitingForConnection'))
  } else if (props.planModeActive || props.goal.continuationDeferredReason === 'plan_mode') {
    parts.push(t('chat.goal.waitingForPlan'))
  } else if (props.goal.executionState === 'queued') {
    parts.push(t('chat.goal.queued'))
  } else if (props.goal.executionState === 'working') {
    parts.push(t('chat.goal.working'))
  }
  if (props.elapsed) parts.push(t('chat.goal.activeTime', { duration: props.elapsed }))
  if (props.goal.turnsSettled > 0) {
    parts.push(t('chat.goal.turns', { turns: props.goal.turnsSettled }))
  }
  if (props.goal.usage.totalTokens > 0) {
    parts.push(t('chat.goal.tokens', {
      tokens: props.goal.usage.totalTokens.toLocaleString(),
    }))
  }
  return parts.join(' · ')
})

const progressSummary = computed(() => {
  const steps = props.goal.progress?.steps ?? []
  const completed = steps.filter(step => step.status === 'completed').length
  return t('chat.goal.progressSummary', { completed, total: steps.length })
})

const showMetaInline = computed(() => (
  objectiveExpanded.value
  || props.goal.status !== 'active'
  || props.goal.continuationDeferredReason !== null
  || props.planModeActive === true
))

function beginEdit() {
  closeMenu()
  editText.value = props.goal.objective
  editing.value = true
  void nextTick(() => {
    resizeEditInput()
    editInput.value?.focus()
    editInput.value?.select()
  })
}

function resizeEditInput() {
  const input = editInput.value
  if (!input) return
  input.style.height = 'auto'
  const contentHeight = Math.max(input.scrollHeight, EDIT_INPUT_MIN_HEIGHT)
  const height = Math.min(contentHeight, EDIT_INPUT_MAX_HEIGHT)
  input.style.height = `${height}px`
  input.style.overflowY = contentHeight > EDIT_INPUT_MAX_HEIGHT ? 'auto' : 'hidden'
}

function cancelEdit() {
  if (editSubmitting.value) return
  editing.value = false
  editText.value = props.goal.objective
  void nextTick(() => menuTrigger.value?.focus())
}

function submitEdit() {
  if (editSubmitting.value) return
  const objective = editText.value.trim()
  if (!objective) return
  editSubmitting.value = true
  emit('edit', objective, accepted => {
    editSubmitting.value = false
    if (accepted) {
      editing.value = false
      void nextTick(() => menuTrigger.value?.focus())
      return
    }
    void nextTick(() => editInput.value?.focus())
  })
}

function invokeLifecycleAction() {
  if (lifecycleAction.value === 'pause') emit('pause')
  else if (lifecycleAction.value === 'takeover') emit('takeover')
  else emit('resume')
}

function menuItems(): HTMLButtonElement[] {
  return Array.from(menu.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
}

function focusMenuItem(position: 'first' | 'last') {
  const items = menuItems()
  const target = position === 'last' ? items[items.length - 1] : items[0]
  target?.focus()
}

function openMenu(position: 'first' | 'last' = 'first') {
  if (props.busy || props.goal.status === 'complete') return
  menuOpen.value = true
  void nextTick(() => focusMenuItem(position))
}

function closeMenu(restoreFocus = false) {
  menuOpen.value = false
  if (restoreFocus) void nextTick(() => menuTrigger.value?.focus())
}

function toggleMenu() {
  if (menuOpen.value) closeMenu(true)
  else openMenu()
}

function requestClear() {
  menuOpen.value = false
  menuTrigger.value?.focus()
  emit('clear')
}

function onMenuKeydown(event: KeyboardEvent) {
  const items = menuItems()
  if (!items.length) return
  const current = items.indexOf(document.activeElement as HTMLButtonElement)
  if (event.key === 'Escape') {
    event.preventDefault()
    closeMenu(true)
    return
  }
  if (event.key === 'Tab') {
    menuOpen.value = false
    return
  }
  let nextIndex: number | null = null
  if (event.key === 'ArrowDown') nextIndex = current < 0 ? 0 : (current + 1) % items.length
  if (event.key === 'ArrowUp') {
    nextIndex = current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length
  }
  if (event.key === 'Home') nextIndex = 0
  if (event.key === 'End') nextIndex = items.length - 1
  if (nextIndex === null) return
  event.preventDefault()
  items[nextIndex]?.focus()
}

useDocumentEvent('pointerdown', event => {
  if (!menuOpen.value) return
  if (event.target instanceof Node && !actionsRef.value?.contains(event.target)) {
    menuOpen.value = false
  }
})

watch(() => [props.goal.goalId, props.goal.stateRevision, props.busy], () => {
  menuOpen.value = false
})
</script>

<style scoped>
.goal-ribbon {
  position: relative;
  width: 100%;
  max-width: 100%;
  padding: 7px 8px 7px 9px;
  border: 1px solid color-mix(in srgb, var(--border) 88%, transparent);
  border-inline-start: 2px solid color-mix(in srgb, var(--accent) 72%, transparent);
  border-radius: var(--radius-card);
  background: color-mix(in srgb, var(--bg-elevated, var(--card)) 96%, transparent);
  box-shadow: 0 8px 24px color-mix(in srgb, var(--bg) 34%, transparent);
  font-size: 0.8125rem;
  line-height: 1.4;
}
.goal-ribbon__main {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}
.goal-ribbon__main--expanded,
.goal-ribbon__main--editing {
  align-items: flex-start;
}
.goal-ribbon__main--expanded > .goal-ribbon__icon,
.goal-ribbon__main--editing > .goal-ribbon__icon {
  padding-top: 3px;
}
.goal-ribbon[data-status='paused'],
.goal-ribbon[data-status='usage_limited'] {
  border-inline-start-color: color-mix(in srgb, var(--warn) 72%, transparent);
}
.goal-ribbon[data-status='blocked'] {
  border-inline-start-color: color-mix(in srgb, var(--danger) 72%, transparent);
}
.goal-ribbon[data-status='complete'] {
  border-inline-start-color: color-mix(in srgb, var(--ok) 72%, transparent);
}
.goal-ribbon__icon {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--accent) 9%, transparent);
  color: var(--accent);
}
.goal-ribbon__content {
  flex: 1 1 auto;
  min-width: 0;
}
.goal-ribbon__summary {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.goal-ribbon__title {
  flex: 0 0 auto;
  font-weight: 600;
  letter-spacing: -0.01em;
  white-space: nowrap;
}
.goal-ribbon__text {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1 1 auto;
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-muted, var(--muted));
  cursor: pointer;
  font: inherit;
  text-align: left;
}
.goal-ribbon__text-copy {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.goal-ribbon__text--expanded {
  align-items: flex-start;
}
.goal-ribbon__text--expanded .goal-ribbon__text-copy {
  overflow: visible;
  overflow-wrap: anywhere;
  text-overflow: clip;
  white-space: pre-wrap;
}
.goal-ribbon__text-toggle {
  flex: 0 0 auto;
  margin-top: 2px;
  transition: transform var(--dur-fast) var(--ease-standard);
}
.goal-ribbon__text--expanded .goal-ribbon__text-toggle {
  transform: rotate(180deg);
}
.goal-ribbon__text:hover,
.goal-ribbon__text:focus-visible {
  color: var(--text);
}
.goal-ribbon__text:focus-visible {
  outline: 0;
  border-radius: var(--radius-sm);
  box-shadow: var(--focus-ring);
}
.goal-ribbon__meta {
  display: none;
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 0.72rem;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted, var(--muted));
  white-space: nowrap;
}
.goal-ribbon__meta--visible {
  display: block;
}
.goal-ribbon__actions {
  position: relative;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  margin-left: 2px;
  padding-left: 8px;
  border-left: 1px solid color-mix(in srgb, var(--border) 82%, transparent);
}
.goal-ribbon__primary,
.goal-ribbon__finalizing {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  min-height: 32px;
  padding: 0 9px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted, var(--muted));
  font: inherit;
  font-weight: 500;
  white-space: nowrap;
}
.goal-ribbon__primary {
  cursor: pointer;
}
.goal-ribbon__primary:hover:not(:disabled),
.goal-ribbon__primary:focus-visible {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  color: var(--text);
}
.goal-ribbon__primary:disabled {
  opacity: 0.5;
  cursor: default;
}
.goal-ribbon__finalizing {
  color: color-mix(in srgb, var(--ok) 72%, var(--text));
}
.goal-ribbon__menu-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 32px;
  min-height: 32px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted, var(--muted));
  cursor: pointer;
}
.goal-ribbon__menu-trigger:hover:not(:disabled),
.goal-ribbon__menu-trigger:focus-visible,
.goal-ribbon__menu-trigger.is-open {
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--text);
}
.goal-ribbon__menu-trigger:disabled,
.goal-ribbon__edit-button:disabled {
  opacity: 0.5;
  cursor: default;
}
.goal-ribbon__menu {
  position: absolute;
  right: 0;
  bottom: calc(100% + 6px);
  z-index: 12;
  min-width: 180px;
  padding: 5px;
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  background: var(--bg-elevated, var(--card));
  box-shadow: var(--shadow-lg, var(--shadow-md));
}
.goal-ribbon__menu-item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  min-height: 44px;
  padding: 0 10px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.goal-ribbon__menu-item:hover,
.goal-ribbon__menu-item:focus-visible {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
.goal-ribbon__menu-item--danger {
  color: var(--danger);
}
.goal-ribbon__menu-item--danger:hover,
.goal-ribbon__menu-item--danger:focus-visible {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
}
.goal-ribbon__menu-divider {
  height: 1px;
  margin: 4px 6px;
  background: var(--border);
}
.goal-ribbon__edit {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1 1 auto;
  min-width: 0;
}
.goal-ribbon__edit-input {
  flex: 1 1 auto;
  min-width: 120px;
  min-height: 64px;
  max-height: 180px;
  padding: 4px 7px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg, var(--card));
  color: var(--text);
  font: inherit;
  line-height: 1.4;
  overflow-y: hidden;
  resize: none;
}
.goal-ribbon__edit-button {
  padding: 4px 7px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  cursor: pointer;
}
.goal-ribbon__progress {
  margin: 6px 0 0 23px;
  color: var(--text-muted, var(--muted));
}
.goal-ribbon__progress summary {
  width: max-content;
  cursor: pointer;
  font-weight: 500;
}
.goal-ribbon__explanation {
  margin: 6px 0 4px;
}
.goal-ribbon__steps {
  display: grid;
  gap: 3px;
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
}
.goal-ribbon__steps li {
  display: flex;
  gap: 6px;
}
.goal-ribbon__steps li[data-status='completed'] {
  color: var(--text-muted, var(--muted));
  text-decoration: line-through;
}
.goal-ribbon__steps li[data-status='in_progress'] .goal-ribbon__step-marker {
  color: var(--accent);
}
.goal-ribbon__step-marker {
  flex: 0 0 1em;
  text-align: center;
}
.goal-ribbon__sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
@media (max-width: 720px) {
  .goal-ribbon__main {
    gap: 6px;
  }
  .goal-ribbon__summary {
    gap: 6px;
  }
  .goal-ribbon__text {
    flex-basis: 64px;
  }
  .goal-ribbon__primary,
  .goal-ribbon__finalizing {
    min-height: 44px;
    padding-inline: 9px;
  }
  .goal-ribbon__menu-trigger {
    min-width: 44px;
    min-height: 44px;
  }
  .goal-ribbon__title {
    display: none;
  }
}
</style>
