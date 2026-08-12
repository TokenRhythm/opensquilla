<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import DesktopUpdateIndicator from '@/components/DesktopUpdateIndicator.vue'
import Icon from '@/components/Icon.vue'
import { useDesktopUpdate } from '@/composables/useDesktopUpdate'
import { useDesktopUpdatePresentation } from '@/composables/useDesktopUpdatePresentation'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useChatTopbarPopoverCoordination } from '@/composables/useChatTopbarPopoverCoordinator'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import type { ConnectionState } from '@/lib/rpc'
import {
  highestSystemSeverity,
  type SystemHeaderLayout,
  type SystemSeverity,
} from '@/utils/headerLayout'

type SystemAction = 'connection' | 'approval' | 'update'

const props = defineProps<{
  layout: SystemHeaderLayout
  connectionState: ConnectionState
  connectionLabel: string
  approvalCount: number
  canManageConnection: boolean
}>()

const emit = defineEmits<{
  'open-connection': []
  'open-approval': []
  'open-update': []
}>()

const { t } = useI18n()
const update = useDesktopUpdate()
const {
  summary: updateSummary,
  title: updateTitle,
  iconName: updateIconName,
  severity: updateSeverity,
} = useDesktopUpdatePresentation(update)

const rootRef = ref<HTMLDivElement | null>(null)
const connectionRef = ref<HTMLElement | null>(null)
const approvalRef = ref<HTMLButtonElement | null>(null)
const updateWrapRef = ref<HTMLDivElement | null>(null)
const triggerRef = ref<HTMLButtonElement | null>(null)
const menuRef = ref<HTMLDivElement | null>(null)
const menuOpen = ref(false)
useChatTopbarPopoverCoordination('system-status', menuOpen)
const menuIsTopmost = useDialogLayer(computed(() => menuOpen.value))

onMounted(update.init)

const updateVisible = computed(() => update.visible.value)
const normalizedApprovalCount = computed(() => (
  Number.isFinite(props.approvalCount)
    ? Math.max(0, Math.floor(props.approvalCount))
    : 0
))
const shortApprovalCount = computed(() => (
  normalizedApprovalCount.value > 99 ? '99+' : String(normalizedApprovalCount.value)
))
const noticeCount = computed(() => (
  (normalizedApprovalCount.value > 0 ? 1 : 0)
  + (updateVisible.value ? 1 : 0)
))
const showMenuTrigger = computed(() => (
  props.layout === 'tight'
  || (props.layout === 'compact' && noticeCount.value > 0)
))
const menuActions = computed<SystemAction[]>(() => {
  const actions: SystemAction[] = []
  if (props.layout === 'tight') actions.push('connection')
  if (normalizedApprovalCount.value > 0) actions.push('approval')
  if (updateVisible.value) actions.push('update')
  return actions
})

const connectionSeverity = computed<SystemSeverity>(() => {
  if (props.connectionState === 'disconnected') return 'danger'
  if (props.connectionState === 'connecting') return 'warning'
  return 'normal'
})
const severity = computed(() => highestSystemSeverity([
  connectionSeverity.value,
  normalizedApprovalCount.value > 0 ? 'danger' : 'normal',
  updateVisible.value ? updateSeverity.value : 'normal',
]))
const systemSummaryLabel = computed(() => t('chrome.systemStatusSummary', {
  state: props.connectionLabel,
  count: noticeCount.value,
}))
const approvalLabel = computed(() => t('chrome.approvalRequiredCount', {
  count: normalizedApprovalCount.value,
}))
const connectionTitle = computed(() => t('chrome.connectionTitle', {
  state: props.connectionLabel,
}))

function menuItems(): HTMLButtonElement[] {
  return Array.from(
    menuRef.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [],
  )
}

function focusMenuItem(position: 'first' | 'last') {
  const items = menuItems()
  const target = position === 'last' ? items[items.length - 1] : items[0]
  target?.focus()
}

function openMenu(position: 'first' | 'last' = 'first') {
  menuOpen.value = true
  void nextTick(() => focusMenuItem(position))
}

function closeMenu(restoreFocus = false) {
  // Restore focus before a routed action can unmount the stable trigger.
  if (restoreFocus) triggerRef.value?.focus()
  menuOpen.value = false
}

function toggleMenu() {
  if (menuOpen.value) closeMenu(true)
  else openMenu()
}

function actionForElement(element: Element | null): SystemAction | null {
  if (!(element instanceof HTMLElement)) return null
  const owner = element.closest<HTMLElement>('[data-system-action]')
  const action = owner?.dataset.systemAction
  return action === 'connection' || action === 'approval' || action === 'update'
    ? action
    : null
}

function focusAction(action: SystemAction | null): boolean {
  if (action === 'connection' && connectionRef.value) {
    connectionRef.value.focus()
    return true
  }
  if (action === 'approval' && approvalRef.value) {
    approvalRef.value.focus()
    return true
  }
  if (action === 'update') {
    const updateTrigger = updateWrapRef.value?.querySelector<HTMLButtonElement>(
      '[data-testid="desktop-update-indicator"]',
    )
    if (updateTrigger) {
      updateTrigger.focus()
      return true
    }
  }
  if (triggerRef.value) {
    triggerRef.value.focus()
    return true
  }
  if (connectionRef.value) {
    connectionRef.value.focus()
    return true
  }
  approvalRef.value?.focus()
  return Boolean(approvalRef.value)
}

function invoke(action: SystemAction) {
  if (action === 'connection' && !props.canManageConnection) return
  closeMenu(true)
  if (action === 'connection') emit('open-connection')
  if (action === 'approval') emit('open-approval')
  if (action === 'update') emit('open-update')
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
  if (nextIndex == null) return
  event.preventDefault()
  items[nextIndex]?.focus()
}

useDocumentEvent('click', event => {
  if (!menuOpen.value) return
  if (event.target instanceof Node && !rootRef.value?.contains(event.target)) {
    menuOpen.value = false
  }
})

useDocumentEvent('keydown', event => {
  if (event.defaultPrevented || event.key !== 'Escape') return
  if (!menuOpen.value || !menuIsTopmost.value) return
  event.preventDefault()
  closeMenu(true)
})

watch(() => props.layout, () => {
  const active = document.activeElement
  const restoreFocus = Boolean(active instanceof Node && rootRef.value?.contains(active))
  const action = actionForElement(active)
  menuOpen.value = false
  if (restoreFocus) void nextTick(() => focusAction(action))
})

watch(showMenuTrigger, (show, wasShown) => {
  if (show || !wasShown || props.layout !== 'compact') return
  const active = document.activeElement
  if (active === triggerRef.value || (active instanceof Node && menuRef.value?.contains(active))) {
    menuOpen.value = false
    void nextTick(() => connectionRef.value?.focus())
  }
})

watch(menuActions, actions => {
  if (!menuOpen.value) return
  const activeAction = actionForElement(document.activeElement)
  if (!activeAction || actions.includes(activeAction)) return
  void nextTick(() => {
    const first = menuItems()[0]
    if (first) first.focus()
    else closeMenu(true)
  })
})
</script>

<template>
  <div
    ref="rootRef"
    class="chat-system-status"
    :data-layout="layout"
    :data-severity="severity"
    data-testid="chat-system-status"
  >
    <button
      v-if="layout === 'wide' && normalizedApprovalCount > 0"
      ref="approvalRef"
      type="button"
      class="approval-inline chat-system-status__approval topbar-state topbar-state--approval"
      data-state="danger"
      data-system-action="approval"
      data-testid="chat-system-approval"
      :title="t('chrome.openBlockedSession')"
      @click="emit('open-approval')"
    >
      {{ approvalLabel }}
    </button>

    <button
      v-if="layout !== 'tight' && canManageConnection"
      ref="connectionRef"
      type="button"
      class="conn-pill conn-pill--link chat-system-status__connection topbar-state topbar-state--connection"
      :class="connectionState"
      :data-state="connectionSeverity"
      data-system-action="connection"
      data-testid="connection-status"
      :title="connectionTitle"
      :aria-label="connectionTitle"
      @click="emit('open-connection')"
    >
      <span class="chat-system-status__state-dot" aria-hidden="true"></span>
      <span class="chat-system-status__connection-label">{{ connectionLabel }}</span>
    </button>
    <span
      v-else-if="layout !== 'tight'"
      ref="connectionRef"
      class="conn-pill chat-system-status__connection topbar-state topbar-state--connection"
      :class="connectionState"
      :data-state="connectionSeverity"
      data-testid="connection-status"
      tabindex="-1"
    >
      <span class="chat-system-status__state-dot" aria-hidden="true"></span>
      <span class="chat-system-status__connection-label">{{ connectionLabel }}</span>
    </span>

    <div
      v-if="layout === 'wide' && updateVisible"
      ref="updateWrapRef"
      class="chat-system-status__update-wide"
      data-system-action="update"
    >
      <DesktopUpdateIndicator />
    </div>

    <button
      v-if="showMenuTrigger"
      ref="triggerRef"
      type="button"
      class="chat-system-status__trigger topbar-state topbar-state--system"
      :class="[
        { 'conn-pill': layout === 'tight' },
        layout === 'tight' ? connectionState : `is-${severity}`,
      ]"
      :data-state="severity"
      data-testid="chat-system-status-trigger"
      :title="t('chrome.systemStatus')"
      :aria-label="systemSummaryLabel"
      aria-haspopup="menu"
      :aria-expanded="menuOpen"
      aria-controls="chat-system-status-menu"
      @click.stop="toggleMenu"
      @keydown.down.prevent="openMenu('first')"
      @keydown.up.prevent="openMenu('last')"
    >
      <Icon name="gauge" :size="16" aria-hidden="true" />
      <span
        v-if="layout === 'tight'"
        class="chat-system-status__state-dot"
        aria-hidden="true"
      ></span>
      <span class="chat-system-status__sr">{{ connectionLabel }}</span>
      <span
        v-if="normalizedApprovalCount > 0"
        class="chat-system-status__badge"
        data-testid="chat-system-status-badge"
        aria-hidden="true"
      >{{ shortApprovalCount }}</span>
      <span
        v-else-if="updateVisible"
        class="chat-system-status__update-dot"
        aria-hidden="true"
      ></span>
    </button>

    <div
      v-if="menuOpen"
      id="chat-system-status-menu"
      ref="menuRef"
      class="chat-system-status__menu"
      role="menu"
      :aria-label="t('chrome.systemStatus')"
      data-testid="chat-system-status-menu"
      data-chat-topbar-popover="system-status"
      @keydown="onMenuKeydown"
    >
      <button
        v-if="menuActions.includes('connection')"
        type="button"
        class="chat-system-status__menu-item"
        role="menuitem"
        data-system-action="connection"
        data-testid="chat-system-connection"
        :aria-disabled="!canManageConnection"
        :title="connectionTitle"
        @click="invoke('connection')"
      >
        <span
          class="chat-system-status__menu-icon conn-pill topbar-state topbar-state--connection"
          :class="connectionState"
          :data-state="connectionSeverity"
          aria-hidden="true"
        >
          <span class="chat-system-status__state-dot"></span>
        </span>
        <span class="chat-system-status__menu-copy">
          <strong>{{ connectionLabel }}</strong>
          <small>{{ t('chrome.manageConnection') }}</small>
        </span>
      </button>

      <button
        v-if="menuActions.includes('approval')"
        type="button"
        class="chat-system-status__menu-item"
        role="menuitem"
        data-system-action="approval"
        data-testid="chat-system-approval"
        @click="invoke('approval')"
      >
        <span
          class="chat-system-status__menu-icon is-danger topbar-state topbar-state--approval"
          data-state="danger"
          aria-hidden="true"
        >
          <Icon name="shield" :size="16" />
        </span>
        <span class="chat-system-status__menu-copy">
          <strong>{{ approvalLabel }}</strong>
          <small>{{ t('chrome.openBlockedSession') }}</small>
        </span>
      </button>

      <button
        v-if="menuActions.includes('update')"
        type="button"
        class="chat-system-status__menu-item"
        role="menuitem"
        data-system-action="update"
        data-testid="chat-system-update"
        :title="updateTitle"
        @click="invoke('update')"
      >
        <span
          class="chat-system-status__menu-icon topbar-state topbar-state--update"
          :class="`is-${updateSeverity}`"
          :data-state="updateSeverity"
          aria-hidden="true"
        >
          <Icon :name="updateIconName" :size="16" />
        </span>
        <span class="chat-system-status__menu-copy">
          <strong>{{ updateSummary }}</strong>
          <small>{{ updateTitle }}</small>
        </span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.chat-system-status {
  align-items: center;
  display: flex;
  flex: 0 0 auto;
  gap: var(--sp-2);
  min-width: 0;
  position: relative;
}

.chat-system-status__connection {
  align-items: center;
  display: inline-flex;
  flex: 0 0 auto;
  font-family: inherit;
  gap: 6px;
  justify-content: center;
  min-height: 30px;
}

.chat-system-status__connection.topbar-state {
  background: var(--topbar-state-fill);
  border-color: var(--topbar-state-border);
  color: var(--topbar-state-channel);
}

button.chat-system-status__connection {
  cursor: pointer;
}

button.chat-system-status__connection:hover {
  filter: brightness(1.08);
}

button.chat-system-status__connection:focus-visible,
.chat-system-status__trigger:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 2px;
}

.chat-system-status__state-dot {
  background: currentColor;
  border-radius: 999px;
  display: inline-block;
  flex: 0 0 7px;
  height: 7px;
  width: 7px;
}

.chat-system-status[data-layout='compact'] .chat-system-status__connection {
  min-width: 30px;
  padding: 0;
  width: 30px;
}

.chat-system-status[data-layout='compact'] .chat-system-status__connection-label {
  border: 0;
  clip: rect(0 0 0 0);
  height: 1px;
  margin: -1px;
  overflow: hidden;
  padding: 0;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}

.chat-system-status__approval {
  min-height: 30px;
  white-space: nowrap;
}

.chat-system-status__approval.topbar-state {
  background: var(--topbar-state-channel);
  border: 1px solid var(--topbar-state-border);
}

.chat-system-status__update-wide {
  align-items: center;
  display: flex;
}

.chat-system-status__trigger {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  flex: 0 0 30px;
  height: 30px;
  justify-content: center;
  min-height: 30px;
  min-width: 30px;
  padding: 0;
  position: relative;
  width: 30px;
}

.chat-system-status__trigger.topbar-state {
  background: var(--topbar-state-fill);
  border-color: var(--topbar-state-border);
  color: var(--topbar-state-channel);
}

.chat-system-status__trigger:hover,
.chat-system-status__trigger[aria-expanded='true'] {
  background: color-mix(in srgb, var(--topbar-state-channel) 14%, var(--bg-elevated));
  border-color: var(--topbar-state-border);
  color: var(--topbar-state-channel);
}

.chat-system-status__trigger.is-warning {
  color: var(--warn);
}

.chat-system-status__trigger.is-danger {
  color: var(--danger);
}

.chat-system-status__trigger.conn-pill {
  gap: 4px;
  letter-spacing: 0;
  padding: 0;
}

.chat-system-status__badge {
  background: var(--danger);
  border: 2px solid var(--bg-surface);
  border-radius: 999px;
  color: var(--accent-foreground);
  font-size: 0.5625rem;
  font-weight: 700;
  left: calc(100% - 9px);
  line-height: 14px;
  min-width: 18px;
  padding: 0 3px;
  position: absolute;
  text-align: center;
  top: -6px;
}

.chat-system-status__update-dot {
  background: var(--topbar-state-channel);
  border: 2px solid var(--bg-surface);
  border-radius: 999px;
  height: 9px;
  position: absolute;
  right: -2px;
  top: -2px;
  width: 9px;
}

.chat-system-status__menu {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  max-width: calc(100vw - (2 * var(--sp-3)));
  min-width: min(280px, calc(100vw - (2 * var(--sp-3))));
  padding: var(--sp-1);
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 80;
}

.chat-system-status__menu-item {
  align-items: center;
  background: none;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  font: inherit;
  gap: var(--sp-2);
  min-height: 44px;
  padding: 8px 10px;
  text-align: left;
  width: 100%;
}

.chat-system-status__menu-item:hover,
.chat-system-status__menu-item:focus-visible {
  background: var(--bg-hover);
  color: var(--text);
  outline: none;
}

.chat-system-status__menu-item[aria-disabled='true'] {
  cursor: not-allowed;
  opacity: 0.62;
}

.chat-system-status__menu-icon {
  align-items: center;
  border-radius: var(--radius-sm);
  display: inline-flex;
  flex: 0 0 28px;
  height: 28px;
  justify-content: center;
  min-width: 28px;
  padding: 0;
  width: 28px;
}

.chat-system-status__menu-icon.topbar-state {
  background: var(--topbar-state-fill);
  border: 1px solid var(--topbar-state-border);
  color: var(--topbar-state-channel);
}

.chat-system-status__menu-icon.is-info {
  color: var(--accent);
}

.chat-system-status__menu-icon.is-warning {
  color: var(--warn);
}

.chat-system-status__menu-icon.is-danger {
  color: var(--danger);
}

.chat-system-status__menu-copy,
.chat-system-status__menu-copy > strong,
.chat-system-status__menu-copy > small {
  display: block;
  min-width: 0;
}

.chat-system-status__menu-copy {
  overflow: hidden;
}

.chat-system-status__menu-copy > strong,
.chat-system-status__menu-copy > small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-system-status__menu-copy > strong {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
}

.chat-system-status__menu-copy > small {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin-top: 2px;
}

.chat-system-status__sr {
  border: 0;
  clip: rect(0 0 0 0);
  height: 1px;
  margin: -1px;
  overflow: hidden;
  padding: 0;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}

@media (max-width: 768px), (pointer: coarse) {
  .chat-system-status[data-layout='compact'] .chat-system-status__connection,
  .chat-system-status__trigger {
    flex-basis: 44px;
    height: 44px;
    min-height: 44px;
    min-width: 44px;
    width: 44px;
  }
}

@media (max-width: 480px) {
  .chat-system-status__menu {
    left: var(--sp-3);
    max-width: none;
    min-width: 0;
    position: fixed;
    right: var(--sp-3);
    top: calc(64px + env(safe-area-inset-top, 0px));
    width: auto;
  }
}
</style>
