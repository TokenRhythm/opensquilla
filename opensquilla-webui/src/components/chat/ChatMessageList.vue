<template>
  <div
    ref="listRootRef"
    class="chat-message-list"
    :data-virtualized="virtualizationEnabled ? 'true' : 'false'"
    :data-rendered-message-count="renderEntries.length"
  >
    <div
      v-if="virtualizationEnabled"
      class="chat-message-list__spacer"
      :style="spacerStyle(variableLayout.topSpacer)"
      data-testid="chat-history-top-spacer"
      aria-hidden="true"
    />
    <template v-for="entry in renderEntries" :key="entry.key">
      <div
        v-if="virtualizationEnabled && entry.gapBefore > 0"
        class="chat-message-list__spacer"
        :style="spacerStyle(entry.gapBefore)"
        data-testid="chat-history-gap-spacer"
        aria-hidden="true"
      />
      <div
        :ref="element => setRowElement(element, entry.key)"
        class="chat-message-list__row"
        :class="{ 'chat-message-list__row--last': entry.index === messages.length - 1 }"
        :data-chat-message-key="entry.key"
        :data-chat-message-index="entry.index"
        :data-chat-message-forced="forcedIndexes.has(entry.index) ? 'true' : 'false'"
        data-testid="chat-message-row"
      >
        <slot
          v-if="messages[entry.index].isRouterStrip"
          name="router-strip"
          :message="messages[entry.index]"
          :index="entry.index"
        />
        <UserMessage
          v-else-if="messages[entry.index].displayRole === 'user'"
          :id="`chat-turn-${entry.index}`"
          :data-chat-turn-key="chatMessageKey(messages[entry.index], entry.index)"
          tabindex="-1"
          :message="messages[entry.index]"
          :share-mode="shareMode"
          :share-selected="selectedMessageIds.has(chatMessageKey(messages[entry.index], entry.index))"
          :share-message-id="chatMessageKey(messages[entry.index], entry.index)"
          :strip-time-prefix="stripTimePrefix"
          :copy-message="copyMessage"
          :download-attachment="downloadAttachment"
          :show-turn-outcome="isTurnTip(entry.index)"
          :is-streaming="isStreaming"
          :is-goal-source="isGoalSource(messages[entry.index])"
          :can-reuse-prompt-annotations="canReusePromptAnnotations === true"
          :workbench-resource-preview-enabled="workbenchResourcePreviewEnabled === true"
          :workbench-resource-edit-enabled="workbenchResourceEditEnabled === true"
          :workbench-attachment-resources="workbenchAttachmentResources"
          @edit="$emit('editMessage', $event)"
          @edit-attachment="$emit('editAttachment', $event)"
          @preview-attachment="$emit('previewAttachment', $event)"
          @reuse-prompt-annotation="$emit('reusePromptAnnotation', $event)"
          @toggle-share="$emit('toggleShareMessage', $event)"
        />
        <CompactionEvent
          v-else-if="messages[entry.index].displayRole === 'maintenance' && messages[entry.index].maintenance?.kind === 'context_compaction'"
          :message="messages[entry.index]"
        />
        <AssistantMessage
          v-else-if="messages[entry.index].displayRole === 'assistant'"
          :message="messages[entry.index]"
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
          :auth-token="authToken"
          :workbench-enabled="workbenchEnabled"
          :artifact-navigation-items="artifactNavigationItems"
          :copy-message="copyMessage"
          :regenerate-available="assistantRegenerateAvailable(entry.index)"
          :is-tip="isForkableAssistant(entry.index)"
          :fork-busy="forkBusy"
          :plan-action-pending="planActionPending"
          :plan-actions-disabled="planActionsDisabled"
          :show-turn-outcome="isTurnTip(entry.index)"
          :goal-outcome="goalOutcomeFor(messages[entry.index], entry.index)"
          :goal-elapsed="goalElapsed"
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
        />
        <SystemMessage
          v-else
          :message="messages[entry.index]"
          :subagent-summary="subagentSummary"
          :subagent-body="subagentBody"
          :retry-available="usageBarrierRetryAvailable(entry.index)"
          @resume="$emit('resumeSandbox')"
          @retry="forwardSystemRetry"
        />
      </div>
    </template>
    <div
      v-if="virtualizationEnabled"
      class="chat-message-list__spacer"
      :style="spacerStyle(variableLayout.bottomSpacer)"
      data-testid="chat-history-bottom-spacer"
      aria-hidden="true"
    />
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
  type ComponentPublicInstance,
} from 'vue'
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
import type { PromptAnnotationSnapshot } from '@/types/promptAnnotations'
import type { WorkbenchResource } from '@/types/workbenchResources'
import { chatMessageKey } from '@/utils/chat/messageIdentity'
import { applyProgrammaticScroll } from '@/utils/chat/scrollMutation'
import {
  isUsageAccountingBarrierMessage,
  strictUsageBarrierRetryUserMessageIndex,
} from '@/utils/chat/usageAccountingFailure'
import {
  buildVariableWindow,
  CHAT_HISTORY_VIRTUALIZATION_THRESHOLD,
  type ChatMessageListVirtualizer,
  type VariableWindowEntry,
} from '@/utils/chat/variableMessageWindow'

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
  workbenchResourcePreviewEnabled?: boolean
  workbenchResourceEditEnabled?: boolean
  workbenchAttachmentResources?: ReadonlyMap<string, WorkbenchResource>
  canReusePromptAnnotations?: boolean
  forkBusy?: boolean
  planActionPending?: PlanCardAction | null
  planActionsDisabled?: boolean
  isStreaming?: boolean
  goal?: GoalSnapshot | null
  goalElapsed?: string
  /** Required for long-history virtualization; omitted by legacy embedders. */
  scrollContainer?: HTMLElement | null
  /** Session/render epoch used to invalidate deferred scroll corrections. */
  scrollEpoch?: number
  /** Preview/export paths can force a complete, canonical DOM. */
  virtualizationDisabled?: boolean
  /** Current search match or another externally owned focus target. */
  forceMountMessageKeys?: ReadonlySet<string>
  /** Keep the live edge pinned while estimated row heights settle. */
  followLiveEdge?: boolean
}>()

const emit = defineEmits<{
  editMessage: [message: ChatRenderedMessage]
  editAttachment: [attachment: import('@/types/chat').DisplayAttachment]
  previewAttachment: [attachment: import('@/types/chat').DisplayAttachment]
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
  resumeSandbox: []
  planImplementCurrent: [target: PlanCardActionTarget]
  planImplementNew: [target: PlanCardActionTarget]
  planReplan: [target: PlanCardActionTarget]
}>()

const VIRTUALIZATION_STORAGE_KEY = 'opensquilla.chat.virtualizeHistory'
const MESSAGE_GAP_PX = 4

function forwardSystemRetry(
  message: ChatRenderedMessage,
  settle: (accepted: boolean) => void,
) {
  emit('regenerateMessage', message, settle)
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
const viewportStart = ref(0)
const viewportSize = ref(1)
const measurementVersion = ref(0)
const virtualizationAllowed = ref(readVirtualizationPreference())
const focusedMessageKey = ref<string | null>(null)
const ensuredMessageKeys = ref<ReadonlySet<string>>(new Set())
const rowElements = new Map<string, HTMLElement>()
const measuredSizes = new Map<string, number>()

let attachedContainer: HTMLElement | null = null
let rowResizeObserver: ResizeObserver | null = null
let viewportResizeObserver: ResizeObserver | null = null
let viewportFrame = 0
let pendingAnchorAdjustment = 0
let anchorAdjustmentScheduled = false
let liveEdgePinScheduled = false
let deferredScrollGeneration = 0

function currentScrollEpoch(): number {
  return props.scrollEpoch ?? 0
}

function resetDeferredScrollWork() {
  deferredScrollGeneration += 1
  pendingAnchorAdjustment = 0
  anchorAdjustmentScheduled = false
  liveEdgePinScheduled = false
}

function readVirtualizationPreference(): boolean {
  if (typeof window === 'undefined') return true
  try {
    return window.localStorage.getItem(VIRTUALIZATION_STORAGE_KEY) !== '0'
  } catch {
    // Storage can be denied in hardened/private contexts. The feature is a
    // rendering optimization and remains safe without persistent settings.
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
    return 104
      + Math.min(1_800, Math.ceil(textLength / 88) * 24)
      + Math.min(640, toolCount * 48)
      + MESSAGE_GAP_PX
  }
  return 76 + Math.min(520, Math.ceil(textLength / 84) * 22) + MESSAGE_GAP_PX
}

const windowRows = computed(() => props.messages.map((message, index) => ({
  key: chatMessageKey(message, index),
  estimatedSize: estimatedMessageSize(message),
})))

const virtualizationEnabled = computed(() => (
  virtualizationAllowed.value
  && !props.shareMode
  && !props.virtualizationDisabled
  && Boolean(props.scrollContainer)
  && props.messages.length >= CHAT_HISTORY_VIRTUALIZATION_THRESHOLD
))

function messageNeedsForcedMount(message: ChatRenderedMessage, index: number): boolean {
  void index
  // Forced mounts are correctness leases for live/interactive state, not a
  // permanent archive of every row that once ended unusually. Settled stop,
  // failure, Goal and maintenance rows can remount normally when they enter
  // the viewport; retaining them all would defeat the 30-row DOM ceiling.
  if (message.isStreaming || message.maintenance?.state === 'running') return true
  return Boolean(message.parts?.some(part => part.type === 'interrupt' && !part.resolution))
}

const forcedIndexes = computed(() => {
  const forced = new Set<number>()
  const externalKeys = props.forceMountMessageKeys
  windowRows.value.forEach((row, index) => {
    if (
      ensuredMessageKeys.value.has(row.key)
      || focusedMessageKey.value === row.key
      || externalKeys?.has(row.key)
      || messageNeedsForcedMount(props.messages[index], index)
    ) forced.add(index)
  })
  // The settled tail can still own turn actions while the next turn streams in
  // the parent. Keep only that one row mounted rather than expanding the whole
  // window from a reader's historical position to the live edge.
  if (props.isStreaming && props.messages.length > 0) forced.add(props.messages.length - 1)
  return forced
})

const variableLayout = computed(() => {
  // Map mutations are intentionally non-reactive; a scalar invalidation avoids
  // proxying a growing height cache on every ResizeObserver delivery.
  void measurementVersion.value
  return buildVariableWindow({
    rows: windowRows.value,
    measuredSizes,
    viewportStart: viewportStart.value,
    viewportSize: viewportSize.value,
    forceIndexes: forcedIndexes.value,
  })
})

const renderEntries = computed<VariableWindowEntry[]>(() => {
  if (virtualizationEnabled.value) return variableLayout.value.entries
  return windowRows.value.map((row, index) => ({ index, key: row.key, gapBefore: 0 }))
})

function spacerStyle(height: number): Record<string, string> {
  return { height: `${Math.max(0, height)}px` }
}

function frame(callback: () => void): number {
  if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
    return window.requestAnimationFrame(() => callback())
  }
  return globalThis.setTimeout(callback, 0) as unknown as number
}

function cancelScheduledFrame(id: number) {
  if (!id || typeof window === 'undefined') return
  if (typeof window.cancelAnimationFrame === 'function') window.cancelAnimationFrame(id)
  else window.clearTimeout(id)
}

function listStartInContainer(): number | null {
  const root = listRootRef.value
  const container = props.scrollContainer
  if (!root || !container) return null
  const rootRect = root.getBoundingClientRect()
  const containerRect = container.getBoundingClientRect()
  return rootRect.top - containerRect.top + container.scrollTop
}

function measureViewport() {
  viewportFrame = 0
  const container = props.scrollContainer
  const listStart = listStartInContainer()
  if (!container || listStart === null) return
  viewportStart.value = Math.max(0, container.scrollTop - listStart)
  viewportSize.value = Math.max(1, container.clientHeight)
}

function scheduleViewportMeasure() {
  if (viewportFrame) return
  viewportFrame = frame(measureViewport)
}

function queueAnchorAdjustment(delta: number) {
  const container = props.scrollContainer
  if (!container || Math.abs(delta) < 0.5) return
  const epoch = currentScrollEpoch()
  const sessionKey = props.sessionKey
  const generation = deferredScrollGeneration
  pendingAnchorAdjustment += delta
  if (anchorAdjustmentScheduled) return
  anchorAdjustmentScheduled = true
  void nextTick(() => {
    if (deferredScrollGeneration !== generation) return
    anchorAdjustmentScheduled = false
    const adjustment = pendingAnchorAdjustment
    pendingAnchorAdjustment = 0
    if (!props.scrollContainer || props.scrollContainer !== container) return
    if (props.sessionKey !== sessionKey || currentScrollEpoch() !== epoch) return
    applyProgrammaticScroll(container, () => {
      container.scrollTop += adjustment
    })
    scheduleViewportMeasure()
  })
}

function queueLiveEdgePin() {
  const container = props.scrollContainer
  if (!container || liveEdgePinScheduled) return
  const epoch = currentScrollEpoch()
  const sessionKey = props.sessionKey
  const generation = deferredScrollGeneration
  liveEdgePinScheduled = true
  void nextTick(() => {
    if (deferredScrollGeneration !== generation) return
    liveEdgePinScheduled = false
    if (!props.followLiveEdge || props.scrollContainer !== container) return
    if (props.sessionKey !== sessionKey || currentScrollEpoch() !== epoch) return
    applyProgrammaticScroll(container, () => {
      container.scrollTop = container.scrollHeight
    })
    scheduleViewportMeasure()
  })
}

function measuredRowHeight(target: HTMLElement): number {
  const height = target.getBoundingClientRect().height
  return Number.isFinite(height) && height > 0 ? height : 0
}

function onRowsResize(entries: ResizeObserverEntry[]) {
  if (!virtualizationEnabled.value) return
  const container = props.scrollContainer
  const containerTop = container?.getBoundingClientRect().top ?? 0
  let changed = false
  let anchorDelta = 0
  for (const entry of entries) {
    const target = entry.target
    if (!(target instanceof HTMLElement)) continue
    const key = target.dataset.chatMessageKey
    const index = Number(target.dataset.chatMessageIndex)
    if (!key || !Number.isInteger(index) || index < 0 || index >= windowRows.value.length) continue
    const height = measuredRowHeight(target)
    if (height <= 0) continue
    const previous = measuredSizes.get(key) ?? windowRows.value[index].estimatedSize
    if (Math.abs(previous - height) < 0.5) continue
    measuredSizes.set(key, height)
    changed = true
    const delta = height - previous
    // Estimated offsets can disagree with the physical viewport precisely
    // while variable heights are settling. Derive the row's pre-resize bottom
    // from its current DOM rect and the observed delta; if that old edge was
    // above the viewport, preserve the reader's visible anchor by the same
    // amount. This remains correct when a growing row now overlaps the viewport.
    const previousBottom = target.getBoundingClientRect().bottom - delta
    if (container && previousBottom <= containerTop + 0.5) {
      anchorDelta += delta
    }
  }
  if (!changed) return
  measurementVersion.value += 1
  if (props.followLiveEdge) {
    queueLiveEdgePin()
    return
  }
  queueAnchorAdjustment(anchorDelta)
}

function elementFromRef(value: Element | ComponentPublicInstance | null): HTMLElement | null {
  if (value instanceof HTMLElement) return value
  const componentElement = value && typeof value === 'object' && '$el' in value
    ? value.$el
    : null
  return componentElement instanceof HTMLElement ? componentElement : null
}

function setRowElement(value: Element | ComponentPublicInstance | null, key: string) {
  const previous = rowElements.get(key)
  const next = elementFromRef(value)
  if (previous && previous !== next) rowResizeObserver?.unobserve(previous)
  if (!next) {
    rowElements.delete(key)
    return
  }
  rowElements.set(key, next)
  if (virtualizationEnabled.value) {
    rowResizeObserver?.observe(next, { box: 'border-box' })
  }
}

function onContainerFocusIn(event: FocusEvent) {
  const target = event.target
  if (!(target instanceof Element) || !listRootRef.value?.contains(target)) return
  const row = target.closest<HTMLElement>('[data-chat-message-key]')
  focusedMessageKey.value = row?.dataset.chatMessageKey || null
}

function onContainerFocusOut() {
  void nextTick(() => {
    const active = document.activeElement
    if (!(active instanceof Element) || !listRootRef.value?.contains(active)) {
      focusedMessageKey.value = null
      return
    }
    focusedMessageKey.value = active.closest<HTMLElement>('[data-chat-message-key]')
      ?.dataset.chatMessageKey || null
  })
}

function detachContainer() {
  attachedContainer?.removeEventListener('scroll', scheduleViewportMeasure)
  attachedContainer?.removeEventListener('focusin', onContainerFocusIn)
  attachedContainer?.removeEventListener('focusout', onContainerFocusOut)
  viewportResizeObserver?.disconnect()
  viewportResizeObserver = null
  attachedContainer = null
}

function attachContainer(container: HTMLElement | null | undefined) {
  if (container === attachedContainer) {
    scheduleViewportMeasure()
    return
  }
  detachContainer()
  if (!container) return
  attachedContainer = container
  container.addEventListener('scroll', scheduleViewportMeasure, { passive: true })
  container.addEventListener('focusin', onContainerFocusIn)
  container.addEventListener('focusout', onContainerFocusOut)
  if (typeof ResizeObserver !== 'undefined') {
    viewportResizeObserver = new ResizeObserver(entries => {
      scheduleViewportMeasure()
      // A container-height change has no new stream event to trigger the
      // ordinary bottom pin. Keep a reader already following the live edge at
      // the true bottom; historical readers retain their existing anchor.
      if (props.followLiveEdge && entries.some(entry => entry.target === container)) {
        queueLiveEdgePin()
      }
    })
    viewportResizeObserver.observe(container)
    if (listRootRef.value) viewportResizeObserver.observe(listRootRef.value)
  }
  scheduleViewportMeasure()
}

function syncRowObservation() {
  rowResizeObserver?.disconnect()
  if (!virtualizationEnabled.value) return
  // measuredRowHeight() and the virtualizer cache both use the rendered
  // border-box. Observe the same box so padding/border changes above the
  // viewport cannot bypass the scroll-anchor correction.
  rowElements.forEach(element => rowResizeObserver?.observe(element, { box: 'border-box' }))
}

function syncPreference(event: StorageEvent) {
  if (event.key === null || event.key === VIRTUALIZATION_STORAGE_KEY) {
    virtualizationAllowed.value = readVirtualizationPreference()
  }
}

function messageElement(index: number): HTMLElement | null {
  const row = windowRows.value[index]
  if (!row) return null
  const wrapper = rowElements.get(row.key)
  if (!wrapper) return null
  return wrapper.querySelector<HTMLElement>('[data-chat-turn-key]')
    || (wrapper.firstElementChild as HTMLElement | null)
    || wrapper
}

async function ensureMessageVisible(index: number): Promise<HTMLElement | null> {
  const row = windowRows.value[index]
  if (!row) return null
  if (!virtualizationEnabled.value) return messageElement(index)
  ensuredMessageKeys.value = new Set([...ensuredMessageKeys.value, row.key])
  await nextTick()
  return messageElement(index)
}

function releaseEnsuredMessage(index?: number) {
  if (index === undefined) {
    if (ensuredMessageKeys.value.size > 0) ensuredMessageKeys.value = new Set()
    return
  }
  const key = windowRows.value[index]?.key
  if (!key || !ensuredMessageKeys.value.has(key)) return
  const next = new Set(ensuredMessageKeys.value)
  next.delete(key)
  ensuredMessageKeys.value = next
}

function messageOffset(index: number): number | null {
  if (!Number.isInteger(index) || index < 0 || index >= windowRows.value.length) return null
  const container = props.scrollContainer
  const mounted = rowElements.get(windowRows.value[index].key)
  if (container && mounted) {
    return mounted.getBoundingClientRect().top
      - container.getBoundingClientRect().top
      + container.scrollTop
  }
  const listStart = listStartInContainer()
  if (listStart === null) return null
  return listStart + (variableLayout.value.offsets[index] ?? 0)
}

defineExpose<ChatMessageListVirtualizer>({
  ensureMessageVisible,
  releaseEnsuredMessage,
  messageOffset,
  isVirtualized: () => virtualizationEnabled.value,
})

watch(() => props.scrollContainer, container => {
  void nextTick(() => attachContainer(container))
})
watch(virtualizationEnabled, () => {
  void nextTick(() => {
    syncRowObservation()
    scheduleViewportMeasure()
  })
})
watch(
  [() => props.sessionKey, () => props.scrollEpoch],
  () => {
    resetDeferredScrollWork()
    measuredSizes.clear()
    ensuredMessageKeys.value = new Set()
    focusedMessageKey.value = null
    measurementVersion.value += 1
    void nextTick(scheduleViewportMeasure)
  },
  { flush: 'sync' },
)
watch(() => windowRows.value.map(row => row.key), nextKeys => {
  const retained = new Set(nextKeys)
  for (const key of measuredSizes.keys()) {
    if (!retained.has(key)) measuredSizes.delete(key)
  }
  const nextEnsured = new Set([...ensuredMessageKeys.value].filter(key => retained.has(key)))
  if (nextEnsured.size !== ensuredMessageKeys.value.size) ensuredMessageKeys.value = nextEnsured
  measurementVersion.value += 1
  void nextTick(scheduleViewportMeasure)
})

onMounted(() => {
  if (typeof ResizeObserver !== 'undefined') rowResizeObserver = new ResizeObserver(onRowsResize)
  if (typeof window !== 'undefined') window.addEventListener('storage', syncPreference)
  attachContainer(props.scrollContainer)
  syncRowObservation()
})

onBeforeUnmount(() => {
  if (typeof window !== 'undefined') window.removeEventListener('storage', syncPreference)
  detachContainer()
  rowResizeObserver?.disconnect()
  rowResizeObserver = null
  cancelScheduledFrame(viewportFrame)
  viewportFrame = 0
  rowElements.clear()
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

.chat-message-list__row--last {
  padding-bottom: 0;
}

.chat-message-list__spacer {
  flex: 0 0 auto;
  width: 1px;
  min-height: 0;
  pointer-events: none;
}
</style>
