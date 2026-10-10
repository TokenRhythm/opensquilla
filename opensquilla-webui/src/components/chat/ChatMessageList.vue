<template>
  <div
    ref="listRootRef"
    class="chat-message-list"
    :data-virtualized="virtualizationEnabled ? 'true' : 'false'"
    :data-rendered-message-count="renderEntries.filter(entry => entry.index < messages.length).length"
  >
    <div
      v-if="variableLayout.topSpacer > 0"
      class="chat-message-list__spacer"
      :style="spacerStyle(variableLayout.topSpacer)"
      data-testid="chat-history-top-spacer"
      aria-hidden="true"
    />
    <template v-for="entry in renderEntries" :key="entry.key">
      <div
        v-if="entry.gapBefore > 0"
        class="chat-message-list__spacer"
        :style="spacerStyle(entry.gapBefore)"
        data-testid="chat-history-gap-spacer"
        aria-hidden="true"
      />
      <div
        v-if="entry.index === messages.length"
        :ref="setRowElement"
        :data-index="entry.index"
        class="chat-message-list__trailing"
        data-testid="chat-message-trailing"
      >
        <slot name="trailing" />
      </div>
      <div
        v-else
        :ref="setRowElement"
        :data-index="entry.index"
        class="chat-message-list__row"
        :class="{ 'chat-message-list__row--last': entry.index === messages.length - 1 }"
        :data-chat-message-key="entry.key"
        :data-chat-message-index="entry.index"
        :data-chat-message-forced="forcedIndexes.has(entry.index) ? 'true' : 'false'"
        data-testid="chat-message-row"
      >
        <div
          v-if="showContentHydrationStatus(messages[entry.index], entry.index)"
          class="chat-history-content-hydration"
          data-testid="chat-history-content-hydration"
          role="status"
        >
          <button
            v-if="contentHydrationError(messages[entry.index], entry.index)"
            type="button"
            class="chat-history-content-hydration__button"
            :disabled="contentHydrationPending(messages[entry.index], entry.index)"
            @click.stop="hydrateContent(messages[entry.index], entry.index)"
          >
            {{ t('chat.retry') }}
          </button>
          <span v-else>{{ contentHydrationPending(messages[entry.index], entry.index) ? t('shared.loading') : contentAvailabilityLabel(messages[entry.index]) }}</span>
          <span v-if="contentHydrationError(messages[entry.index], entry.index)" class="chat-history-content-hydration__error">
            {{ contentHydrationError(messages[entry.index], entry.index) }}
          </span>
        </div>
        <div
          v-if="messages[entry.index].historyPayloadPreview?.detailsTruncated"
          class="chat-history-content-hydration"
          data-testid="chat-history-detail-preview"
          role="status"
        >
          {{ t('historyContent.detailsPreview') }}
        </div>
        <slot
          v-if="messages[entry.index].isRouterStrip"
          name="router-strip"
          :message="hydratedMessage(messages[entry.index], entry.index)"
          :index="entry.index"
        />
        <UserMessage
          v-else-if="messages[entry.index].displayRole === 'user'"
          :id="`chat-turn-${entry.index}`"
          :data-chat-turn-key="chatMessageKey(messages[entry.index], entry.index)"
          tabindex="-1"
          :message="hydratedMessage(messages[entry.index], entry.index)"
          :share-mode="shareMode"
          :share-selected="selectedMessageIds.has(chatMessageKey(messages[entry.index], entry.index))"
          :share-message-id="chatMessageKey(messages[entry.index], entry.index)"
          :strip-time-prefix="stripTimePrefix"
          :copy-message="copyMessage"
          :download-attachment="downloadAttachment"
          :session-key="sessionKey"
          :show-turn-outcome="shouldShowTurnOutcome(entry.index)"
          :is-streaming="isStreaming"
          :is-goal-source="isGoalSource(messages[entry.index])"
          :can-reuse-prompt-annotations="canReusePromptAnnotations === true"
          :workbench-resource-preview-enabled="workbenchResourcePreviewEnabled === true"
          :workbench-resource-edit-enabled="workbenchResourceEditEnabled === true"
          :workbench-attachment-resources="workbenchAttachmentResources"
          @edit="$emit('editMessage', $event)"
          @edit-attachment="$emit('editAttachment', $event)"
          @preview-attachment="$emit('previewAttachment', $event)"
          @preview-image="$emit('previewImage', $event, messages[entry.index].attachments || [])"
          @reuse-prompt-annotation="$emit('reusePromptAnnotation', $event)"
          @toggle-share="$emit('toggleShareMessage', $event)"
        />
        <CompactionEvent
          v-else-if="messages[entry.index].displayRole === 'maintenance' && messages[entry.index].maintenance?.kind === 'context_compaction'"
          :message="messages[entry.index]"
        />
        <AssistantMessage
          v-else-if="messages[entry.index].displayRole === 'assistant'"
          :message="hydratedMessage(messages[entry.index], entry.index)"
          :index="entry.index"
          :share-mode="shareMode"
          :share-selected="selectedMessageIds.has(chatMessageKey(messages[entry.index], entry.index))"
          :share-message-id="chatMessageKey(messages[entry.index], entry.index)"
          :render-markdown="renderMarkdown"
          :fmt-tok="fmtTok"
          :tool-call-groups="toolCallGroups"
          :is-tool-group-open="isToolGroupOpen"
          :is-tool-item-open="isToolItemOpen"
          :tool-group-status-text="toolGroupStatusText"
          :tool-status-text="toolStatusText"
          :tool-secondary-text="toolSecondaryText"
          :session-key="sessionKey"
          :workbench-enabled="workbenchEnabled"
          :artifact-navigation-items="artifactNavigationItems"
          :copy-message="copyMessage"
          :regenerate-available="assistantRegenerateAvailable(entry.index)"
          :is-tip="isForkableAssistant(entry.index)"
          :fork-busy="forkBusy"
          :plan-action-pending="planActionPending"
          :plan-actions-disabled="planActionsDisabled"
          :plan-presentations="planPresentations"
          :plan-presentation-available="planPresentationAvailable && !shareMode"
          :plan-presentation-pending="planPresentationPending"
          :show-turn-outcome="shouldShowTurnOutcome(entry.index)"
          :has-error-notice="hasTurnErrorNotice(entry.index)"
          :goal-outcome="goalOutcomeFor(messages[entry.index], entry.index)"
          :goal-elapsed="goalElapsed"
          :goal-removable="goalRemovable && !shareMode"
          :goal-busy="goalBusy"
          :resolve-session-availability="resolveSessionAvailability"
          :resolve-workspace-preview-resource="resolveWorkspacePreviewResource"
          @fork="$emit('forkConversation', forkThroughTurnId(entry.index))"
          @regenerate="$emit('regenerateMessage', $event)"
          @toggle-share="$emit('toggleShareMessage', $event)"
          @download-artifact="$emit('downloadArtifact', $event)"
          @open-artifact="$emit('openArtifact', $event)"
          @toggle-tool-group="$emit('toggleToolGroup', $event)"
          @toggle-tool-item="$emit('toggleToolItem', $event)"
          @show-tool-result="(content, title, context) => $emit('showToolResult', content, title, context)"
          @open-session="$emit('openSession', $event)"
          @resolve-interrupt="(id, decision) => $emit('resolveInterrupt', id, decision)"
          @extend-interrupt="id => $emit('extendInterrupt', id)"
          @clarify-submit="(fields, request) => $emit('clarifySubmit', fields, request)"
          @clarify-dismiss="$emit('clarifyDismiss')"
          @plan-implement-current="$emit('planImplementCurrent', $event)"
          @plan-implement-new="$emit('planImplementNew', $event)"
          @plan-replan="$emit('planReplan', $event)"
          @plan-presentation-change="$emit('planPresentationChange', $event)"
          @goal-clear="$emit('goalClear', $event)"
        />
        <SystemMessage
          v-else
          :message="hydratedMessage(messages[entry.index], entry.index)"
          :subagent-summary="subagentSummary"
          :subagent-body="subagentBody"
          :retry-available="usageBarrierRetryAvailable(entry.index)"
          :resume-available="sandboxResumeAvailable(messages[entry.index])"
          :has-partial-answer="Boolean(messages[entry.index].turnId && visibleAnswerTurns.has(messages[entry.index].turnId!))"
          @resume="forwardSandboxResume"
          @retry="forwardSystemRetry"
        />
        <SkillLoadStatus
          v-if="messages[entry.index].displayRole !== 'assistant'"
          standalone
          :receipts="messages[entry.index]?.skillLoads || []"
        />
      </div>
    </template>
    <div
      v-if="variableLayout.bottomSpacer > 0"
      class="chat-message-list__spacer"
      :style="spacerStyle(variableLayout.bottomSpacer)"
      data-testid="chat-history-bottom-spacer"
      aria-hidden="true"
    />
  </div>
</template>

<script setup lang="ts">
import SkillLoadStatus from './SkillLoadStatus.vue'
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  onUpdated,
  reactive,
  ref,
  useSlots,
  watch,
  type ComponentPublicInstance,
} from 'vue'
import { useI18n } from 'vue-i18n'
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
import type { ArtifactPayload } from '@/types/artifacts'
import {
  goalHasSettledTerminalOutcome,
  type GoalSnapshot,
} from '@/composables/chat/useChatGoals'
import type { PlanCardAction, PlanCardActionTarget, PlanPresentationSnapshot, PlanPresentationRequest } from '@/types/plans'
import type { PromptAnnotationSnapshot } from '@/types/promptAnnotations'
import type { WorkbenchResource } from '@/types/workbenchResources'
import { chatMessageKey } from '@/utils/chat/messageIdentity'
import { applyProgrammaticScroll } from '@/utils/chat/scrollMutation'
import { captureVisibleTextScrollAnchor, restoreTextScrollAnchor, type TextScrollAnchor } from '@/utils/chat/scrollAnchor'
import { readDistanceFromEnd, remeasureVirtualizer, type VirtualizerAnchor } from '@/utils/virtualizerLayout'
import { sandboxResumeMessageTurnId } from '@/utils/chat/sandboxResumeGuard'
import { isProcessRestartOutcome, turnOutcomePresentation } from '@/utils/chat/turnOutcome'
import { resolveAssistantAnswer } from '@/utils/chat/assistantActivity'
import { ContentRangeCache, type ContentRangeRef } from '@/utils/chat/contentRangeCache'
import {
  isUsageAccountingBarrierMessage,
  strictUsageBarrierRetryUserMessageIndex,
} from '@/utils/chat/usageAccountingFailure'
import { defaultRangeExtractor, elementScroll, measureElement as measureVirtualElement, observeElementOffset, observeElementRect, useVirtualizer, type Rect, type ScrollToOptions, type Virtualizer } from '@tanstack/vue-virtual'
import type { ChatMessageListVirtualizer } from '@/types/chatVirtualizer'

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
  workbenchEnabled?: boolean
  workbenchResourcePreviewEnabled?: boolean
  workbenchResourceEditEnabled?: boolean
  workbenchAttachmentResources?: ReadonlyMap<string, WorkbenchResource>
  canReusePromptAnnotations?: boolean
  forkBusy?: boolean
  sandboxResumeTurnId?: string
  planActionPending?: PlanCardAction | null
  planActionsDisabled?: boolean
  planPresentations?: Record<string, PlanPresentationSnapshot>
  planPresentationAvailable?: boolean
  planPresentationPending?: string | null
  isStreaming?: boolean
  goal?: GoalSnapshot | null
  goalElapsed?: string
  goalRemovable?: boolean
  goalBusy?: boolean
  resolveSessionAvailability?: (sessionKey: string) => Promise<boolean>
  resolveWorkspacePreviewResource?: (sessionKey: string, documentId: string) => Promise<WorkbenchResource | null>
  /** Required for long-history virtualization; omitted by legacy embedders. */
  scrollContainer?: HTMLElement | null
  /** Header outside the row range; its size contributes to scrollMargin. */
  layoutHeader?: HTMLElement | null
  /** Session/render epoch used to invalidate deferred scroll corrections. */
  scrollEpoch?: number
  /** Preview/export paths can force a complete, canonical DOM. */
  virtualizationDisabled?: boolean
  /** Current search match or another externally owned focus target. */
  forceMountMessageKeys?: ReadonlySet<string>
  /** Keep the live edge pinned while estimated row heights settle. */
  followLiveEdge?: boolean
  /** Trailing breathing room, rendered once by this list. */
  bottomPadding?: number
  /** Identity of the live/status slot, stable across stream updates. */
  trailingKey?: string
}>()

const emit = defineEmits<{
  editMessage: [message: ChatRenderedMessage]
  editAttachment: [attachment: import('@/types/chat').DisplayAttachment]
  previewAttachment: [attachment: import('@/types/chat').DisplayAttachment]
  previewImage: [attachment: import('@/types/chat').DisplayAttachment, attachments: import('@/types/chat').DisplayAttachment[]]
  reusePromptAnnotation: [annotation: PromptAnnotationSnapshot]
  regenerateMessage: [
    message: ChatRenderedMessage,
    settle?: (accepted: boolean) => void,
  ]
  toggleShareMessage: [messageId: string]
  downloadArtifact: [artifact: ArtifactPayload]
  openArtifact: [artifact: ArtifactPayload]
  toggleToolGroup: [groupId: string]
  toggleToolItem: [renderKey: string]
  showToolResult: [content: string, title: string, context?: ToolResultContext]
  openSession: [sessionKey: string]
  forkConversation: [throughTurnId?: string]
  resolveInterrupt: [id: string, decision: 'allow-once' | 'allow-always' | 'deny']
  extendInterrupt: [id: string]
  clarifySubmit: [fields: Record<string, string>, request?: NonNullable<Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }>['clarify']>]
  clarifyDismiss: []
  resumeSandbox: [message: ChatRenderedMessage, sourceSessionKey: string]
  planImplementCurrent: [target: PlanCardActionTarget]
  planImplementNew: [target: PlanCardActionTarget]
  planReplan: [target: PlanCardActionTarget]
  planPresentationChange: [request: PlanPresentationRequest]
  goalClear: [goal: GoalSnapshot]
}>()

const VIRTUALIZATION_STORAGE_KEY = 'opensquilla.chat.virtualizeHistory'
const MESSAGE_GAP_PX = 4
const { t } = useI18n()

function forwardSystemRetry(
  message: ChatRenderedMessage,
  settle: (accepted: boolean) => void,
) {
  emit('regenerateMessage', message, settle)
}

function sandboxResumeAvailable(message: ChatRenderedMessage): boolean {
  return Boolean(props.sessionKey && props.sandboxResumeTurnId
    && !props.shareMode && !props.forkBusy && !props.isStreaming
    && sandboxResumeMessageTurnId(message) === props.sandboxResumeTurnId)
}

function forwardSandboxResume(message: ChatRenderedMessage) {
  if (!sandboxResumeAvailable(message) || !props.sessionKey) return
  emit('resumeSandbox', message, props.sessionKey)
}

const visibleAnswerTurns = computed(() => new Set(props.messages
  .filter(message => {
    if (message.displayRole !== 'assistant' || !message.turnId) return false
    const outcome = turnOutcomePresentation(message.turnOutcome)
    const lifecycle = outcome === 'stopped' || outcome === 'interrupted' || message.interrupted
      ? 'interrupted' as const
      : outcome === 'timeout' || outcome === 'failed' || message.terminalFailure
        ? 'failed' as const
        : message.isStreaming
          ? 'working' as const
          : 'settled' as const
    return Boolean(resolveAssistantAnswer(
      message,
      message.timelineItems ?? [],
      lifecycle,
    ).text.trim())
  })
  .map(message => message.turnId!)))

function outcomeTurnIdentity(message: ChatRenderedMessage): string {
  const directTurnId = message.turnId?.trim()
  const outcomeTurnId = message.turnOutcome?.turnId?.trim()
  if (directTurnId && outcomeTurnId && directTurnId !== outcomeTurnId) return ''
  const turnId = directTurnId || outcomeTurnId
  if (turnId) return `id:${turnId}`
  return message.turnKey ? `key:${message.turnKey}` : ''
}

const errorNoticeTurns = computed(() => new Set(props.messages
  .filter(message => message.displayRole === 'error')
  .map(outcomeTurnIdentity)
  .filter(Boolean)))

function hasTurnErrorNotice(index: number): boolean {
  const message = props.messages[index]
  return Boolean(message && errorNoticeTurns.value.has(outcomeTurnIdentity(message)))
}

function shouldShowTurnOutcome(index: number): boolean {
  if (!isTurnTip(index)) return false
  // The centered reason already explains this failed turn. Keep the outcome
  // fallback when no notice exists, and keep cancellation/restart guidance.
  const outcome = props.messages[index]?.turnOutcome
  if (isProcessRestartOutcome(outcome)) return true
  const presentation = turnOutcomePresentation(outcome)
  return !(['failed', 'timeout'].includes(presentation) && hasTurnErrorNotice(index))
}

function usageBarrierRetryAvailable(index: number): boolean {
  const message = props.messages[index]
  return Boolean(
    message
    && isUsageAccountingBarrierMessage(message)
    && strictUsageBarrierRetryUserMessageIndex(props.messages, index, message) >= 0,
  )
}

function assistantRegenerateAvailable(index: number): boolean {
  const message = props.messages[index]
  if (!message || !isUsageAccountingBarrierMessage(message)) return true
  return strictUsageBarrierRetryUserMessageIndex(props.messages, index, message) >= 0
}

const listRootRef = ref<HTMLElement | null>(null)
const slots = useSlots()
const scrollMargin = ref(0)
const measurementVersion = ref(0)
const virtualizationAllowed = ref(readVirtualizationPreference())
const focusedMessageKey = ref<string | null>(null)
const ensuredMessageKeys = ref<ReadonlySet<string>>(new Set())
const layoutAnchorKey = ref<string | null>(null)
const measuredRowElements = new WeakSet<HTMLElement>()
let layoutObserver: ResizeObserver | null = null
let layoutWidth = 0
let layoutGeneration = 0
let liveEdgePinScheduled = false
let cancelledSeek = false
let nativeSmoothSeek = false
let liveEdgeSeek = false
const layoutPending = ref(false)
let readingTextAnchor: { key: string; anchor: TextScrollAnchor } | null = null
let readingAnchor: VirtualizerAnchor | null = null
let readingPosition = -1
const activeNavigation = ref<{ key: string; options: ScrollToOptions } | null>(null)
const scrollHandoff = ref<symbol | null>(null)
let synchronizeScrollOffset: (() => void) | null = null

// Hydrate mounted history previews into the existing message renderer.
// Reads are serialized. Mounted content remains visible; only offscreen cached
// bodies are evicted when the retained-content cache exceeds its budget.
const contentRangeCache = new ContentRangeCache()
const hydratedContent = reactive(new Map<string, string>())
const hydratedContentBytes = new Map<string, number>()
let hydratedContentTotalBytes = 0
const hydrationLoading = reactive(new Set<string>())
const hydrationErrors = reactive(new Map<string, string>())
const hydrationControllers = new Map<string, AbortController>()
let contentHydrationGeneration = 0
const hydrationAttempted = new Set<string>()
const hydrationUnavailable = reactive(new Set<string>())
let automaticHydrationOwner: { generation: number; key?: string } | undefined
let contentHydrationDisposed = false
const MAX_HYDRATED_CONTENT_BYTES = 16 * 1024 * 1024

function contentHydrationKey(message: ChatRenderedMessage, _index: number): string {
  const ref = message.contentRef
  if (ref?.version === 1 && ref.sessionKey && ref.sessionId && ref.messageId) {
    // Message ids are only unique inside a session. Include the complete
    // content identity so a reused ChatMessageList cannot install a late
    // response from the previous session or revision.
    return [
      'content', ref.sessionKey, ref.sessionId, ref.messageId,
      ref.source ?? '', ref.view ?? 'raw', String(ref.byteLength ?? ''),
      ref.revision ?? '', ref.sha256 ?? '', message.contentRevision ?? '',
    ].join('|')
  }
  return chatMessageKey(message, _index)
}

function contentRefFor(message: ChatRenderedMessage): ContentRangeRef | undefined {
  const ref = message.contentRef
  if (!ref || ref.version !== 1 || !ref.sessionKey || !ref.sessionId || !ref.messageId) return undefined
  return ref
}

function canHydrateContent(message: ChatRenderedMessage): boolean {
  if (message.previewComplete === true) return false
  // Raw ranges are safe for plain user rows. Rows with persisted
  // protocol JSON, tool results, or legacy control prompts carry
  // `contentRef.view=display`; that endpoint reapplies the server history
  // projection before any bytes reach the renderer, so assistant/tool rows
  // can be inspected without reintroducing internal wire text.
  if (
    message.turnRunKind === 'plan_implementation'
    || message.provenanceKind === 'plan_implementation'
  ) return false
  // A transformed assistant segment shares the canonical row's contentRef.
  // Only the canonical row owns the automatic read; all slices update
  // from that shared result and must never issue duplicate display exports.
  if (message.clientId?.startsWith('history-model-call-segment:')) return false
  if (message.contentRef?.view === 'display') {
    return ['user', 'assistant', 'tool'].includes(message.displayRole)
  }
  return message.displayRole === 'user' && !message.toolCalls?.length
}

function showContentHydrationStatus(message: ChatRenderedMessage, index: number): boolean {
  if (props.shareMode || message.previewComplete === true) return false
  if (message.clientId?.startsWith('history-model-call-segment:')
    || message.turnRunKind === 'plan_implementation' || message.provenanceKind === 'plan_implementation') return false
  const key = contentHydrationKey(message, index)
  if (hydratedContent.has(key)) return false
  return Boolean(contentRefFor(message) && canHydrateContent(message))
    || message.previewComplete === false || Boolean(message.contentUnavailableReason)
}

function contentAvailabilityLabel(message: ChatRenderedMessage): string {
  if (hydrationUnavailable.has(contentHydrationKey(message, 0))) return t('historyContent.unavailable')
  return message.contentAvailability === 'preparing' || message.contentUnavailableReason === 'content_metadata_pending'
    ? t('shared.loading')
    : t('historyContent.unavailable')
}

function hydratedMessage(message: ChatRenderedMessage, index: number): ChatRenderedMessage {
  // A new complete server projection owns the body even if a previous preview
  // for this content identity was hydrated locally.
  if (message.previewComplete === true) return message
  const key = contentHydrationKey(message, index)
  const text = hydratedContent.get(key)
  if (text === undefined) return message
  const slice = message.contentSlice
  if (!slice) return {
    ...message, text,
    timelineItems: hydrateTimelineText(message, text) ?? message.timelineItems,
  }
  if (
    !Number.isSafeInteger(slice.startCodepoint)
    || !Number.isSafeInteger(slice.endCodepoint)
    || slice.startCodepoint < 0
    || slice.endCodepoint < slice.startCodepoint
  ) return message
  const codepoints = Array.from(text)
  if (slice.endCodepoint > codepoints.length) return message
  return {
    ...message,
    text: codepoints.slice(slice.startCodepoint, slice.endCodepoint).join(''),
  }
}

function hydrateTimelineText(message: ChatRenderedMessage, text: string): ChatRenderedMessage['timelineItems'] {
  const lengths = message.historyPayloadPreview?.textUtf16Lengths
  const timeline = message.timelineItems
  if (!lengths || !timeline || timeline.filter(item => item.type === 'text').length !== lengths.length) return undefined
  // Finalizer stores either compact text or readable paragraph boundaries.
  // Restore only when every prefix and original length agrees with the full
  // semantic body. Tool/interrupt positions, presentation and activityOrder
  // stay unchanged; an unrelated body falls back to canonical text.
  for (const readable of [false, true]) {
    let cursor = 0
    let index = 0
    let previous = ''
    let valid = true
    const restored = timeline.map(item => {
      if (item.type !== 'text') return item
      const length = lengths[index++]!
      const preview = item.rawText ?? ''
      if (readable && previous && !/\s$/.test(previous) && !/^\s/.test(preview)) {
        if (text.slice(cursor, cursor + 2) !== '\n\n') valid = false
        cursor += 2
      }
      const rawText = text.slice(cursor, cursor + length)
      cursor += length
      if (!Number.isSafeInteger(length) || length < preview.length || rawText.length !== length || !rawText.startsWith(preview)) valid = false
      previous = rawText
      return { ...item, rawText }
    })
    if (valid && cursor === text.length) return restored.map(item => item.type === 'text'
      ? { ...item, html: props.renderMarkdown(item.rawText ?? '') } : item)
  }
  return undefined
}

function rememberHydratedContent(key: string, text: string): void {
  const previous = hydratedContentBytes.get(key)
  if (previous !== undefined) hydratedContentTotalBytes -= previous
  hydratedContent.delete(key)
  hydratedContentBytes.delete(key)

  const size = new TextEncoder().encode(text).byteLength
  hydratedContent.set(key, text)
  hydratedContentBytes.set(key, size)
  hydratedContentTotalBytes += size

  trimHydratedContent()
}

function mountedContentKeys(): Set<string> {
  return new Set(renderEntries.value.flatMap(({ index }) => {
    const message = props.messages[index]
    return message ? [contentHydrationKey(message, index)] : []
  }))
}

function trimHydratedContent(): void {
  const mounted = mountedContentKeys()
  for (const key of hydratedContent.keys()) {
    if (hydratedContentTotalBytes <= MAX_HYDRATED_CONTENT_BYTES) break
    // A cache budget must not replace already displayed full text with a
    // preview. Mounted rows own their bodies until they leave the list.
    if (mounted.has(key)) continue
    const evicted = hydratedContentBytes.get(key) ?? 0
    hydratedContent.delete(key)
    hydratedContentBytes.delete(key)
    hydrationAttempted.delete(key)
    hydratedContentTotalBytes -= evicted
  }
}

function contentHydrationError(message: ChatRenderedMessage, index: number): string | undefined {
  return hydrationErrors.get(contentHydrationKey(message, index))
}

function contentHydrationPending(message: ChatRenderedMessage, index: number): boolean {
  return hydrationLoading.has(contentHydrationKey(message, index))
}

async function hydrateContent(message: ChatRenderedMessage, index: number): Promise<void> {
  const ref = contentRefFor(message)
  if (!ref || props.shareMode || !canHydrateContent(message)) return
  const key = contentHydrationKey(message, index)
  if (hydratedContent.has(key) || hydrationLoading.has(key)) return
  hydrationAttempted.add(key)
  if (ref.view !== 'display' && !contentRangeCache.canReadText(ref)) {
    hydrationUnavailable.add(key)
    return
  }
  const controller = new AbortController()
  const generation = contentHydrationGeneration
  hydrationControllers.set(key, controller)
  hydrationLoading.add(key)
  hydrationErrors.delete(key)
  try {
    const text = ref.view === 'display'
      ? await contentRangeCache.readDisplay(ref, { signal: controller.signal })
      : await contentRangeCache.readText(ref, { signal: controller.signal })
    if (generation === contentHydrationGeneration && !controller.signal.aborted) {
      rememberHydratedContent(key, text)
    }
  } catch (error) {
    if (!controller.signal.aborted && generation === contentHydrationGeneration) {
      hydrationErrors.set(key, error instanceof Error ? error.message : t('historyContent.unavailable'))
    }
  } finally {
    if (hydrationControllers.get(key) === controller) {
      hydrationLoading.delete(key)
      hydrationControllers.delete(key)
    }
  }
}

async function hydrateMountedContent(): Promise<void> {
  if (contentHydrationDisposed || props.shareMode
    || automaticHydrationOwner?.generation === contentHydrationGeneration) return
  const generation = contentHydrationGeneration
  const owner = { generation, key: undefined as string | undefined }
  automaticHydrationOwner = owner
  try {
    while (!contentHydrationDisposed && automaticHydrationOwner === owner
      && generation === contentHydrationGeneration && !props.shareMode) {
      const entry = renderEntries.value.find(({ index }) => {
        const message = props.messages[index]
        if (!message || !contentRefFor(message) || !canHydrateContent(message)) return false
        const key = contentHydrationKey(message, index)
        return !hydratedContent.has(key) && !hydrationAttempted.has(key)
          && !hydrationLoading.has(key)
          && !hydrationErrors.has(key) && !hydrationUnavailable.has(key)
      })
      if (!entry) break
      owner.key = contentHydrationKey(props.messages[entry.index], entry.index)
      await hydrateContent(props.messages[entry.index], entry.index)
    }
  } finally {
    // A superseded shared range may finish after a new session/revision read.
    // It cannot release the new pump's lease.
    if (automaticHydrationOwner === owner) automaticHydrationOwner = undefined
  }
}

function resetContentHydrationState(): void {
  contentHydrationGeneration += 1
  for (const controller of hydrationControllers.values()) controller.abort()
  hydrationControllers.clear()
  hydrationLoading.clear()
  hydrationErrors.clear()
  hydratedContent.clear()
  hydrationAttempted.clear()
  hydrationUnavailable.clear()
  hydratedContentBytes.clear()
  hydratedContentTotalBytes = 0
  contentRangeCache.clear()
}

function readVirtualizationPreference(): boolean {
  try {
    return typeof window === 'undefined'
      || window.localStorage.getItem(VIRTUALIZATION_STORAGE_KEY) !== '0'
  } catch {
    return true
  }
}

function estimatedMessageSize(message: ChatRenderedMessage): number {
  const textLength = message.text?.length || 0
  const toolCount = message.toolCalls?.length || 0
  if (message.isRouterStrip) return 76 + MESSAGE_GAP_PX
  if (message.displayRole === 'maintenance') return 52 + MESSAGE_GAP_PX
  if (message.displayRole === 'user') {
    return 68 + Math.min(480, Math.ceil(textLength / 72) * 22) + MESSAGE_GAP_PX
  }
  if (message.displayRole === 'assistant') {
    return 104 + Math.min(1_800, Math.ceil(textLength / 88) * 24)
      + Math.min(640, toolCount * 48) + MESSAGE_GAP_PX
  }
  return 76 + Math.min(520, Math.ceil(textLength / 84) * 22) + MESSAGE_GAP_PX
}

function updateSizeAdjustmentPolicy(
  instance: Virtualizer<HTMLElement, HTMLElement>,
  repeatedObservation = false,
) {
  // Core only caches an initial size when it differs from the estimate. A
  // repeated observation of the same DOM row is still a resize even when that
  // first size happened to equal its estimate. No sizes/keys are stored here.
  instance.shouldAdjustScrollPositionOnItemSizeChange = scrollHandoff.value ? () => false : (
    activeNavigation.value || layoutPending.value || repeatedObservation
  ) ? (item, _delta, current) => item.end <= (current.scrollOffset ?? 0) : undefined
}

const messageKeys = computed(() => props.messages.map(chatMessageKey))
const hasTrailing = computed(() => Boolean(slots.trailing))
const virtualizationEnabled = computed(() => (
  virtualizationAllowed.value && !props.shareMode && !props.virtualizationDisabled
  && Boolean(props.scrollContainer) && props.messages.length >= 60
))
const MAX_NON_VIRTUALIZED_ROWS = 120
const forcedIndexes = computed(() => {
  const forced = new Set<number>()
  props.messages.forEach((message, index) => {
    const key = messageKeys.value[index]
    if (
      ensuredMessageKeys.value.has(key) || focusedMessageKey.value === key
      || layoutAnchorKey.value === key
      || props.forceMountMessageKeys?.has(key)
      || message.isStreaming || message.maintenance?.state === 'running'
      || message.parts?.some(part => part.type === 'interrupt' && !part.resolution)
    ) forced.add(index)
  })
  if (props.isStreaming && props.messages.length) forced.add(props.messages.length - 1)
  if (hasTrailing.value) forced.add(props.messages.length)
  return forced
})

// The end anchor preserves keyed reading positions on prepend. In the pinned
// core version a negative threshold also disables resize end-pinning. Keep
// this policy together: followOnAppend=false alone does NOT stop resize pins.
const virtualizer = useVirtualizer<HTMLElement, HTMLElement>(computed(() => {
  const keys = messageKeys.value
  const forced = forcedIndexes.value
  const virtualized = virtualizationEnabled.value
  const count = keys.length + Number(hasTrailing.value)
  return {
    count,
    getScrollElement: () => props.scrollContainer ?? null,
    getItemKey: (index: number) => index < keys.length
      ? keys[index] : 'trailing:' + (props.trailingKey ?? props.sessionKey ?? ''),
    estimateSize: (index: number) => index < props.messages.length
      ? estimatedMessageSize(props.messages[index]) : 0,
    overscan: 5,
    scrollMargin: scrollMargin.value,
    paddingEnd: props.bottomPadding ?? 0,
    scrollPaddingStart: 16,
    // The live tail becomes a different canonical message row on completion.
    // During that semantic handoff the text anchor, not the old tail key, owns Y.
    anchorTo: scrollHandoff.value ? 'start' as const : 'end' as const,
    followOnAppend: props.followLiveEdge === true,
    scrollEndThreshold: props.followLiveEdge ? 80 : -1,
    rangeExtractor: (range: Parameters<typeof defaultRangeExtractor>[0]) => virtualized
      ? [...new Set([...defaultRangeExtractor(range), ...forced])].sort((a, b) => a - b)
      : Array.from({ length: count }, (_, index) => index),
    scrollToFn: (offset: number, options: Parameters<typeof elementScroll>[1], instance: Virtualizer<HTMLElement, HTMLElement>) => {
      // A cancelled index/end seek can still reconcile on the next animation
      // frame. Allow library geometry corrections (no behavior / adjustments),
      // but never let that old navigation take ownership back from the reader.
      if (cancelledSeek && options.behavior !== undefined && options.adjustments === undefined) return
      if (scrollHandoff.value && options.behavior === undefined) return
      const container = props.scrollContainer
      if (options.behavior !== undefined) nativeSmoothSeek = options.behavior === 'smooth'
      if (container && options.behavior === undefined && options.adjustments === undefined
        && offset > container.scrollHeight - container.clientHeight) {
        // Vue's adapter applies keyed prepend anchors in its pre-render
        // watcher. An anchor beyond the old DOM's range would be clamped and
        // its native scroll event would replace the new keyed position.
        // Let only this structural write wait for the larger DOM to commit.
        const generation = layoutGeneration
        void nextTick(() => {
          if (generation !== layoutGeneration || container !== props.scrollContainer) return
          applyProgrammaticScroll(container, () => elementScroll(
            instance.scrollOffset ?? offset, options, instance,
          ))
        })
        return
      }
      if (container) applyProgrammaticScroll(container, () => elementScroll(offset, options, instance))
    },
    observeElementRect: (instance: Virtualizer<HTMLElement, HTMLElement>, callback: (rect: Rect) => void) => (
      observeElementRect(instance, rect => {
        callback(rect)
        if (props.followLiveEdge) queueLiveEdgePin()
      })
    ),
    observeElementOffset: (instance: Virtualizer<HTMLElement, HTMLElement>, callback: (offset: number, scrolling: boolean) => void) => {
      const container = instance.scrollElement
      const synchronize = () => { if (container) callback(container.scrollTop, false) }
      synchronizeScrollOffset = synchronize
      const cleanup = observeElementOffset(instance, (offset, scrolling) => {
        callback(offset, scrolling)
        rememberReadingAnchor()
        if (!scrolling) void nextTick(captureReadingText)
      })
      return () => {
        if (synchronizeScrollOffset === synchronize) synchronizeScrollOffset = null
        cleanup?.()
      }
    },
    // Keep fractional RO measurements, but let TanStack reuse its measurements
    // during Vue ref updates. Synchronously measuring every ref again can race
    // the native scroll event after a width-restoration write.
    measureElement: (element: HTMLElement, entry: ResizeObserverEntry | undefined, instance: Virtualizer<HTMLElement, HTMLElement>) => {
      updateSizeAdjustmentPolicy(instance, Boolean(entry && measuredRowElements.has(element)))
      measuredRowElements.add(element)
      return entry?.borderBoxSize?.[0]?.blockSize ?? measureVirtualElement(element, entry, instance)
    },
    onChange: () => {
      measurementVersion.value += 1
      if (props.followLiveEdge) queueLiveEdgePin()
    },
  }
}))

// During an explicit seek/reflow, late code controls above the reading line
// still need compensation after an upward move. Keep TanStack's default
// first-measure/prepend policy for ordinary reader scrolling.
watch([activeNavigation, layoutPending, scrollHandoff], () => {
  updateSizeAdjustmentPolicy(virtualizer.value)
}, { immediate: true, flush: 'sync' })

const variableLayout = computed(() => {
  // Populate the same geometry even when an export renders every row.
  const totalSize = virtualizer.value.getTotalSize()
  if (!virtualizationEnabled.value) {
    // Share/export views intentionally render every row. Interactive views
    // without a scroll container still get a hard DOM ceiling so a large
    // history cannot freeze the renderer before virtualization is available.
    const first = props.shareMode
      ? 0
      : Math.max(0, messageKeys.value.length - MAX_NON_VIRTUALIZED_ROWS)
    const entries = messageKeys.value.slice(first).map((key, index) => ({
      key,
      index: first + index,
      gapBefore: 0,
    }))
    if (hasTrailing.value) entries.push({
      key: 'trailing:' + (props.trailingKey ?? props.sessionKey ?? ''),
      index: props.messages.length,
      gapBefore: 0,
    })
    return {
      entries,
      topSpacer: first > 0 ? first * 76 : 0,
      bottomSpacer: props.bottomPadding ?? 0,
    }
  }
  const items = virtualizer.value.getVirtualItems()
  const margin = scrollMargin.value
  return {
    entries: items.map((item, index) => ({
      key: String(item.key),
      index: item.index,
      gapBefore: index ? Math.max(0, item.start - items[index - 1].end) : 0,
    })),
    topSpacer: Math.max(0, (items[0]?.start ?? margin) - margin),
    bottomSpacer: Math.max(0, totalSize
      - ((items[items.length - 1]?.end ?? margin) - margin)),
  }
})
const renderEntries = computed(() => variableLayout.value.entries)

watch(() => [props.shareMode, props.sessionKey, props.scrollEpoch, ...renderEntries.value.map(({ index }) => {
  const message = props.messages[index]
  return message ? [contentHydrationKey(message, index), message.previewComplete, canHydrateContent(message)] : null
})], () => {
  const mounted = mountedContentKeys()
  const previousOwner = automaticHydrationOwner
  if (previousOwner?.key && !mounted.has(previousOwner.key)) {
    automaticHydrationOwner = undefined
    hydrationControllers.get(previousOwner.key)?.abort()
    // Releasing the waiter must be synchronous: a shared raw transport can
    // remain pending after abort, while the same row is already mounted again.
    // The old finally block checks controller identity before touching a new read.
    hydrationControllers.delete(previousOwner.key)
    hydrationLoading.delete(previousOwner.key)
  }
  // An evicted successful body can be restored when its row is mounted again.
  // Failed reads retain their explicit retry state instead of polling.
  for (const key of hydrationAttempted) if (!mounted.has(key)) hydrationAttempted.delete(key)
  trimHydratedContent()
  void hydrateMountedContent()
}, { immediate: true, flush: 'post' })

function spacerStyle(height: number): Record<string, string> {
  return { height: height + 'px' }
}

function setRowElement(value: Element | ComponentPublicInstance | null) {
  const element = value instanceof HTMLElement ? value : null
  virtualizer.value.measureElement(element)
}

function messageRow(index: number): HTMLElement | null {
  return listRootRef.value?.querySelector<HTMLElement>(
    '[data-chat-message-index="' + index + '"]',
  ) ?? null
}

function messageElement(index: number): HTMLElement | null {
  const row = messageRow(index)
  return row?.querySelector<HTMLElement>('[data-chat-turn-key]')
    || row?.firstElementChild as HTMLElement | null || row
}

async function ensureMessageVisible(index: number): Promise<HTMLElement | null> {
  const key = messageKeys.value[index]
  if (!key) return null
  const session = props.sessionKey
  const epoch = props.scrollEpoch
  ensuredMessageKeys.value = new Set([...ensuredMessageKeys.value, key])
  await nextTick()
  if (session !== props.sessionKey || epoch !== props.scrollEpoch) return null
  return messageElement(messageKeys.value.indexOf(key))
}

function releaseEnsuredMessage(index?: number) {
  const next = new Set(ensuredMessageKeys.value)
  if (index === undefined) next.clear()
  else next.delete(messageKeys.value[index])
  ensuredMessageKeys.value = next
  if (activeNavigation.value && !next.has(activeNavigation.value.key)) activeNavigation.value = null
}

function scrollToMessage(index: number, options: ScrollToOptions = {}) {
  const key = messageKeys.value[index]
  if (!key) return
  activeNavigation.value = ensuredMessageKeys.value.has(key) ? { key, options } : null
  scrollHandoff.value = null
  liveEdgeSeek = false
  cancelledSeek = false
  virtualizer.value.scrollToIndex(index, options)
}

function scrollToEnd(options?: Pick<ScrollToOptions, 'behavior'>) {
  activeNavigation.value = null
  scrollHandoff.value = null
  liveEdgeSeek = true
  cancelledSeek = false
  virtualizer.value.scrollToEnd(options)
}

function cancelScroll() {
  activeNavigation.value = null
  scrollHandoff.value = null
  liveEdgeSeek = false
  cancelledSeek = true
  layoutGeneration += 1
  layoutPending.value = false
  layoutAnchorKey.value = null
  readingTextAnchor = null
  readingAnchor = null
  readingPosition = -1
  // Do not write the current scrollTop here: Chromium may already have applied
  // the wheel delta before dispatching wheel. Marking that position as an app
  // write would swallow the genuine scroll event and restart bottom following.
  // Replacing the old index seek also releases its smooth-scroll measurement
  // restriction. The writer guard above makes this command state-only.
  const container = props.scrollContainer
  if (container) virtualizer.value.scrollToOffset(container.scrollTop, { behavior: 'auto' })
  // A nested scroller's consumed wheel still has to stop an outer smooth
  // animation. This native no-op stops it without tagging the reader's delta.
  if (nativeSmoothSeek && props.scrollContainer) {
    props.scrollContainer.scrollTo({ top: props.scrollContainer.scrollTop, behavior: 'instant' })
    nativeSmoothSeek = false
  }
}

function beginScrollHandoff(): () => void {
  cancelScroll()
  const owner = Symbol('terminal-answer')
  scrollHandoff.value = owner
  return () => {
    if (scrollHandoff.value !== owner) return
    // The semantic owner may have moved the DOM directly. Publish that actual
    // offset through the current public observer before re-enabling geometry
    // corrections; never replay the old live-tail position.
    synchronizeScrollOffset?.()
    scrollHandoff.value = null
  }
}

function stopScrollReconciliation() {
  const container = props.scrollContainer
  if (container) {
    cancelledSeek = false
    virtualizer.value.scrollToOffset(container.scrollTop, { behavior: 'auto' })
  }
}

function queueLiveEdgePin() {
  if (liveEdgePinScheduled) return
  liveEdgePinScheduled = true
  const generation = layoutGeneration
  void nextTick(() => {
    liveEdgePinScheduled = false
    if (generation !== layoutGeneration || !props.followLiveEdge) return
    const container = props.scrollContainer
    if (container && container.scrollHeight <= container.clientHeight) {
      // A fitting transcript never emits the native scroll event that would
      // acknowledge a clamped size correction. Publish its committed offset
      // through core's observer so an old seek can finish without RAF polling.
      if (virtualizer.value.scrollOffset !== container.scrollTop) synchronizeScrollOffset?.()
      return
    }
    scrollToEnd()
  })
}

function updateLayout() {
  const root = listRootRef.value
  const container = props.scrollContainer
  if (!root || !container) return
  const rect = root.getBoundingClientRect()
  const initialized = layoutWidth > 0
  const changedWidth = initialized && Math.abs(layoutWidth - rect.width) > 0.5
  layoutWidth = rect.width
  const nextMargin = rect.top - container.getBoundingClientRect().top + container.scrollTop
  if (Math.abs(scrollMargin.value - nextMargin) > 0.1) {
    if (initialized && !changedWidth && container.scrollTop > 0) {
      // The history/recovery header is outside the virtual rows. TanStack
      // anchors keyed prepends, but changing scrollMargin alone is not a
      // prepend; reuse the same layout transaction without discarding sizes.
      preserveLayoutChange(() => { scrollMargin.value = nextMargin })
    } else scrollMargin.value = nextMargin
    if (props.followLiveEdge) queueLiveEdgePin()
  }
  if (changedWidth) remeasure()
  else {
    rememberReadingAnchor()
    captureReadingText()
  }
}

function rememberReadingAnchor() {
  const container = props.scrollContainer
  const root = listRootRef.value
  if (!container || !root || props.followLiveEdge || layoutPending.value || !layoutWidth
    || Math.abs(root.getBoundingClientRect().width - layoutWidth) > 0.5) return
  const offset = container.scrollTop
  const item = virtualizer.value.getVirtualItemForOffset(offset)
  if (!item) { readingAnchor = null; return }
  let anchor = item
  // A small leftover tail is not the reader's primary content. Anchor the
  // next complete row, while near-boundary and oversized-message reads keep
  // their current row (with text anchoring inside long answers).
  if (offset - item.start > item.size / 2 && item.size < container.clientHeight) {
    anchor = virtualizer.value.getVirtualItems().find(candidate => (
      candidate.index > item.index && candidate.index < props.messages.length
      && candidate.start >= offset && candidate.end <= offset + container.clientHeight
    )) ?? item
  }
  readingAnchor = { key: anchor.key, intraOffset: offset - anchor.start }
}

function captureReadingText() {
  const container = props.scrollContainer
  if (!container || props.followLiveEdge || layoutPending.value || virtualizer.value.isScrolling) return
  if (readingPosition === container.scrollTop) return
  readingPosition = container.scrollTop
  const item = virtualizer.value.getVirtualItemForOffset(container.scrollTop)
  // Near a row boundary, that boundary is the reading anchor. Preserving a
  // token which merely wraps to another line would unnecessarily move it.
  // Semantic anchoring is for a reader already inside a long message.
  const deepInside = item && readingAnchor?.key === item.key && container.scrollTop - item.start > 64
  const anchor = deepInside ? captureVisibleTextScrollAnchor(container, messageRow(item.index)) : null
  readingTextAnchor = anchor && item ? { key: String(item.key), anchor } : null
}

/** Width/font changes invalidate offscreen heights, which RO cannot observe. */
function remeasure() {
  preserveLayoutChange()
}

function preserveLayoutChange(applyLayoutChange?: () => void) {
  cancelledSeek = false
  const generation = ++layoutGeneration
  const anchorOverride = applyLayoutChange ? null : readingAnchor
  const textAnchor = !applyLayoutChange && readingTextAnchor?.key === anchorOverride?.key
    ? readingTextAnchor : null
  layoutPending.value = true
  // A leased explicit destination owns navigation until arrival/cancellation.
  // Width reflow must refresh that seek, not turn it into a reading-position
  // restoration somewhere along its smooth-scroll journey.
  const navigation = activeNavigation.value
  if (navigation) {
    if (applyLayoutChange) applyLayoutChange()
    else virtualizer.value.measure()
    void nextTick(() => {
      if (generation !== layoutGeneration) return
      if (activeNavigation.value === navigation) {
        const index = messageKeys.value.indexOf(navigation.key)
        if (index >= 0) virtualizer.value.scrollToIndex(index, navigation.options)
      }
      layoutPending.value = false
    })
    return
  }
  void remeasureVirtualizer(virtualizer.value, {
    shouldFollowEnd: () => props.followLiveEdge === true,
    isCurrent: () => generation === layoutGeneration,
    getElement: messageRow,
    applyLayoutChange,
    anchorOverride,
    keepAnchorMounted: key => {
      layoutAnchorKey.value = String(key)
      return () => {
        if (generation === layoutGeneration) layoutAnchorKey.value = null
      }
    },
  }).then(restored => {
    if (generation !== layoutGeneration) return
    if (restored && !props.followLiveEdge && textAnchor) {
      const index = messageKeys.value.indexOf(textAnchor.key)
      if (restoreTextScrollAnchor(textAnchor.anchor, messageRow(index))) stopScrollReconciliation()
    }
    layoutPending.value = false
    readingPosition = -1
    rememberReadingAnchor()
    captureReadingText()
  })
}

function onFocusIn(event: FocusEvent) {
  const target = event.target
  focusedMessageKey.value = target instanceof Element
    ? target.closest<HTMLElement>('[data-chat-message-key]')?.dataset.chatMessageKey ?? null
    : null
}

function onFocusOut() {
  void nextTick(() => {
    const active = document.activeElement
    if (!(active instanceof Element) || !listRootRef.value?.contains(active)) {
      focusedMessageKey.value = null
    }
  })
}

function hasPendingLayout(): boolean {
  const container = props.scrollContainer
  const rect = virtualizer.value.scrollRect
  if (!container || !rect) return layoutPending.value
  // Native scroll clamping is delivered before ResizeObserver. Until core
  // sees this viewport, its previous end seek must not become reader intent.
  return layoutPending.value
    || Math.abs(rect.width - container.offsetWidth) > 0.5
    || Math.abs(rect.height - container.offsetHeight) > 0.5
}

function syncPreference(event: StorageEvent) {
  if (event.key === null || event.key === VIRTUALIZATION_STORAGE_KEY) {
    virtualizationAllowed.value = readVirtualizationPreference()
  }
}

defineExpose<ChatMessageListVirtualizer>({
  ensureMessageVisible,
  releaseEnsuredMessage,
  messageIndexAtOffset: offset => {
    const index = virtualizer.value.getVirtualItemForOffset(offset)?.index
    return index === undefined || !props.messages.length ? null
      : Math.min(index, props.messages.length - 1)
  },
  scrollToMessage,
  scrollToEnd,
  getDistanceFromEnd: () => readDistanceFromEnd(props.scrollContainer, virtualizer.value),
  hasPendingLayout,
  cancelScroll,
  beginScrollHandoff,
  geometryVersion: () => measurementVersion.value,
  remeasure,
  isVirtualized: () => virtualizationEnabled.value,
})

watch([() => props.sessionKey, () => props.scrollEpoch], (current, previous) => {
  if (current[0] !== previous[0] || current[1] !== previous[1]) resetContentHydrationState()
  cancelScroll()
  ensuredMessageKeys.value = new Set()
  focusedMessageKey.value = null
  virtualizer.value.measure()
}, { flush: 'sync' })
watch(messageKeys, keys => {
  const retained = new Set(keys)
  ensuredMessageKeys.value = new Set([...ensuredMessageKeys.value].filter(key => retained.has(key)))
  if (readingAnchor && !retained.has(String(readingAnchor.key))) readingAnchor = null
})
watch(() => props.followLiveEdge, follow => {
  // Scrollbar/middle-button navigation can pause follow without an input
  // callback. Retire its previous end seek before a viewport resize moves
  // that target; a newly issued message navigation still owns its own seek.
  if (follow) {
    cancelledSeek = false
    queueLiveEdgePin()
  } else if (liveEdgeSeek) cancelScroll()
}, { flush: 'sync' })
watch(() => props.bottomPadding, () => {
  if (props.followLiveEdge) queueLiveEdgePin()
})
watch(() => props.layoutHeader, (header, previous) => {
  if (previous) layoutObserver?.unobserve(previous)
  if (header) layoutObserver?.observe(header)
  void nextTick(updateLayout)
})
onMounted(() => {
  window.addEventListener('storage', syncPreference)
  document.fonts?.addEventListener('loadingdone', remeasure)
  listRootRef.value?.addEventListener('focusin', onFocusIn)
  listRootRef.value?.addEventListener('focusout', onFocusOut)
  if (typeof ResizeObserver !== 'undefined') {
    layoutObserver = new ResizeObserver(updateLayout)
    if (listRootRef.value) layoutObserver.observe(listRootRef.value)
    if (props.layoutHeader) layoutObserver.observe(props.layoutHeader)
  }
  updateLayout()
})
onUpdated(updateLayout)
onBeforeUnmount(() => {
  contentHydrationDisposed = true
  layoutGeneration += 1
  scrollHandoff.value = null
  resetContentHydrationState()
  window.removeEventListener('storage', syncPreference)
  document.fonts?.removeEventListener('loadingdone', remeasure)
  listRootRef.value?.removeEventListener('focusin', onFocusIn)
  listRootRef.value?.removeEventListener('focusout', onFocusOut)
  layoutObserver?.disconnect()
})

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

<style scoped>
.chat-message-list {
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  width: 100%;
  min-width: 0;
}

.chat-message-list__row {
  display: flow-root;
  flex: 0 0 auto;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding-bottom: 0.25rem;
}

.chat-message-list__trailing {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  padding-top: 0.25rem;
  flex: 0 0 auto;
  min-width: 0;
}

.chat-message-list__row--last {
  padding-bottom: 0;
}

.chat-history-content-hydration {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.25rem 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.chat-history-content-hydration__button {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.2rem 0.45rem;
  color: inherit;
  background: var(--bg-surface);
  cursor: pointer;
}

.chat-history-content-hydration__button:disabled {
  cursor: progress;
  opacity: 0.7;
}

.chat-history-content-hydration__error {
  color: var(--status-warning, var(--text-muted));
}

.chat-message-list__spacer {
  flex: 0 0 auto;
  width: 1px;
  min-height: 0;
  pointer-events: none;
  /* Virtual offsets must commit atomically, including under the global
     reduced-motion rule which otherwise gives every element a transition. */
  transition: none !important;
}
</style>
