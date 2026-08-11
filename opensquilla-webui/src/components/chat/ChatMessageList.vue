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
      :show-turn-outcome="isTurnTip(index)"
      :is-streaming="isStreaming"
      :is-goal-source="isGoalSource(message)"
      @edit="$emit('editMessage', $event)"
      @toggle-share="$emit('toggleShareMessage', $event)"
    />
    <CompactionEvent
      v-else-if="message.displayRole === 'maintenance' && message.maintenance?.kind === 'context_compaction'"
      :message="message"
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
      :workbench-enabled="workbenchEnabled"
      :artifact-navigation-items="artifactNavigationItems"
      :copy-message="copyMessage"
      :is-tip="isForkableAssistant(index)"
      :fork-busy="forkBusy"
      :plan-action-pending="planActionPending"
      :plan-actions-disabled="planActionsDisabled"
      :show-turn-outcome="isTurnTip(index)"
      :goal-outcome="goalOutcomeFor(message, index)"
      :goal-elapsed="goalElapsed"
      @fork="$emit('forkConversation', forkThroughTurnId(index))"
      @regenerate="$emit('regenerateMessage', $event)"
      @toggle-share="$emit('toggleShareMessage', $event)"
      @download-artifact="$emit('downloadArtifact', $event)"
      @open-artifact="$emit('openArtifact', $event)"
      @toggle-tool-group="$emit('toggleToolGroup', $event)"
      @toggle-tool-item="$emit('toggleToolItem', $event)"
      @show-tool-result="(content, title, context) => $emit('showToolResult', content, title, context)"
      @resolve-interrupt="(id, decision) => $emit('resolveInterrupt', id, decision)"
      @extend-interrupt="id => $emit('extendInterrupt', id)"
      @clarify-submit="(fields, request) => $emit('clarifySubmit', fields, request)"
      @clarify-dismiss="$emit('clarifyDismiss')"
      @plan-implement-current="$emit('planImplementCurrent', $event)"
      @plan-implement-new="$emit('planImplementNew', $event)"
      @plan-replan="$emit('planReplan', $event)"
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
import CompactionEvent from '@/components/chat/CompactionEvent.vue'
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
import {
  goalHasSettledTerminalOutcome,
  type GoalSnapshot,
} from '@/composables/chat/useChatGoals'
import type { PlanCardAction, PlanCardActionTarget } from '@/types/plans'
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
  workbenchEnabled?: boolean
  forkBusy?: boolean
  planActionPending?: PlanCardAction | null
  planActionsDisabled?: boolean
  isStreaming?: boolean
  goal?: GoalSnapshot | null
  goalElapsed?: string
}>()

defineEmits<{
  editMessage: [message: ChatRenderedMessage]
  regenerateMessage: [message: ChatRenderedMessage]
  toggleShareMessage: [messageId: string]
  downloadArtifact: [artifact: ArtifactPayload]
  openArtifact: [artifact: ArtifactPayload]
  toggleToolGroup: [groupId: string]
  toggleToolItem: [renderKey: string]
  showToolResult: [content: string, title: string, context?: ToolResultContext]
  forkConversation: [throughTurnId?: string]
  resolveInterrupt: [id: string, decision: 'allow-once' | 'allow-always' | 'deny']
  extendInterrupt: [id: string]
  clarifySubmit: [fields: Record<string, string>, request?: NonNullable<Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }>['clarify']>]
  clarifyDismiss: []
  resumeSandbox: []
  planImplementCurrent: [target: PlanCardActionTarget]
  planImplementNew: [target: PlanCardActionTarget]
  planReplan: [target: PlanCardActionTarget]
}>()

// Legacy transcripts can only use the whole-conversation fallback at the
// current tip. Historical branches require a durable terminal turn identity so
// the server, rather than a DOM/message index, owns the inclusive boundary.
const lastAssistantIndex = computed(() => {
  for (let i = props.messages.length - 1; i >= 0; i--) {
    if (props.messages[i].displayRole === 'assistant' && !props.messages[i].stopNotice) return i
  }
  return -1
})

function forkThroughTurnId(index: number): string | undefined {
  const turnId = props.messages[index]?.turnOutcome?.turnId?.trim()
  return turnId || undefined
}

function isForkableAssistant(index: number): boolean {
  const message = props.messages[index]
  if (
    props.isStreaming
    || message?.displayRole !== 'assistant'
    || message.stopNotice
  ) return false
  if (forkThroughTurnId(index)) return isTurnTip(index)
  if (index !== lastAssistantIndex.value) return false
  return !props.messages.slice(index + 1).some(next => (
    next.displayRole === 'user' || next.displayRole === 'assistant'
  ))
}

function isTurnTip(index: number): boolean {
  const message = props.messages[index]
  if (!message?.turnOutcome || !message.turnKey) return false
  for (let nextIndex = index + 1; nextIndex < props.messages.length; nextIndex++) {
    const next = props.messages[nextIndex]
    if (next.turnKey === message.turnKey) {
      if (next.displayRole === 'user' || next.displayRole === 'assistant') return false
      continue
    }
    if (next.displayRole === 'user') break
  }
  return true
}

function isGoalSource(message: ChatRenderedMessage): boolean {
  const sourceMessageId = String(props.goal?.sourceMessageId || '').trim()
  return Boolean(sourceMessageId && message.messageId === sourceMessageId)
}

function goalOutcomeFor(message: ChatRenderedMessage, index: number): GoalSnapshot | null {
  const goal = props.goal
  const terminalTurnId = String(goal?.terminalTurnId || '').trim()
  if (
    !goalHasSettledTerminalOutcome(goal)
    || !terminalTurnId
    || message.stopNotice
    || message.turnId !== terminalTurnId
  ) return null

  // A turn may persist more than one assistant row while tools execute. Bind
  // the durable outcome to the final visible assistant row in that turn so it
  // is rendered exactly once beside the actual final response.
  for (let nextIndex = index + 1; nextIndex < props.messages.length; nextIndex += 1) {
    const next = props.messages[nextIndex]
    if (
      next.displayRole === 'assistant'
      && !next.stopNotice
      && next.turnId === terminalTurnId
    ) return null
  }
  return goal ?? null
}
</script>
