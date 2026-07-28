<template>
  <div
    class="msg-ai"
    :class="{
      'msg-ai--share-mode': shareMode && !message.stopNotice,
      'msg-ai--share-selected': shareSelected && !message.stopNotice,
      'msg-ai--stop-notice': message.stopNotice,
    }"
    :data-message-id="message.messageId"
    :data-share-message-id="message.stopNotice ? undefined : shareMessageId"
    :data-share-selected="shareSelected && !message.stopNotice ? 'true' : undefined"
    @click="onMessageClick"
  >
    <button
      v-if="shareMode && !message.stopNotice"
      type="button"
      class="chat-share-picker"
      :class="{ 'is-selected': shareSelected }"
      :aria-pressed="shareSelected"
      :title="shareSelected ? 'Remove from share image' : 'Add to share image'"
      :aria-label="shareSelected ? 'Remove from share image' : 'Add to share image'"
      @click.stop="emit('toggleShare', shareMessageId)"
    >
      <Icon v-if="shareSelected" name="check" :size="13" />
    </button>
    <div class="msg-ai-main">
      <PlanCard
        v-for="part in planParts"
        :key="part.key"
        class="plan-message-card"
        :plan="part.plan"
        :disabled="planActionsDisabled"
        :pending-action="planActionPending"
        @implement-current="$emit('planImplementCurrent', $event)"
        @implement-new="$emit('planImplementNew', $event)"
        @replan="$emit('planReplan', $event)"
      />
      <template v-if="activityProjection.canSeparateActivity">
        <ActivityDisclosure
          v-if="hasActivity"
          :lifecycle="activityLifecycle"
          :step-count="activityStepCount"
          :failure-count="0"
          :duration-seconds="activityDurationSeconds"
          :summary-label="activitySummaryLabel"
          :detail-label="activityDetailLabel"
          :completion-confirmed="activityCompletionConfirmed"
          :default-open="activityDefaultOpen"
          :state-key="activityStateKey"
          :continuity-key="activityContinuityKey"
        >
          <ReasoningPart
            v-if="reasoningPart"
            :part="reasoningPart"
            :live="activityLifecycle === 'working' || activityLifecycle === 'answering'"
            nested
          />
          <AssistantActivityTimeline
            v-if="
              visibleActivityItems.length
              || activityProjection.statusSteps.length
            "
            :projection="visibleActivityProjection"
            :timeline-items="visibleActivityItems"
            :state-scope="toolStateScope"
            :is-tool-group-open="isToolGroupOpen"
            :is-tool-item-open="isToolItemOpen"
            :tool-group-status-text="toolGroupStatusText"
            :tool-status-text="toolStatusText"
            :tool-secondary-text="toolSecondaryText"
            @toggle-group="$emit('toggleToolGroup', $event)"
            @toggle-item="$emit('toggleToolItem', $event)"
            @show-result="(content, title, context) => $emit('showToolResult', content, title, context)"
          >
            <template #interrupt="{ part }">
              <InterruptPart
                v-if="part.resolution"
                :part="part"
                timeline
                @resolve="(id, decision, note) => $emit('resolveInterrupt', id, decision, note)"
                @extend="id => $emit('extendInterrupt', id)"
                @clarify-submit="(fields, request) => $emit('clarifySubmit', fields, request)"
                @clarify-dismiss="$emit('clarifyDismiss')"
              />
            </template>
          </AssistantActivityTimeline>
        </ActivityDisclosure>
        <TextPart
          v-if="activityProjection.answerPart"
          :part="activityProjection.answerPart"
          :sources="message.sources ?? []"
          @citation="onCitation"
        />
      </template>

      <!-- Compatibility path for older history rows that have timeline text
           but no canonical message.text. Preserve their original order and
           visibility instead of guessing which fragment was the answer. -->
      <template v-else>
        <ReasoningPart v-if="reasoningPart" :part="reasoningPart" />
        <ToolCallTimeline
          :items="visibleLegacyTimelineItems"
          :state-scope="toolStateScope"
          :is-tool-group-open="isToolGroupOpen"
          :is-tool-item-open="isToolItemOpen"
          :tool-group-status-text="toolGroupStatusText"
          :tool-status-text="toolStatusText"
          :tool-secondary-text="toolSecondaryText"
          @toggle-group="$emit('toggleToolGroup', $event)"
          @toggle-item="$emit('toggleToolItem', $event)"
          @show-result="(content, title, context) => $emit('showToolResult', content, title, context)"
        >
          <template #interrupt="{ part }">
            <InterruptPart
              v-if="part.resolution"
              :part="part"
              timeline
              @resolve="(id, decision, note) => $emit('resolveInterrupt', id, decision, note)"
              @extend="id => $emit('extendInterrupt', id)"
              @clarify-submit="(fields, request) => $emit('clarifySubmit', fields, request)"
              @clarify-dismiss="$emit('clarifyDismiss')"
            />
          </template>
        </ToolCallTimeline>
        <StatusHistoryPart
          v-if="statusHistory.length"
          :entries="statusHistory"
        />
      </template>

      <div
        class="msg-ai-ending"
        :class="{ 'msg-ai-ending--done': showDoneBlock }"
        :data-testid="showDoneBlock ? 'done-block' : undefined"
      >
        <ChatArtifactList
          v-if="message.artifacts?.length"
          :artifacts="message.artifacts"
          :navigation-artifacts="artifactNavigationItems"
          :session-key="sessionKey"
          :auth-token="authToken"
          :prefer-workbench="workbenchEnabled"
          @download="$emit('downloadArtifact', $event)"
          @open="$emit('openArtifact', $event)"
        />

        <SourcesRow v-if="message.toolCalls?.length" ref="sourcesRowRef" :calls="message.toolCalls" :sources="message.sources ?? []" />
      </div>

      <div v-if="showFooter" class="msg-ai-footer">
        <span
          v-if="isCronMessage"
          class="msg-provenance-chip"
          :title="cronBadgeTitle"
        >
          <Icon name="cron" :size="11" />
          {{ t('chat.provenance.scheduled') }}
        </span>
        <div v-if="message.meta" class="msg-ai-meta">
          <span
            v-if="hasMetaDetails"
            ref="metaMoreRef"
            class="msg-meta__more"
            @mouseenter="metaHovered = true"
            @mouseleave="metaHovered = false"
            @keydown.escape.stop="closeMetaDetails"
            @focusout="onMetaFocusOut"
          >
            <button
              ref="metaTriggerRef"
              type="button"
              class="msg-meta__more-btn"
              :aria-expanded="metaDetailsOpen"
              :aria-controls="metaDetailsId"
              :aria-label="t('chat.usageDetails')"
              @click="metaPinned = !metaPinned"
            >
              <Icon name="info" :size="12" />
            </button>
            <div
              v-if="metaDetailsOpen"
              :id="metaDetailsId"
              class="msg-meta-popover"
              role="group"
              :aria-label="t('chat.usageDetails')"
            >
              <div v-if="message.meta.model && !message.meta.ensemble" class="msg-meta-popover__row">
                <span class="msg-meta-popover__label">{{ t('chat.msgMeta.model') }}</span>
                <span class="msg-meta-popover__value">{{ message.meta.modelShort }}</span>
              </div>
              <div v-if="message.meta.costUsd && !message.meta.ensemble" class="msg-meta-popover__row">
                <span class="msg-meta-popover__label">{{ t('chat.msgMeta.cost') }}</span>
                <span class="msg-meta-popover__value">{{ fmtUsd(message.meta.costUsd) }}</span>
              </div>
              <div v-if="message.meta.hasTokens" class="msg-meta-popover__row">
                <span class="msg-meta-popover__label">{{ t('chat.msgMeta.tokens') }}</span>
                <span class="msg-meta-popover__value">&#8593;{{ fmtTok(message.meta.input) }} &#8595;{{ fmtTok(message.meta.output) }}</span>
              </div>
              <div v-if="message.meta.cachedTokens" class="msg-meta-popover__row">
                <span class="msg-meta-popover__label">{{ t('chat.msgMeta.cache') }}</span>
                <span class="msg-meta-popover__value">{{ fmtTok(message.meta.cachedTokens) }}</span>
              </div>
              <div v-if="message.meta.reasoningTokens" class="msg-meta-popover__row">
                <span class="msg-meta-popover__label">{{ t('chat.msgMeta.think') }}</span>
                <span class="msg-meta-popover__value">{{ fmtTok(message.meta.reasoningTokens) }}</span>
              </div>
              <template v-if="message.meta.ensemble">
                <div class="msg-meta-popover__divider"></div>
                <div class="msg-meta-popover__row">
                  <span class="msg-meta-popover__label">{{ t('chat.msgMeta.ensemble') }}</span>
                  <span class="msg-meta-popover__value">{{ ensembleSummary }}</span>
                </div>
                <div class="msg-meta-popover__row">
                  <span class="msg-meta-popover__label">{{ t('chat.msgMeta.cost') }}</span>
                  <span class="msg-meta-popover__value">{{ fmtUsd(message.meta.ensemble.costUsd || message.meta.costUsd) }}</span>
                </div>
                <div v-if="message.meta.ensemble.fallbackUsed" class="msg-meta-popover__row">
                  <span class="msg-meta-popover__label">{{ t('chat.msgMeta.fallback') }}</span>
                  <span class="msg-meta-popover__value">{{ t('chat.msgMeta.fallbackUsed') }}</span>
                </div>
                <div class="msg-meta-popover__models" :aria-label="t('chat.msgMeta.ensembleModelsAria')">
                  <div
                    v-for="member in message.meta.ensemble.models"
                    :key="`${member.role}:${member.provider}:${member.model}`"
                    class="msg-meta-popover__model"
                  >
                    <span class="msg-meta-popover__model-role">{{ ensembleRole(member.role, member.label) }}</span>
                    <span class="msg-meta-popover__model-name" :title="member.model">{{ member.modelShort }}</span>
                    <span class="msg-meta-popover__model-cost">{{ fmtUsd(member.costUsd) }}</span>
                  </div>
                </div>
              </template>
            </div>
          </span>
        </div>
        <div v-if="!shareMode && !message.stopNotice" class="msg-ai-actions">
          <button
            type="button"
            class="msg-action"
            :class="{ 'msg-action--ok': copyState === 'ok', 'msg-action--err': copyState === 'err' }"
            :title="copyTitle"
            :aria-label="copyTitle"
            @click="onCopyClick"
          >
            <Icon :name="copyIconName" :size="12" />
          </button>
          <span class="msg-copy-live" aria-live="polite">{{ copyLiveText }}</span>
          <button type="button" class="msg-action" :title="t('chat.regenerate')" :aria-label="t('chat.regenerate')" @click="$emit('regenerate', message)">
            <Icon name="refresh" :size="12" />
          </button>
          <template v-if="feedbackDecisionId">
            <button
              type="button"
              class="msg-action msg-action--vote"
              :class="{ 'msg-action--ok': feedbackRating === 'up' }"
              :disabled="feedbackBusy"
              :aria-pressed="feedbackRating === 'up'"
              :title="feedbackUpTitle"
              :aria-label="feedbackUpTitle"
              @click="onFeedbackClick('up')"
            >
              <Icon name="thumbs-up" :size="12" />
            </button>
            <button
              type="button"
              class="msg-action msg-action--vote"
              :class="{ 'msg-action--err': feedbackRating === 'down' }"
              :disabled="feedbackBusy"
              :aria-pressed="feedbackRating === 'down'"
              :title="feedbackDownTitle"
              :aria-label="feedbackDownTitle"
              @click="onFeedbackClick('down')"
            >
              <Icon name="thumbs-down" :size="12" />
            </button>
          </template>
          <button
            v-if="isTip"
            type="button"
            class="msg-action msg-action--fork"
            data-testid="fork-conversation"
            :disabled="forkBusy"
            :title="t('chat.forkConversation')"
            :aria-label="t('chat.forkConversation')"
            @click="$emit('fork')"
          >
            <Icon name="fork" :size="12" />
          </button>
          <time v-if="timeIso" class="msg-time" :datetime="timeIso" :title="timeFull">
            <span class="msg-time__abs">{{ timeAbs }}</span>
            <span v-if="timeRel" class="msg-time__dot" aria-hidden="true">·</span>
            <span v-if="timeRel" class="msg-time__rel">{{ timeRel }}</span>
          </time>
        </div>
      </div>

      <!-- A pending interrupt is the turn's active control and must remain the
           final item. Once resolved it folds back into the activity timeline. -->
      <InterruptPart
        v-for="part in standaloneInterruptParts"
        :key="part.key"
        :part="part"
        @resolve="(id, decision, note) => $emit('resolveInterrupt', id, decision, note)"
        @extend="id => $emit('extendInterrupt', id)"
        @clarify-submit="(fields, request) => $emit('clarifySubmit', fields, request)"
        @clarify-dismiss="$emit('clarifyDismiss')"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ActivityDisclosure from '@/components/chat/ActivityDisclosure.vue'
import AssistantActivityTimeline from '@/components/chat/AssistantActivityTimeline.vue'
import ChatArtifactList from '@/components/chat/ChatArtifactList.vue'
import SourcesRow from '@/components/chat/SourcesRow.vue'
import ToolCallTimeline from '@/components/chat/ToolCallTimeline.vue'
import InterruptPart from '@/components/chat/parts/InterruptPart.vue'
import PlanCard from '@/components/chat/PlanCard.vue'
import ReasoningPart from '@/components/chat/parts/ReasoningPart.vue'
import StatusHistoryPart from '@/components/chat/parts/StatusHistoryPart.vue'
import TextPart from '@/components/chat/parts/TextPart.vue'
import { useChatRouteFeedback } from '@/composables/chat/useChatRouteFeedback'
import { useCopyFeedback } from '@/composables/chat/useCopyFeedback'
import { useRelativeNow } from '@/composables/useRelativeNow'
import type {
  ChatRenderedMessage,
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
  ToolResultContext,
} from '@/types/chat'
import type { ChatPart } from '@/types/parts'
import type { ArtifactPayload } from '@/types/rpc'
import type {
  PlanCardAction,
  PlanCardActionTarget,
} from '@/types/plans'
import {
  projectAssistantActivity,
  type AssistantActivityLifecycle,
} from '@/utils/chat/assistantActivity'
import {
  readAssistantActivityDuration,
  writeAssistantActivityDuration,
} from '@/utils/chat/activityDisclosureState'
import { absoluteTime, fullTime, isoTime, relativeTime } from '@/utils/messageTime'

const props = defineProps<{
  message: ChatRenderedMessage
  index: number
  shareMode: boolean
  shareSelected: boolean
  shareMessageId: string
  renderMarkdown: (text: string) => string
  fmtTok: (value: number) => string
  toolCallGroups: (calls: ChatToolCall[], baseKey: string) => ChatToolCallGroup[]
  isToolGroupOpen: (groupId: string) => boolean
  isToolItemOpen: (renderKey: string) => boolean
  toolGroupStatusText: (group: ChatToolCallGroup) => string
  toolStatusText: (call: ChatToolCallRenderItem) => string
  toolSecondaryText: (call: ChatToolCallRenderItem) => string
  copyMessage: (message: ChatRenderedMessage) => Promise<boolean>
  artifactNavigationItems?: ArtifactPayload[]
  sessionKey?: string
  authToken?: string
  workbenchEnabled?: boolean
  /** True on the thread's last assistant message — the only place the whole-conversation fork action renders. */
  isTip?: boolean
  forkBusy?: boolean
  planActionPending?: PlanCardAction | null
  planActionsDisabled?: boolean
}>()

const emit = defineEmits<{
  regenerate: [message: ChatRenderedMessage]
  toggleShare: [messageId: string]
  downloadArtifact: [artifact: ArtifactPayload]
  openArtifact: [artifact: ArtifactPayload]
  toggleToolGroup: [groupId: string]
  toggleToolItem: [renderKey: string]
  showToolResult: [content: string, title: string, context?: ToolResultContext]
  fork: []
  resolveInterrupt: [id: string, decision: 'allow-once' | 'allow-always' | 'deny', note?: string]
  extendInterrupt: [id: string]
  clarifySubmit: [fields: Record<string, string>, request?: NonNullable<Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }>['clarify']>]
  clarifyDismiss: []
  planImplementCurrent: [target: PlanCardActionTarget]
  planImplementNew: [target: PlanCardActionTarget]
  planReplan: [target: PlanCardActionTarget]
}>()

// Absolute label is static; only the relative label subscribes to the shared
// clock, so a tick re-evaluates one cheap computed per visible bubble.
const { t } = useI18n()

// Routing feedback: buttons only exist when the turn carries a V017 decision
// id (router actually decided this turn). The copy differs by execution kind —
// a single-model rating judges the tier choice, an ensemble rating judges the
// aggregated answer (backend excludes it from tier training accordingly).
const routeFeedback = useChatRouteFeedback()
const feedbackDecisionId = computed(() => props.message.meta?.decisionId)
const feedbackRating = computed(() => routeFeedback.ratingFor(feedbackDecisionId.value))
const feedbackBusy = computed(() => routeFeedback.busy(feedbackDecisionId.value))
const feedbackUpTitle = computed(() =>
  props.message.meta?.ensemble ? t('chat.routeFeedback.upEnsemble') : t('chat.routeFeedback.up'),
)
const feedbackDownTitle = computed(() =>
  props.message.meta?.ensemble ? t('chat.routeFeedback.downEnsemble') : t('chat.routeFeedback.down'),
)
function onFeedbackClick(rating: 'up' | 'down') {
  const id = feedbackDecisionId.value
  if (id) void routeFeedback.submit(id, rating)
}

const now = useRelativeNow()
const timeIso = computed(() => isoTime(props.message.ts))
const timeAbs = computed(() => absoluteTime(props.message.ts))
const timeRel = computed(() => relativeTime(props.message.ts, now.value))
const timeFull = computed(() => fullTime(props.message.ts))

// Reasoning still comes from the normalized parts surface. The visible answer
// is projected separately from authoritative message.text below; timeline text
// is never treated as a terminal-answer heuristic.
const reasoningPart = computed(
  () =>
    props.message.parts?.find(
      (part): part is Extract<ChatPart, { type: 'reasoning' }> => part.type === 'reasoning',
    ) ?? null,
)
// Inline interrupt parts (approval / clarify) fold into the body order after
// text/tools and before the ending; render them through the shared adapter.
const interruptParts = computed(
  () =>
    props.message.parts?.filter(
      (part): part is Extract<ChatPart, { type: 'interrupt' }> => part.type === 'interrupt',
    ) ?? [],
)
const timelineResolvedInterruptKeys = computed(() => new Set(
  props.message.timelineItems
    ?.filter(
      (item): item is Extract<import('@/types/chat').ChatStreamTimelineItem, { type: 'interrupt' }> =>
        item.type === 'interrupt' && !!item.part.resolution,
    )
    .map(item => item.part.key) ?? [],
))
const standaloneInterruptParts = computed(() =>
  interruptParts.value.filter(part => !timelineResolvedInterruptKeys.value.has(part.key)),
)
const planParts = computed(
  () =>
    props.message.parts?.filter(
      (part): part is Extract<ChatPart, { type: 'plan' }> => part.type === 'plan',
    ) ?? [],
)
// The persisted activity timeline for this finished turn. Empty (fold hidden)
// for OFF-mode turns and reloaded threads, which carry no snapshot.
const statusHistory = computed(() => props.message.statusHistory ?? [])

function epochMilliseconds(value: string | number | null | undefined): number {
  if (value == null) return 0
  const parsed = typeof value === 'number'
    ? value
    : /^\d+(?:\.\d+)?$/.test(value.trim())
      ? Number(value)
      : Date.parse(value)
  if (!Number.isFinite(parsed) || parsed <= 0) return 0
  return parsed < 100_000_000_000 ? parsed * 1000 : parsed
}

const measuredActivityDurationSeconds = computed(() => {
  const startedAt = statusHistory.value
    .map(entry => epochMilliseconds(entry.at))
    .filter(value => Number.isFinite(value) && value > 0)
    .sort((left, right) => left - right)[0]
  const endedAt = epochMilliseconds(props.message.ts)
  if (!startedAt || !Number.isFinite(endedAt) || endedAt <= startedAt) return 0
  const duration = Math.floor((endedAt - startedAt) / 1000)
  return duration > 0 && duration < 24 * 60 * 60 ? duration : 0
})
const isCronMessage = computed(() => props.message.provenanceKind === 'cron')
const safeCronSourceTool = computed(() => {
  const value = String(props.message.provenanceSourceTool || '').trim()
  return /^[a-zA-Z0-9_.:-]{1,80}$/.test(value) ? value : ''
})
const cronBadgeTitle = computed(() => safeCronSourceTool.value
  ? t('chat.provenance.cronSource', { tool: safeCronSourceTool.value })
  : t('chat.provenance.cron'))
const showFooter = computed(() =>
  planParts.value.length === 0
  && (!!props.message.meta || (!props.shareMode && !props.message.stopNotice)),
)

// A citation pill in the body asks the paired SourcesRow to reveal + highlight
// the source it points at. No-op when no SourcesRow is mounted (which only
// happens when there are no sources, so the body has no pills either).
const sourcesRowRef = ref<InstanceType<typeof SourcesRow> | null>(null)
function onCitation(sourceId: number) {
  sourcesRowRef.value?.focusSource(sourceId)
}

const { copyState, copyIconName, copyTitle, copyLiveText, onCopyClick } = useCopyFeedback(
  () => props.copyMessage(props.message),
)

const metaMoreRef = ref<HTMLElement | null>(null)
const metaTriggerRef = ref<HTMLButtonElement | null>(null)
const metaPinned = ref(false)
const metaHovered = ref(false)
const metaDetailsOpen = computed(() => metaPinned.value || metaHovered.value)

// A completed turn that produced artifacts keeps the deliverable and its sources
// together. The message receipt remains a sibling so it never reads as artifact
// metadata or inherits the artifact surface.
const showDoneBlock = computed(() =>
  !!props.message.artifacts?.length && !props.message.isStreaming && !props.message.interrupted,
)

const hasMetaDetails = computed(() => {
  const meta = props.message.meta
  if (!meta) return false
  return !!(
    meta.model
    || meta.costUsd
    || meta.hasTokens
    || meta.cachedTokens > 0
    || meta.reasoningTokens > 0
    || meta.ensemble
  )
})

const ensembleSummary = computed(() => {
  const ensemble = props.message.meta?.ensemble
  if (!ensemble) return ''
  const requests = ensemble.requestCount > 0 ? `${ensemble.requestCount} requests` : ''
  const profile = ensemble.profile && ensemble.profile !== 'llm_ensemble' ? ensemble.profile : ''
  return [profile, requests].filter(Boolean).join(' · ') || `${ensemble.modelCount} models`
})

const metaDetailsId = computed(
  () => `msg-meta-details-${props.message.messageId || props.message.id || props.index}`,
)

function closeMetaDetails() {
  if (!metaDetailsOpen.value) return
  metaPinned.value = false
  metaHovered.value = false
  metaTriggerRef.value?.focus()
}

function onMetaFocusOut(event: FocusEvent) {
  const next = event.relatedTarget
  if (next instanceof Node && metaMoreRef.value?.contains(next)) return
  if (next === null) return
  metaPinned.value = false
}

function onDocumentPointerDown(event: PointerEvent) {
  const root = metaMoreRef.value
  if (!root) return
  if (event.target instanceof Node && root.contains(event.target)) return
  metaPinned.value = false
  metaHovered.value = false
}

watch(metaDetailsOpen, open => {
  if (open) document.addEventListener('pointerdown', onDocumentPointerDown, true)
  else document.removeEventListener('pointerdown', onDocumentPointerDown, true)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocumentPointerDown, true)
})

const toolMessageIdentity = computed(() => (
  props.message.messageId
  || props.message.id
  || `${props.message.role}-${props.message.sourceIndex ?? props.index}`
))

const toolStateScope = computed(() => JSON.stringify([
  props.sessionKey || '',
  toolMessageIdentity.value,
]))

const legacyTimelineItems = computed<ChatStreamTimelineItem[]>(() => {
  const calls = props.message.toolCalls || []
  // message.id is always set ("${role}-${sourceIndex}") and equals the
  // composable's ownerKey when messageId is absent, so tool renderKeys match the
  // keys toParts folds. The final term only types the fallback and reconstructs
  // the same owner the composable used; it is unreachable while id is set.
  return props.toolCallGroups(calls, toolMessageIdentity.value).map(group => ({
    type: 'tool-group',
    key: group.groupId,
    group,
  }))
})

const activityLifecycle = computed<AssistantActivityLifecycle>(() => {
  if (props.message.interrupted) return 'interrupted'
  if (props.message.terminalFailure) return 'failed'
  const hasTerminalFailure = !props.message.text.trim()
    && (
      (props.message.toolCalls || []).some(call => call.isError || call.status === 'error')
      || (props.message.timelineItems || []).some(item =>
        item.type === 'tool-group'
        && item.group.calls.some(call => call.isError || call.status === 'error'),
      )
  )
  if (hasTerminalFailure) return 'failed'
  return props.message.isStreaming ? 'working' : 'settled'
})

const activityProjection = computed(() =>
  projectAssistantActivity(
    props.message,
    props.renderMarkdown,
    legacyTimelineItems.value,
    {
      lifecycle: activityLifecycle.value,
      statusHistory: statusHistory.value,
    },
  ),
)

function withoutFailedActivity(
  items: ChatStreamTimelineItem[],
): ChatStreamTimelineItem[] {
  return items.flatMap((item): ChatStreamTimelineItem[] => {
    if (item.type !== 'tool-group') return [item]
    const failedCalls = item.group.calls.filter(
      call => call.isError || call.status === 'error',
    )
    // Some restored histories only carry the failure marker on the group.
    // Treat that group-level state as authoritative when no call-level marker
    // survived serialization.
    if (
      (item.group.isError || item.group.status === 'error')
      && failedCalls.length === 0
    ) {
      return []
    }
    const calls = item.group.calls.filter(
      call => !call.isError && call.status !== 'error',
    )
    if (calls.length === 0) return []
    const isRunning = calls.some(call => call.isRunning)
    return [{
      ...item,
      group: {
        ...item.group,
        calls,
        isRunning,
        isError: false,
        status: isRunning
          ? ''
          : calls.every(call => call.status === 'success')
            ? 'success'
            : '',
      },
    }]
  })
}

const visibleActivityItems = computed(() =>
  withoutFailedActivity(activityProjection.value.activityItems),
)
const visibleLegacyTimelineItems = computed(() =>
  withoutFailedActivity(props.message.timelineItems ?? []),
)
const visibleActivityCallKeys = computed(() => new Set(
  visibleActivityItems.value.flatMap(item =>
    item.type === 'tool-group'
      ? item.group.calls.map(call => call.renderKey)
      : [],
  ),
))
const visibleActivityClusters = computed(() =>
  activityProjection.value.activityClusters.filter(cluster =>
    !cluster.isFailure
    && cluster.calls.some(call => visibleActivityCallKeys.value.has(call.renderKey)),
  ),
)
const visibleActivityProjection = computed(() => ({
  ...activityProjection.value,
  activityClusters: visibleActivityClusters.value,
}))
const hasVisibleActivityItem = computed(() => visibleActivityItems.value.length > 0)
const hasActivity = computed(() =>
  !!reasoningPart.value
  || hasVisibleActivityItem.value
  || statusHistory.value.length > 0,
)

const activityStepCount = computed(() => Math.max(
  1,
  visibleActivityClusters.value.length
    + activityProjection.value.statusSteps.length
    + (reasoningPart.value ? 1 : 0),
))
const activityDefaultOpen = computed(() => false)
const activityCompletionConfirmed = computed(() =>
  activityLifecycle.value === 'settled'
  && !props.message.isStreaming
  && interruptParts.value.every(part =>
    !part.busy
    && (part.resolution === 'approved' || part.resolution === 'replied'),
  ),
)
const activityTurnIdentity = computed(() =>
  props.message.turnKey || toolMessageIdentity.value,
)
const activityStateKey = computed(() => JSON.stringify([
  props.sessionKey || '',
  'assistant-activity',
  activityTurnIdentity.value,
  toolMessageIdentity.value,
]))
const activityContinuityKey = computed(() =>
  props.message.turnKey
    ? JSON.stringify([
        props.sessionKey || '',
        'assistant-activity-turn',
        props.message.turnKey,
      ])
    : '',
)
const activityDurationSeconds = computed(() => {
  const measured = measuredActivityDurationSeconds.value
  if (measured > 0) return measured
  const persisted = readAssistantActivityDuration(
    activityStateKey.value,
    activityContinuityKey.value,
  )
  if (persisted > 0) return persisted
  const reasoningSeconds = Math.floor(Number(reasoningPart.value?.seconds || 0))
  return reasoningSeconds > 0 ? reasoningSeconds : 0
})

// Persisting a measured duration is a side effect, so it lives in a watcher
// rather than the computed read path above: renders stay pure, and the value
// is recorded even on rows that never render a disclosure to read it.
watch(
  () => [
    measuredActivityDurationSeconds.value,
    activityStateKey.value,
    activityContinuityKey.value,
  ] as const,
  ([measured, stateKey, continuityKey]) => {
    if (measured > 0) writeAssistantActivityDuration(stateKey, measured, continuityKey)
  },
  { immediate: true },
)

const activityElapsedLabel = computed(() => {
  const seconds = Math.max(0, Math.floor(activityDurationSeconds.value || 0))
  if (seconds <= 0) return ''
  if (seconds < 60) return t('chat.workedForSeconds', { seconds })
  return t('chat.workedForMinutes', {
    minutes: Math.floor(seconds / 60),
    seconds: seconds % 60,
  })
})

const activityCompactElapsedLabel = computed(() => {
  const seconds = Math.max(0, Math.floor(activityDurationSeconds.value || 0))
  if (seconds <= 0) return ''
  if (seconds < 60) return String(t('chat.activityDurationSeconds', { seconds }))
  return String(t('chat.activityDurationMinutes', {
    minutes: Math.floor(seconds / 60),
    seconds: seconds % 60,
  }))
})

// Expanded metadata keeps the activity footprint (capped at two kinds plus a
// "{count} more" descriptor) and the verbose elapsed copy. The collapsed,
// completed row uses the compact elapsed label above instead of an arbitrary
// item count.
const activityDetailLabel = computed(() => {
  const counts = new Map<string, number>()
  for (const cluster of visibleActivityClusters.value) {
    const code = cluster.footprint.code
    const count = Number(cluster.footprint.params.count ?? cluster.callCount)
    counts.set(code, (counts.get(code) ?? 0) + count)
  }
  const descriptors = [...counts].map(([code, count]) => ({ code, count }))
  const parts = descriptors.slice(0, 2).map(part =>
    String(t(part.code, { count: part.count })),
  )
  if (descriptors.length > 2) {
    const remainingCount = descriptors
      .slice(2)
      .reduce((total, part) => total + part.count, 0)
    parts.push(String(t('chat.activity.more', { count: remainingCount })))
  }
  if (activityElapsedLabel.value) parts.push(activityElapsedLabel.value)
  return parts.join(' · ')
})

const activitySummaryLabel = computed(() => {
  if (activityCompletionConfirmed.value) {
    return [
      String(t('chat.activity.lifecycle.settled')),
      activityCompactElapsedLabel.value,
    ].filter(Boolean).join(' · ')
  }
  return activityDetailLabel.value
})

function onMessageClick(event: MouseEvent) {
  if (!props.shareMode) return
  if (props.message.stopNotice) return
  if ((event.target as HTMLElement | null)?.closest('button,a,input,textarea,select')) return
  emit('toggleShare', props.shareMessageId)
}

function fmtUsd(value: number): string {
  const n = Number.isFinite(value) ? Math.max(0, value) : 0
  if (n === 0) return '$0'
  if (n < 0.0001) return '<$0.0001'
  return `$${n.toFixed(6).replace(/\.?0+$/, '')}`
}

function ensembleRole(role: string, label: string): string {
  const normalized = String(role || '').replace(/_/g, ' ')
  if (normalized === 'proposer') return 'proposer'
  if (normalized === 'aggregator') return 'aggregator'
  if (normalized === 'fallback single') return 'fallback'
  return label || normalized || 'member'
}
</script>

<style scoped>
.msg-ai-main > :deep(.approval-card),
.msg-ai-main > :deep(.clarify-card) {
  width: 100%;
  max-width: 100%;
  margin-inline: 0;
  box-sizing: border-box;
}

.msg-ai {
  position: relative;
  display: flex;
  gap: 0.625rem;
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  margin: 0 auto;
  padding: 0.5rem 0;
  align-items: flex-start;
  max-width: calc(100% - 48px);
}

.msg-ai--share-mode {
  cursor: pointer;
  width: min(calc(100% - 16px), 1012px);
  max-width: calc(100% - 16px);
  box-sizing: border-box;
  padding: 0.5rem 1rem 0.5rem 2.5rem;
  border-radius: var(--radius-lg);
  transition: background var(--dur-base) var(--ease-standard), box-shadow var(--dur-base) var(--ease-standard);
}

.msg-ai--share-mode:hover {
  background: color-mix(in srgb, var(--accent) 5%, transparent);
}

.msg-ai--share-selected {
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  box-shadow: inset 0 0 0 2px var(--accent);
}

/* Checkbox-style selection indicator: empty outlined circle when unselected,
   accent-filled with a check when selected. Always visible in share mode. */
.chat-share-picker {
  position: absolute;
  left: 0.45rem;
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

.msg-ai-main {
  flex: 1;
  min-width: 0;
  max-width: none;
  padding-top: 0.0625rem;
}

.plan-message-card {
  width: 100%;
  max-width: none;
  margin: 0;
}

.msg-ai--stop-notice .msg-ai-main {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  flex: 0 1 auto;
  max-width: min(30rem, 100%);
  padding: 0.375rem 0.625rem;
  border: 1px solid color-mix(in srgb, var(--warn) 38%, var(--border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--warn) 10%, var(--bg-surface));
  color: var(--warn);
}

.msg-ai--stop-notice .msg-ai-main::before {
  content: "";
  width: 0.4375rem;
  height: 0.4375rem;
  flex: 0 0 auto;
  border-radius: var(--radius-full);
  background: var(--warn);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 12%, transparent);
}

.msg-ai--stop-notice :deep(.msg-ai-text) {
  margin: 0;
  font-size: 0.8125rem;
  line-height: 1.35;
  color: inherit;
}

.msg-ai-footer {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  margin-top: 0.25rem;
}

.msg-provenance-chip {
  align-items: center;
  background: color-mix(in srgb, var(--accent) 8%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--accent) 26%, var(--border));
  border-radius: var(--radius-full);
  color: var(--text-muted);
  display: inline-flex;
  font-size: var(--fs-xs);
  gap: var(--sp-1);
  padding: 1px var(--sp-2);
}

.msg-ai-ending--done {
  margin-top: 0.625rem;
}

.msg-ai-ending--done :deep(.msg-artifacts) {
  margin: 0;
}

.msg-ai-ending--done :deep(.sources-row) {
  margin: 0.5rem 0 0;
}

.msg-ai-ending--done + .msg-ai-footer {
  margin-top: 0.5rem;
}

.msg-ai-actions {
  display: flex;
  gap: 0.125rem;
  opacity: 0;
  transition: opacity var(--dur-fast);
}

.msg-ai:hover .msg-ai-actions,
.msg-ai-actions:focus-within {
  opacity: 1;
}

/* Touch screens have no hover to reveal the cluster — keep it always visible
   and give the buttons real tap targets. */
@media (hover: none) {
  .msg-ai-actions {
    opacity: 1;
  }

  .msg-action {
    min-width: 2.75rem;
    min-height: 2.75rem;
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
  padding: 0.25rem;
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

.msg-action:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

/* Fork creates something new — its hover signal is the accent, not text-muted. */
.msg-action--fork:hover {
  color: var(--accent);
}

.msg-action--fork:disabled {
  cursor: progress;
  opacity: 0.55;
}

.msg-action.msg-action--ok,
.msg-action.msg-action--ok:hover {
  color: var(--ok);
}

.msg-action.msg-action--err,
.msg-action.msg-action--err:hover {
  color: var(--danger);
}

.msg-action--vote:disabled {
  cursor: progress;
  opacity: 0.55;
}

/* A cast vote stays visible without hover — the row otherwise fades out and
   the user would lose the only cue that their rating registered. */
.msg-ai-actions:has(.msg-action--vote[aria-pressed='true']) {
  opacity: 1;
}

.msg-copy-live {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.msg-ai-meta {
  display: flex;
  align-items: center;
  color: color-mix(in srgb, var(--text-muted) 56%, transparent);
}

.msg-meta__more {
  position: relative;
  display: inline-flex;
  align-items: center;
}

.msg-meta__more-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.25rem;
  height: 1.25rem;
  padding: 0;
  background: none;
  border: none;
  border-radius: var(--radius-full);
  color: var(--text-dim);
  cursor: pointer;
  transition: color var(--transition), background var(--transition);
}

.msg-meta__more-btn:hover,
.msg-meta__more-btn[aria-expanded='true'] {
  color: var(--text-muted);
  background: var(--bg-hover);
}

.msg-meta__more-btn:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.msg-meta-popover {
  position: absolute;
  bottom: calc(100% + 0.375rem);
  left: 50%;
  transform: translateX(-50%);
  z-index: 20;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  min-width: 10rem;
  max-width: min(24rem, calc(100vw - 2rem));
  padding: 0.5rem 0.625rem;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.4;
  white-space: nowrap;
}

.msg-meta-popover__row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.75rem;
}

.msg-meta-popover__label {
  color: var(--text-dim);
}

.msg-meta-popover__value {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--text);
}

.msg-meta-popover__divider {
  height: 1px;
  margin: 0.125rem 0;
  background: var(--hairline);
}

.msg-meta-popover__models {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
  min-width: 0;
}

.msg-meta-popover__model {
  display: grid;
  grid-template-columns: minmax(4.75rem, 0.8fr) minmax(7rem, 1fr) auto;
  align-items: baseline;
  gap: 0.5rem;
  min-width: 0;
}

.msg-meta-popover__model-role {
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
}

.msg-meta-popover__model-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text);
}

.msg-meta-popover__model-cost {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

@media (max-width: 768px) {
  .msg-ai-footer {
    min-width: 0;
  }

  .msg-ai-meta {
    flex: 0 0 auto;
  }

  .msg-meta__more {
    flex-shrink: 0;
  }
}

@media (max-width: 640px) {
  .msg-ai--share-mode {
    width: min(calc(100% - 12px), 1012px);
    max-width: calc(100% - 12px);
    padding: 0.5rem 0.75rem 0.5rem 2.25rem;
  }

  .chat-share-picker {
    left: 0.35rem;
  }
}
</style>
