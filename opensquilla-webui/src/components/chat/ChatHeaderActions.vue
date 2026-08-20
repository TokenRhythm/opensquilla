<template>
  <div
    ref="rootRef"
    class="chat-header"
    :data-layout="layout"
    data-testid="chat-header-actions"
  >
    <div class="chat-header__identity">
      <h1 class="chat-header__title chat-label" :title="title">{{ title }}</h1>
      <button
        v-if="layout === 'wide'"
        ref="wideCopyRef"
        type="button"
        class="chat-header__copy"
        :class="{ 'is-success': copyState === 'ok' }"
        :title="copyLabel"
        :aria-label="copyLabel"
        @click="emit('copy-session-key')"
      >
        <Icon :name="copyIcon" :size="14" />
      </button>
      <span class="chat-header__copy-live" aria-live="polite">{{ copyLiveText }}</span>
    </div>

    <span class="chat-header__spacer" aria-hidden="true"></span>

    <div v-if="layout === 'wide'" class="chat-header__actions">
      <button
        v-if="deliverableCount > 0"
        ref="wideDeliverablesRef"
        type="button"
        class="chat-header__action chat-header__action--deliverables chat-share-btn chat-deliverables-btn topbar-state topbar-state--deliverables"
        data-state="normal"
        :title="deliverablesLabel"
        :aria-label="deliverablesLabel"
        data-testid="chat-session-action-deliverables"
        @click="emit('open-deliverables')"
      >
        <Icon name="fileText" :size="14" />
        <span class="chat-share-btn__label">{{ t('chat.deliverables') }}</span>
        <span class="chat-header__count-badge" aria-hidden="true">{{ deliverableBadge }}</span>
      </button>
      <button
        v-if="!shareMode"
        ref="wideShareRef"
        type="button"
        class="chat-header__action chat-share-btn"
        :aria-disabled="!canShare"
        :title="shareLabel"
        :aria-label="shareAriaLabel"
        data-testid="chat-session-action-share"
        @click="invoke('share')"
      >
        <Icon name="share" :size="14" />
        <span class="chat-share-btn__label">{{ t('chat.share') }}</span>
      </button>
    </div>

    <div v-else ref="compactActionsRef" class="chat-header__actions chat-header__actions--compact">
      <button
        v-if="primaryAction"
        ref="primaryActionRef"
        type="button"
        class="chat-header__action chat-header__action--icon"
        :class="{
          'chat-header__action--deliverables chat-deliverables-btn topbar-state topbar-state--deliverables': primaryAction === 'deliverables',
        }"
        :title="primaryActionLabel"
        :aria-label="primaryActionLabel"
        :data-action="primaryAction"
        :data-state="primaryAction === 'deliverables' ? 'normal' : undefined"
        data-testid="chat-header-primary-action"
        @click="invoke(primaryAction)"
      >
        <Icon :name="primaryActionIcon" :size="16" />
        <span
          v-if="primaryAction === 'deliverables'"
          class="chat-header__count-badge chat-header__count-badge--corner"
          aria-hidden="true"
        >{{ deliverableBadge }}</span>
      </button>

      <button
        ref="menuTriggerRef"
        type="button"
        class="chat-header__action chat-header__action--icon"
        :class="{ 'is-open': menuOpen }"
        :title="t('chat.sessionActions')"
        :aria-label="t('chat.sessionActions')"
        aria-haspopup="menu"
        :aria-expanded="menuOpen"
        aria-controls="chat-session-actions-menu"
        data-testid="chat-session-actions-trigger"
        @click.stop="toggleMenu"
        @keydown.down.prevent="openMenu('first')"
        @keydown.up.prevent="openMenu('last')"
      >
        <Icon name="moreHorizontal" :size="18" />
        <span
          v-if="layout === 'tight' && deliverableCount > 0"
          class="chat-header__count-badge chat-header__count-badge--corner topbar-state topbar-state--deliverables"
          data-state="normal"
          aria-hidden="true"
        >{{ deliverableBadge }}</span>
      </button>

      <div
        v-if="menuOpen"
        id="chat-session-actions-menu"
        ref="menuRef"
        class="chat-header__menu"
        role="menu"
        :aria-label="t('chat.sessionActions')"
        data-testid="chat-session-actions-menu"
        data-chat-topbar-popover="session-actions"
        @keydown="onMenuKeydown"
      >
        <button
          v-if="menuActions.includes('deliverables')"
          type="button"
          class="chat-header__menu-item topbar-state topbar-state--deliverables"
          data-state="normal"
          role="menuitem"
          :aria-label="deliverablesLabel"
          data-testid="chat-session-action-deliverables"
          @click="invoke('deliverables', true)"
        >
          <Icon name="fileText" :size="16" />
          <span>{{ t('chat.deliverables') }}</span>
          <span class="chat-header__count-badge" aria-hidden="true">{{ deliverableBadge }}</span>
        </button>
        <button
          v-if="menuActions.includes('share')"
          type="button"
          class="chat-header__menu-item"
          role="menuitem"
          :aria-disabled="!canShare"
          data-testid="chat-session-action-share"
          @click="invoke('share', true)"
        >
          <Icon name="share" :size="16" />
          <span class="chat-header__menu-copy">
            <span>{{ t('chat.share') }}</span>
            <small v-if="!canShare">{{ t('chat.shareSendFirst') }}</small>
          </span>
        </button>
        <div
          v-if="menuActions.length > 1"
          class="chat-header__menu-divider"
          role="separator"
        ></div>
        <button
          type="button"
          class="chat-header__menu-item"
          role="menuitem"
          data-testid="chat-session-action-copy"
          @click="invoke('copy-session-key', true)"
        >
          <Icon :name="copyIcon" :size="16" />
          <span>{{ copyLabel }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useChatTopbarPopoverCoordination } from '@/composables/useChatTopbarPopoverCoordinator'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import {
  resolveSessionHeaderLayout,
  type SessionHeaderLayout,
} from '@/utils/headerLayout'
import type { IconName } from '@/utils/icons'

type Action = 'deliverables' | 'share' | 'copy-session-key'

const COARSE_POINTER_QUERY = '(pointer: coarse)'

const props = defineProps<{
  title: string
  copyState: string | null
  copyIcon: IconName
  copyLiveText: string
  deliverableCount: number
  shareMode: boolean
  shareableMessageCount: number
}>()

const emit = defineEmits<{
  'open-deliverables': []
  'start-share': []
  'copy-session-key': []
}>()

const { t } = useI18n()
const rootRef = ref<HTMLDivElement | null>(null)
const compactActionsRef = ref<HTMLDivElement | null>(null)
const menuTriggerRef = ref<HTMLButtonElement | null>(null)
const menuRef = ref<HTMLDivElement | null>(null)
const primaryActionRef = ref<HTMLButtonElement | null>(null)
const wideDeliverablesRef = ref<HTMLButtonElement | null>(null)
const wideShareRef = ref<HTMLButtonElement | null>(null)
const wideCopyRef = ref<HTMLButtonElement | null>(null)
const layout = ref<SessionHeaderLayout>('wide')
const menuOpen = ref(false)
useChatTopbarPopoverCoordination('session-actions', menuOpen)
const menuIsTopmost = useDialogLayer(computed(() => menuOpen.value))
let resizeObserver: ResizeObserver | null = null
let coarsePointerMedia: MediaQueryList | null = null
let layoutFrame: number | null = null
let hasMeasuredLayout = false

const copyLabel = computed(() => props.copyState === 'ok' ? t('chat.copied') : t('chat.copySessionKey'))
const deliverablesLabel = computed(() => t('chat.deliverablesCount', { count: props.deliverableCount }))
const deliverableBadge = computed(() => props.deliverableCount > 99
  ? '99+'
  : String(props.deliverableCount))
const canShare = computed(() => props.shareableMessageCount > 0)
const shareLabel = computed(() => !canShare.value
  ? t('chat.shareSendFirst')
  : t('chat.shareSelectHint'))
const shareAriaLabel = computed(() => !canShare.value
  ? t('chat.shareSendFirst')
  : t('chat.share'))

const primaryAction = computed<Action | null>(() => {
  if (layout.value === 'tight') return null
  if (props.deliverableCount > 0) return 'deliverables'
  if (!props.shareMode && canShare.value) return 'share'
  return null
})

const primaryActionLabel = computed(() => {
  if (primaryAction.value === 'deliverables') return deliverablesLabel.value
  return shareAriaLabel.value
})
const primaryActionIcon = computed<IconName>(() => {
  if (primaryAction.value === 'deliverables') return 'fileText'
  return 'share'
})

const menuActions = computed<Action[]>(() => {
  const actions: Action[] = []
  if (props.deliverableCount > 0 && primaryAction.value !== 'deliverables') actions.push('deliverables')
  if (!props.shareMode && primaryAction.value !== 'share') actions.push('share')
  actions.push('copy-session-key')
  return actions
})

function isVisible(element: HTMLElement | null): element is HTMLElement {
  return Boolean(element && element.getClientRects().length > 0)
}

function actionForElement(element: Element | null): Action | null {
  if (element === wideDeliverablesRef.value) return 'deliverables'
  if (element === wideShareRef.value) return 'share'
  if (element === wideCopyRef.value) return 'copy-session-key'
  if (element === primaryActionRef.value) return primaryAction.value
  if (!(element instanceof HTMLElement)) return null
  const testId = element.dataset.testid
  if (testId === 'chat-session-action-deliverables') return 'deliverables'
  if (testId === 'chat-session-action-share') return 'share'
  if (testId === 'chat-session-action-copy') return 'copy-session-key'
  return null
}

function focusWideFallback() {
  const fallback = wideDeliverablesRef.value
    || wideShareRef.value
    || wideCopyRef.value
  fallback?.focus()
}

function syncLayout() {
  const width = rootRef.value?.getBoundingClientRect().width ?? 0
  const next = resolveSessionHeaderLayout({
    availableWidth: width,
    previousLayout: hasMeasuredLayout ? layout.value : null,
    mobile: window.innerWidth <= 768,
    coarseOnly: coarsePointerMedia?.matches ?? false,
  })
  hasMeasuredLayout = true
  if (next === layout.value) return

  const activeElement = document.activeElement
  const focusedAction = actionForElement(activeElement)
  const restoreHeaderFocus = Boolean(
    activeElement instanceof Node && rootRef.value?.contains(activeElement),
  )
  layout.value = next
  menuOpen.value = false
  if (restoreHeaderFocus) {
    void nextTick(() => {
      if (focusedAction && focusAction(focusedAction)) return
      if (next !== 'wide') {
        menuTriggerRef.value?.focus()
        return
      }
      focusWideFallback()
    })
  }
}

function scheduleLayout() {
  if (layoutFrame != null) return
  layoutFrame = window.requestAnimationFrame(() => {
    layoutFrame = null
    syncLayout()
  })
}

function menuItems(): HTMLButtonElement[] {
  return Array.from(menuRef.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
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
  if (restoreFocus) menuTriggerRef.value?.focus()
  menuOpen.value = false
}

function toggleMenu() {
  if (menuOpen.value) closeMenu(true)
  else openMenu()
}

function invoke(action: Action, fromMenu = false) {
  if (action === 'share' && !canShare.value) return
  // Dialogs capture their invoker synchronously. Move focus to a stable node
  // before the menu item is unmounted so close-focus never falls back to body.
  if (fromMenu) closeMenu(true)
  if (action === 'deliverables') emit('open-deliverables')
  if (action === 'share') emit('start-share')
  if (action === 'copy-session-key') emit('copy-session-key')
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
  if (event.key === 'ArrowUp') nextIndex = current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length
  if (event.key === 'Home') nextIndex = 0
  if (event.key === 'End') nextIndex = items.length - 1
  if (nextIndex == null) return
  event.preventDefault()
  items[nextIndex]?.focus()
}

function focusAction(action: Action): boolean {
  const direct = action === 'deliverables'
    ? wideDeliverablesRef.value
    : action === 'share'
      ? wideShareRef.value
      : wideCopyRef.value
  if (isVisible(direct)) {
    direct.focus()
    return true
  }
  if (primaryAction.value === action && isVisible(primaryActionRef.value)) {
    primaryActionRef.value.focus()
    return true
  }
  if (isVisible(menuTriggerRef.value)) {
    menuTriggerRef.value.focus()
    return true
  }
  return false
}

useDocumentEvent('click', (event) => {
  if (!menuOpen.value) return
  if (event.target instanceof Node && !compactActionsRef.value?.contains(event.target)) {
    menuOpen.value = false
  }
})

useDocumentEvent('keydown', (event) => {
  if (event.defaultPrevented || event.key !== 'Escape') return
  if (!menuOpen.value || !menuIsTopmost.value) return
  event.preventDefault()
  closeMenu(true)
})

watch(() => [
  props.deliverableCount,
  props.shareMode,
  props.shareableMessageCount,
], () => {
  if (menuOpen.value) closeMenu(true)
})

onMounted(() => {
  coarsePointerMedia = typeof window.matchMedia === 'function'
    ? window.matchMedia(COARSE_POINTER_QUERY)
    : null
  coarsePointerMedia?.addEventListener('change', scheduleLayout)
  syncLayout()
  if (typeof ResizeObserver !== 'undefined' && rootRef.value) {
    resizeObserver = new ResizeObserver(scheduleLayout)
    resizeObserver.observe(rootRef.value)
  }
  window.addEventListener('resize', scheduleLayout)
})

onUnmounted(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  coarsePointerMedia?.removeEventListener('change', scheduleLayout)
  coarsePointerMedia = null
  window.removeEventListener('resize', scheduleLayout)
  if (layoutFrame != null) {
    window.cancelAnimationFrame(layoutFrame)
    layoutFrame = null
  }
})

defineExpose({ focusAction, closeMenu })
</script>

<style scoped>
.chat-header {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
  height: 100%;
  min-width: 0;
  width: 100%;
}

.chat-header__identity {
  align-items: center;
  display: flex;
  flex: 0 1 auto;
  gap: var(--sp-1);
  min-width: 0;
}

.chat-header__title {
  color: var(--text);
  flex: 0 1 auto;
  font-size: 0.9375rem;
  font-weight: 500;
  line-height: 1.3;
  margin: 0;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-header__spacer {
  flex: 1 1 auto;
  min-width: 0;
}

.chat-header__copy {
  align-items: center;
  background: none;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  flex: 0 0 auto;
  justify-content: center;
  min-height: 30px;
  min-width: 30px;
  padding: 4px;
}

.chat-header__copy:hover,
.chat-header__copy:focus-visible {
  color: var(--text);
}

.chat-header__copy.is-success {
  color: var(--ok);
}

.chat-header__copy-live {
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

.chat-header__actions {
  align-items: center;
  display: flex;
  flex: 0 0 auto;
  gap: var(--sp-2);
  min-width: 0;
  position: relative;
}

.chat-header__actions--compact {
  gap: var(--sp-1);
}

.chat-header__action {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font: inherit;
  font-size: 0.8125rem;
  gap: var(--sp-1);
  justify-content: center;
  min-height: 30px;
  padding: 0.25rem 0.625rem;
  white-space: nowrap;
}

.chat-header__action:hover:not(:disabled):not([aria-disabled='true']),
.chat-header__action:focus-visible,
.chat-header__action.is-open {
  background: var(--bg-hover);
  border-color: var(--border-strong);
  color: var(--text);
}

.chat-header__action:disabled,
.chat-header__action[aria-disabled='true'] {
  cursor: not-allowed;
  opacity: 0.6;
}

.chat-header__action--icon {
  background: var(--bg-elevated);
  flex: 0 0 44px;
  height: 44px;
  min-height: 44px;
  min-width: 44px;
  padding: 0;
  position: relative;
  width: 44px;
}

.chat-header__action--icon-small {
  min-width: 30px;
  padding: 0.25rem;
}

.chat-header__action--deliverables.topbar-state {
  background: var(--topbar-state-fill);
  border-color: var(--topbar-state-border);
  color: var(--topbar-state-channel);
}

.chat-header__count-badge {
  align-items: center;
  background: var(--bg-hover);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-full);
  box-sizing: border-box;
  color: var(--text-muted);
  display: inline-flex;
  font-size: 0.625rem;
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  height: 18px;
  justify-content: center;
  line-height: 16px;
  min-width: 18px;
  padding-inline: 4px;
}

.chat-header__count-badge--corner {
  inset-block-start: 1px;
  inset-inline-end: 1px;
  position: absolute;
}

.topbar-state--deliverables .chat-header__count-badge,
.chat-header__count-badge.topbar-state--deliverables {
  background: var(--topbar-state-fill);
  border-color: var(--topbar-state-border);
  color: var(--topbar-state-channel);
}

.chat-header__menu {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  max-width: calc(100vw - (2 * var(--sp-3)));
  min-width: min(250px, calc(100vw - (2 * var(--sp-3))));
  padding: var(--sp-1);
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 80;
}

.chat-header__menu-item {
  align-items: center;
  background: none;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  font: inherit;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  min-height: 44px;
  padding: 8px 10px;
  text-align: left;
  width: 100%;
}

.chat-header__menu-item:hover:not([aria-disabled='true']),
.chat-header__menu-item:focus-visible {
  background: var(--bg-hover);
  color: var(--text);
  outline: none;
}

.chat-header__menu-item[aria-disabled='true'] {
  cursor: not-allowed;
  opacity: 0.62;
}

.chat-header__menu-item > .chat-header__count-badge {
  margin-inline-start: auto;
}

.chat-header__menu-divider {
  border-top: 1px solid var(--border);
  margin: var(--sp-1) var(--sp-2);
}

.chat-header__menu-copy,
.chat-header__menu-copy > span,
.chat-header__menu-copy > small {
  display: block;
}

.chat-header__menu-copy {
  min-width: 0;
}

.chat-header__menu-copy > small {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin-top: 2px;
}

@media (max-width: 768px) {
  .chat-header__copy,
  .chat-header__action {
    min-height: 44px;
  }
}

@media (max-width: 480px) {
  .chat-header__menu {
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
