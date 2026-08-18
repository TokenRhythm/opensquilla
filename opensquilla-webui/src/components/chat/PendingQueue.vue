<template>
  <TransitionGroup
    v-if="items.length > 0"
    name="chat-pending-list"
    tag="section"
    class="chat-pending"
    :aria-label="t('chat.pending.label', { count: items.length, max: effectiveMaxPending })"
  >
    <article
      v-for="(item, index) in items"
      :key="item.pendingUiId"
      class="chat-pending-card"
      :class="{
        'is-reorderable': canReorderItem(item),
        'is-reorder-arming': pointerReorder?.item === item && !pointerReorder.active,
        'is-reordering': draggingItem === item,
      }"
      :data-queue-key="item.pendingUiId"
      :data-pending-ui-id="item.pendingUiId"
      :data-delivery-state="pendingCardState(item)"
      :aria-busy="isSteering(item) ? 'true' : undefined"
      :aria-label="canReorderItem(item)
        ? `${displayText(item)}. ${t('chat.pending.reorderHint')}`
        : undefined"
      :aria-describedby="attachmentBlockMessage(item) ? attachmentStatusId(item) : undefined"
      :aria-keyshortcuts="canReorderItem(item) ? 'Alt+ArrowUp Alt+ArrowDown' : undefined"
      :tabindex="canReorderItem(item) ? 0 : undefined"
      @keydown="onCardKeydown(item, $event)"
      @pointerdown="onCardPointerDown(item, $event)"
    >
      <p class="chat-pending-text" :title="displayText(item)">
        {{ displayText(item) }}
      </p>
      <span
        v-if="item.pendingPersistenceState === 'saving'"
        class="chat-pending-save-status"
        role="status"
      >
        {{ t('chat.saving') }}
      </span>
      <span
        v-if="reorderPending && !draggingItem && index === 0"
        class="chat-pending-save-status"
        role="status"
      >
        {{ t('chat.pending.reorderRecovering') }}
      </span>
      <span v-if="item.attachments?.length" class="chat-pending-attachments">
        {{ item.attachments.length }} · 📎
        <span
          v-if="attachmentBlockMessage(item)"
          :id="attachmentStatusId(item)"
          class="chat-pending-attachment-status"
          :title="attachmentBlockMessage(item)"
        >
          {{ t('chat.pending.attachmentNeedsAttention') }}:
          {{ attachmentBlockMessage(item) }}
        </span>
      </span>
      <div class="chat-pending-actions">
        <button
          v-if="canShowSteer(item)"
          type="button"
          class="chat-pending-action chat-pending-action--steer"
          :title="steerTitle(item)"
          :disabled="isSteerDisabled(item)"
          :aria-describedby="attachmentBlockMessage(item) ? attachmentStatusId(item) : undefined"
          @click="emit('steer', item.pendingUiId)"
        >
          <span aria-hidden="true">↪</span>
          <span :aria-live="pendingCardState(item) !== 'queued' ? 'polite' : undefined">
            {{ steerActionLabel(item) }}
          </span>
        </button>
        <button
          type="button"
          class="chat-pending-action chat-pending-action--icon"
          :aria-label="removeLabel(item, index)"
          :title="removeLabel(item, index)"
          :disabled="isSteering(item) || isQueueReordering"
          @click="emit('remove', item.pendingUiId)"
        >
          <Icon name="trash" :size="14" />
        </button>
        <div v-if="!item.hiddenControl" class="chat-pending-more-wrap">
          <button
            type="button"
            class="chat-pending-action chat-pending-action--icon"
            :class="{ 'is-active': openMenuUiId === item.pendingUiId }"
            :aria-label="t('chrome.more')"
            :title="t('chrome.more')"
            aria-haspopup="menu"
            :aria-expanded="openMenuUiId === item.pendingUiId && !isSteering(item) ? 'true' : 'false'"
            :disabled="isSteering(item) || isQueueReordering"
            @click.stop="toggleMenu(item.pendingUiId)"
          >
            <Icon name="moreHorizontal" :size="16" />
          </button>
          <div
            v-if="openMenuUiId === item.pendingUiId && !isSteering(item)"
            class="chat-pending-menu"
            role="menu"
            :aria-label="t('chrome.more')"
          >
            <button
              type="button"
              role="menuitem"
              :disabled="!!item.deliveryState || !!item.steerAttempt || hasUneditableMaterial(item)"
              @click="chooseEdit(item.pendingUiId)"
            >
              <Icon name="pencil" :size="15" />
              <span>{{ t('chat.pending.editMessage') }}</span>
            </button>
            <button type="button" role="menuitem" @click="chooseClear">
              <Icon name="trash" :size="15" />
              <span>{{ t('chat.pending.clearQueue') }}</span>
            </button>
          </div>
        </div>
      </div>
    </article>
    <span key="reorder-announcement" class="chat-pending-announcement" aria-live="polite">
      {{ reorderAnnouncement }}
    </span>
  </TransitionGroup>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import type { Attachment, PendingSteerAttempt } from '@/types/chat'
import {
  hasSendableModelInputImageAttachment,
  isSendableAttachment,
} from '@/utils/chat/attachments'
import { isControlInput } from '@/utils/chat/inputSemantics'

const { t } = useI18n()

interface PendingQueueItem {
  pendingUiId: string
  text: string
  pendingInputId?: string
  displayTextOverride?: string
  hiddenControl?: boolean
  attachments?: Attachment[]
  deliveryState?: 'steering' | 'retryable'
  steerAttempt?: PendingSteerAttempt
  pendingPersistenceState?: 'saving' | 'staged' | 'local_only' | 'retryable' | 'cancelling'
}

type PendingSteerBlocker =
  | 'controlInput'
  | 'attachment'
  | 'capability'
  | 'otherDelivery'
  | 'steering'

const props = withDefaults(defineProps<{
  items: PendingQueueItem[]
  maxPending: number
  reorderEnabled?: boolean
  reorderPending?: boolean
  imageBlockedMessage?: string
  steerAvailable?: boolean
  durableSteerAvailable?: boolean
  steerUnavailableMessage?: string
}>(), {
  reorderEnabled: true,
})

const emit = defineEmits<{
  clear: []
  edit: [pendingUiId: string]
  remove: [pendingUiId: string]
  reorder: [fromIndex: number, toIndex: number]
  reorderEnd: []
  reorderStart: [index: number]
  steer: [pendingUiId: string]
}>()

const LONG_PRESS_MS = 750
const LONG_PRESS_DEADZONE_PX = 7
const openMenuUiId = ref<string | null>(null)
const draggingItem = shallowRef<PendingQueueItem | null>(null)
const reorderAnnouncement = ref('')
let longPressTimer: ReturnType<typeof setTimeout> | null = null
const pointerReorder = shallowRef<{
  active: boolean
  card: HTMLElement
  item: PendingQueueItem
  pointerId: number
  startX: number
  startY: number
} | null>(null)
const isQueueReordering = computed(() => (
  draggingItem.value !== null || props.reorderPending === true
))
const effectiveMaxPending = computed(() => (
  props.maxPending + (
    props.items.some(item => item.steerAttempt) || props.items.length > props.maxPending
      ? 1
      : 0
  )
))

function displayText(item: PendingQueueItem): string {
  return item.displayTextOverride || item.text
}

function queueCanReorder(): boolean {
  return props.reorderEnabled !== false
    && props.reorderPending !== true
    && props.items.length > 1 && props.items.every(item => (
    !item.hiddenControl
    && !item.deliveryState
    && !item.steerAttempt
    && item.pendingPersistenceState !== 'saving'
    && item.pendingPersistenceState !== 'cancelling'
  ))
}

function canReorderItem(item: PendingQueueItem): boolean {
  return queueCanReorder() && props.items.includes(item)
}

function isSteering(item: PendingQueueItem): boolean {
  return item.deliveryState === 'steering'
    || item.steerAttempt?.phase === 'submitting'
    || item.pendingPersistenceState === 'saving'
    || item.pendingPersistenceState === 'cancelling'
}

function isSteerRetry(item: PendingQueueItem): boolean {
  return item.steerAttempt?.phase === 'retryable_rejected'
    || item.steerAttempt?.phase === 'acceptance_unknown'
}

function pendingCardState(item: PendingQueueItem): 'queued' | 'busy' | 'attention' {
  if (isSteering(item)) return 'busy'
  if (item.deliveryState === 'retryable' || isSteerRetry(item)) return 'attention'
  return 'queued'
}

function steerActionLabel(item: PendingQueueItem): string {
  switch (item.steerAttempt?.phase) {
    case 'submitting':
      return t('chat.pending.steerSubmitting')
    case 'retryable_rejected':
      return t('chat.pending.steerRetryRejected')
    case 'acceptance_unknown':
      return t('chat.pending.steerRetryUnknown')
    default:
      return item.deliveryState === 'retryable' ? t('chat.retry') : t('chat.steerMode')
  }
}

function removeLabel(item: PendingQueueItem, index: number): string {
  if (item.steerAttempt?.phase === 'acceptance_unknown') {
    return t('chat.pending.removeUnknownSteer', { index: index + 1 })
  }
  return t('chat.pending.removeMessage', { index: index + 1 })
}

function canShowSteer(item: PendingQueueItem): boolean {
  return !item.hiddenControl
}

function hasUnsendableAttachment(item: PendingQueueItem): boolean {
  return item.attachments?.some(attachment => (
    !attachment.durable_material && !isSendableAttachment(attachment)
  )) === true
}

function hasUneditableMaterial(item: PendingQueueItem): boolean {
  return item.attachments?.some(attachment => (
    attachment.durable_material
    || (item.pendingPersistenceState === 'staged'
      && attachment.kind === 'staged'
      && !attachment.file)
  )) === true
}

function attachmentBlockMessage(item: PendingQueueItem): string {
  if (hasUnsendableAttachment(item)) {
    return t('chat.pending.fixAttachmentBeforeSteer')
  }
  if (
    props.imageBlockedMessage
    && hasSendableModelInputImageAttachment(item.attachments || [])
  ) {
    return props.imageBlockedMessage
  }
  return ''
}

function pendingSteerBlocker(item: PendingQueueItem): PendingSteerBlocker | null {
  if (isControlInput(item.text)) return 'controlInput'
  if (item.attachments?.length) return 'attachment'
  if (
    item.pendingInputId
    && item.pendingPersistenceState === 'staged'
    && props.durableSteerAvailable !== true
  ) return 'capability'
  if (!props.steerAvailable && item.deliveryState !== 'retryable' && !isSteerRetry(item)) {
    return 'capability'
  }
  if (props.items.some(
    candidate => candidate !== item && Boolean(candidate.deliveryState || candidate.steerAttempt),
  )) return 'otherDelivery'
  if (isSteering(item)) return 'steering'
  return null
}

function isSteerDisabled(item: PendingQueueItem): boolean {
  return isQueueReordering.value || pendingSteerBlocker(item) !== null
}

function steerTitle(item: PendingQueueItem): string {
  switch (pendingSteerBlocker(item)) {
    case 'controlInput':
      return t('chat.sendQueues')
    case 'attachment':
      return attachmentBlockMessage(item) || t('chat.pending.steerUnavailable.attachment')
    case 'capability':
      if (
        item.pendingInputId
        && item.pendingPersistenceState === 'staged'
        && props.durableSteerAvailable !== true
      ) {
        return t('chat.pending.steerUnavailable.gatewayUnsupported')
      }
      return props.steerUnavailableMessage?.trim() || t('chat.sendQueues')
    case 'otherDelivery':
      return t('chat.pending.steerUnavailable.deliveryInProgress')
    case 'steering':
      return t('chat.pending.steerUnavailable.steeringInProgress')
    default:
      if (item.steerAttempt?.phase === 'retryable_rejected') {
        return t('chat.pending.steerRetryRejectedHint')
      }
      if (item.steerAttempt?.phase === 'acceptance_unknown') {
        return t('chat.pending.steerRetryUnknownHint')
      }
      return item.deliveryState === 'retryable' ? t('chat.retry') : t('chat.pending.steerHint')
  }
}

function attachmentStatusId(item: PendingQueueItem): string {
  return `chat-pending-attachment-status-${item.pendingUiId}`
}

function itemByUiId(pendingUiId: string): PendingQueueItem | undefined {
  return props.items.find(item => item.pendingUiId === pendingUiId)
}

function toggleMenu(pendingUiId: string) {
  const item = itemByUiId(pendingUiId)
  if (isQueueReordering.value || !item || isSteering(item)) return
  openMenuUiId.value = openMenuUiId.value === pendingUiId ? null : pendingUiId
}

function chooseEdit(pendingUiId: string) {
  openMenuUiId.value = null
  const item = itemByUiId(pendingUiId)
  if (!item || item.deliveryState || item.steerAttempt) return
  emit('edit', pendingUiId)
}

function chooseClear() {
  openMenuUiId.value = null
  emit('clear')
}

function clearLongPressTimer() {
  if (!longPressTimer) return
  clearTimeout(longPressTimer)
  longPressTimer = null
}

function finishPointerReorder() {
  const reorder = pointerReorder.value
  clearLongPressTimer()
  pointerReorder.value = null
  if (!reorder?.active) return
  draggingItem.value = null
  emit('reorderEnd')
}

function cancelPointerReorder() {
  const reorder = pointerReorder.value
  clearLongPressTimer()
  pointerReorder.value = null
  if (!reorder?.active) return
  draggingItem.value = null
  emit('reorderEnd')
}

function announcePosition(item: PendingQueueItem) {
  const index = props.items.indexOf(item)
  if (index < 0) return
  reorderAnnouncement.value = t('chat.pending.reorderPosition', {
    count: props.items.length,
    label: displayText(item),
    position: index + 1,
  })
}

function activatePointerReorder(reorder: NonNullable<typeof pointerReorder.value>) {
  if (pointerReorder.value !== reorder || !canReorderItem(reorder.item)) return
  reorder.active = true
  draggingItem.value = reorder.item
  openMenuUiId.value = null
  reorder.card.setPointerCapture?.(reorder.pointerId)
  const index = props.items.indexOf(reorder.item)
  if (index < 0) return cancelPointerReorder()
  emit('reorderStart', index)
  reorderAnnouncement.value = t('chat.pending.reorderStarted', {
    label: displayText(reorder.item),
  })
}

function onCardPointerDown(item: PendingQueueItem, event: PointerEvent) {
  if (event.button > 0 || !canReorderItem(item)) return
  const target = event.target as Element | null
  if (target?.closest?.('button, a, input, textarea, select, [role="menu"]')) return
  cancelPointerReorder()
  const card = event.currentTarget as HTMLElement | null
  if (!card?.classList.contains('chat-pending-card')) return
  const reorder = {
    active: false,
    card,
    item,
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
  }
  pointerReorder.value = reorder
  longPressTimer = setTimeout(() => activatePointerReorder(reorder), LONG_PRESS_MS)
}

function onCardKeydown(item: PendingQueueItem, event: KeyboardEvent) {
  if (!event.altKey || !canReorderItem(item)) return
  const fromIndex = props.items.indexOf(item)
  const toIndex = event.key === 'ArrowUp'
    ? fromIndex - 1
    : event.key === 'ArrowDown'
      ? fromIndex + 1
      : fromIndex
  if (toIndex === fromIndex || toIndex < 0 || toIndex >= props.items.length) return
  event.preventDefault()
  emit('reorderStart', fromIndex)
  emit('reorder', fromIndex, toIndex)
  emit('reorderEnd')
  announcePosition(item)
}

watch(() => props.items.map(item => item.pendingUiId), ids => {
  if (openMenuUiId.value && !ids.includes(openMenuUiId.value)) {
    openMenuUiId.value = null
  }
})

useDocumentEvent('pointerdown', (event) => {
  const target = event.target
  if (target instanceof Element && target.closest('.chat-pending-more-wrap')) return
  openMenuUiId.value = null
})

useDocumentEvent('pointermove', (event) => {
  const reorder = pointerReorder.value
  if (!reorder || event.pointerId !== reorder.pointerId) return
  if (!reorder.active) {
    if (
      Math.hypot(event.clientX - reorder.startX, event.clientY - reorder.startY)
      <= LONG_PRESS_DEADZONE_PX
    ) return
    cancelPointerReorder()
    return
  }
  event.preventDefault()
  const target = document.elementFromPoint(event.clientX, event.clientY)
    ?.closest<HTMLElement>('.chat-pending-card[data-queue-key]')
  if (!target) return
  const targetItem = props.items.find(item => item.pendingUiId === target.dataset.queueKey)
  if (!targetItem || targetItem === reorder.item || !canReorderItem(targetItem)) return
  const fromIndex = props.items.indexOf(reorder.item)
  const toIndex = props.items.indexOf(targetItem)
  if (fromIndex < 0 || toIndex < 0) return
  const rect = target.getBoundingClientRect()
  const crossedMidpoint = fromIndex < toIndex
    ? event.clientY > rect.top + rect.height / 2
    : event.clientY < rect.top + rect.height / 2
  if (!crossedMidpoint) return
  emit('reorder', fromIndex, toIndex)
  announcePosition(reorder.item)
}, { passive: false })

useDocumentEvent('pointerup', (event) => {
  if (event.pointerId !== pointerReorder.value?.pointerId) return
  finishPointerReorder()
})

useDocumentEvent('pointercancel', (event) => {
  if (event.pointerId !== pointerReorder.value?.pointerId) return
  cancelPointerReorder()
})

useDocumentEvent('keydown', (event) => {
  if (event.key === 'Escape' && pointerReorder.value) {
    event.preventDefault()
    cancelPointerReorder()
    return
  }
  if (event.key !== 'Escape' || openMenuUiId.value === null) return
  event.preventDefault()
  openMenuUiId.value = null
})

watch(
  () => !pointerReorder.value || props.items.includes(pointerReorder.value.item),
  itemStillExists => {
    if (!itemStillExists) cancelPointerReorder()
  },
)

watch(queueCanReorder, (canReorder) => {
  if (!canReorder && pointerReorder.value) cancelPointerReorder()
})

onBeforeUnmount(() => {
  cancelPointerReorder()
})
</script>

<style scoped>
.chat-pending {
  position: relative;
  z-index: 1;
  display: grid;
  gap: 8px;
  width: min(calc(100% - 3rem), calc(var(--composer-col, 820px) - 1rem));
  margin: 0 auto 10px;
  padding: 0;
}

.chat-pending-card {
  position: relative;
  display: flex;
  align-items: center;
  min-width: 0;
  min-height: 50px;
  gap: 10px;
  padding: 9px 10px 9px 15px;
  border: 1px solid color-mix(in srgb, var(--text) 8%, transparent);
  border-radius: var(--radius-lg);
  background: linear-gradient(
    135deg,
    color-mix(in srgb, var(--bg-surface) 98%, var(--accent) 2%),
    color-mix(in srgb, var(--bg-surface) 96%, var(--bg-surface-2))
  );
  box-shadow:
    inset 0 1px 0 var(--elev-highlight),
    0 12px 30px -25px color-mix(in srgb, var(--text) 38%, transparent);
  transition:
    border-color var(--dur-fast) var(--ease-standard),
    box-shadow var(--dur-fast) var(--ease-standard),
    opacity var(--dur-fast) var(--ease-standard);
}

.chat-pending-card.is-reorderable {
  cursor: grab;
  touch-action: none;
  -webkit-touch-callout: none;
}

.chat-pending-card.is-reordering {
  z-index: 3;
  border-color: color-mix(in srgb, var(--accent) 34%, var(--border));
  cursor: grabbing;
  scale: 1.012;
  box-shadow:
    inset 0 1px 0 var(--elev-highlight),
    0 18px 38px -22px color-mix(in srgb, var(--accent) 42%, transparent);
  animation: chat-pending-reorder-ready var(--dur-enter) var(--ease-standard);
  user-select: none;
}

.chat-pending-card::before {
  content: "";
  width: 6px;
  height: 6px;
  flex: 0 0 auto;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--accent) 82%, var(--text));
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 10%, transparent);
}

.chat-pending-card[data-delivery-state="attention"] {
  border-color: color-mix(in srgb, var(--warn) 24%, var(--border));
  background: linear-gradient(
    135deg,
    color-mix(in srgb, var(--bg-surface) 95%, var(--warn-fill)),
    var(--bg-surface)
  );
}

.chat-pending-card[data-delivery-state="attention"]::before {
  background: var(--warn);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--warn) 11%, transparent);
}

.chat-pending-card[data-delivery-state="busy"]::before {
  animation: chat-pending-pulse 1.8s var(--ease-standard) infinite;
}

.chat-pending-text {
  min-width: 0;
  flex: 1;
  margin: 0;
  overflow: hidden;
  color: var(--text);
  font-size: var(--fs-sm);
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-pending-attachments {
  min-width: 0;
  max-width: min(45%, 360px);
  flex: 0 0 auto;
  margin-top: 1px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.chat-pending-attachment-status {
  display: block;
  margin-top: 2px;
  line-height: 1.35;
  white-space: normal;
}

.chat-pending-actions {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 3px;
}

.chat-pending-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 26px;
  gap: 4px;
  padding: 0 7px;
  border: 0;
  border-radius: var(--radius-control);
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
  transition:
    background var(--dur-fast) var(--ease-standard),
    color var(--dur-fast) var(--ease-standard);
}

.chat-pending-action--icon {
  width: 26px;
  padding: 0;
}

.chat-pending-action--steer {
  gap: 3px;
  min-height: 28px;
  padding-inline: 8px;
  font-size: var(--fs-xs);
}

.chat-pending-card[data-delivery-state="attention"] .chat-pending-action--steer {
  background: color-mix(in srgb, var(--warn) 10%, transparent);
  color: color-mix(in srgb, var(--warn) 84%, var(--text));
}

.chat-pending-card[data-delivery-state="attention"] .chat-pending-action--steer:hover,
.chat-pending-card[data-delivery-state="attention"] .chat-pending-action--steer:focus-visible {
  background: color-mix(in srgb, var(--warn) 16%, transparent);
}

.chat-pending-action:hover,
.chat-pending-action:focus-visible,
.chat-pending-action.is-active {
  outline: 0;
  background: color-mix(in srgb, var(--text) 6%, transparent);
  color: var(--text);
}

.chat-pending-action:disabled {
  cursor: default;
  opacity: 0.55;
}

.chat-pending-action:focus-visible {
  box-shadow: var(--focus-ring);
}

.chat-pending-more-wrap {
  position: relative;
  display: inline-flex;
}

.chat-pending-menu {
  position: absolute;
  z-index: 10;
  right: 0;
  bottom: calc(100% + 6px);
  width: max-content;
  min-width: 172px;
  padding: 5px;
  border: 1px solid color-mix(in srgb, var(--text) 10%, transparent);
  border-radius: var(--radius-lg);
  background: color-mix(in srgb, var(--bg-surface) 96%, transparent);
  box-shadow: var(--shadow-lg);
  backdrop-filter: blur(18px);
}

.chat-pending-menu button {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 36px;
  gap: 9px;
  padding: 0 9px;
  border: 0;
  border-radius: var(--radius-control);
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
  text-align: left;
  cursor: pointer;
}

.chat-pending-menu button:hover,
.chat-pending-menu button:focus-visible {
  outline: 0;
  background: var(--bg-hover);
}

.chat-pending-menu button:disabled {
  cursor: default;
  opacity: 0.5;
}

.chat-pending-announcement {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  border: 0;
  white-space: nowrap;
}

.chat-pending-list-move {
  transition: transform var(--dur-base) var(--ease-standard);
}

.chat-pending-list-enter-active,
.chat-pending-list-leave-active {
  transition:
    opacity var(--dur-fast) var(--ease-standard),
    translate var(--dur-fast) var(--ease-standard),
    scale var(--dur-fast) var(--ease-standard);
}

.chat-pending-list-enter-from,
.chat-pending-list-leave-to {
  opacity: 0;
  translate: 0 6px;
  scale: 0.985;
}

.chat-pending-list-leave-active {
  position: absolute;
  inset-inline: 0;
}

@keyframes chat-pending-pulse {
  50% {
    opacity: 0.45;
    transform: scale(0.82);
  }
}

@keyframes chat-pending-reorder-ready {
  0% {
    translate: 0 0;
    scale: 1;
  }

  38% {
    translate: 0 -3px;
    scale: 1.045;
  }

  68% {
    translate: 0 1px;
    scale: 0.992;
  }

  100% {
    translate: 0 0;
    scale: 1.012;
  }
}

@media (max-width: 640px) {
  .chat-pending {
    width: calc(100% - 2rem);
  }

  .chat-pending-card {
    flex-wrap: wrap;
    gap: 7px 9px;
    padding-inline: 10px;
  }

  .chat-pending-text {
    flex-basis: calc(100% - 18px);
  }

  .chat-pending-actions {
    width: 100%;
    justify-content: flex-end;
    padding-top: 7px;
    border-top: 1px solid color-mix(in srgb, var(--text) 7%, transparent);
  }

  .chat-pending-action {
    min-height: 36px;
  }

  .chat-pending-action--icon {
    width: 36px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .chat-pending-card,
  .chat-pending-list-move,
  .chat-pending-list-enter-active,
  .chat-pending-list-leave-active {
    transition: none;
  }

  .chat-pending-card.is-reordering {
    animation: none;
  }

  .chat-pending-card[data-delivery-state="busy"]::before {
    animation: none;
  }
}
</style>
