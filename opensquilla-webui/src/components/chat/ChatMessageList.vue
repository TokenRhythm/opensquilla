<template>
  <template v-for="(message, index) in messages" :key="chatMessageKey(message, index)">
    <slot
      v-if="message.isRouterStrip"
      name="router-strip"
      :message="message"
      :index="index"
    />
    <UserMessage
      v-else-if="message.displayRole === 'user'"
      :id="`chat-turn-${index}`"
      :data-chat-turn-key="chatMessageKey(message, index)"
      tabindex="-1"
      :message="message"
      :share-mode="shareMode"
      :share-selected="selectedMessageIds.has(chatMessageKey(message, index))"
      :share-message-id="chatMessageKey(message, index)"
      :strip-time-prefix="stripTimePrefix"
      :copy-message="copyMessage"
      :download-attachment="downloadAttachment"
      @edit="$emit('editMessage', $event)"
      @toggle-share="$emit('toggleShareMessage', $event)"
    />
    <AssistantMessage
      v-else-if="message.displayRole === 'assistant'"
      :message="message"
      :index="index"
      :share-mode="shareMode"
      :share-selected="selectedMessageIds.has(chatMessageKey(message, index))"
      :share-message-id="chatMessageKey(message, index)"
      :render-markdown="renderMarkdown"
      :fmt-tok="fmtTok"
      :tool-call-groups="toolCallGroups"
      :is-tool-group-open="isToolGroupOpen"
      :is-tool-item-open="isToolItemOpen"
      :tool-group-status-text="toolGroupStatusText"
      :tool-status-text="toolStatusText"
      :tool-secondary-text="toolSecondaryText"
      :session-key="sessionKey"
      :auth-token="authToken"
      :artifact-navigation-items="artifactNavigationItems"
      :copy-message="copyMessage"
      :is-tip="index === lastAssistantIndex"
      :fork-busy="forkBusy"
      @fork="$emit('forkConversation')"
      @regenerate="$emit('regenerateMessage', $event)"
      @toggle-share="$emit('toggleShareMessage', $event)"
      @download-artifact="$emit('downloadArtifact', $event)"
      @toggle-tool-group="$emit('toggleToolGroup', $event)"
      @toggle-tool-item="$emit('toggleToolItem', $event)"
      @show-tool-result="(content, title, context) => $emit('showToolResult', content, title, context)"
      @resolve-interrupt="(id, decision, note) => $emit('resolveInterrupt', id, decision, note)"
      @extend-interrupt="id => $emit('extendInterrupt', id)"
      @clarify-submit="(fields, request) => $emit('clarifySubmit', fields, request)"
      @clarify-dismiss="$emit('clarifyDismiss')"
    />
    <SystemMessage
      v-else
      :message="message"
      :subagent-summary="subagentSummary"
      :subagent-body="subagentBody"
      @resume="$emit('resumeSandbox')"
    />
  </template>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import AssistantMessage from '@/components/chat/AssistantMessage.vue'
import SystemMessage from '@/components/chat/SystemMessage.vue'
import UserMessage from '@/components/chat/UserMessage.vue'
import type {
  ChatRenderedMessage,
  ChatToolCall,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
  ToolResultContext,
} from '@/types/chat'
import type { ArtifactPayload } from '@/types/rpc'
import { chatMessageKey } from '@/utils/chat/messageIdentity'

const props = defineProps<{
  messages: ChatRenderedMessage[]
  shareMode: boolean
  selectedMessageIds: Set<string>
  stripTimePrefix: (text: string) => string
  renderMarkdown: (text: string) => string
  fmtTok: (value: number) => string
  subagentSummary: (text: string) => string
  subagentBody: (text: string) => string
  toolCallGroups: (calls: ChatToolCall[], baseKey: string) => ChatToolCallGroup[]
  isToolGroupOpen: (groupId: string) => boolean
  isToolItemOpen: (renderKey: string) => boolean
  toolGroupStatusText: (group: ChatToolCallGroup) => string
  toolStatusText: (call: ChatToolCallRenderItem) => string
  toolSecondaryText: (call: ChatToolCallRenderItem) => string
  copyMessage: (message: ChatRenderedMessage) => Promise<boolean>
  downloadAttachment: (attachment: import('@/types/chat').DisplayAttachment) => Promise<boolean>
  artifactNavigationItems?: ArtifactPayload[]
  sessionKey?: string
  authToken?: string
  forkBusy?: boolean
}>()

defineEmits<{
  editMessage: [message: ChatRenderedMessage]
  regenerateMessage: [message: ChatRenderedMessage]
  toggleShareMessage: [messageId: string]
  downloadArtifact: [artifact: ArtifactPayload]
  toggleToolGroup: [groupId: string]
  toggleToolItem: [renderKey: string]
  showToolResult: [content: string, title: string, context?: ToolResultContext]
  forkConversation: []
  resolveInterrupt: [id: string, decision: 'allow-once' | 'allow-always' | 'deny', note?: string]
  extendInterrupt: [id: string]
  clarifySubmit: [fields: Record<string, string>, request?: NonNullable<Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }>['clarify']>]
  clarifyDismiss: []
  resumeSandbox: []
}>()

// The conversation tip: forking is whole-conversation in this release, so the
// fork action only renders on the thread's last assistant message.
const lastAssistantIndex = computed(() => {
  for (let i = props.messages.length - 1; i >= 0; i--) {
    if (props.messages[i].displayRole === 'assistant' && !props.messages[i].stopNotice) return i
  }
  return -1
})
</script>
