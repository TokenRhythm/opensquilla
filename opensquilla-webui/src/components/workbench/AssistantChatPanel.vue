<template>
  <section class="assistant-chat-panel" data-testid="assistant-chat-panel">
    <p v-if="state.error" class="assistant-chat-panel__notice" role="alert">{{ state.error }}</p>
    <ChatView ref="chatView" :key="ownerScope" embedded
      :session-key-override="state.child?.key || ''"
      :policy="SIDE_CONVERSATION_POLICY"
      :workbench-owner-session-key="parentSessionKey"
      :pending-input-wal="pendingOwner.wal"
      :pending-queue-policy="pendingOwner.policy"
      :draft-storage="pendingOwner.draftStorage"
      :attachment-draft-store="pendingOwner.attachmentDraftStore"
      :draft-identity="ownerBinding"
      :inherited-run-mode="state.child?.inherited.runMode"
      :expected-workspace-id="workspaceId || null"
      :owner-blocked="state.closing || state.requiresNewSession"
      :prepare-session="prepareSession" />
  </section>
</template>

<script setup lang="ts">
import { computed, inject, onBeforeUnmount, shallowRef, watch } from 'vue'
import ChatView from '@/views/ChatView.vue'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { SESSION_LIFECYCLE_KEY } from '@/modules/sessionLifecycle'
import { SIDE_CONVERSATION_POLICY } from '@/modules/conversationPolicy'
import { assistantChatBindingKey, createAssistantChatOwner, type AssistantChatOwnerState } from '@/modules/assistantChat'
import { assistantChatPendingOwner } from '@/modules/assistantChatPendingOwner'
import type { SideChatLaunchHandle, SideChatLaunchRequest } from '@/modules/sideChatLauncher'

const props = defineProps<{ parentSessionKey: string; workspaceId?: string; ownerId: string }>()
const chatView = shallowRef<SideChatLaunchHandle | null>(null)
defineExpose({ launchSideChat: (request: SideChatLaunchRequest) => chatView.value?.launchSideChat(request) ?? Promise.resolve(false) })
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const lifecycle = inject(SESSION_LIFECYCLE_KEY, null)
const lastIdentity = shallowRef(gateway?.deliveryIdentity || '')
watch(() => gateway?.deliveryIdentity, identity => { if (identity) lastIdentity.value = identity }, { flush: 'sync' })
const ownerBinding = computed(() => assistantChatBindingKey(lastIdentity.value, props.parentSessionKey, props.ownerId))
const pendingOwner = computed(() => assistantChatPendingOwner(ownerBinding.value))
const ownerScope = computed(() => JSON.stringify([ownerBinding.value, props.workspaceId || '']))
const state = shallowRef<AssistantChatOwnerState>({ child: null, preparing: false, closing: false, error: '', requiresNewSession: false })
let owner: ReturnType<typeof createAssistantChatOwner> | null = null
let revision = 0

function gatewayAvailable() {
  return Boolean(gateway?.deliveryIdentity && gateway.availability === 'available' && gateway.isAvailable && !gateway.isResuming
    && gateway.connectionHealth === 'healthy' && (!gateway.connectionPhase || gateway.connectionPhase === 'healthy'))
}

// A reconnect changes the shared chat reader, not the side owner or composer.
// Only a different parent, tab, workspace or Gateway authority replaces them.
watch(ownerScope, () => {
  owner?.dispose()
  owner = null
  const version = ++revision
  state.value = { child: null, preparing: false, closing: false, error: '', requiresNewSession: false }
  if (!lifecycle || !lastIdentity.value) return
  const identity = lastIdentity.value
  const scope = ownerScope.value
  owner = createAssistantChatOwner({
    identity, parentKey: props.parentSessionKey, ownerId: props.ownerId, lifecycle,
    isCurrent: () => version === revision && ownerScope.value === scope && gatewayAvailable(),
    ownerAvailable: () => gateway?.deliveryIdentity === identity && gatewayAvailable(),
    onChange: next => { state.value = next },
  })
}, { immediate: true, flush: 'sync' })
watch(gatewayAvailable, available => { if (available) owner?.restore() })

// ChatView invokes this only after a valid explicit send. The Gateway takes
// the parent snapshot once here; all subsequent sending belongs to ChatView.
async function prepareSession(): Promise<string> {
  if (!gatewayAvailable()) return ''
  const active = owner
  const child = await active?.prepare()
  return active === owner ? child?.key || '' : ''
}

onBeforeUnmount(() => { revision++; owner?.dispose(); owner = null })
</script>

<style scoped>
.assistant-chat-panel { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; background: var(--bg-surface); }
.assistant-chat-panel__notice { flex-shrink: 0; margin: 8px 16px; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-md); color: var(--text-muted); font-size: var(--fs-xs); overflow-wrap: anywhere; }
</style>
