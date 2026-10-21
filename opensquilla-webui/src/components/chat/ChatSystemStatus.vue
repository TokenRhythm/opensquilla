<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { RpcConnectionState } from '@/utils/chat/chatConnectionState'
import type { SystemHeaderLayout } from '@/utils/headerLayout'

const props = withDefaults(defineProps<{
  layout: SystemHeaderLayout
  connectionState: RpcConnectionState
  connectionLabel: string
  connectionNoticeVisible?: boolean
  approvalCount: number
  canManageConnection: boolean
}>(), { connectionNoticeVisible: true })
const emit = defineEmits<{
  'open-connection': []
  'open-approval': []
}>()
const { t } = useI18n()
const rootRef = ref<HTMLElement | null>(null)
const connectionVisible = computed(() => props.connectionState !== 'connected'
  && props.connectionNoticeVisible !== false)
const approvalCount = computed(() => Number.isFinite(props.approvalCount)
  ? Math.max(0, Math.floor(props.approvalCount)) : 0)
const approvalLabel = computed(() => t('chrome.approvalRequiredCount', { count: approvalCount.value }))
const shortApprovalCount = computed(() => approvalCount.value > 99 ? '99+' : String(approvalCount.value))
const connectionTitle = computed(() => t('chrome.connectionTitle', { state: props.connectionLabel }))

// A resolved status may remove a focused control. Restore focus only in that
// case, never on a background connection change while the user is typing.
watch([connectionVisible, approvalCount], () => {
  const active = document.activeElement
  const root = rootRef.value
  if (!(active instanceof HTMLElement) || !root?.contains(active)) return
  const topbar = root.closest('header')
  void nextTick(() => {
    if (active.isConnected) return
    const sibling = rootRef.value?.querySelector<HTMLButtonElement>('button')
    const stable = Array.from(topbar?.querySelectorAll<HTMLButtonElement>(
      '[data-testid="chat-session-actions-trigger"], [data-testid="sidebar-toggle-collapsed"]',
    ) ?? []).find(button => !button.closest('[inert], [hidden], [style*="display: none"]'))
    const settings = document.querySelector<HTMLButtonElement>('.sidebar-foot button[data-icon="settings"]')
    const sidebarFallback = settings?.closest('[inert], [hidden], [style*="display: none"]') ? null : settings
    ;(sibling || stable || sidebarFallback)?.focus()
  })
})
</script>

<template>
  <div
    v-if="connectionVisible || approvalCount > 0"
    ref="rootRef"
    class="chat-system-status"
    :data-layout="layout"
    data-testid="chat-system-status"
  >
    <button
      v-if="approvalCount > 0"
      type="button"
      class="chat-system-status__control chat-system-status__approval topbar-state topbar-state--approval"
      data-state="warning"
      data-testid="chat-system-approval"
      :title="`${approvalLabel} · ${t('chrome.openBlockedSession')}`"
      :aria-label="`${approvalLabel} · ${t('chrome.openBlockedSession')}`"
      @click="emit('open-approval')"
    >
      <Icon name="shield" :size="16" aria-hidden="true" />
      <span v-if="layout === 'wide'">{{ approvalLabel }}</span>
      <span v-else class="chat-system-status__count" aria-hidden="true">{{ shortApprovalCount }}</span>
    </button>
    <component
      :is="canManageConnection ? 'button' : 'span'"
      v-if="connectionVisible"
      :type="canManageConnection ? 'button' : undefined"
      class="chat-system-status__control chat-system-status__connection topbar-state topbar-state--connection"
      :class="connectionState"
      :data-state="connectionState === 'disconnected' ? 'danger' : 'warning'"
      data-testid="connection-status"
      :title="connectionTitle"
      :aria-label="connectionTitle"
      @click="canManageConnection && emit('open-connection')"
    >
      <Icon :name="connectionState === 'connecting' ? 'refresh' : 'info'" :size="16" aria-hidden="true" />
      <span class="chat-system-status__connection-label">{{ connectionLabel }}</span>
    </component>
  </div>
</template>

<style scoped>
.chat-system-status {
  align-items: center;
  display: flex;
  flex: 0 0 auto;
  gap: var(--sp-1);
  min-width: 0;
}
.chat-system-status__control {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--topbar-state-channel, var(--text-muted));
  display: inline-flex;
  flex: 0 0 auto;
  font: inherit;
  font-size: var(--fs-sm);
  gap: var(--sp-1);
  justify-content: center;
  min-height: 32px;
  padding: 0 var(--sp-2);
  white-space: nowrap;
}
.chat-system-status__approval { color: var(--warn); }
.chat-system-status__connection.disconnected { color: var(--danger); }
.chat-system-status__connection.connecting { color: var(--warn); }
button.chat-system-status__control { cursor: pointer; }
button.chat-system-status__control:hover { background: var(--bg-hover); }
button.chat-system-status__control:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.chat-system-status__count { font-variant-numeric: tabular-nums; }
.chat-system-status .chat-system-status__control.topbar-state {
  box-shadow: none;
}
.chat-system-status .chat-system-status__control.topbar-state::after {
  content: none;
}
.chat-system-status:not([data-layout='wide']) .chat-system-status__connection {
  width: 32px;
  padding: 0;
}
.chat-system-status:not([data-layout='wide']) .chat-system-status__connection-label {
  position: absolute;
  width: 1px;
  height: 1px;
  clip-path: inset(50%);
  overflow: hidden;
  white-space: nowrap;
}
@media (max-width: 768px), (pointer: coarse) {
  .chat-system-status__control { min-height: 44px; min-width: 44px; }
}
</style>
