<template>
  <div
    class="msg-user"
    :class="{ 'msg-user--share-mode': shareMode, 'msg-user--share-selected': shareSelected }"
    :data-message-id="message.messageId"
    :data-share-message-id="shareMessageId"
    :data-share-selected="shareSelected ? 'true' : undefined"
    @click="onMessageClick"
  >
    <button
      v-if="shareMode"
      type="button"
      class="chat-share-picker"
      :class="{ 'is-selected': shareSelected }"
      :aria-pressed="shareSelected"
      :title="shareSelected ? t('chat.removeFromShare') : t('chat.addToShare')"
      :aria-label="shareSelected ? t('chat.removeFromShare') : t('chat.addToShare')"
      @click.stop="emit('toggleShare', shareMessageId)"
    >
      <Icon v-if="shareSelected" name="check" :size="13" />
    </button>
    <!-- The container grammar: attachments are standalone objects stacked
         above the text bubble, never packed inside it — text gets a filled
         bubble, images render as bordered bare media, files as icon chips. -->
    <div class="msg-user-stack">
      <div v-if="message.attachments?.length" class="msg-attachments">
        <template v-for="attachment in message.attachments" :key="attachment.renderKey">
          <button
            v-if="isImageDisplayAttachment(attachment) && (attachment.dataUrl || attachment.data)"
            type="button"
            class="msg-thumb-button"
            :title="attachmentDownloadLabel(attachment)"
            :aria-label="attachmentDownloadLabel(attachment)"
            :aria-busy="downloadingAttachments.has(attachment.renderKey)"
            :disabled="downloadingAttachments.has(attachment.renderKey)"
            @click.stop="downloadAttachment(attachment)"
          >
            <img
              class="msg-thumb"
              :src="attachmentImageSrc(attachment)"
              :alt="attachment.name"
            />
            <span v-if="downloadingAttachments.has(attachment.renderKey)" class="msg-thumb-button__busy" aria-hidden="true">
              <span class="spinner msg-file-chip__spinner" />
            </span>
          </button>
          <button
            v-else
            type="button"
            class="msg-file-chip"
            :class="{ 'msg-file-chip--failed': failedDownloads.has(attachment.renderKey) }"
            :title="attachmentDownloadLabel(attachment)"
            :aria-label="attachmentDownloadLabel(attachment)"
            :aria-busy="downloadingAttachments.has(attachment.renderKey)"
            :disabled="downloadingAttachments.has(attachment.renderKey)"
            @click.stop="downloadAttachment(attachment)"
          >
            <span class="msg-file-chip__icon" aria-hidden="true">
              <span v-if="downloadingAttachments.has(attachment.renderKey)" class="spinner msg-file-chip__spinner" />
              <Icon v-else-if="failedDownloads.has(attachment.renderKey)" name="refresh" :size="16" />
              <Icon v-else name="fileText" :size="16" />
            </span>
            <span class="msg-file-chip__body">
              <span class="msg-file-chip__name">{{ attachment.name }}</span>
              <span class="msg-file-chip__meta">{{ attachmentMeta(attachment) }}</span>
            </span>
          </button>
        </template>
      </div>
      <div v-if="message.text" class="msg-user-bubble">
        {{ stripTimePrefix(message.text) }}
      </div>
    </div>
    <div v-if="!shareMode" class="msg-user-actions">
      <button
        type="button"
        class="msg-action"
        :class="{ 'msg-action--ok': copyState === 'ok', 'msg-action--err': copyState === 'err' }"
        :title="copyTitle"
        @click="onCopyClick"
      >
        <Icon :name="copyIconName" :size="12" />
      </button>
      <span class="msg-copy-live" aria-live="polite">{{ copyLiveText }}</span>
      <button type="button" class="msg-action" :title="t('chat.edit')" :aria-label="t('chat.edit')" @click="$emit('edit', message)">
        <Icon name="edit" :size="12" />
      </button>
      <time v-if="timeIso" class="msg-time" :datetime="timeIso" :title="timeFull">
        <span class="msg-time__abs">{{ timeAbs }}</span>
        <span class="msg-time__dot" aria-hidden="true">·</span>
        <span class="msg-time__rel">{{ timeRel }}</span>
      </time>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useCopyFeedback } from '@/composables/chat/useCopyFeedback'
import { useRelativeNow } from '@/composables/useRelativeNow'
import type { ChatRenderedMessage, DisplayAttachment } from '@/types/chat'
import { isImageDisplayAttachment } from '@/utils/chat/attachments'
import { absoluteTime, fullTime, isoTime, relativeTime } from '@/utils/messageTime'

const { t } = useI18n()

const props = defineProps<{
  message: ChatRenderedMessage
  shareMode: boolean
  shareSelected: boolean
  shareMessageId: string
  stripTimePrefix: (text: string) => string
  copyMessage: (message: ChatRenderedMessage) => Promise<boolean>
  downloadAttachment: (attachment: DisplayAttachment) => Promise<boolean>
}>()

const emit = defineEmits<{
  edit: [message: ChatRenderedMessage]
  toggleShare: [messageId: string]
}>()

const { copyState, copyIconName, copyTitle, copyLiveText, onCopyClick } = useCopyFeedback(
  () => props.copyMessage(props.message),
)

// Absolute label is static; only the relative label subscribes to the shared
// clock, so a tick re-evaluates one cheap computed per visible bubble.
const now = useRelativeNow()
const timeIso = computed(() => isoTime(props.message.ts))
const timeAbs = computed(() => absoluteTime(props.message.ts))
const timeRel = computed(() => relativeTime(props.message.ts, now.value))
const timeFull = computed(() => fullTime(props.message.ts))
const downloadingAttachments = reactive(new Set<string>())
const failedDownloads = reactive(new Set<string>())

function onMessageClick(event: MouseEvent) {
  if (!props.shareMode) return
  if ((event.target as HTMLElement | null)?.closest('button,a,input,textarea,select')) return
  emit('toggleShare', props.shareMessageId)
}

function attachmentImageSrc(attachment: DisplayAttachment): string {
  return attachment.dataUrl || `data:${attachment.mime || 'image/png'};base64,${attachment.data || ''}`
}

function attachmentDownloadLabel(attachment: DisplayAttachment): string {
  return failedDownloads.has(attachment.renderKey)
    ? `${t('chat.retry')} ${attachment.name}`
    : t('chat.downloadTitle', { title: attachment.name })
}

async function downloadAttachment(attachment: DisplayAttachment) {
  const key = attachment.renderKey
  if (downloadingAttachments.has(key)) return
  downloadingAttachments.add(key)
  failedDownloads.delete(key)
  try {
    if (!await props.downloadAttachment(attachment)) failedDownloads.add(key)
  } catch {
    failedDownloads.add(key)
  } finally {
    downloadingAttachments.delete(key)
  }
}

function attachmentMeta(attachment: DisplayAttachment): string {
  const mime = attachment.mime || 'attachment'
  const subtype = mime.includes('/') ? mime.split('/').pop() || mime : mime
  const label = (subtype.includes('.') ? subtype.split('.').pop() || subtype : subtype).toUpperCase()
  // Same meta idiom as artifact file cards: `TYPE · N KB` (utils/chat/artifacts.ts).
  const size = Number(attachment.size)
  if (!Number.isFinite(size) || size <= 0) return label
  return `${label} · ${Math.max(1, Math.round(size / 1024))} KB`
}
</script>

<style scoped>
.msg-user {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  /* Shared conversation column, defined on .chat — keeps user bubbles in the
     same column as assistant content at every viewport width. */
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  margin: 0 auto;
  padding: 0.5rem 0;
  max-width: calc(100% - 48px);
}

.msg-user:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 0.25rem;
  border-radius: var(--radius-md);
}

.msg-user--share-mode {
  cursor: pointer;
  width: min(calc(100% - 16px), 1012px);
  max-width: calc(100% - 16px);
  box-sizing: border-box;
  padding: 0.5rem 2.5rem 0.5rem 1rem;
  border-radius: var(--radius-lg);
  transition: background var(--dur-base) var(--ease-standard), box-shadow var(--dur-base) var(--ease-standard);
}

.msg-user--share-mode:hover {
  background: color-mix(in srgb, var(--accent) 5%, transparent);
}

.msg-user--share-selected {
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  box-shadow: inset 0 0 0 2px var(--accent);
}

/* Checkbox-style selection indicator: empty outlined circle when unselected,
   accent-filled with a check when selected. Always visible in share mode. */
.chat-share-picker {
  position: absolute;
  right: 0.45rem;
  top: 0.65rem;
  z-index: 2;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.5rem;
  height: 1.5rem;
  border: 2px solid var(--border-strong);
  border-radius: var(--radius-full);
  background: var(--bg-surface);
  color: var(--text-muted);
  box-shadow: var(--shadow-md);
  cursor: pointer;
  transition: transform var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard), background var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.chat-share-picker:hover {
  transform: translateY(-1px);
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border-strong));
}

.chat-share-picker:focus-visible {
  outline: none;
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}

.chat-share-picker.is-selected {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--accent-foreground);
}

@media (prefers-reduced-motion: reduce) {
  .chat-share-picker {
    transition: none;
  }
}

/* Stretch to the full conversation column so the 82% caps on the bubble and
   the attachment row resolve against the column, not shrink-to-fit content. */
.msg-user-stack {
  position: relative;
  align-self: stretch;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 0.375rem;
  min-width: 0;
}

/* Arrival feedback stays local to the destination instead of washing the full
   conversation row with accent color. The guide glides in beside the user
   bubble, settles, then fades, preserving orientation without a screen flash. */
.msg-user.is-history-target .msg-user-stack::after {
  position: absolute;
  inset: 0.375rem -0.625rem 0.375rem auto;
  width: 2px;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--accent) 78%, transparent);
  content: '';
  pointer-events: none;
  transform-origin: center;
  animation: history-target-arrival calc(var(--dur-base) * 3) var(--ease-out) both;
}

@keyframes history-target-arrival {
  0% {
    opacity: 0;
    transform: translateX(4px) scaleY(0.55);
  }

  24% {
    opacity: 0.78;
    transform: translateX(0) scaleY(1);
  }

  68% {
    opacity: 0.78;
    transform: translateX(0) scaleY(1);
  }

  100% {
    opacity: 0;
    transform: translateX(0) scaleY(0.88);
  }
}

@media (prefers-reduced-motion: reduce) {
  .msg-user.is-history-target .msg-user-stack::after {
    animation: none;
    opacity: 0.78;
    transform: none;
  }
}

@media (forced-colors: active) {
  .msg-user.is-history-target .msg-user-stack::after {
    background: Highlight;
  }
}

.msg-user-bubble {
  background: var(--msg-bubble);
  color: var(--text);
  padding: 0.5625rem 0.875rem;
  border-radius: var(--radius-panel);
  font-size: 0.875rem;
  line-height: 1.5;
  max-width: 82%;
  white-space: pre-wrap;
  word-break: break-word;
}

.msg-user-actions {
  display: flex;
  gap: 0.125rem;
  margin-top: 0.125rem;
  opacity: 0;
  transition: opacity var(--dur-fast);
  justify-content: flex-end;
}

.msg-user:hover .msg-user-actions {
  opacity: 1;
}

/* Touch screens have no hover to reveal the row — keep it visible there. */
@media (hover: none) {
  .msg-user-actions {
    opacity: 1;
  }
}

.msg-time {
  display: inline-flex;
  align-items: baseline;
  gap: 0.25rem;
  margin-left: 0.25rem;
  align-self: center;
  font-size: var(--fs-xs);
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.msg-time__rel {
  color: color-mix(in srgb, var(--text-dim) 80%, transparent);
}

.msg-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0.125rem;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-dim);
  border-radius: var(--radius-sm);
  font-size: 0.6875rem;
}

.msg-action:hover {
  color: var(--text-muted);
  background: var(--bg-hover);
}

.msg-action.msg-action--ok,
.msg-action.msg-action--ok:hover {
  color: var(--ok);
}

.msg-action.msg-action--err,
.msg-action.msg-action--err:hover {
  color: var(--danger);
}

.msg-copy-live {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.msg-attachments {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.375rem;
  max-width: 82%;
}

/* Bare media object: the 1px border keeps white-ish screenshots from
   dissolving into a light canvas. */
.msg-thumb {
  display: block;
  max-width: 200px;
  max-height: 200px;
  border: 1px solid var(--msg-obj-border);
  border-radius: var(--radius-card);
  object-fit: cover;
}

.msg-thumb-button {
  position: relative;
  appearance: none;
  padding: 0;
  border: 0;
  border-radius: var(--radius-card);
  background: transparent;
  cursor: pointer;
}

.msg-thumb-button:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.msg-thumb-button:disabled {
  cursor: wait;
}

.msg-thumb-button__busy {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  border-radius: var(--radius-card);
  background: color-mix(in srgb, var(--bg-surface) 72%, transparent);
  color: var(--accent);
}

.msg-file-chip__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 2rem;
  width: 2rem;
  height: 2rem;
  background: color-mix(in srgb, var(--accent) 12%, var(--bg-surface));
  border-radius: var(--radius-control);
  color: var(--accent);
}

.msg-file-chip {
  appearance: none;
  display: inline-flex;
  align-items: center;
  gap: 0.625rem;
  min-width: min(15rem, 100%);
  max-width: min(100%, 24rem);
  padding: 0.4375rem 0.875rem 0.4375rem 0.4375rem;
  border: 1px solid var(--msg-obj-border);
  border-radius: var(--radius-card);
  background: var(--bg-surface);
  color: inherit;
  font: inherit;
  font-size: 0.8125rem;
  text-align: left;
  cursor: pointer;
}

.msg-file-chip:hover:not(:disabled) {
  border-color: var(--border-strong);
  box-shadow: var(--shadow-sm);
}

.msg-file-chip:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.msg-file-chip:disabled {
  cursor: wait;
  opacity: 0.72;
}

.msg-file-chip--failed {
  border-color: color-mix(in srgb, var(--danger) 45%, var(--border));
}

.msg-file-chip__spinner {
  width: 1rem;
  height: 1rem;
}

.msg-file-chip__body {
  display: grid;
  gap: 0.0625rem;
  min-width: 0;
}

.msg-file-chip__name {
  font-weight: 500;
  line-height: 1.25;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-file-chip__meta {
  font-family: var(--font-mono);
  font-size: 0.6875rem;
  font-weight: 500;
  letter-spacing: 0.06em;
  color: var(--text-dim);
  line-height: 1.2;
  text-transform: uppercase;
}

@media (max-width: 640px) {
  .msg-user--share-mode {
    width: min(calc(100% - 12px), 1012px);
    max-width: calc(100% - 12px);
    padding: 0.5rem 2.25rem 0.5rem 0.75rem;
  }

  .chat-share-picker {
    right: 0.35rem;
  }

  .msg-user-bubble,
  .msg-attachments {
    max-width: 90%;
  }
}
</style>
