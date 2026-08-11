<template>
  <section
    v-if="items.length > 0"
    class="chat-pending"
    :aria-label="t('chat.pending.label', { count: items.length, max: effectiveMaxPending })"
  >
    <article
      v-for="(item, index) in items"
      :key="index"
      class="chat-pending-card"
      :aria-busy="isSteering(item) ? 'true' : undefined"
      :aria-describedby="attachmentBlockMessage(item) ? attachmentStatusId(index) : undefined"
    >
      <p class="chat-pending-text" :title="displayText(item)">
        {{ displayText(item) }}
      </p>
      <span v-if="item.attachments?.length" class="chat-pending-attachments">
        {{ item.attachments.length }} · 📎
        <span
          v-if="attachmentBlockMessage(item)"
          :id="attachmentStatusId(index)"
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
          :aria-describedby="attachmentBlockMessage(item) ? attachmentStatusId(index) : undefined"
          @click="emit('steer', index)"
        >
          <span aria-hidden="true">↪</span>
          <span>{{ steerActionLabel(item) }}</span>
        </button>
        <button
          type="button"
          class="chat-pending-action chat-pending-action--icon"
          :aria-label="removeLabel(item, index)"
          :title="removeLabel(item, index)"
          :disabled="isSteering(item)"
          @click="emit('remove', index)"
        >
          <Icon name="trash" :size="14" />
        </button>
        <div v-if="!item.hiddenControl" class="chat-pending-more-wrap">
          <button
            type="button"
            class="chat-pending-action chat-pending-action--icon"
            :class="{ 'is-active': openMenuIndex === index }"
            :aria-label="t('chrome.more')"
            :title="t('chrome.more')"
            aria-haspopup="menu"
            :aria-expanded="openMenuIndex === index && !isSteering(item) ? 'true' : 'false'"
            :disabled="isSteering(item)"
            @click.stop="toggleMenu(index)"
          >
            <Icon name="moreHorizontal" :size="16" />
          </button>
          <div
            v-if="openMenuIndex === index && !isSteering(item)"
            class="chat-pending-menu"
            role="menu"
            :aria-label="t('chrome.more')"
          >
            <button
              type="button"
              role="menuitem"
              :disabled="!!item.deliveryState || !!item.steerAttempt"
              @click="chooseEdit(index)"
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
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
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
  text: string
  displayTextOverride?: string
  hiddenControl?: boolean
  attachments?: Attachment[]
  deliveryState?: 'steering' | 'retryable'
  steerAttempt?: PendingSteerAttempt
}

type PendingSteerBlocker =
  | 'controlInput'
  | 'attachment'
  | 'capability'
  | 'otherDelivery'
  | 'steering'

const props = defineProps<{
  items: PendingQueueItem[]
  maxPending: number
  imageBlockedMessage?: string
  steerAvailable?: boolean
  steerUnavailableMessage?: string
}>()

const emit = defineEmits<{
  clear: []
  edit: [index: number]
  remove: [index: number]
  steer: [index: number]
}>()

const openMenuIndex = ref<number | null>(null)
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

function isSteering(item: PendingQueueItem): boolean {
  return item.deliveryState === 'steering' || item.steerAttempt?.phase === 'submitting'
}

function isSteerRetry(item: PendingQueueItem): boolean {
  return item.steerAttempt?.phase === 'retryable_rejected'
    || item.steerAttempt?.phase === 'acceptance_unknown'
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
  return item.attachments?.some(attachment => !isSendableAttachment(attachment)) === true
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
  return pendingSteerBlocker(item) !== null
}

function steerTitle(item: PendingQueueItem): string {
  switch (pendingSteerBlocker(item)) {
    case 'controlInput':
      return t('chat.sendQueues')
    case 'attachment':
      return attachmentBlockMessage(item) || t('chat.pending.steerUnavailable.attachment')
    case 'capability':
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

function attachmentStatusId(index: number): string {
  return `chat-pending-attachment-status-${index}`
}

function toggleMenu(index: number) {
  if (props.items[index]?.deliveryState === 'steering') return
  openMenuIndex.value = openMenuIndex.value === index ? null : index
}

function chooseEdit(index: number) {
  openMenuIndex.value = null
  if (props.items[index]?.deliveryState || props.items[index]?.steerAttempt) return
  emit('edit', index)
}

function chooseClear() {
  openMenuIndex.value = null
  emit('clear')
}

useDocumentEvent('pointerdown', (event) => {
  const target = event.target
  if (target instanceof Element && target.closest('.chat-pending-more-wrap')) return
  openMenuIndex.value = null
})

useDocumentEvent('keydown', (event) => {
  if (event.key !== 'Escape' || openMenuIndex.value === null) return
  event.preventDefault()
  openMenuIndex.value = null
})
</script>

<style scoped>
.chat-pending {
  position: relative;
  z-index: 1;
  display: grid;
  gap: 6px;
  width: min(calc(100% - 3rem), var(--composer-col, 820px));
  margin: 0 auto -10px;
  padding: 0;
}

.chat-pending-card {
  position: relative;
  display: flex;
  align-items: center;
  min-height: 48px;
  gap: 8px;
  padding: 8px 12px 13px;
  border: 1px solid color-mix(in srgb, var(--text) 9%, transparent);
  border-radius: var(--radius-lg) var(--radius-lg) var(--radius-md) var(--radius-md);
  background: color-mix(in srgb, var(--bg-surface) 98%, var(--bg-surface-2));
  box-shadow:
    inset 0 1px 0 var(--elev-highlight),
    0 10px 26px -22px color-mix(in srgb, var(--text) 34%, transparent);
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
  gap: 2px;
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
  font-size: var(--fs-xs);
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

@media (max-width: 640px) {
  .chat-pending {
    width: calc(100% - 2rem);
  }

  .chat-pending-card {
    padding-inline: 10px;
  }
}
</style>
