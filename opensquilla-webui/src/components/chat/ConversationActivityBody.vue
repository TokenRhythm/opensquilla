<template>
  <UnifiedAssistantActivityTimeline
    v-if="hasUnifiedActivityOrder"
    :session-key="sessionKey"
    :executing="executing"
    variant="checklist"
    :projection="displayProjection"
    :tool-input-previews="toolInputPreviews"
    :timeline-items="timelineItems"
    :reasoning-blocks="reasoningBlocks"
    :reasoning-collapse-active="reasoningCollapseActive"
    :reasoning-timeline-phase="false"
    :state-scope="toolStateScope"
    :is-tool-group-open="isToolGroupOpen"
    :is-tool-item-open="isToolItemOpen"
    :tool-group-status-text="toolGroupStatusText"
    :tool-status-text="toolStatusText"
    :tool-secondary-text="toolSecondaryText"
    :tool-elapsed-text="toolElapsedText"
    @toggle-group="(id, open) => emit('toggle-group', id, open)"
    @toggle-item="(id, open) => emit('toggle-item', id, open)"
    @show-result="(content, title, context) => emit('show-result', content, title, context)"
  >
    <template #interrupt="{ part }">
      <InterruptPart
        v-if="part.resolution" :part="part" timeline
        @resolve="(id, decision) => emit('resolve', id, decision)"
        @extend="id => emit('extend', id)"
        @clarify-submit="(fields, request) => emit('clarify-submit', fields, request)"
        @clarify-dismiss="emit('clarify-dismiss')"
      />
    </template>
  </UnifiedAssistantActivityTimeline>

  <template v-else>
    <ReasoningTimeline
      v-if="reasoningBlocks.length"
      :executing="executing" :session-key="sessionKey" :blocks="reasoningBlocks"
      :collapse-active="reasoningCollapseActive" nested
    />
    <ReasoningPart v-else-if="reasoningPart" :part="reasoningPart" live nested />
    <AssistantActivityTimeline
      v-if="timelineItems.length || displayProjection.statusSteps.length"
      :session-key="sessionKey" :executing="executing" variant="checklist"
      :projection="displayProjection" :timeline-items="timelineItems" :state-scope="toolStateScope"
      :is-tool-group-open="isToolGroupOpen" :is-tool-item-open="isToolItemOpen"
      :tool-group-status-text="toolGroupStatusText" :tool-status-text="toolStatusText"
      :tool-secondary-text="toolSecondaryText" :tool-elapsed-text="toolElapsedText"
      @toggle-group="(id, open) => emit('toggle-group', id, open)"
      @toggle-item="(id, open) => emit('toggle-item', id, open)"
      @show-result="(content, title, context) => emit('show-result', content, title, context)"
    >
      <template #interrupt="{ part }">
        <InterruptPart
          v-if="part.resolution" :part="part" timeline
          @resolve="(id, decision) => emit('resolve', id, decision)"
          @extend="id => emit('extend', id)"
          @clarify-submit="(fields, request) => emit('clarify-submit', fields, request)"
          @clarify-dismiss="emit('clarify-dismiss')"
        />
      </template>
    </AssistantActivityTimeline>
    <ToolInputPreview
      v-for="preview in toolInputPreviews" :key="preview.previewId"
      :preview="preview" :executing="executing" :state-scope="toolStateScope"
      :open="isToolItemOpen(toolStateScope ? `${toolStateScope}:${preview.toolUseId}` : preview.toolUseId) === true"
      @toggle-item="(id, open) => emit('toggle-item', id, open)"
    />
  </template>
</template>

<script setup lang="ts">
import AssistantActivityTimeline from './AssistantActivityTimeline.vue'
import UnifiedAssistantActivityTimeline from './UnifiedAssistantActivityTimeline.vue'
import ReasoningTimeline from './ReasoningTimeline.vue'
import ToolInputPreview from './ToolInputPreview.vue'
import InterruptPart from './parts/InterruptPart.vue'
import ReasoningPart from './parts/ReasoningPart.vue'
import type { ChatStreamTimelineItem, ChatToolCallGroup, ChatToolCallRenderItem, ToolResultContext } from '@/types/chat'
import type { ChatPart } from '@/types/parts'
import type { ReasoningBlock } from '@/types/turnlog'
import type { LiveToolInputPreview } from '@/types/toolInputPreview'
import type { AssistantActivityTimelineProjection } from '@/utils/chat/assistantActivity'

type InterruptPartValue = Extract<ChatPart, { type: 'interrupt' }>

withDefaults(defineProps<{
  sessionKey: string
  executing: boolean
  hasUnifiedActivityOrder: boolean
  displayProjection: AssistantActivityTimelineProjection
  timelineItems?: ChatStreamTimelineItem[]
  reasoningBlocks?: ReasoningBlock[]
  reasoningPart?: Extract<ChatPart, { type: 'reasoning' }> | null
  reasoningCollapseActive?: boolean
  toolInputPreviews?: LiveToolInputPreview[]
  toolStateScope?: string
  isToolGroupOpen: (groupId: string) => boolean | undefined
  isToolItemOpen: (renderKey: string) => boolean | undefined
  toolGroupStatusText: (group: ChatToolCallGroup) => string
  toolStatusText: (call: ChatToolCallRenderItem) => string
  toolSecondaryText: (call: ChatToolCallRenderItem) => string
  toolElapsedText?: (call: ChatToolCallRenderItem) => string
}>(), {
  timelineItems: () => [], reasoningBlocks: () => [], reasoningPart: null, reasoningCollapseActive: false,
  toolInputPreviews: () => [], toolStateScope: '', toolElapsedText: undefined,
})

const emit = defineEmits<{
  'toggle-group': [groupId: string, open: boolean]
  'toggle-item': [renderKey: string, open: boolean]
  'show-result': [content: string, title: string, context?: ToolResultContext]
  resolve: [id: string, decision: 'allow-once' | 'allow-always' | 'deny']
  extend: [id: string]
  'clarify-submit': [fields: Record<string, string>, request: NonNullable<InterruptPartValue['clarify']>]
  'clarify-dismiss': []
}>()
</script>
