<template>
  <div v-if="visible" class="conversation-live-edge" :class="{ 'msg-ai': !embedded }" data-history-role="assistant">
    <div :class="{ 'msg-ai-main': !embedded }">
      <ActivityDisclosure
        default-open
        lifecycle="working"
        :continuity-key="continuityKey"
        :executing="executing"
        :step-count="stepCount"
        :failure-count="failureCount"
        :phase-label="phaseLabel"
        :phase-announcement="phaseAnnouncement"
        :phase-waiting="phaseWaiting"
        :elapsed-label="elapsedLabel"
        :stale="stale"
        :queued="queued"
      >
        <ConversationActivityBody
          :session-key="sessionKey" :executing="executing"
          :has-unified-activity-order="hasUnifiedActivityOrder" :display-projection="displayProjection"
          :timeline-items="timelineItems" :reasoning-blocks="reasoningBlocks" :reasoning-part="reasoningPart"
          :reasoning-collapse-active="reasoningCollapseActive" :tool-input-previews="toolInputPreviews"
          :tool-state-scope="toolStateScope" :is-tool-group-open="isToolGroupOpen" :is-tool-item-open="isToolItemOpen"
          :tool-group-status-text="toolGroupStatusText" :tool-status-text="toolStatusText"
          :tool-secondary-text="toolSecondaryText" :tool-elapsed-text="toolElapsedText"
          @toggle-group="(id, open) => emit('toggle-group', id, open)"
          @toggle-item="(id, open) => emit('toggle-item', id, open)"
          @show-result="(content, title, context) => emit('show-result', content, title, context)"
          @resolve="(id, decision) => emit('resolve', id, decision)"
          @extend="id => emit('extend', id)"
          @clarify-submit="(fields, request) => emit('clarify-submit', fields, request)"
          @clarify-dismiss="emit('clarify-dismiss')"
        />
      </ActivityDisclosure>

      <div v-if="answerPart" class="live-answer">
        <StreamingTextPart :raw-text="answerPart.rawText" :render-markdown="renderMarkdown"
          :visualize-receipts="visualizeReceipts" :session-key="sessionKey" />
      </div>
      <span
        v-if="answerPart && executing && !stale && currentPhaseCode === 'chat.activity.lifecycle.answering'"
        class="stream-caret"
        aria-hidden="true"
      />

      <ChatArtifactList
        v-if="artifacts.length"
        :artifacts="artifacts"
        :navigation-artifacts="navigationArtifacts"
        :session-key="sessionKey"
        :prefer-workbench="preferWorkbench"
        @download="artifact => emit('download', artifact)"
        @open="artifact => emit('open', artifact)"
      />
    </div>
  </div>

  <InterruptPart
    v-for="part in pendingInterruptParts"
    :key="part.key"
    :part="part"
    @resolve="(id, decision) => emit('resolve', id, decision)"
    @extend="id => emit('extend', id)"
    @clarify-submit="(fields, request) => emit('clarify-submit', fields, request)"
    @clarify-dismiss="() => emit('clarify-dismiss')"
  />
</template>

<script setup lang="ts">
import { toRefs } from 'vue'
import ActivityDisclosure from '@/components/chat/ActivityDisclosure.vue'
import ConversationActivityBody from './ConversationActivityBody.vue'
import ChatArtifactList from '@/components/chat/ChatArtifactList.vue'
import InterruptPart from '@/components/chat/parts/InterruptPart.vue'
import StreamingTextPart from '@/components/chat/parts/StreamingTextPart.vue'
import type { ArtifactPayload } from '@/types/artifacts'
import type { VisualizeReceipt } from '@/visualize/types'
import type { ChatPart } from '@/types/parts'
import type {
  ChatStreamTimelineItem,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
  ToolResultContext,
} from '@/types/chat'
import type { ReasoningBlock } from '@/types/turnlog'
import type { LiveToolInputPreview } from '@/types/toolInputPreview'
import type { AssistantActivityTimelineProjection } from '@/utils/chat/assistantActivity'

type InterruptPartValue = Extract<ChatPart, { type: 'interrupt' }>
type AnswerPart = { rawText: string }

const props = withDefaults(defineProps<{
  visible: boolean
  /** Used by the shared transcript row; its TextPart owns file links/citations. */
  embedded?: boolean
  sessionKey: string
  continuityKey: string
  executing: boolean
  stepCount: number
  failureCount: number
  phaseLabel: string
  phaseAnnouncement?: string
  phaseWaiting?: boolean
  elapsedLabel: string
  stale: boolean
  queued?: boolean
  hasUnifiedActivityOrder: boolean
  displayProjection: AssistantActivityTimelineProjection
  timelineItems?: ChatStreamTimelineItem[]
  reasoningBlocks?: ReasoningBlock[]
  reasoningPart?: Extract<ChatPart, { type: 'reasoning' }> | null
  reasoningCollapseActive?: boolean
  toolInputPreviews?: LiveToolInputPreview[]
  toolStateScope?: string
  answerPart?: AnswerPart | null
  visualizeReceipts?: VisualizeReceipt[]
  currentPhaseCode?: string
  artifacts?: ArtifactPayload[]
  navigationArtifacts?: ArtifactPayload[]
  preferWorkbench?: boolean
  renderMarkdown: (text: string) => string
  isToolGroupOpen: (groupId: string) => boolean | undefined
  isToolItemOpen: (renderKey: string) => boolean | undefined
  toolGroupStatusText: (group: ChatToolCallGroup) => string
  toolStatusText: (call: ChatToolCallRenderItem) => string
  toolSecondaryText: (call: ChatToolCallRenderItem) => string
  toolElapsedText?: (call: ChatToolCallRenderItem) => string
  pendingInterruptParts?: InterruptPartValue[]
}>(), {
  phaseAnnouncement: '',
  embedded: false,
  reasoningPart: null,
  phaseWaiting: false,
  queued: false,
  timelineItems: () => [],
  reasoningBlocks: () => [],
  reasoningCollapseActive: false,
  toolInputPreviews: () => [],
  toolStateScope: '',
  answerPart: null,
  visualizeReceipts: () => [],
  currentPhaseCode: '',
  artifacts: () => [],
  navigationArtifacts: () => [],
  preferWorkbench: false,
  toolElapsedText: undefined,
  pendingInterruptParts: () => [],
})

const emit = defineEmits<{
  'toggle-group': [groupId: string, open: boolean]
  'toggle-item': [renderKey: string, open: boolean]
  'show-result': [content: string, title: string, context?: ToolResultContext]
  resolve: [id: string, decision: 'allow-once' | 'allow-always' | 'deny']
  extend: [id: string]
  'clarify-submit': [fields: Record<string, string>, request: NonNullable<InterruptPartValue['clarify']>]
  'clarify-dismiss': []
  download: [artifact: ArtifactPayload]
  open: [artifact: ArtifactPayload]
}>()

const {
  visible, embedded, sessionKey, continuityKey, executing, stepCount, failureCount,
  phaseLabel, phaseAnnouncement, phaseWaiting, elapsedLabel, stale, queued,
  hasUnifiedActivityOrder, displayProjection, timelineItems, reasoningBlocks, reasoningPart,
  reasoningCollapseActive, toolInputPreviews, toolStateScope, answerPart, visualizeReceipts,
  currentPhaseCode, artifacts, navigationArtifacts, preferWorkbench, renderMarkdown,
  isToolGroupOpen, isToolItemOpen, toolGroupStatusText, toolStatusText,
  toolSecondaryText, toolElapsedText, pendingInterruptParts,
} = toRefs(props)
</script>

<style scoped src="../../styles/chat-assistant-row.css"></style>

<style scoped>
/* Gateway-confirmed answer text streams in its final visual position. */
.live-answer {
  margin-top: 0;
  padding-top: 0.5rem;
  border-top: 0;
}

.stream-caret {
  display: inline-block;
  width: 0.5rem;
  height: 1.05em;
  margin-left: 1px;
  vertical-align: text-bottom;
  background: var(--accent);
  border-radius: 1px; /* radius-allow: 1px stream caret geometry */
  animation: stream-caret-blink 1s steps(2, start) infinite;
}

@keyframes stream-caret-blink {
  0%, 50% { opacity: 1; }
  50.01%, 100% { opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .stream-caret {
    animation: none;
  }
}
</style>
