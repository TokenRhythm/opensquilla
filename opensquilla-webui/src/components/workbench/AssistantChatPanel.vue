<template>
  <section class="assistant-chat-panel" data-testid="assistant-chat-panel">
    <p class="assistant-chat-panel__hint">{{ t('workbench.assistantChat.contextHint') }}</p>
    <div ref="transcript" class="assistant-chat-panel__messages" role="log" :aria-label="t('workbench.newPanel.assistantChat')" aria-live="polite">
      <p v-if="!state.messages.length" class="assistant-chat-panel__empty">{{ t('workbench.assistantChat.empty') }}</p>
      <article v-for="message in state.messages" :key="message.id" :class="['assistant-chat-panel__message', `is-${message.role}`]">
        <span class="sr-only">{{ message.role === 'user' ? t('workbench.assistantChat.you') : t('workbench.newPanel.assistantChat') }}</span>
        <p v-if="message.role === 'user'">{{ message.text }}</p>
        <StreamingTextPart v-else :raw-text="message.text" :render-markdown="renderMarkdown" />
        <div v-if="message.previewComplete === false" class="assistant-chat-panel__preview" data-testid="assistant-chat-preview">
          <span>{{ t('workbench.assistantChat.preview') }}</span>
          <button type="button" class="btn btn--ghost" @click="openFullConversation">{{ t('workbench.assistantChat.openFull') }}</button>
        </div>
      </article>
    </div>
    <div class="assistant-chat-panel__status" role="status">
      <span v-if="!available">{{ t('workbench.newPanel.requiresConnection') }}</span>
      <span v-else-if="state.phase === 'loading'">{{ t('workbench.assistantChat.loading') }}</span>
      <span v-else-if="state.phase === 'sending' || state.phase === 'running'">{{ t('workbench.assistantChat.working') }}</span>
      <button v-if="state.childKey" type="button" class="btn btn--ghost" @click="openFullConversation">{{ t('workbench.assistantChat.openFull') }}</button>
    </div>
    <div v-if="state.error || state.needsAttention || state.uncertainDelivery" class="assistant-chat-panel__notice" role="alert">
      <p v-if="state.error">{{ state.error }}</p>
      <p v-if="state.needsAttention">{{ t('workbench.assistantChat.needsAttention') }}</p>
      <p v-if="state.uncertainDelivery">{{ t('workbench.assistantChat.uncertain') }}</p>
      <button v-if="available && state.childKey && !state.requiresNewSession" type="button" class="btn btn--ghost" @click="refresh">{{ t('workbench.assistantChat.refresh') }}</button>
      <button v-if="available && state.requiresNewSession" type="button" class="btn btn--ghost" @click="startNew">{{ t('workbench.assistantChat.newConversation') }}</button>
    </div>
    <form class="assistant-chat-panel__composer" @submit.prevent="send">
      <textarea v-model="draft" rows="3" :aria-label="t('workbench.assistantChat.placeholder')" :placeholder="t('workbench.assistantChat.placeholder')" :disabled="!available || state.requiresNewSession" @keydown="onKeydown" />
      <button v-if="state.phase === 'running'" type="button" class="btn btn--ghost" :disabled="!available" @click="controller?.stop()">{{ t('workbench.assistantChat.stop') }}</button>
      <button v-else type="submit" class="btn btn--primary" :disabled="!canSend">{{ t('workbench.assistantChat.send') }}</button>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { SESSION_LIFECYCLE_KEY } from '@/modules/sessionLifecycle'
import { SESSION_READ_LIFECYCLE_FACTORY_KEY } from '@/modules/sessionReadLifecycle'
import { TURN_COMMANDS_KEY } from '@/modules/turnCommands'
import { CONVERSATION_EVENTS_KEY } from '@/modules/conversationEvents'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { createAssistantChat, type AssistantChatState } from '@/modules/assistantChat'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import { useChatMessageContent } from '@/composables/chat/useChatMessageContent'
import StreamingTextPart from '@/components/chat/parts/StreamingTextPart.vue'

const props = defineProps<{ parentSessionKey: string; workspaceId?: string }>()
const { t } = useI18n()
const router = useRouter()
const lifecycle = inject(SESSION_LIFECYCLE_KEY, null)
const reads = inject(SESSION_READ_LIFECYCLE_FACTORY_KEY, null)
const commands = inject(TURN_COMMANDS_KEY, null)
const events = inject(CONVERSATION_EVENTS_KEY, null)
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const { renderMarkdown } = useChatTextRendering()
const emptyState = (): AssistantChatState => ({ childKey: '', messages: [], phase: 'empty', error: '', requiresNewSession: false, uncertainDelivery: false, needsAttention: false })
const state = ref(emptyState())
const { readMessageText } = useChatMessageContent(computed(() => state.value.childKey))
const draft = ref('')
const transcript = ref<HTMLElement | null>(null)
const available = computed(() => Boolean(gateway?.availability === 'available' && gateway.isAvailable && !gateway.isResuming && gateway.connectionHealth === 'healthy'
  && (!gateway.connectionPhase || gateway.connectionPhase === 'healthy') && gateway.deliveryIdentity))
const canSend = computed(() => available.value && Boolean(controller) && Boolean(draft.value.trim())
  && !['loading', 'sending', 'running'].includes(state.value.phase) && !state.value.requiresNewSession && !state.value.uncertainDelivery)
let controller: ReturnType<typeof createAssistantChat> | null = null
let generation = 0
let lastParent = props.parentSessionKey
let lastWorkspace = props.workspaceId
let lastIdentity = gateway?.deliveryIdentity

function initialize() {
  const old = controller
  controller = null
  void old?.close().catch(() => {})
  const version = ++generation
  if (lastParent !== props.parentSessionKey || lastWorkspace !== props.workspaceId
    || (lastIdentity && gateway?.deliveryIdentity && lastIdentity !== gateway.deliveryIdentity)) {
    state.value = emptyState(); draft.value = ''
  }
  lastParent = props.parentSessionKey; lastWorkspace = props.workspaceId
  if (gateway?.deliveryIdentity) lastIdentity = gateway.deliveryIdentity
  if (!available.value || !gateway?.deliveryIdentity || !lifecycle || !reads || !commands || !events) return
  const identity = gateway.deliveryIdentity
  const parentKey = props.parentSessionKey
  const workspaceId = props.workspaceId
  const epoch = gateway.subscriptionEpoch
  controller = createAssistantChat({
    parentKey, workspaceId: props.workspaceId, identity, title: t('workbench.newPanel.assistantChat'),
    lifecycle, reads, commands, events, readMessageText, storage: {
      getItem: key => localStorage.getItem(key),
      setItem: (key, value) => localStorage.setItem(key, value),
      removeItem: key => localStorage.removeItem(key),
    },
    isCurrent: () => generation === version && available.value && gateway.deliveryIdentity === identity && gateway.subscriptionEpoch === epoch && props.parentSessionKey === parentKey && props.workspaceId === workspaceId,
    generation: () => gateway.subscriptionEpoch,
    onChange(next) {
      const nearBottom = !transcript.value || transcript.value.scrollHeight - transcript.value.scrollTop - transcript.value.clientHeight < 80
      state.value = next
      if (nearBottom) void nextTick(() => { transcript.value?.scrollTo({ top: transcript.value.scrollHeight }) })
    },
  })
  void controller.restore()
}

async function send() {
  if (!canSend.value || !controller) return
  const message = draft.value
  if (await controller.send(message)) {
    if (draft.value === message) draft.value = ''
  }
}
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); void send() }
}
function refresh() { void controller?.refresh().catch(error => { state.value.error = String(error) }) }
function startNew() { controller?.forget(); state.value = emptyState(); initialize() }
function openFullConversation() {
  if (state.value.childKey) void router.push({ path: '/chat', query: { session: state.value.childKey } })
}
watch(() => [props.parentSessionKey, props.workspaceId, gateway?.deliveryIdentity, gateway?.subscriptionEpoch, available.value], initialize, { immediate: true })
onBeforeUnmount(() => { generation++; void controller?.close().catch(() => {}); controller = null })
</script>

<style scoped>
.assistant-chat-panel { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; gap: var(--sp-3); padding: var(--sp-3); }
.assistant-chat-panel__hint, .assistant-chat-panel__empty { margin: 0; color: var(--text-dim); font-size: var(--fs-xs); line-height: 1.6; }
.assistant-chat-panel__messages { flex: 1; min-height: 0; overflow: auto; overflow-wrap: anywhere; }
.assistant-chat-panel__message { margin-block: var(--sp-3); }
.assistant-chat-panel__message.is-user { margin-inline-start: var(--sp-5); padding: var(--sp-3); border-radius: var(--radius-md); background: var(--bg-elevated); }
.assistant-chat-panel__message p { margin: 0; white-space: pre-wrap; }
.assistant-chat-panel__status { display: flex; align-items: center; justify-content: space-between; color: var(--text-dim); font-size: var(--fs-xs); }
.assistant-chat-panel__notice { color: var(--text-dim); font-size: var(--fs-xs); }
.assistant-chat-panel__notice p { margin-block: var(--sp-1); }
.assistant-chat-panel__preview { color: var(--text-dim); font-size: var(--fs-xs); }
.assistant-chat-panel__composer { display: flex; align-items: flex-end; gap: var(--sp-2); }
.assistant-chat-panel__composer textarea { flex: 1; min-width: 0; resize: vertical; max-height: 12rem; padding: var(--sp-2); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-elevated); color: var(--text); font: inherit; font-size: var(--fs-sm); }
.assistant-chat-panel__composer textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
</style>
