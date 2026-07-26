<template>
  <div ref="composerEl" class="chat-composer" :class="{ 'chat-composer--new-landing': isNewLanding }">
    <div class="chat-composer-inner">
      <div v-if="attachments.length > 0" class="chat-attachments">
        <div
          v-for="(att, i) in attachments"
          :key="att.local_id"
          class="attachment-chip"
          :class="{ 'attachment-chip--busy': isAttachmentBusy(att), 'attachment-chip--failed': att.kind === 'failed' }"
          :data-mime="att.mime || ''"
          :title="attachmentTitle(att)"
        >
          <span class="attachment-chip__icon" aria-hidden="true">
            <span v-if="isAttachmentBusy(att)" class="spinner attachment-chip__spinner" />
            <Icon v-else-if="att.kind === 'failed'" name="info" :size="15" />
            <img v-else-if="isImageDisplayAttachment(att) && att.dataUrl" class="attachment-chip__thumb" :src="att.dataUrl" alt="" />
            <Icon v-else :name="attachmentIcon(att)" :size="15" />
          </span>
          <span class="attachment-chip__name">{{ att.name }}</span>
          <span class="attachment-chip__meta">{{ attachmentMeta(att) }}</span>
          <button v-if="att.kind === 'failed' && att.file" class="attachment-action" title="Retry upload" aria-label="Retry upload" @click="emit('retryAttachment', i)">
            <Icon name="refresh" :size="12" />
          </button>
          <button class="attachment-action attachment-remove" :title="t('chat.remove')" :aria-label="t('chat.remove')" @click="emit('removeAttachment', i)">
            <Icon name="x" :size="12" />
          </button>
        </div>
      </div>
      <div class="chat-input-panel">
        <div class="chat-input-wrap">
          <textarea
            ref="textareaEl"
            v-model="inputText"
            class="chat-textarea"
            rows="1"
            :placeholder="placeholder"
            maxlength="100000"
            :aria-label="t('chat.messageToSend')"
            :aria-describedby="sendBlockedMessage ? 'chat-composer-image-send-status' : undefined"
            @beforeinput="emit('beforeinput', $event)"
            @input="emit('input', $event)"
            @keydown="emit('keydown', $event)"
            @compositionstart="emit('compositionChange', true)"
            @compositionend="emit('compositionChange', false)"
          />
        </div>
        <div class="chat-input-footer">
          <div class="chat-input-actions chat-input-actions--left">
            <button class="btn btn--icon btn--ghost chat-plus-btn" :title="t('chat.attachFilesTitle')" :aria-label="t('chat.attachFiles')" @click="fileInputEl?.click()">
              <Icon name="plus" :size="18" />
            </button>
            <div ref="settingsAnchorEl" class="chat-settings-anchor">
              <button
                class="btn btn--icon btn--ghost"
                :title="t('chat.composerSettings')"
                :aria-label="t('chat.composerSettings')"
                :aria-expanded="settingsOpen ? 'true' : 'false'"
                @click="toggleSettings"
              >
                <Icon name="settings" :size="17" />
              </button>
              <ChatComposerSettings
                v-if="settingsOpen"
                :visual-effects-enabled="routerVisualEffectsEnabled"
                :coding-mode-enabled="codingModeEnabled"
                :coding-mode-settings-busy="codingModeSettingsBusy"
                @close="settingsOpen = false"
                @set-visual-effects-enabled="emit('setVisualEffectsEnabled', $event)"
                @set-coding-mode-enabled="emit('setCodingModeEnabled', $event)"
              />
            </div>
            <div ref="modelRoutingAnchorEl" class="chat-settings-anchor">
              <button
                class="btn btn--icon btn--ghost chat-model-routing-btn"
                :class="[
                  `chat-model-routing-btn--${modelRoutingMode}`,
                  { 'is-active': modelRoutingOpen || modelRoutingMode !== 'off' },
                ]"
                :title="t('chat.composer.modelRouting')"
                :aria-label="t('chat.composer.modelRouting')"
                :aria-expanded="modelRoutingOpen ? 'true' : 'false'"
                @click="toggleModelRouting"
              >
                <Icon name="router" :size="17" />
                <span
                  v-if="showRouterNewBadge"
                  class="chat-model-routing-btn__new"
                  aria-hidden="true"
                >{{ t('chat.composer.badgeNew') }}</span>
              </button>
              <ChatComposerModelRouting
                v-if="modelRoutingOpen"
                :model-routing-mode="modelRoutingMode"
                :busy="modelRoutingSettingsBusy"
                @close="modelRoutingOpen = false"
                @set-model-routing-mode="emit('setModelRoutingMode', $event)"
              />
            </div>
            <div ref="runModeAnchorEl" class="chat-settings-anchor">
              <button
                class="btn btn--icon btn--ghost chat-run-mode-btn"
                :class="[`chat-run-mode-btn--${runMode}`, { 'is-active': runModeOpen }]"
                :title="t('chat.composer.runMode')"
                :aria-label="t('chat.composer.runMode')"
                :aria-expanded="runModeOpen ? 'true' : 'false'"
                @click="toggleRunMode"
              >
                <Icon name="shield" :size="17" />
              </button>
              <ChatComposerRunMode
                v-if="runModeOpen"
                :run-mode="runMode"
                :allowed-run-modes="allowedRunModes"
                @close="runModeOpen = false"
                @set-run-mode="emit('setRunMode', $event)"
              />
            </div>
            <button
              class="btn btn--icon btn--ghost"
              :class="{ 'is-active': voiceRecording, 'chat-mic--needs-setup': !voiceReady }"
              :title="voiceReady ? t('chat.recordVoice') : t('chat.voiceUnavailableHint')"
              :aria-label="voiceReady ? t('chat.recordVoice') : t('chat.voiceUnavailableHint')"
              :disabled="voiceBusy"
              @click="voiceReady ? emit('voiceInput') : emit('voiceSetup')"
            >
              <Icon name="microphone" :size="17" />
            </button>
            <button class="btn btn--icon btn--ghost" :title="t('chat.exportMarkdown')" :aria-label="t('chat.exportMarkdown')" @click="emit('exportMarkdown')">
              <Icon name="download" :size="17" />
            </button>
          </div>
          <div class="chat-input-actions chat-input-actions--right">
            <Transition name="composer-ctl">
              <div v-if="isStreaming" class="chat-busy-mode" role="group" :aria-label="t('chat.deliveryModeLabel')">
                <button
                  class="chat-busy-mode__btn"
                  :class="{ 'is-active': busySendMode === 'queue' }"
                  :aria-pressed="busySendMode === 'queue' ? 'true' : 'false'"
                  :title="t('chat.queueModeHint')"
                  @click="emit('setBusySendMode', 'queue')"
                >
                  {{ t('chat.queueMode') }}
                </button>
                <button
                  class="chat-busy-mode__btn"
                  :class="{ 'is-active': busySendMode === 'steer' }"
                  :aria-pressed="busySendMode === 'steer' ? 'true' : 'false'"
                  :title="t('chat.steerModeHint')"
                  @click="emit('setBusySendMode', 'steer')"
                >
                  {{ t('chat.steerMode') }}
                </button>
              </div>
            </Transition>
            <button
              class="btn btn--icon btn--primary chat-send-btn"
              :class="{ 'is-ready': hasSendContent && !sendBlockedMessage }"
              :title="sendBlockedMessage || sendButtonTitle"
              :aria-label="t('chat.send')"
              :aria-describedby="sendBlockedMessage ? 'chat-composer-image-send-status' : undefined"
              :disabled="Boolean(sendBlockedMessage)"
              @click="emit('send')"
            >
              <Icon name="arrowUp" :size="17" />
            </button>
            <Transition name="composer-ctl">
              <button v-if="canStop" class="btn btn--icon btn--danger chat-send-btn" :title="t('chat.stopResponseEsc')" :aria-label="t('chat.stopResponse')" @click="emit('stop')">
                <Icon name="stop" :size="16" />
              </button>
            </Transition>
          </div>
        </div>
      </div>
      <p
        v-if="sendBlockedMessage"
        id="chat-composer-image-send-status"
        class="chat-composer-send-status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >{{ sendBlockedMessage }}</p>
      <p class="chat-ai-disclaimer" role="note">{{ t('chat.aiDisclaimer') }}</p>
    </div>
    <input
      ref="fileInputEl"
      type="file"
      multiple
      class="hidden"
      @change="emit('fileChange', $event)"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { IconName } from '@/utils/icons'
import ChatComposerModelRouting from '@/components/chat/ChatComposerModelRouting.vue'
import ChatComposerSettings from '@/components/chat/ChatComposerSettings.vue'
import ChatComposerRunMode from '@/components/chat/ChatComposerRunMode.vue'
import type { Attachment } from '@/types/chat'
import type { ModelRoutingMode } from '@/types/modelRouting'
import type { SandboxRunMode } from '@/types/sandbox'
import { isAttachmentBusy, isImageDisplayAttachment } from '@/utils/chat/attachments'

interface ChatComposerExpose {
  composerElement: () => HTMLElement | null
  focusTextarea: () => void
  isTextareaFocused: () => boolean
  resizeTextarea: () => void
}

defineProps<{
  attachments: Attachment[]
  busySendMode: 'queue' | 'steer'
  hasSendContent: boolean
  isStreaming: boolean
  canStop: boolean
  isNewLanding: boolean
  placeholder: string
  sendButtonTitle: string
  sendBlockedMessage?: string
  runMode: SandboxRunMode
  allowedRunModes: SandboxRunMode[]
  modelRoutingMode: ModelRoutingMode
  modelRoutingSettingsBusy: boolean
  routerVisualEffectsEnabled: boolean
  codingModeEnabled: boolean
  codingModeSettingsBusy: boolean
  voiceBusy: boolean
  voiceRecording: boolean
  voiceReady: boolean
}>()

const emit = defineEmits<{
  beforeinput: [event: InputEvent]
  compositionChange: [value: boolean]
  fileChange: [event: Event]
  input: [event: Event]
  keydown: [event: KeyboardEvent]
  removeAttachment: [index: number]
  retryAttachment: [index: number]
  send: []
  setBusySendMode: [mode: 'queue' | 'steer']
  setRunMode: [mode: 'standard' | 'trusted' | 'full']
  setModelRoutingMode: [mode: ModelRoutingMode]
  setVisualEffectsEnabled: [enabled: boolean]
  setCodingModeEnabled: [enabled: boolean]
  voiceInput: []
  voiceSetup: []
  exportMarkdown: []
  stop: []
}>()

const { t } = useI18n()

const inputText = defineModel<string>({ required: true })
const composerEl = ref<HTMLElement | null>(null)
const textareaEl = ref<HTMLTextAreaElement | null>(null)
const fileInputEl = ref<HTMLInputElement | null>(null)
const settingsOpen = ref(false)
const modelRoutingOpen = ref(false)

// "NEW" badge on the routing control — the single-model AI router is now the
// default, so flag it until the user first opens the control, then never again.
const ROUTER_NEW_BADGE_KEY = 'opensquilla.composer.routerNewBadgeSeen'
const routerNewBadgeSeen = ref(false)
try {
  routerNewBadgeSeen.value = localStorage.getItem(ROUTER_NEW_BADGE_KEY) === '1'
} catch { /* localStorage unavailable */ }
const showRouterNewBadge = computed(() => !routerNewBadgeSeen.value)
function dismissRouterNewBadge() {
  if (routerNewBadgeSeen.value) return
  routerNewBadgeSeen.value = true
  try {
    localStorage.setItem(ROUTER_NEW_BADGE_KEY, '1')
  } catch { /* localStorage unavailable */ }
}
const runModeOpen = ref(false)
const settingsAnchorEl = ref<HTMLElement | null>(null)
const modelRoutingAnchorEl = ref<HTMLElement | null>(null)
const runModeAnchorEl = ref<HTMLElement | null>(null)

const anyPopoverOpen = computed(() => settingsOpen.value || modelRoutingOpen.value || runModeOpen.value)

function eventInsideRoot(event: PointerEvent, root: HTMLElement | null): boolean {
  if (!root) return false
  const path = typeof event.composedPath === 'function' ? event.composedPath() : []
  if (path.includes(root)) return true
  return event.target instanceof Node && root.contains(event.target)
}

function closeOpenPopoversFromOutside(event: PointerEvent) {
  if (settingsOpen.value && !eventInsideRoot(event, settingsAnchorEl.value)) {
    settingsOpen.value = false
  }
  if (modelRoutingOpen.value && !eventInsideRoot(event, modelRoutingAnchorEl.value)) {
    modelRoutingOpen.value = false
  }
  if (runModeOpen.value && !eventInsideRoot(event, runModeAnchorEl.value)) {
    runModeOpen.value = false
  }
}

watch(anyPopoverOpen, (open) => {
  if (open) {
    document.addEventListener('pointerdown', closeOpenPopoversFromOutside, true)
  } else {
    document.removeEventListener('pointerdown', closeOpenPopoversFromOutside, true)
  }
}, { immediate: true })

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', closeOpenPopoversFromOutside, true)
})

function toggleSettings() {
  settingsOpen.value = !settingsOpen.value
  if (settingsOpen.value) {
    modelRoutingOpen.value = false
    runModeOpen.value = false
  }
}

function toggleModelRouting() {
  modelRoutingOpen.value = !modelRoutingOpen.value
  if (modelRoutingOpen.value) {
    dismissRouterNewBadge()
    settingsOpen.value = false
    runModeOpen.value = false
  }
}

function toggleRunMode() {
  runModeOpen.value = !runModeOpen.value
  if (runModeOpen.value) {
    settingsOpen.value = false
    modelRoutingOpen.value = false
  }
}

function attachmentIcon(att: Attachment): IconName {
  return isImageDisplayAttachment(att) ? 'image' : 'fileText'
}

function attachmentMeta(att: Attachment): string {
  if (att.kind === 'failed') return att.error ? `FAILED · ${att.error}` : 'FAILED'
  const mime = att.mime || ''
  const subtype = mime.includes('/') ? mime.split('/')[1] : mime
  const label = subtype ? subtype.toUpperCase() : 'FILE'
  const size = typeof att.size === 'number'
    ? `${Math.max(1, Math.round(att.size / 1024))} KB`
    : ''
  return [label, size].filter(Boolean).join(' · ')
}

function attachmentTitle(att: Attachment): string {
  if (att.kind === 'failed') {
    return att.error ? `${att.name}: ${att.error}` : `${att.name}: failed`
  }
  return att.name
}

function composerElement(): HTMLElement | null {
  return composerEl.value
}

function documentCanReceiveFocus(): boolean {
  return document.visibilityState === 'visible' && document.hasFocus()
}

function focusTextarea() {
  // Programmatic focus must never reactivate a background/minimized browser
  // window. Recheck inside nextTick because the page can lose focus between
  // scheduling and execution (GitHub issue 382).
  if (!documentCanReceiveFocus()) return
  nextTick(() => {
    if (documentCanReceiveFocus()) textareaEl.value?.focus()
  })
}

function isTextareaFocused(): boolean {
  return document.activeElement === textareaEl.value
}

function resizeTextarea() {
  nextTick(() => {
    const ta = textareaEl.value
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'
  })
}

defineExpose<ChatComposerExpose>({
  composerElement,
  focusTextarea,
  isTextareaFocused,
  resizeTextarea,
})
</script>

<style scoped>
.hidden {
  display: none !important;
}

.chat-composer {
  padding: 0.75rem 1.5rem 1.875rem;
  border-top: 0;
  background: var(--bg-surface);
  flex-shrink: 0;
}

.chat-composer--new-landing {
  width: min(calc(100% - 48px), 820px);
  margin: 0 auto;
  padding: 0;
  background: transparent;
}

.chat-composer-inner {
  width: min(100%, var(--composer-col, 820px));
  margin: 0 auto;
}

.chat-composer--new-landing .chat-composer-inner {
  width: 100%;
}

.chat-ai-disclaimer {
  margin: 0.5rem 0 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
  text-align: center;
}

.chat-composer-send-status {
  margin: 0.5rem 0 0;
  color: var(--warning, var(--text-muted));
  font-size: var(--fs-sm);
  line-height: 1.5;
  text-align: center;
}

.chat-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 0.375rem;
  margin-bottom: 0.5rem;
}

.attachment-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.25rem 0.5rem;
  max-width: min(100%, 360px);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  font-size: 0.8125rem;
}

.attachment-chip--busy {
  opacity: 0.7;
}

.attachment-chip--failed {
  border-color: color-mix(in srgb, var(--danger) 38%, var(--border));
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-elevated));
}

.attachment-chip--failed .attachment-chip__icon,
.attachment-chip--failed .attachment-chip__meta {
  color: var(--danger);
}

.attachment-chip__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  color: var(--text-muted);
}

.attachment-chip__thumb {
  width: 16px;
  height: 16px;
  border-radius: var(--radius-sm);
  object-fit: cover;
}

.attachment-chip__spinner {
  width: 12px;
  height: 12px;
  border: 2px solid var(--text-muted);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.attachment-chip__name {
  font-weight: 500;
  max-width: 150px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-chip__meta {
  color: var(--text-dim);
  font-size: 0.6875rem;
  min-width: 0;
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 16px;
  padding: 0;
  width: 16px;
  height: 16px;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-muted);
  font-size: 0.875rem;
}

.attachment-action:hover {
  color: var(--text);
}

.chat-input-panel {
  display: flex;
  flex-direction: column;
  min-height: 128px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-modal);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xs);
  position: relative;
}

.chat-composer--new-landing .chat-input-panel {
  min-height: 168px;
  border-color: var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--shadow-lg);
}

.chat-composer--new-landing .chat-input-panel:focus-within {
  border-color: var(--border-focus);
  box-shadow: var(--shadow-xl);
}

.chat-input-footer,
.chat-input-actions {
  display: flex;
  align-items: center;
}

.chat-input-footer {
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.25rem 0.625rem 0.625rem;
}

.chat-input-actions {
  gap: 0.25rem;
  min-width: 0;
}

.chat-settings-anchor {
  position: relative;
  display: inline-flex;
}

.chat-input-actions--right {
  flex-shrink: 0;
}

.chat-busy-mode {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  padding: 2px;
  gap: 2px;
  margin-right: var(--sp-1);
}

.chat-busy-mode__btn {
  border: 0;
  background: none;
  border-radius: var(--radius-full);
  padding: 0.125rem 0.5rem;
  font-size: var(--fs-xs);
  font-weight: 600;
  line-height: 1.4;
  color: var(--text-muted);
  cursor: pointer;
  transition: var(--transition);
}

.chat-busy-mode__btn:hover {
  color: var(--text);
}

.chat-busy-mode__btn.is-active {
  background: color-mix(in srgb, var(--accent) 14%, var(--bg-surface));
  color: var(--accent);
}

.chat-input-wrap {
  flex: 1;
  min-width: 0;
  display: flex;
}

.chat-textarea {
  width: 100%;
  min-height: 68px;
  max-height: 160px;
  padding: 1rem 1rem 0.375rem;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--text);
  font-size: 0.9375rem;
  line-height: 1.5;
  resize: none;
  outline: none;
  font-family: inherit;
}

.chat-composer--new-landing .chat-textarea {
  min-height: 108px;
  padding: 1.25rem 1.5rem 0.5rem;
  font-size: 1rem;
}

.chat-textarea:focus {
  border-color: transparent;
  box-shadow: none;
}

.chat-input-panel:focus-within {
  border-color: var(--border-focus);
  box-shadow: var(--shadow-sm);
}

.btn--icon {
  width: 34px;
  height: 34px;
  min-width: 34px;
  min-height: 34px;
  border-radius: var(--radius-full);
  padding: 0;
}

.chat-plus-btn {
  color: var(--text-muted);
}

.btn--ghost.is-active {
  background: color-mix(in srgb, var(--ok) 12%, var(--bg-surface));
  color: var(--ok);
}

/* Voice not configured: keep the button clickable (it routes to setup) but
   dim it so it still reads as "not active"; brighten on hover to invite it. */
.chat-mic--needs-setup {
  opacity: var(--state-disabled-opacity);
}

.chat-mic--needs-setup:hover {
  opacity: 1;
}

.chat-model-routing-btn {
  position: relative;
  border-color: transparent;
  background: transparent;
  color: var(--text-muted);
}

.chat-model-routing-btn__new {
  position: absolute;
  top: -3px;
  right: -5px;
  padding: 1px 4px;
  border-radius: 999px;
  background: var(--accent);
  color: var(--bg-surface);
  font-size: 8px;
  font-weight: 600;
  line-height: 1.25;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  pointer-events: none;
}

.chat-model-routing-btn.btn--ghost:not(:disabled):hover {
  border-color: color-mix(in srgb, var(--accent) 18%, transparent);
  background: color-mix(in srgb, var(--accent) 6%, var(--bg-surface));
  color: var(--accent);
}

.chat-model-routing-btn--off.btn--ghost:not(:disabled):hover {
  border-color: color-mix(in srgb, var(--text-dim) 14%, transparent);
  background: color-mix(in srgb, var(--text-dim) 6%, var(--bg-surface));
  color: var(--text-muted);
}

.chat-model-routing-btn.btn--ghost.is-active {
  border-color: color-mix(in srgb, var(--accent) 24%, transparent);
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-surface));
  color: var(--accent);
}

.chat-model-routing-btn--off.btn--ghost.is-active {
  border-color: color-mix(in srgb, var(--text-dim) 18%, transparent);
  background: color-mix(in srgb, var(--text-dim) 8%, var(--bg-surface));
  color: var(--text-muted);
}

.chat-model-routing-btn--squilla_router.btn--ghost.is-active::after {
  content: "";
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 6px;
  height: 2px;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--accent) 62%, transparent);
}

.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active {
  border-color: color-mix(in srgb, var(--accent) 30%, transparent);
  background: color-mix(in srgb, var(--accent) 11%, var(--bg-surface));
}

.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active::before,
.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active::after {
  content: "";
  position: absolute;
  bottom: 6px;
  width: 6px;
  height: 2px;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--accent) 62%, transparent);
}

.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active::before {
  left: 10px;
}

.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active::after {
  right: 10px;
}

.chat-run-mode-btn {
  --run-mode-tone: var(--text-muted);
  --run-mode-tint: transparent;
  --run-mode-border: transparent;
  --run-mode-marker: var(--text-dim);
  position: relative;
  border-color: var(--run-mode-border);
  background: var(--run-mode-tint);
  color: var(--run-mode-tone);
}

.chat-run-mode-btn::after {
  content: "";
  position: absolute;
  right: 7px;
  bottom: 7px;
  width: 6px;
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--run-mode-marker);
  box-shadow: 0 0 0 2px var(--bg-surface);
}

.chat-run-mode-btn--trusted {
  --run-mode-tone: var(--ok);
  --run-mode-tint: color-mix(in srgb, var(--ok) 12%, var(--bg-surface));
  --run-mode-border: color-mix(in srgb, var(--ok) 34%, transparent);
  --run-mode-marker: var(--ok);
}

.chat-run-mode-btn--full {
  --run-mode-tone: color-mix(in srgb, var(--warn) 72%, var(--text-muted));
  --run-mode-tint: color-mix(in srgb, var(--warn) 5%, var(--bg-surface));
  --run-mode-border: color-mix(in srgb, var(--warn) 18%, transparent);
  --run-mode-marker: color-mix(in srgb, var(--warn-fill) 70%, var(--text-dim));
}

.chat-run-mode-btn.btn--ghost:not(:disabled):hover,
.chat-run-mode-btn.btn--ghost.is-active {
  border-color: var(--run-mode-border);
  background: color-mix(in srgb, var(--run-mode-marker) 16%, var(--bg-surface));
  color: var(--run-mode-tone);
}

.chat-send-btn.btn--primary {
  background: var(--bg-hover);
  color: var(--text-dim);
  border-color: var(--bg-hover);
}

.chat-send-btn.btn--primary:hover {
  background: var(--bg-hover);
  border-color: var(--bg-hover);
}

.chat-send-btn.btn--primary.is-ready {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-foreground);
}

.chat-send-btn.btn--primary.is-ready:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Streaming-only controls (Stop, Queue/Steer) ease in/out at turn boundaries
   instead of popping into the action cluster. */
.composer-ctl-enter-active,
.composer-ctl-leave-active {
  transition: opacity var(--dur-fast) var(--ease-out),
              transform var(--dur-fast) var(--ease-out);
}
.composer-ctl-enter-from,
.composer-ctl-leave-to {
  opacity: 0;
  transform: scale(0.9);
}

@media (prefers-reduced-motion: reduce) {
  .composer-ctl-enter-active,
  .composer-ctl-leave-active {
    transition: none;
  }
}
</style>
