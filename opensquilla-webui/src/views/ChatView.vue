<template>
  <div
    class="chat"
    :class="{ 'chat--new-landing': isNewChatLanding, 'chat--drag-over': threadDragOver }"
    @dragenter="onChatDragEnter"
    @dragover="onChatDragOver"
    @dragleave="onChatDragLeave"
    @drop="onChatDrop"
  >
    <div v-if="threadDragOver" class="chat-drop-overlay" role="status" aria-live="polite" aria-atomic="true">
      <div class="chat-drop-overlay__frame" aria-hidden="true"></div>
      <div class="chat-drop-overlay__beacon">
        <span class="chat-drop-overlay__glyph" aria-hidden="true">
          <Icon name="fileText" :size="30" />
          <Icon class="chat-drop-overlay__plus" name="plus" :size="12" />
        </span>
        <span class="chat-drop-overlay__copy">
          <span class="chat-drop-overlay__title">{{ t('chat.dropOverlayTitle') }}</span>
          <span class="chat-drop-overlay__hint">{{ t('chat.dropOverlayHint') }}</span>
        </span>
      </div>
    </div>

    <!-- Chat owns the actions; App owns their stable, in-flow shell position. -->
    <Teleport to="#app-route-header">
      <ChatHeaderActions
        v-if="!isNewChatLanding"
        ref="chatHeaderActionsRef"
        :title="currentChatTitle"
        :session-key="sessionKey"
        :copy-state="sessionCopyState"
        :copy-icon="sessionCopyIcon"
        :copy-live-text="sessionCopyLiveText"
        :deliverable-count="sessionArtifacts.length"
        :run-history-visible="appStore.features.metaRuns"
        :share-mode="shareMode"
        :shareable-message-count="shareableMessageCount"
        @open-deliverables="openDeliverables"
        @open-run-history="openMetaRunHistory"
        @start-share="startShareMode"
        @copy-session-key="onSessionCopyClick"
      />
    </Teleport>

    <!-- Thread -->
    <div class="chat-body">
      <!-- Share-mode banner stays pinned above the scrolling thread. -->
      <div
        v-if="shareMode"
        ref="shareBannerRef"
        class="chat-share-banner"
        tabindex="-1"
        role="group"
        :aria-label="t('chat.shareSelectedMessages')"
        data-testid="share-banner"
      >
        <span class="chat-share-banner__hint">{{ t('chat.shareBannerHint') }}</span>
        <span class="chat-share-banner__count" role="status" aria-live="polite">{{ t('chat.shareSelectedCount', { count: selectedShareCount }) }}</span>
        <button
          type="button"
          class="chat-share-btn chat-share-btn--save"
          :disabled="selectedShareCount === 0 || shareSaving"
          :title="selectedShareCount === 0 ? t('chat.shareSelectAtLeastOne') : t('chat.shareSavePngHint')"
          @click="saveShareImage"
        >
          <Icon name="download" :size="14" />
          <span>{{ shareSaving ? t('chat.saving') : t('chat.savePng') }}</span>
        </button>
        <button type="button" class="chat-share-btn" :title="t('chat.shareCancelHint')" @click="endShareMode">
          {{ t('common.cancel') }}
        </button>
      </div>
      <div class="chat-thread-shell">
        <div
          ref="threadRef"
          class="chat-thread"
          role="region"
          tabindex="0"
          :aria-label="t('chat.conversation')"
          :aria-busy="isStreaming || initialSessionLoadState === 'loading'"
          @scroll="onThreadScroll"
        >
        <template v-if="isNewChatLanding">
          <div class="chat-landing-brand" :aria-label="t('chat.newChatBrand')">
            <EmptyStateChips
              :key="landingAgentId"
              :agent-id="landingAgentId"
              :meta-skills="metaSkillChoices"
              :suppressed="landingPrefilled"
              @pick="applyLandingSuggestion"
            />
          </div>
        </template>
        <ChatSessionLoadState
          v-else-if="initialSessionLoadState"
          :key="sessionKey"
          :state="initialSessionLoadState"
          @retry="retryHistory"
        />
        <div
          v-else-if="messages.length === 0 && !isStreaming"
          class="chat-empty"
        >
          {{ t('chat.noMessagesYet') }}
        </div>
        <HistoryLoadSentinel
          v-if="!isNewChatLanding && !initialSessionLoadState"
          :scroll-container="threadRef"
          :has-more="historyState.hasMore"
          :loading="historySentinelLoading"
          :blocked="historyState.loading"
          :error="historySentinelError"
          :canonical-available="historyState.canonicalAvailable"
          :canonical-complete="historyState.canonicalComplete"
          :cursor="historyState.oldestCursor"
          :session-key="sessionKey"
          @load-earlier="loadEarlierHistory"
          @retry="retryHistory"
        />

        <ChatMessageList
          :messages="renderedMessages"
          :session-key="sessionKey"
          :auth-token="readAuthToken()"
          :artifact-navigation-items="sessionArtifacts"
          :share-mode="shareMode"
          :selected-message-ids="selectedShareMessageIds"
          :strip-time-prefix="stripTimePrefix"
          :render-markdown="renderMarkdown"
          :fmt-tok="fmtTok"
          :subagent-summary="subagentSummary"
          :subagent-body="subagentBody"
          :tool-call-groups="toolCallGroups"
          :is-tool-group-open="isToolGroupOpen"
          :is-tool-item-open="isToolItemOpen"
          :tool-group-status-text="toolGroupStatusText"
          :tool-status-text="toolStatusText"
          :tool-secondary-text="toolSecondaryText"
          :copy-message="copyMessage"
          :download-attachment="downloadAttachment"
          :fork-busy="forkInFlight"
          @fork-conversation="forkConversation"
          @edit-message="editMessage"
          @regenerate-message="regenerateMessage"
          @toggle-share-message="toggleShareMessage"
          @download-artifact="downloadArtifact"
          @toggle-tool-group="toggleToolGroup"
          @toggle-tool-item="toggleToolItem"
          @show-tool-result="showToolResultModal"
          @resolve-interrupt="resolveInterrupt"
          @extend-interrupt="extendInterrupt"
          @clarify-submit="submitClarify"
          @clarify-dismiss="dismissClarify"
          @resume-sandbox="resumeSandbox"
        >
          <template #router-strip="{ message: msg }">
            <RouterFxStrip v-if="shouldRenderRouterStrip(msg)" :message="msg" />
          </template>
        </ChatMessageList>

        <!-- Pre-reveal router phase: shown only before the live activity owns
             the turn. Once activity is visible, execution status becomes
             the single primary progress surface. -->
        <RouterFxStrip
          v-if="routerStripReserve"
          class="router-fx-reserve"
          :message="routerStripReserve"
          aria-hidden="true"
        />

        <!-- MetaSkill run cards: preflight checkpoint + progress ribbon,
             grouped per run_id above the live activity area. -->
        <template v-for="runId in metaRuns.ribbonOrder.value" :key="`meta-${runId}`">
          <MetaPreflightCard
            v-if="metaRuns.preflights.value.has(runId)"
            :state="metaRuns.preflights.value.get(runId)!.state"
            :phase="metaRuns.preflights.value.get(runId)!.phase"
            :error-text="metaRuns.preflights.value.get(runId)!.errorText"
            @action="metaRuns.onPreflightAction"
          />
          <MetaRibbon
            v-if="metaRuns.ribbons.value.has(runId)"
            :run="metaRuns.ribbons.value.get(runId)!"
            @action="metaRuns.onRibbonAction"
            @chip-select="metaRuns.onChipSelect"
          />
        </template>

        <!-- Streaming AI message: activity stays open and flat while the turn
             is live. The trailing text segment is rendered below it as the
             current answer candidate; if a later tool starts, that text moves
             back into the chronological activity transcript. -->
        <!-- No blanket aria-live here: the phase label inside ActivityDisclosure
             is the single live announcement point, so streaming DOM churn (tool
             rows, answer tokens) is not read out mutation-by-mutation. -->
        <div v-if="isStreaming && streamBubble && answerRevealOpen" class="msg-ai" data-history-role="assistant">
          <div class="msg-ai-main">
            <ActivityDisclosure
              :lifecycle="liveAnswerPart ? 'answering' : 'working'"
              :step-count="liveActivityStepCount"
              :failure-count="liveActivityFailureCount"
              :phase-label="liveActivityPhaseLabel"
              :elapsed-label="streamPhaseElapsed"
              :stale="streamActivityStale"
            >
              <!-- Reasoning remains available as a flat, secondary disclosure,
                   rendered by the same part component settled turns use so the
                   chevron affordance and wording stay consistent; `live`
                   selects the streaming "Thinking · Ns" label. -->
              <ReasoningPart v-if="liveReasoningPart" :part="liveReasoningPart" live />

              <AssistantActivityTimeline
                v-if="
                  liveActivityProjection.activityClusters.length
                  || liveActivityProjection.statusSteps.length
                "
                variant="checklist"
                :projection="liveActivityProjection"
                :timeline-items="liveActivityTimelineItems"
                :state-scope="liveToolStateScope"
                :is-tool-group-open="isToolGroupOpen"
                :is-tool-item-open="isToolItemOpen"
                :tool-group-status-text="toolGroupStatusText"
                :tool-status-text="toolStatusText"
                :tool-secondary-text="toolSecondaryText"
                :tool-elapsed-text="liveToolElapsedText"
                @toggle-group="toggleToolGroup"
                @toggle-item="toggleToolItem"
                @show-result="showToolResultModal"
              />
            </ActivityDisclosure>

            <!-- Provisional answer candidate: the left rule + draft tag mark
                 it as not-final, because a later tool start moves this text
                 back into the activity transcript. The tag sits outside the
                 candidate box so the candidate's own text stays the answer. -->
            <template v-if="liveAnswerPart">
              <span class="live-answer-candidate-tag">{{ t('chat.metaRuns.draft') }}</span>
              <div class="live-answer-candidate">
                <TextPart
                  :part="liveAnswerPart"
                  :sources="[]"
                />
              </div>
            </template>
            <span
              v-if="liveAnswerPart && !streamActivityStale"
              class="stream-caret"
              aria-hidden="true"
            />

            <!-- Live inline interrupts (fold-driven): approval / clarify cards
                 that block the in-flight turn, rendered after the activity body
                 and before the deliverables. -->
            <InterruptPart
              v-for="part in liveInterruptParts"
              :key="part.key"
              :part="part"
              @resolve="resolveInterrupt"
              @extend="extendInterrupt"
              @clarify-submit="(fields, request) => submitClarify(fields, request)"
              @clarify-dismiss="dismissClarify"
            />

            <ChatArtifactList
              :artifacts="liveArtifacts"
              :navigation-artifacts="sessionArtifacts"
              :session-key="sessionKey"
              :auth-token="readAuthToken()"
              @download="downloadArtifact"
            />

          </div>
        </div>

        <!-- Soft long-running banner: content events crossed the high watchdog
             threshold while no backend-deadline-owned phase (tool, approval,
             ensemble) explains it. "Keep waiting" suppresses this silence
             episode; "Interrupt" uses the composer stop path. -->
        <ChatStallNotice
          v-if="stallActive"
          :seconds="stallSeconds"
          @wait="stallWatchdog.dismiss()"
          @interrupt="onStop"
        />

        <!-- Thinking indicator -->
        <div v-if="thinkingVisible && answerRevealOpen" class="msg-ai thinking" role="status" aria-live="polite">
          <div class="msg-ai-main">
            <div class="thinking-status">
              <span class="stream-activity-dot" aria-hidden="true" />
              <span class="thinking-elapsed activity-shimmer" aria-live="off">{{ thinkingText }}</span>
            </div>
          </div>
        </div>

        <!-- Legacy standalone approval / clarify block. The interrupt parts now
             carry these through the fold (InterruptPart over the same cards), so
             this side-list only renders on the foldLiveTurn=0 rollback branch —
             the one-flag kill switch — to avoid a double-render. Kept for one
             release as the rollback lever, mirroring the foldLiveTurn discipline. -->
        <template v-if="foldLiveTurnMode === false">
          <!-- In-thread approval cards: blocked runs ask for a decision here -->
          <ApprovalCard
            v-for="entry in approvalEntries"
            :key="entry.approval.id"
            :approval="entry.approval"
            :resolution="entry.resolution"
            :busy="approvalBusyIds.has(entry.approval.id)"
            :error="entry.error"
            @allow-once="resolveApproval(entry, 'allow-once')"
            @allow-always="resolveApproval(entry, 'allow-always')"
            @deny="note => resolveApproval(entry, 'deny', note)"
            @extend="extendInterrupt(entry.approval.id)"
          />

          <!-- In-thread clarify card: pending agent questions render as a form -->
          <ClarifyCard
            v-if="pendingClarify"
            :request="pendingClarify"
            :submitted="clarifySubmitted"
            :busy="clarifyBusy"
            :error="clarifyError"
            @submit="submitClarify"
            @dismiss="dismissClarify"
          />
        </template>
        </div>
        <!-- Keep the loading announcer outside the busy conversation region:
             assistive technology may defer live-region updates inside an
             aria-busy subtree until after this short-lived state unmounts. -->
        <span
          class="chat-session-load-announcer"
          role="status"
          aria-live="polite"
          aria-atomic="true"
          data-testid="chat-session-load-announcer"
        >{{ initialSessionLoadAnnouncement }}</span>
        <ConversationMinimap
          v-if="!isNewChatLanding && !shareMode"
          :messages="renderedMessages"
          :scroll-container="threadRef"
          :strip-time-prefix="stripTimePrefix"
          :session-key="sessionKey"
          :history-has-more="historyState.hasMore"
          @navigate="onHistoryNavigate"
          @navigate-end="onHistoryNavigateEnd"
        />
      </div>
    </div>

    <PendingQueue
      :items="pendingQueue"
      :max-pending="maxPending"
      :mode="isStreaming ? busySendMode : null"
      @clear="clearPendingQueue"
      @remove="removePendingChip"
    />

    <!-- Compaction maintenance card -->
    <div v-if="compactStatus.visible" class="chat-compact-status" :class="`chat-compact-status--${compactStatus.tone}`" role="status" aria-live="polite">
      <div class="chat-compact-status__head">
        <span class="chat-compact-status__dot" :class="{ 'chat-compact-status__dot--pulsing': compactStatus.isBusy }" aria-hidden="true" />
        <span class="chat-compact-status__title">{{ compactStatus.message }}</span>
        <span v-if="compactElapsed" class="chat-compact-status__elapsed">{{ compactElapsed }}</span>
      </div>
      <p v-if="compactStatus.detail" class="chat-compact-status__detail">{{ compactStatus.detail }}</p>
      <div v-if="compactGaugeVisible" class="chat-compact-status__gauge" aria-hidden="true">
        <span
          class="chat-compact-status__gauge-fill"
          :class="{
            'chat-compact-status__gauge-fill--breathing': compactStatus.isBusy,
            'chat-compact-status__gauge-fill--done': compactStatus.status === 'completed',
          }"
          :style="compactStatus.occupancyPercent !== null ? { width: `${compactStatus.occupancyPercent}%` } : undefined"
        />
      </div>
      <div v-if="compactGaugeVisible && compactStatus.occupancyPercent !== null" class="chat-compact-status__legend">
        <span>context {{ compactStatus.occupancyPercent }}%</span>
        <span v-if="compactStatus.contextWindowLabel">{{ compactStatus.contextWindowLabel }}</span>
      </div>
    </div>

    <SandboxSetupBanner
      v-if="sandboxSetupVisible"
      :status="sandboxSetupStatus"
      :can-setup="sandboxSetupCanSetup"
      :ensuring="sandboxSetupEnsuring"
      :error="sandboxSetupError"
      @setup="ensureSandboxSetup"
      @dismiss="dismissSandboxSetup"
    />

    <!-- Composer dock: positioning context so the slash menu anchors directly
         above the composer in any layout. The new-chat landing centers the
         composer instead of pinning it to the bottom, so the menu must not
         anchor to the chat container's bottom edge. -->
    <div class="chat-composer-dock">
    <!-- Jump-to-latest: floats above the composer once the reader has scrolled up
         off the live edge, so a long streaming answer is never lost below the fold. -->
    <Transition name="jump-latest">
      <button
        v-if="showJumpToLatest"
        type="button"
        class="chat-jump-latest"
        :aria-label="t('chat.jumpToLatest')"
        :title="t('chat.jumpToLatest')"
        @click="jumpToLatest"
      >
        <Icon name="chevronRight" :size="14" class="chat-jump-latest__icon" />
        <span>{{ t('chat.latest') }}</span>
      </button>
    </Transition>
    <!-- Slash command menu -->
    <div v-if="slashOpen" class="chat-slash">
      <div
        v-for="(cmd, i) in filteredSlashCmds"
        :key="cmd.cmd"
        class="chat-slash-item"
        :class="{ 'chat-slash-item--active': i === slashIdx }"
        @click="selectSlashCmd(cmd)"
      >
        <span class="chat-slash-cmd">{{ cmd.cmd }}</span>
        <span class="chat-slash-desc" :title="cmd.desc">{{ cmd.desc }}</span>
      </div>
    </div>

    <ChatComposer
      ref="composerRef"
      v-model="inputText"
      :attachments="pendingAttachments"
      :busy-send-mode="busySendMode"
      :has-send-content="hasSendContent"
      :is-streaming="isStreaming"
      :can-stop="canStop"
      :is-new-landing="isNewChatLanding"
      :placeholder="composerPlaceholder"
      :send-button-title="sendButtonTitle"
      :send-blocked-message="modelImageSendBlockedMessage"
      :run-mode="runMode"
      :allowed-run-modes="allowedRunModes"
      :model-routing-mode="modelRoutingMode"
      :model-routing-settings-busy="modelRoutingSettingsBusy"
      :router-visual-effects-enabled="routerVisualEffectsEnabled"
      :coding-mode-enabled="codingModeEnabled"
      :coding-mode-settings-busy="codingModeSettingsBusy"
      :voice-busy="voiceBusy"
      :voice-recording="voiceRecording"
      :voice-ready="voiceReady"
      @composition-change="composing = $event"
      @beforeinput="onTextareaBeforeInput"
      @file-change="onFileInputChange"
      @input="onTextareaInput"
      @keydown="onTextareaKeydown"
      @remove-attachment="removeAttachment"
      @retry-attachment="retryAttachment"
      @set-busy-send-mode="busySendMode = $event"
      @set-run-mode="setComposerRunMode"
      @set-model-routing-mode="setComposerModelRoutingMode"
      @set-visual-effects-enabled="setComposerVisualEffectsEnabled"
      @set-coding-mode-enabled="setComposerCodingModeEnabled"
      @voice-input="onVoiceInput"
      @voice-setup="onVoiceSetup"
      @export-markdown="exportMarkdown"
      @send="onSend"
      @stop="onStop"
    />
    </div>

    <ToolResultModal
      :open="toolResultModal.open"
      :title="toolResultModal.title"
      :content="toolResultModal.content"
      :context="toolResultModal.context"
      @close="toolResultModal.open = false"
    />

    <DeliverablesDrawer
      :open="deliverablesOpen"
      :artifacts="sessionArtifacts"
      :session-key="sessionKey"
      :auth-token="readAuthToken()"
      @close="closeDeliverables"
      @download="downloadArtifact"
    />

    <MetaRunHistoryDrawer
      v-if="appStore.features.metaRuns"
      :open="metaRunsHistoryOpen"
      :rpc="rpc"
      :session-key="sessionKey"
      @close="closeMetaRunHistory"
    />

    <SharePreviewModal
      :open="!!sharePreview"
      :image-url="sharePreview?.url || ''"
      :filename="sharePreview?.filename || ''"
      :theme="shareTheme"
      :copy-supported="copySupported"
      :busy="shareSaving"
      @close="closeSharePreview"
      @download="onShareDownload"
      @copy="onShareCopy"
      @set-theme="onShareSetTheme"
    />

    <!-- Persistent completion announcer: the live block's role="status" phase
         label unmounts with the block when streaming ends, so on its own the
         settle would never reach a screen reader. This region stays mounted
         across the streaming boundary; it fills when a live turn settles and
         clears when the next turn starts so repeat turns announce again. -->
    <span class="chat-turn-settled-announcer" role="status" aria-live="polite">{{ turnSettledAnnouncement }}</span>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch, watchEffect } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { useRpcStore } from '@/stores/rpc'
import { useRpcCall } from '@/composables/useRpc'
import { useAppStore } from '@/stores/app'
import ApprovalCard from '@/components/chat/ApprovalCard.vue'
import ActivityDisclosure from '@/components/chat/ActivityDisclosure.vue'
import AssistantActivityTimeline from '@/components/chat/AssistantActivityTimeline.vue'
import ChatArtifactList from '@/components/chat/ChatArtifactList.vue'
import ChatHeaderActions from '@/components/chat/ChatHeaderActions.vue'
import DeliverablesDrawer from '@/components/chat/DeliverablesDrawer.vue'
import ChatComposer from '@/components/chat/ChatComposer.vue'
import ChatMessageList from '@/components/chat/ChatMessageList.vue'
import ChatSessionLoadState from '@/components/chat/ChatSessionLoadState.vue'
import ChatStallNotice from '@/components/chat/ChatStallNotice.vue'
import ClarifyCard from '@/components/chat/ClarifyCard.vue'
import ConversationMinimap from '@/components/chat/ConversationMinimap.vue'
import EmptyStateChips from '@/components/chat/EmptyStateChips.vue'
import InterruptPart from '@/components/chat/parts/InterruptPart.vue'
import ReasoningPart from '@/components/chat/parts/ReasoningPart.vue'
import TextPart from '@/components/chat/parts/TextPart.vue'
import MetaPreflightCard from '@/components/chat/MetaPreflightCard.vue'
import MetaRibbon from '@/components/chat/MetaRibbon.vue'
import MetaRunHistoryDrawer from '@/components/chat/MetaRunHistoryDrawer.vue'
import PendingQueue from '@/components/chat/PendingQueue.vue'
import RouterFxStrip from '@/components/chat/RouterFxStrip.vue'
import SandboxSetupBanner from '@/components/chat/SandboxSetupBanner.vue'
import SharePreviewModal from '@/components/chat/SharePreviewModal.vue'
import ToolResultModal from '@/components/chat/ToolResultModal.vue'
import Icon from '@/components/Icon.vue'
import HistoryLoadSentinel from '@/components/HistoryLoadSentinel.vue'
import { useChatApprovals } from '@/composables/chat/useChatApprovals'
import { useChatAttachments } from '@/composables/chat/useChatAttachments'
import { useChatCompaction } from '@/composables/chat/useChatCompaction'
import { useChatComposerShortcuts } from '@/composables/chat/useChatComposerShortcuts'
import { useChatDraftPersistence } from '@/composables/chat/useChatDraftPersistence'
import { useChatElevatedMode } from '@/composables/chat/useChatElevatedMode'
import { useChatFeatureToggles } from '@/composables/chat/useChatFeatureToggles'
import { useChatHistory } from '@/composables/chat/useChatHistory'
import { useChatMarkdownExport } from '@/composables/chat/useChatMarkdownExport'
import { useChatMessageActions } from '@/composables/chat/useChatMessageActions'
import {
  useChatPendingQueue,
  type PendingQueueOwnerContext,
} from '@/composables/chat/useChatPendingQueue'
import { useChatShareExport } from '@/composables/chat/useChatShareExport'
import type { ShareExportTheme } from '@/composables/chat/useChatShareExport'
import { useMediaQuery } from '@/composables/chat/useMediaQuery'
import {
  fmtTok,
  truncate,
  useChatRenderedMessages,
} from '@/composables/chat/useChatRenderedMessages'
import { messagesWithStoppedOutputNotice } from '@/composables/chat/stoppedOutputNotice'
import { useChatRouterDecisionRuntime } from '@/composables/chat/useChatRouterDecisionRuntime'
import { useChatAnswerReveal } from '@/composables/chat/useChatAnswerReveal'
import { useChatRpcEventHandlers } from '@/composables/chat/useChatRpcEventHandlers'
import { useChatRpcSubscriptions } from '@/composables/chat/useChatRpcSubscriptions'
import { useChatSend } from '@/composables/chat/useChatSend'
import { useSandboxSetupRecovery } from '@/composables/chat/useSandboxSetupRecovery'
import { useChatStallWatchdog } from '@/composables/chat/useChatStallWatchdog'
import { useMetaRuns } from '@/composables/chat/useMetaRuns'
import { runStatusLabelText as sessionRunStatusLabelText } from '@/composables/useSessions'
import { useChatSessionRoute } from '@/composables/chat/useChatSessionRoute'
import { useChatRunModePreference, type RunModePolicy } from '@/composables/chat/useChatRunModePreference'
import { useChatSessionRuntime } from '@/composables/chat/useChatSessionRuntime'
import { useChatSessionSubscription } from '@/composables/chat/useChatSessionSubscription'
import { useChatSlashCommands } from '@/composables/chat/useChatSlashCommands'
import { useChatStream } from '@/composables/chat/useChatStream'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import { useChatUsageWidget } from '@/composables/chat/useChatUsageWidget'
import { useVoiceInput } from '@/composables/chat/useVoiceInput'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { hasOpenDialogLayer } from '@/composables/useDialogA11y'
import { useToasts } from '@/composables/useToasts'
import type {
  ChatMessage,
  ChatRenderedMessage,
  ChatRunStatus,
  ChatRunStatusSource,
  ChatRunStatusState,
  ChatStreamTimelineItem,
  ChatToolCall,
  DisplayAttachment,
  ToolResultContext,
} from '@/types/chat'
import type {
  ArtifactPayload,
  SessionEventPayload,
} from '@/types/rpc'
import type { ModelRoutingMode } from '@/types/modelRouting'
import type { SandboxRunMode } from '@/types/sandbox'
import type { ChatPart, InterruptViewState } from '@/types/parts'
import { artifactDownloadUrl } from '@/utils/chat/artifacts'
import { fetchDisplayAttachmentBlob } from '@/utils/chat/attachmentAccess'
import { createHistoryNavigationScrollLock } from '@/utils/chat/historyNavigationScrollLock'
import {
  PENDING_STREAM_TASK_ID,
  STOPPED_STREAM_TASK_ID,
  isCurrentSessionPayload as payloadIsCurrentSession,
} from '@/utils/chat/streamEvents'
import { copyTextWithFallback, copyImageToClipboard, downloadBlob, shareCopyImageSupported } from '@/utils/browser'
import { useCopyFeedback } from '@/composables/chat/useCopyFeedback'
import { recordSessionNavigationDiag } from '@/utils/chat/sessionNavigationDiag'
import {
  toolCallGroups,
  toolGroupStatusText,
  toolSecondaryText,
  toolStatusText,
} from '@/utils/chat/toolDisplay'
import {
  collectClipboardFiles,
  hasSendableModelInputImageAttachment,
  isSendableAttachment,
  shouldCaptureFilePaste,
} from '@/utils/chat/attachments'
import { isShareableChatMessage } from '@/utils/chat/messageIdentity'
import { agentIdFromSessionKey } from '@/utils/chat/sessionKeys'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
import {
  resolveChatSessionLoadState,
  shouldShowHistorySentinelError,
  shouldShowHistorySentinelLoading,
} from '@/utils/chat/sessionLoadState'
import {
  isSemanticActivityStatusStep,
  projectAssistantActivityTimeline,
  splitLiveAssistantTimeline,
} from '@/utils/chat/assistantActivity'

/* ── Types ─────────────────────────────────────────────────────────── */

interface ChatComposerHandle {
  composerElement: () => HTMLElement | null
  focusTextarea: () => void
  isTextareaFocused: () => boolean
  resizeTextarea: () => void
}

type Message = ChatMessage

interface RpcAuthPayload {
  runModePolicy?: RunModePolicy
}

/* ── Constants ─────────────────────────────────────────────────────── */

const CHAT_RUN_STATUS_VALUES: ChatRunStatusState[] = [
  'queued',
  'running',
  'approval_pending',
  'interrupted',
  'failed',
  'timeout',
  'cancelled',
]

const toolResultModal = ref<{
  open: boolean
  title: string
  content: string
  context?: ToolResultContext
}>({ open: false, title: '', content: '' })

/* ── Stores / Router ───────────────────────────────────────────────── */

const rpc = useRpcStore()
const appStore = useAppStore()
const router = useRouter()
const { t, locale } = useI18n()
const { pushToast } = useToasts()
const isCompactViewport = useMediaQuery('(max-width: 480px)')
const isDesktopViewport = useMediaQuery('(min-width: 769px)')
const landingAgentId = computed(() => agentIdFromSessionKey(sessionKey.value))
// True when the current draft opened with prefilled composer text (Sessions
// Hub task input); the landing suggestion chips stay out of the way then.
const landingPrefilled = ref(false)
// Holds the prefill text when the Sessions Hub hand-off requested a one-step
// send ("Start task"). Flushed in onMounted once the draft subscription is live
// so the first turn streams into this view. Empty string = nothing pending.
const pendingAutoSend = ref('')

/* ── DOM refs ──────────────────────────────────────────────────────── */

const threadRef = ref<HTMLElement | null>(null)
const composerRef = ref<ChatComposerHandle | null>(null)
type ChatHeaderActionsHandle = {
  focusAction: (action: 'deliverables' | 'runs' | 'share' | 'copy-session-key') => boolean
}
const chatHeaderActionsRef = ref<ChatHeaderActionsHandle | null>(null)

/* ── State ─────────────────────────────────────────────────────────── */

const sessionKey = ref('')
const inputText = ref('')
const aborted = ref(false)
const autoScroll = ref(true)
const historyNavigationScrollLock = createHistoryNavigationScrollLock(autoScroll)
const composing = ref(false)
const messages = ref<Message[]>([])

// Session / UI
const lastHeaderRole = ref('')
const lastHeaderDay = ref('')
const threadDragOver = ref(false)
const threadDragDepth = ref(0)
const shareMode = ref(false)
const shareSaving = ref(false)
const selectedShareMessageIds = ref<Set<string>>(new Set())
const shareBannerRef = ref<HTMLElement | null>(null)
// Preview-before-download: Save renders the PNG to a blob and opens the modal
// instead of downloading blind. The view owns the object-URL lifecycle.
const sharePreview = ref<{ url: string; blob: Blob; filename: string } | null>(null)
const shareTheme = ref<ShareExportTheme>('light')
// Whether the browser can copy an image to the clipboard. Resolved once: the
// capability does not change within a session, and the modal hides Copy when false.
const copySupported = shareCopyImageSupported()

const chatElevatedMode = useChatElevatedMode({
  sessionKey,
})
// Persist the composer draft per session so a refresh / session switch / crash
// before the backend accepts a send cannot silently lose typed text (issue 248).
useChatDraftPersistence({ sessionKey, inputText })
const {
  elevatedMode,
  loadElevatedMode,
  setGlobalElevatedMode,
  normalizeElevatedMode,
} = chatElevatedMode

const {
  runMode,
  allowedRunModes,
  setRunMode: setPersistedRunMode,
} = useChatRunModePreference({
  runModePolicy: () => {
    const auth = rpc.auth as RpcAuthPayload | null
    return auth?.runModePolicy
  },
})

const sandboxSetupRecovery = useSandboxSetupRecovery({
  rpc,
  connectionState: computed(() => rpc.state),
  runMode,
})
const {
  status: sandboxSetupStatus,
  ensuring: sandboxSetupEnsuring,
  error: sandboxSetupError,
  visible: sandboxSetupVisible,
  canSetup: sandboxSetupCanSetup,
  ensureSetup: ensureSandboxSetup,
  dismiss: dismissSandboxSetup,
} = sandboxSetupRecovery

// Run status
const runStatus = ref<ChatRunStatus>({ status: 'idle', label: t('chat.status.idle'), task: null })

// Epoch / seq
const currentEpoch = ref(0)
const lastStreamSeq = ref(0)
const activeTaskGroups = ref<Set<string>>(new Set())
// Task id whose output the live stream renders; binds late events to the
// current turn so a prior task can't leak into it (issue 344).
const activeStreamTaskId = ref<string>('')
const activeStreamSessionKey = ref<string>('')
let bindActiveStreamTask = (taskId: string) => { activeStreamTaskId.value = taskId }

// Pending session intent
const pendingSessionIntent = ref<string | null>(null)
const pendingForkBeforeMessageId = ref<string | null>(null)
let applySessionRunState: (source: ChatRunStatusSource | null | undefined) => void = () => {}
let resetComposerInputHistory: () => void = () => {}

const chatTextRendering = useChatTextRendering()
const {
  renderMarkdown,
  sanitizeCopyText,
  stripDirectiveTags,
  stripGeneratedArtifactMarkers,
  stripTimePrefix,
} = chatTextRendering

// Resolution side-map for inline interrupt parts, owned here so it can be shared
// between the stream (which threads it into the turn-log fold) and the approvals
// composable (its sole writer). Constructed before the stream because the stream
// reads it at build time; the approvals composable, built later, drives it.
const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map())

const chatStream = useChatStream({
  messages,
  lastHeaderRole,
  aborted,
  autoScroll,
  applySessionRunState: source => applySessionRunState(source),
  renderMarkdown,
  stripDirectiveTags,
  stripGeneratedArtifactMarkers,
  scrollToBottom,
  interruptState,
  rpcPolicy: () => rpc.policy,
})
const {
  isStreaming,
  streamArtifacts,
  streamBubble,
  streamHasVisibleOutput,
  streamTimelineItems,
  streamActivityStale,
  streamPhaseLabel,
  streamPhaseElapsed,
  streamToolElapsedText,
  streamIdleTimeoutMs,
  thinkingVisible,
  thinkingText,
  startStreaming,
  resetStreamForRouterReplay,
  resetLiveTurnState: resetStreamLiveTurnState,
  resetStreamIdleTimer,
  setStreamActivity,
  isToolGroupOpen,
  toggleToolGroup,
  isToolItemOpen,
  toggleToolItem,
  cleanup: cleanupStream,
  assertLiveParity,
  useReducer: foldLiveTurnMode,
  foldedTurn,
  appendInterruptFrame,
  ensureInterruptBubble,
} = chatStream
const chatAttachments = useChatAttachments()
const {
  pendingAttachments,
  onFileInputChange,
  addAttachments,
  removeAttachment,
  retryAttachment,
  hasPendingAttachmentWork,
  prepareAttachmentsForSend,
} = chatAttachments

let sendCurrentInput: () => void = () => {}
// Late-bound: dispatchHiddenSend is created below (useChatSend) but the /meta
// slash handler (useChatSlashCommands, created earlier) needs it at call time.
let dispatchHiddenForMeta: (providerText: string, displayText: string) => void = () => {}
let isCompactInFlightForCurrentSession: () => boolean = () => false
let dispatchHiddenControl: (providerText: string, displayText: string) => void = () => {}
const pendingQueueOwnerContext = ref<PendingQueueOwnerContext | null>(null)
const chatPendingQueue = useChatPendingQueue({
  sessionKey,
  ownerContext: pendingQueueOwnerContext,
  inputText,
  pendingAttachments,
  pendingSessionIntent,
  isStreaming,
  isBlocked: () => (
    isCompactInFlightForCurrentSession()
    || pendingQueueOwnerContext.value?.sessionKey === sessionKey.value
  ),
  autoResizeTextarea,
  sendCurrentInput: () => sendCurrentInput(),
  resetInputHistory: () => resetComposerInputHistory(),
  hasComposer: () => Boolean(composerRef.value),
  dispatchHiddenControl: (providerText, displayText) => dispatchHiddenControl(providerText, displayText),
})
const {
  pendingQueue,
  canQueueMore,
  busySendMode,
  maxPending,
  enqueuePendingInput,
  enqueueHiddenControl,
  removePendingChip,
  clearPendingQueue,
  switchPendingQueue,
  adoptPendingQueue,
  popPendingTail,
  popAllPendingIntoComposer,
  schedulePendingDrainAfterTerminal,
  flushDeferredPendingDrain,
  cleanup: cleanupPendingQueue,
} = chatPendingQueue

const chatCompaction = useChatCompaction({
  sessionKey,
  schedulePendingDrainAfterTerminal,
  popAllPendingIntoComposer,
})
const {
  compactStatus,
  compactElapsed,
  setCompactInFlight,
  hideCompactStatus,
  showCompactStatus,
  showCompactionToast,
  cleanup: cleanupCompaction,
} = chatCompaction
isCompactInFlightForCurrentSession = chatCompaction.isCompactInFlightForCurrentSession

// The context gauge stays up while compaction runs and settles on completed;
// skipped/failed/cancelled keep the card head only.
const compactGaugeVisible = computed(() =>
  compactStatus.value.isBusy || compactStatus.value.status === 'completed')

const chatUsageWidget = useChatUsageWidget({
  rpc,
  sessionKey,
  tokenVizEnabled: () => appStore.features.tokenViz,
})
const {
  usageAccum,
  usageModel,
  resetSavingsPopupCooldown,
  saveWidgetState,
  restoreWidgetState,
  loadCurrentSessionUsage,
} = chatUsageWidget

const chatFeatureToggles = useChatFeatureToggles({
  rpc,
  setGlobalElevatedMode,
  loadCurrentSessionUsage,
})
const {
  routerSlots,
  routerModels,
  routerEnabled,
  modelRoutingMode,
  modelRoutingSettingsBusy,
  routerVisualEffectsEnabled,
  routerVisualMode,
  codingModeEnabled,
  codingModeSettingsBusy,
  routerTierConfigs,
  loadFeatureToggles,
  setModelRoutingMode,
  setCodingModeEnabled,
  setRouterVisualEffectsEnabled,
  bindFeatureRefresh,
} = chatFeatureToggles

const chatRouterDecisionRuntime = useChatRouterDecisionRuntime({
  messages,
  sessionKey,
  isStreaming,
  autoScroll,
  modelRoutingMode,
  streamBubble,
  streamHasVisibleOutput,
  startStreaming,
  resetStreamForRouterReplay,
  resetStreamIdleTimer,
  setStreamActivity,
  scrollToBottom,
})
const {
  pendingDecision,
  handleRouterControlReplay,
  queueRouterDecision,
  appendEnsembleProgress,
  markEnsembleHandoff,
  flushPendingRouterDecision,
  clearPendingRouterDecision,
} = chatRouterDecisionRuntime

// Gate the live answer's reveal to a [MIN,MAX] window so the model-router panel
// decides (and animates) first, then the answer follows. Self-cleans via the
// composable's onScopeDispose.
const { answerRevealOpen, revealNow } = useChatAnswerReveal({
  isStreaming,
  routerEnabled,
  routerVisualEffectsEnabled,
  routerDecided: () => pendingDecision.value,
})

const chatSessionRoute = useChatSessionRoute(sessionKey)
const {
  route,
  createSessionKey,
  draftAgentId,
  goToDraft,
  hasLegacyNewChatQuery,
  isDraftRoute,
  persistSession,
  resolveInitialSession,
} = chatSessionRoute

const renderSourceMessages = computed(() =>
  messagesWithStoppedOutputNotice(
    messages.value,
    runStatus.value,
    t('sessions.status.outputInterrupted'),
  ),
)
const chatRenderedMessages = useChatRenderedMessages({
  messages: renderSourceMessages,
  interruptState,
  sessionKey,
  routerSlots,
  routerModels,
  routerTierConfigs,
  routerVisualEffectsEnabled,
  routerVisualMode,
  modelRoutingMode,
  isStreaming,
  renderMarkdown,
  stripGeneratedArtifactMarkers,
  stripTimePrefix,
  isSubagentCompletionMessage,
})
const { renderedMessages, routerDecisionCells } = chatRenderedMessages

function shouldRenderRouterStrip(_message: ChatRenderedMessage): boolean {
  // Always surface the router strip — the live ensemble strip is the primary
  // surface for the synthesizing process and no longer defers to activity.
  return true
}

/**
 * Pre-decision router-strip placeholder. It holds the strip's slot for the short
 * window before the first router_decision / ensemble_progress lands, so the live
 * ensemble strip appears from the very start of the turn rather than popping in.
 */
const routerStripReserve = computed<ChatRenderedMessage | null>(() => {
  if (!isStreaming.value || !routerEnabled.value || !routerVisualEffectsEnabled.value) return null
  const rendered = renderedMessages.value
  for (let i = rendered.length - 1; i >= 0; i--) {
    const msg = rendered[i]
    if (msg.isRouterStrip) return null
    if (msg.displayRole === 'user') break
  }
  if (modelRoutingMode.value === 'llm_ensemble') {
    return {
      id: 'router-strip-reserve',
      role: 'router',
      displayRole: 'router',
      roleLabel: 'Router',
      text: '',
      timeStr: '',
      showHeader: false,
      isRouterStrip: true,
      routerState: 'pending',
      routerSource: 'llm_ensemble',
      routerStatic: false,
      routerPanel: 'llm-ensemble',
      routerMode: 'llm_ensemble',
      gridCells: [],
      winnerIdx: -1,
    }
  }

  const cells = routerDecisionCells({ tier: '', model: '' })
  if (cells.length <= 1) return null
  return {
    id: 'router-strip-reserve',
    role: 'router',
    displayRole: 'router',
    roleLabel: 'Router',
    text: '',
    timeStr: '',
    showHeader: false,
    isRouterStrip: true,
    routerState: 'pending',
    routerSource: 'none',
    routerStatic: false,
    routerPanel: routerVisualMode.value === 'legacy_grid' ? 'legacy-grid' : 'real-candidates',
    gridCells: cells,
    winnerIdx: -1,
  }
})

const aiGeneratedLabel = computed(() => t('chat.aiGeneratedLabel'))

const chatShareExport = useChatShareExport({
  threadRef,
  title: shareTitle,
  aiGeneratedLabel: () => aiGeneratedLabel.value,
})

const preserveHistoryLiveTail = computed(() =>
  isStreaming.value || ['queued', 'running', 'approval_pending'].includes(runStatus.value.status),
)

const chatHistory = useChatHistory({
  rpc,
  sessionKey,
  messages,
  threadRef,
  lastHeaderRole,
  lastHeaderDay,
  preserveLiveTail: preserveHistoryLiveTail,
  autoScroll,
  stripTimePrefix,
  scrollToBottom,
})
const {
  historyState,
  loadHistory,
  loadEarlierHistory,
  retryHistory,
  scheduleHistorySync,
  cancelAnchorStabilization,
  cleanup: cleanupHistory,
} = chatHistory

const voiceInput = useVoiceInput()
const {
  voiceBusy,
  voiceRecording,
  toggleVoiceInput,
  cleanup: cleanupVoiceInput,
} = voiceInput

// Gate the composer mic button on real transcription readiness. onboarding.status
// resolves whether audio is enabled AND an ElevenLabs key is present server-side
// (including env-var keys the browser can't see), so audioConfigured is a true
// "voice will work" signal — this keeps the button from being clicked into a
// guaranteed failure. It's the same snapshot the empty-state chips read.
const voiceCapability = useRpcCall<{ audioConfigured?: boolean }>('onboarding.status')
const voiceReady = computed(() => voiceCapability.data.value?.audioConfigured === true)

const chatMessageActions = useChatMessageActions({
  messages,
  inputText,
  isStreaming,
  sanitizeCopyText,
  stripTimePrefix,
  autoResizeTextarea,
  sendCurrentInput: () => sendCurrentInput(),
  focusComposer: () => composerRef.value?.focusTextarea(),
  pendingForkBeforeMessageId,
  aiGeneratedLabel: () => aiGeneratedLabel.value,
  notifyMessagePending: () => pushToast(t('chat.toast.messageStillSaving'), { tone: 'info' }),
})
const {
  copyMessage,
  regenerateMessage,
  editMessage,
} = chatMessageActions

const chatSessionSubscription = useChatSessionSubscription({
  rpc,
  sessionKey,
  lastStreamSeq,
  runStatus,
  isStreaming,
  hasActiveInterrupt: computed(() =>
    Array.from(interruptState.value.values()).some(state => !state.resolution)),
  activeStreamTaskId,
  activeTaskGroups,
  sessionRunStatus,
  startStreaming,
  loadHistory,
  resetStreamIdleTimer,
  resetStreamLiveTurnState,
  onAuthoritativeIdle: () => {
    const taskId = activeStreamTaskId.value
    if (
      taskId
      && taskId !== PENDING_STREAM_TASK_ID
      && taskId !== STOPPED_STREAM_TASK_ID
    ) {
      schedulePendingDrainAfterTerminal()
    }
  },
})
const {
  isHydrating: isSessionHydrating,
  subscribeSession,
  unsubscribeSession,
} = chatSessionSubscription
applySessionRunState = chatSessionSubscription.applySessionRunState
const canStop = computed(() => !isSessionHydrating.value && (
  isStreaming.value
  || activeTaskGroups.value.size > 0
  || ['queued', 'running', 'approval_pending'].includes(runStatus.value.status)
  || pendingQueueOwnerContext.value?.sessionKey === sessionKey.value
))

const chatSessionRuntime = useChatSessionRuntime({
  sessionKey,
  messages,
  pendingSessionIntent,
  routerDecisionPending: pendingDecision,
  currentEpoch,
  lastStreamSeq,
  activeTaskGroups,
  aborted,
  lastHeaderRole,
  lastHeaderDay,
  usageAccum,
  usageModel,
  createSessionKey,
  persistSession,
  unsubscribeSession,
  subscribeSession,
  loadHistory,
  loadCurrentSessionUsage,
  applySessionRunState,
  setCompactInFlight,
  hideCompactStatus,
  clearPendingQueue,
  switchPendingQueue,
  adoptPendingQueue,
  resetSavingsPopupCooldown,
  restoreWidgetState,
  resetStreamLiveTurnState,
})
const {
  resetCurrentSessionAfterSlash,
  startDraftSession,
  switchToSession,
  adoptResponseSession,
} = chatSessionRuntime

const chatSlashCommands = useChatSlashCommands({
  rpc,
  inputText,
  sessionKey,
  autoResizeTextarea,
  newSession: () => goToDraft({ agentId: 'main' }),
  resetCurrentSession: resetCurrentSessionAfterSlash,
  setCompactInFlight,
  showCompactStatus,
  notify: (message: string) => pushToast(message, { duration: 6000 }),
  dispatchHidden: (providerText: string, displayText: string) => dispatchHiddenForMeta(providerText, displayText),
})
const {
  slashOpen,
  slashIdx,
  metaSkillChoices,
  filteredSlashCmds,
  loadSlashCommands,
  handleSlashInput,
  closeSlashMenu,
  selectSlashCmd,
  executeSlashCommand,
} = chatSlashCommands

const chatComposerShortcuts = useChatComposerShortcuts({
  inputText,
  composing,
  messages,
  pendingQueue,
  canQueueMore,
  slashOpen,
  slashIdx,
  filteredSlashCmds,
  isStreaming,
  autoResizeTextarea,
  handleSlashInput,
  closeSlashMenu,
  selectSlashCmd,
  popPendingTail,
  enqueuePendingInput,
  sendCurrentInput: () => sendCurrentInput(),
})
const {
  onTextareaBeforeInput,
  onTextareaInput,
  onTextareaKeydown,
} = chatComposerShortcuts
resetComposerInputHistory = chatComposerShortcuts.resetInputHistory

const chatSend = useChatSend({
  rpc,
  inputText,
  messages,
  sessionKey,
  pendingQueueOwnerContext,
  busySendMode,
  modelRoutingMode,
  modelRoutingSettingsBusy,
  elevatedMode,
  runMode,
  pendingAttachments,
  pendingSessionIntent,
  pendingForkBeforeMessageId,
  aborted,
  activeStreamTaskId,
  activeStreamSessionKey,
  autoScroll,
  stream: chatStream,
  canStop: () => canStop.value,
  normalizeElevatedMode,
  adoptResponseSession,
  scheduleHistorySync,
  schedulePendingDrainAfterTerminal,
  flushDeferredPendingDrain,
  bindActiveStreamTask: taskId => bindActiveStreamTask(taskId),
  isCompactInFlightForCurrentSession,
  hasPendingAttachmentWork,
  prepareAttachmentsForSend,
  enqueuePendingInput,
  enqueueHiddenControl,
  popAllPendingIntoComposer,
  executeSlashCommand,
  closeSlashMenu,
  autoResizeTextarea,
  scrollToBottom,
})
const { onSend, onStop, dispatchHiddenSend, sendHiddenMetaPreflightConfirmation } = chatSend
sendCurrentInput = onSend
dispatchHiddenForMeta = dispatchHiddenSend
dispatchHiddenControl = dispatchHiddenSend

// Deny notes ride the normal send path: queued while the turn is streaming,
// sent immediately otherwise.
function queueDenyFeedback(note: string) {
  if (isStreaming.value || isCompactInFlightForCurrentSession()) {
    enqueuePendingInput(note)
    return
  }
  const prior = inputText.value
  inputText.value = note
  void onSend()
  if (prior.trim()) {
    inputText.value = prior
    autoResizeTextarea()
  }
}

const chatApprovals = useChatApprovals({
  rpc,
  sessionKey,
  runStatus,
  stream: { isStreaming, appendInterruptFrame, ensureInterruptBubble },
  interruptState,
  onDenyFeedback: queueDenyFeedback,
  onSnapshotCount: count => appStore.setApprovalCount(count),
})
const {
  approvalEntries,
  approvalBusyIds,
  pendingClarify,
  clarifySubmitted,
  clarifyBusy,
  clarifyError,
  resolveApproval,
  resolveInterrupt,
  extendInterrupt,
  submitClarify,
  dismissClarify,
} = chatApprovals

const rpcEventHandlers = useChatRpcEventHandlers({
  sessionKey,
  currentEpoch,
  lastStreamSeq,
  activeTaskGroups,
  activeStreamTaskId,
  aborted,
  messages,
  pendingQueue,
  usageAccum,
  usageModel,
  stream: chatStream,
  normalizeRunStatus,
  sessionRunStatus,
  applySessionRunState,
  queueRouterDecision,
  appendEnsembleProgress,
  markEnsembleHandoff,
  flushPendingRouterDecision,
  clearPendingRouterDecision,
  handleRouterControlReplay,
  showCompactionToast,
  showWarningToast: message => pushToast(message || t('chat.warning.default'), { tone: 'warn', duration: 5000 }),
  scheduleHistorySync,
  schedulePendingDrainAfterTerminal,
  popAllPendingIntoComposer,
  saveWidgetState,
  subscribeSession,
  loadHistory,
  loadCurrentSessionUsage,
})
bindActiveStreamTask = rpcEventHandlers.bindActiveStreamTask
const {
  streamThinkingText,
  streamThinkingElapsedText,
  attachTurnReasoning,
} = rpcEventHandlers

// live-turn shadow parity: in DEV/SHADOW, re-check the fold against the legacy
// live surface whenever a frame lands (the fold and legacy refs are tracked by
// assertLiveParity). Injects the thinking text owned by the event handlers.
// In production ON mode this is a no-op; DEV/SHADOW performs the parity check,
// while explicit OFF keeps the compatibility renderer without fold assertions.
watchEffect(() => assertLiveParity(streamThinkingText))

// Flag-selected live render source. In production the fold is authoritative by
// default; only opensquilla.chat.foldLiveTurn=0 restores legacy. SHADOW and OFF
// return the IDENTICAL legacy refs, so with the flag off the render is byte-identical.
// The activity head (phase/elapsed) stays on the legacy activity refs.
const liveTimelineItems = computed(() =>
  foldLiveTurnMode.value === true ? foldedTurn.value.timelineItems : streamTimelineItems.value,
)
const liveTimelineSplit = computed(() => splitLiveAssistantTimeline(liveTimelineItems.value))
const liveAnswerPart = computed<Extract<ChatPart, { type: 'text' }> | null>(() => {
  const candidate = liveTimelineSplit.value.answerItem
  if (!candidate) return null
  return {
    type: 'text',
    key: `${candidate.key}:answer-candidate`,
    html: candidate.html,
    rawText: candidate.rawText || '',
  }
})
const liveActivityTimelineItems = computed<ChatStreamTimelineItem[]>(() =>
  liveTimelineSplit.value.activityItems,
)
const liveActivityStatusHistory = computed(() =>
  foldLiveTurnMode.value === false ? [] : foldedTurn.value.statusHistory,
)
const liveActivityProjection = computed(() =>
  projectAssistantActivityTimeline(liveActivityTimelineItems.value, {
    lifecycle: liveAnswerPart.value ? 'answering' : 'working',
    statusHistory: liveActivityStatusHistory.value,
  }),
)
const liveActivityPhaseLabel = computed(() => {
  if (streamActivityStale.value) return streamPhaseLabel.value
  const currentStatus = [...liveActivityProjection.value.statusSteps]
    .reverse()
    .find(step => step.isCurrent)
  if (
    currentStatus
    && !currentStatus.label.code.startsWith('chat.activity.lifecycle.')
    && !liveActivityProjection.value.currentClusterKey
  ) {
    return String(t(currentStatus.label.code, currentStatus.label.params))
  }
  return String(t(
    liveAnswerPart.value
      ? 'chat.activity.lifecycle.answering'
      : 'chat.activity.lifecycle.working',
  ))
})
const liveToolStateScope = computed(() => JSON.stringify([sessionKey.value || '', 'stream']))
// Elapsed readouts in the live turn round to whole seconds ("4s"), matching
// streamPhaseElapsed and streamThinkingElapsedText. The shared tool formatter
// (streamToolElapsedText, useChatStream.ts) emits tenths, so normalise its
// output here at the call site instead of changing the shared formatter —
// except sub-second finished tools, which keep their tenths so they never
// read as a nonsensical "0s".
function liveToolElapsedText(call: Pick<ChatToolCall, 'toolId'>): string {
  return streamToolElapsedText(call).replace(/^([1-9]\d*)\.\d+s$/, '$1s')
}
const liveArtifacts = computed(() =>
  foldLiveTurnMode.value === true ? foldedTurn.value.artifacts : streamArtifacts.value,
)
const liveThinkingText = computed(() =>
  foldLiveTurnMode.value === true ? foldedTurn.value.thinkingText : streamThinkingText.value,
)
// Live reasoning rendered through the shared part component, so the live turn
// and settled turns use one wording and one disclosure affordance. The seconds
// derive from the ticking elapsed text, which is always `${seconds}s` live.
const liveReasoningPart = computed<Extract<ChatPart, { type: 'reasoning' }> | null>(() => {
  if (!liveThinkingText.value) return null
  const seconds = Number.parseInt(streamThinkingElapsedText.value, 10)
  return {
    type: 'reasoning',
    key: 'live-reasoning',
    text: liveThinkingText.value,
    seconds: Number.isFinite(seconds) ? seconds : 0,
  }
})
// No clamp and no raw status count: the header chip must agree with the
// visible body, which renders clusters plus only the semantic status steps.
// A text-only turn therefore counts 0 and the disclosure's stepCount > 0
// gate hides the chip instead of claiming "step 1" over an empty body.
const liveActivityStepCount = computed(() =>
  liveActivityProjection.value.activityClusters.length
    + liveActivityProjection.value.statusSteps.filter(isSemanticActivityStatusStep).length
    + (liveThinkingText.value ? 1 : 0),
)
const liveActivityFailureCount = computed(() =>
  liveActivityProjection.value.activityClusters.filter(cluster => cluster.isFailure).length,
)
// Inline interrupt parts for the live turn come from the fold whenever it is
// active (ON or SHADOW — frames are appended in both). Only the foldLiveTurn=0
// OFF rollback renders the legacy standalone ApprovalCard/ClarifyCard block, so
// the two never both show. Unlike the activity body (which has a legacy ref to
// fall back to in SHADOW), interrupts have no legacy live ref, so SHADOW must
// also render them from the fold.
const liveInterruptParts = computed(() =>
  foldLiveTurnMode.value === false
    ? []
    : foldedTurn.value.parts.filter(
        (part): part is Extract<typeof part, { type: 'interrupt' }> => part.type === 'interrupt',
      ),
)

// Feeds the persistent visually-hidden status region in the template. It only
// fills on the true→false streaming transition (a live turn actually settled),
// and empties as soon as the next turn starts so that setting the same
// "Completed" text again is a fresh mutation screen readers re-announce.
const turnSettledAnnouncement = ref('')
watch(isStreaming, (streaming, wasStreaming) => {
  if (streaming) turnSettledAnnouncement.value = ''
  else if (wasStreaming) {
    turnSettledAnnouncement.value = String(t('chat.activity.lifecycle.settled'))
  }
})

// Soft content-silence watchdog: after the high negotiated threshold, surface
// a neutral long-running notice. Backend-deadline-owned Ensemble phases remain
// suppressed, while the hard idle timer continues to mean no events at all.
const stallWatchdog = useChatStallWatchdog({ isStreaming, streamIdleGraceMs: streamIdleTimeoutMs })
const { stallActive, stallSeconds } = stallWatchdog

const chatRpcSubscriptions = useChatRpcSubscriptions(rpc, {
  ...rpcEventHandlers.handlers,
  // The wildcard handler is the one funnel that sees every gateway event with
  // its name; feed the active session's events to the watchdog before the
  // regular handler consumes them (same session filter as existing handlers).
  onAny: (rawEvent, rawPayload) => {
    const payloadObj = (rawPayload && typeof rawPayload === 'object' ? rawPayload : {}) as SessionEventPayload
    if (payloadIsCurrentSession(payloadObj, sessionKey.value)) {
      stallWatchdog.noteEvent(rawEvent, payloadObj)
    }
    rpcEventHandlers.handlers.onAny(rawEvent, rawPayload)
  },
})

// Session switches drop the previous session's stall tracking entirely.
watch(sessionKey, () => {
  stallWatchdog.reset()
  clearAssistantActivityExpansionState()
})

// MetaSkill run UI: preflight checkpoint + run-progress ribbon, driven by the
// four session.event.meta_* frames (delivered via the '*' wildcard, so this
// controller must not re-consume stream_seq).
const metaRuns = useMetaRuns({
  rpc,
  sessionKey,
  currentEpoch,
  lastStreamSeq,
  sendHiddenConfirmation: sendHiddenMetaPreflightConfirmation,
  scrollToStepCard,
  sendComposerText,
  lastUserMessageText,
  // The composer placeholder is a computed prop, so a true placeholder setter
  // is not exposed; surface the switch-skill hint via the toast path (keeping
  // focus) so the vanilla guidance is not silently dropped.
  setComposerPlaceholder: (hint: string) => pushToast(hint, { duration: 6000 }),
  focusComposer: () => composerRef.value?.focusTextarea(),
  pushToast,
})

// Refill the composer with `text` and fire the send path (mirrors vanilla's
// retry/replay tail: `_textarea.value = text; _autoResizeTextarea(); _onSend()`).
function sendComposerText(text: string) {
  const next = String(text || '')
  if (!next) return
  inputText.value = next
  autoResizeTextarea()
  void sendCurrentInput()
}

// The most recent user message text (mirrors vanilla `_latestUserMessageText`).
function lastUserMessageText(): string {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    if (messages.value[i]?.role === 'user') return messages.value[i].text || ''
  }
  return ''
}

// Resolve a step's in-thread tool card and scroll it into view (chip click /
// show-detail). The card carries data-tool-use-id="meta_step_<id>".
function scrollToStepCard(toolUseId: string) {
  const root = threadRef.value
  if (!root) return
  const card = root.querySelector(`[data-tool-use-id="${cssEscapeAttr(toolUseId)}"]`)
  if (card && typeof (card as HTMLElement).scrollIntoView === 'function') {
    const reduceMotion = typeof window !== 'undefined'
      && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    ;(card as HTMLElement).scrollIntoView({ block: 'center', behavior: reduceMotion ? 'auto' : 'smooth' })
  }
}

function cssEscapeAttr(value: string): string {
  if (typeof window !== 'undefined' && window.CSS && typeof window.CSS.escape === 'function') {
    return window.CSS.escape(value)
  }
  return String(value ?? '').replace(/[^a-zA-Z0-9_-]/g, '\\$&')
}

// History syncs replace the messages array; rows carry reasoning text but
// not the measured thinking duration — re-attach this session's records.
watch(messages, () => attachTurnReasoning())

// Unsubscribers
let unsubs: (() => void)[] = []
let chatViewDisposed = false
let composerResizeObserver: ResizeObserver | null = null

/* ── Computed ──────────────────────────────────────────────────────── */

const isNewChatLanding = computed(() => {
  // Only the draft route (/chat/new — bare /chat redirects here) shows the
  // "new chat" landing. Without this gate, switching between existing
  // conversations briefly cleared `messages` and flashed the landing, because
  // the empty-thread moment of a session load looked identical to a new draft.
  return isDraftRoute() &&
    messages.value.length === 0 &&
    !isStreaming.value &&
    pendingQueue.value.length === 0 &&
    !compactStatus.value.visible
})

const initialSessionLoadState = computed<'loading' | 'error' | null>(() => {
  return resolveChatSessionLoadState({
    isDraftLanding: isNewChatLanding.value,
    isStreaming: isStreaming.value,
    messageCount: messages.value.length,
    initialHistoryStatus: historyState.value.initialLoadStatus,
    sessionHydrating: isSessionHydrating.value,
  })
})

const historySentinelError = computed(() => shouldShowHistorySentinelError({
  loadEarlierError: historyState.value.loadEarlierError,
  initialHistoryStatus: historyState.value.initialLoadStatus,
  initialLoadSurface: initialSessionLoadState.value,
}))

const historySentinelLoading = computed(() => shouldShowHistorySentinelLoading({
  loadingEarlier: historyState.value.loadingEarlier,
  historyLoading: historyState.value.loading,
  historyRetrying: historyState.value.retrying,
  initialHistoryStatus: historyState.value.initialLoadStatus,
  initialLoadSurface: initialSessionLoadState.value,
}))

const initialSessionLoadAnnouncement = ref('')
watch(
  [initialSessionLoadState, locale, sessionKey],
  ([state, , activeSession]) => {
    initialSessionLoadAnnouncement.value = ''
    if (state !== 'loading') return
    void nextTick(() => {
      if (
        initialSessionLoadState.value === 'loading'
        && sessionKey.value === activeSession
      ) {
        initialSessionLoadAnnouncement.value = t('chat.loadingSession')
      }
    })
  },
  { immediate: true },
)

const composerPlaceholder = computed(() => {
  if (isNewChatLanding.value) return t('chat.placeholderLanding')
  return isCompactViewport.value ? t('chat.placeholderCompact') : t('chat.placeholder')
})

const hasSendContent = computed(() => {
  return inputText.value.trim().length > 0 || pendingAttachments.value.some(isSendableAttachment)
})

const modelImageSendBlockedMessage = computed(() => {
  if (!hasSendableModelInputImageAttachment(pendingAttachments.value)) return ''
  if (modelRoutingSettingsBusy.value) {
    return t('chat.composer.routingUpdateImageBlocked')
  }
  return modelRoutingMode.value === 'llm_ensemble'
    ? t('chat.composer.ensembleImageUnsupported')
    : ''
})

const sendButtonTitle = computed(() => {
  if (modelImageSendBlockedMessage.value) return modelImageSendBlockedMessage.value
  if (isCompactInFlightForCurrentSession()) return t('chat.sendQueuesUntilCompaction')
  if (isStreaming.value) {
    return busySendMode.value === 'steer'
      ? t('chat.sendSteers')
      : t('chat.sendQueues')
  }
  return t('chat.send')
})

const currentChatTitle = computed(() => {
  const firstUser = messages.value.find(msg => msg.role === 'user' && stripTimePrefix(msg.text || '').trim())
  if (firstUser) {
    return truncate(stripTimePrefix(firstUser.text).replace(/\s+/g, ' ').trim(), 28)
  }
  const suffix = sessionKey.value.split(':').pop() || ''
  if (!suffix || suffix === 'default') return t('chat.newChat')
  return t('chat.chatWithSuffix', { suffix })
})

const chatMarkdownExport = useChatMarkdownExport({
  messages: renderedMessages,
  currentTitle: currentChatTitle,
  aiGeneratedLabel,
})
const { exportMarkdown } = chatMarkdownExport

const shareableMessageCount = computed(() => renderedMessages.value.filter(isShareableChatMessage).length)
const selectedShareCount = computed(() => selectedShareMessageIds.value.size)

/* ── Helpers ───────────────────────────────────────────────────────── */

function readAuthToken(): string {
  try {
    return sessionStorage.getItem('opensquilla.wsToken') || ''
  } catch {
    return ''
  }
}

function setComposerRunMode(mode: SandboxRunMode) {
  setPersistedRunMode(mode)
}

async function setComposerModelRoutingMode(mode: ModelRoutingMode) {
  await setModelRoutingMode(mode)
  scheduleHistorySync()
}

function setComposerVisualEffectsEnabled(enabled: boolean) {
  setRouterVisualEffectsEnabled(enabled)
  scheduleHistorySync()
}

async function setComposerCodingModeEnabled(enabled: boolean) {
  await setCodingModeEnabled(enabled)
}

// A landing suggestion chip replaces the draft composer text; the user still
// reviews and sends it themselves.
function applyLandingSuggestion(text: string) {
  inputText.value = text
  autoResizeTextarea()
  composerRef.value?.focusTextarea()
}

function appendComposerText(text: string) {
  const next = String(text || '').trim()
  if (!next) return
  inputText.value = inputText.value.trim()
    ? `${inputText.value.trimEnd()}\n${next}`
    : next
  autoResizeTextarea()
  composerRef.value?.focusTextarea()
}

function onVoiceInput() {
  void toggleVoiceInput(appendComposerText)
}

// When voice isn't configured the mic button routes here instead of recording:
// tell the user what's missing and take them straight to the audio settings.
function onVoiceSetup() {
  pushToast(t('chat.toast.voiceSetupNeeded'), { tone: 'info' })
  router.push('/settings/capabilities').catch(() => {})
}

function normalizeRunStatus(status: string): ChatRunStatusState {
  const value = String(status || '').toLowerCase()
  if (value === 'abandoned') return 'interrupted'
  if (value === 'killed') return 'cancelled'
  if (['succeeded', 'success', 'complete'].includes(value)) return 'idle'
  if (CHAT_RUN_STATUS_VALUES.includes(value as ChatRunStatusState)) return value as ChatRunStatusState
  return 'idle'
}

function runStatusLabelText(status: ChatRunStatusState, source?: ChatRunStatusSource | null): string {
  if (status === 'cancelled' || status === 'interrupted') {
    return sessionRunStatusLabelText(status, source || undefined)
  }
  const labels: Record<string, string> = {
    queued: t('chat.status.queued'),
    running: t('chat.status.running'),
    approval_pending: t('chat.status.approvalPending'),
    interrupted: t('chat.status.interrupted'),
    failed: t('chat.status.failed'),
    timeout: t('chat.status.timeout'),
    cancelled: t('chat.status.cancelled'),
    idle: t('chat.status.idle'),
  }
  return labels[status] || t('chat.status.idle')
}

function sessionRunStatus(source: ChatRunStatusSource | null | undefined): ChatRunStatus {
  const stateSource = source || {}
  const active = stateSource.active_task || stateSource.activeTask || null
  const last = stateSource.last_task || stateSource.lastTask || null
  const activeStatus = active ? normalizeRunStatus(active.status || '') : ''
  let status = normalizeRunStatus(stateSource.run_status || stateSource.runStatus || active?.status || last?.status || '')
  if (active && (activeStatus === 'queued' || activeStatus === 'running' || activeStatus === 'approval_pending')) status = activeStatus
  const task = active || last || null
  return { status, label: runStatusLabelText(status, stateSource), task }
}

/* ── Subagent ──────────────────────────────────────────────────────── */

function isSubagentCompletionMessage(role: string, text: string, options?: ChatMessage): boolean {
  if (role !== 'system' || !text) return false
  if (options?.provenanceSourceTool === 'subagent_completion') return true
  try {
    const parsed = JSON.parse(text)
    return parsed && parsed.type === 'subagent_completion'
  } catch { return false }
}

function subagentSummary(text: string): string {
  try {
    const parsed = JSON.parse(text)
    return t('chat.subagentPrefix') + (parsed.child_session_key || parsed.session_key || 'completion')
  } catch { return t('chat.subagentCompletion') }
}

function subagentBody(text: string): string {
  try {
    const parsed = JSON.parse(text)
    return JSON.stringify(parsed, null, 2)
  } catch { return text }
}

/* ── Artifacts ─────────────────────────────────────────────────────── */

async function downloadAttachment(attachment: DisplayAttachment): Promise<boolean> {
  const result = await fetchDisplayAttachmentBlob(attachment, {
    baseOrigin: window.location.origin,
    sessionKey: sessionKey.value,
    authToken: readAuthToken(),
  })
  if (!result.ok) {
    if (result.status > 0) {
      pushToast(t('chat.toast.downloadFailedHttp', { status: result.status }), { tone: 'danger' })
    } else {
      pushToast(t('chat.toast.downloadFailed'), { tone: 'danger' })
    }
    return false
  }
  downloadBlob(result.blob, result.filename)
  return true
}

async function downloadArtifact(artifact: ArtifactPayload) {
  const token = readAuthToken()
  const url = artifactDownloadUrl(artifact, window.location.origin, {
    sessionKey: sessionKey.value,
    includeSessionKey: false,
  })
  if (!url) return
  try {
    const headers: Record<string, string> = {}
    const sameOrigin = new URL(url, window.location.origin).origin === window.location.origin
    if (sameOrigin && sessionKey.value) headers['x-opensquilla-session-key'] = sessionKey.value
    if (sameOrigin && token) headers.Authorization = `Bearer ${token}`
    const response = await fetch(url, {
      method: 'GET',
      headers,
      credentials: sameOrigin ? 'same-origin' : 'omit',
    })
    if (!response.ok) {
      pushToast(t('chat.toast.downloadFailedHttp', { status: response.status }), { tone: 'danger' })
      return
    }
    const blob = await response.blob()
    downloadBlob(blob, artifact.name || 'artifact')
  } catch (err) {
    console.warn('Download failed:', err)
    pushToast(t('chat.toast.downloadFailed'), { tone: 'danger' })
  }
}

/**
 * Every deliverable the current session has produced, deduped by identity.
 * Artifacts arrive on completed/replayed assistant turns (`message.artifacts`,
 * filled from chat.history and from the streamed turn that just ended) and on
 * the in-flight turn (`streamArtifacts`); both feed the per-session drawer.
 */
const sessionArtifacts = computed<ArtifactPayload[]>(() => {
  const seen = new Set<string>()
  const collected: ArtifactPayload[] = []
  const consider = (artifact: ArtifactPayload | undefined | null) => {
    if (!artifact) return
    const id = String(artifact.id || artifact.download_url || artifact.name || '')
    if (!id || seen.has(id)) return
    seen.add(id)
    collected.push(artifact)
  }
  for (const message of messages.value) {
    message.artifacts?.forEach(consider)
  }
  streamArtifacts.value.forEach(consider)
  return collected
})

const deliverablesOpen = ref(false)
const metaRunsHistoryOpen = ref(false)

function focusHeaderAction(action: 'deliverables' | 'runs' | 'share' | 'copy-session-key') {
  void nextTick(() => chatHeaderActionsRef.value?.focusAction(action))
}

function openDeliverables() {
  if (sessionArtifacts.value.length === 0) return
  deliverablesOpen.value = true
}

function closeDeliverables() {
  deliverablesOpen.value = false
  focusHeaderAction('deliverables')
}

function openMetaRunHistory() {
  metaRunsHistoryOpen.value = true
}

function closeMetaRunHistory() {
  metaRunsHistoryOpen.value = false
  focusHeaderAction('runs')
}

/* ── Fork ──────────────────────────────────────────────────────────── */

// Whole-conversation fork from the tip: the backend copies the transcript
// into a sibling session, then we navigate there the same way the session
// switcher does (route query change → switchToSession).
const forkInFlight = ref(false)

async function forkConversation() {
  const parentKey = sessionKey.value
  if (!parentKey || forkInFlight.value) return
  if (pendingSessionIntent.value === 'new_chat') return
  forkInFlight.value = true
  try {
    const res = await rpc.call<{ key?: string }>('sessions.fork', { key: parentKey })
    const childKey = typeof res?.key === 'string' ? res.key : ''
    if (!childKey) throw new Error('sessions.fork returned no key')
    router.push({ path: '/chat', query: { session: childKey } }).catch(() => {})
  } catch (err) {
    console.warn('Fork failed:', err)
    pushToast(t('chat.toast.forkFailed'), { tone: 'danger' })
  } finally {
    forkInFlight.value = false
  }
}

// Owner recovery for a run paused by the sandbox denial ledger (the terminal
// error card exposes a Resume button). Clearing the pause lets the next turn
// proceed; the run itself already ended, so we prompt the user to resend.
async function resumeSandbox() {
  const key = sessionKey.value
  if (!key) return
  try {
    await rpc.call('sandbox.resume', { sessionKey: key })
    messages.value.push({
      role: 'system',
      text: t('chat.sandboxResumed'),
      ts: new Date().toISOString(),
    })
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err)
    pushToast(t('chat.sandboxResumeFailed', { error: detail }), { tone: 'danger' })
  }
}

const {
  copyState: sessionCopyState,
  copyIconName: sessionCopyIcon,
  copyLiveText: sessionCopyLiveText,
  onCopyClick: onSessionCopyClick,
} = useCopyFeedback(async () => {
  if (!sessionKey.value) return false
  try {
    await copyTextWithFallback(sessionKey.value)
    return true
  } catch {
    pushToast(t('chat.toast.copyFailed'), { tone: 'danger' })
    return false
  }
})

/* ── Share export ──────────────────────────────────────────────────── */

function startShareMode() {
  if (shareableMessageCount.value === 0) return
  shareMode.value = true
  selectedShareMessageIds.value = new Set()
  nextTick(() => shareBannerRef.value?.focus())
}

function endShareMode() {
  // Exiting tears down the banner and the bubble pickers; if focus was inside
  // ANY of that mode UI it would drop to <body>, so return it to the entry
  // button in every case.
  const active = document.activeElement
  const modeUiHadFocus = !!shareBannerRef.value?.contains(active)
    || !!(active instanceof HTMLElement
      && active.closest('[data-share-control], .msg-user--share-mode, .msg-ai--share-mode'))
  shareMode.value = false
  selectedShareMessageIds.value = new Set()
  // Leaving share mode invalidates any open preview (the selection it rendered
  // is gone), so drop the modal and its object URL alongside the mode.
  if (sharePreview.value) {
    URL.revokeObjectURL(sharePreview.value.url)
    sharePreview.value = null
  }
  if (modeUiHadFocus) focusHeaderAction('share')
}

function toggleShareMessage(messageId: string) {
  const next = new Set(selectedShareMessageIds.value)
  if (next.has(messageId)) next.delete(messageId)
  else next.add(messageId)
  selectedShareMessageIds.value = next
}

// Save renders the selected bubbles to a PNG blob and opens the preview modal;
// it no longer downloads directly. Share mode stays active while previewing so
// the user can still adjust the selection after closing the modal — it only
// ends once they commit with Download.
async function saveShareImage() {
  if (selectedShareMessageIds.value.size === 0 || shareSaving.value) return
  shareSaving.value = true
  try {
    await nextTick()
    const result = await chatShareExport.buildShareImage(selectedShareMessageIds.value, {
      theme: shareTheme.value,
    })
    if (!result) {
      pushToast(t('chat.toast.shareSaveFailed'), { tone: 'danger' })
      return
    }
    const url = URL.createObjectURL(result.blob)
    sharePreview.value = { url, blob: result.blob, filename: result.filename }
  } catch (err) {
    console.warn('Share image export failed:', err)
    pushToast(t('chat.toast.shareSaveFailed'), { tone: 'danger' })
  } finally {
    shareSaving.value = false
  }
}

function onShareDownload() {
  const preview = sharePreview.value
  if (!preview) return
  downloadBlob(preview.blob, preview.filename)
  pushToast(t('chat.toast.saved', { filename: preview.filename }), { duration: 4000 })
  // endShareMode revokes the preview URL and drops the modal. The modal's
  // Download button held focus outside the banner, so restore the best visible
  // Share entry (or the stable session-actions trigger) explicitly.
  endShareMode()
  focusHeaderAction('share')
}

async function onShareCopy() {
  const preview = sharePreview.value
  if (!preview) return
  const ok = await copyImageToClipboard(preview.blob)
  // Approved decision: the modal stays open after a copy so the user can copy
  // again or then download; only Download / Cancel / Escape closes it.
  pushToast(ok ? t('chat.toast.copiedToClipboard') : t('chat.toast.copyNotSupported'), {
    tone: ok ? undefined : 'danger',
  })
}

// Re-render the image in the chosen theme, swapping the object URL in place so
// the modal stays open and shows a busy state during the rebuild.
async function onShareSetTheme(next: ShareExportTheme) {
  if (next === shareTheme.value && sharePreview.value) return
  shareTheme.value = next
  if (!sharePreview.value || shareSaving.value) return
  shareSaving.value = true
  try {
    const result = await chatShareExport.buildShareImage(selectedShareMessageIds.value, { theme: next })
    if (!result) {
      pushToast(t('chat.toast.sharePreviewUpdateFailed'), { tone: 'danger' })
      return
    }
    const previous = sharePreview.value
    sharePreview.value = {
      url: URL.createObjectURL(result.blob),
      blob: result.blob,
      filename: result.filename,
    }
    if (previous) URL.revokeObjectURL(previous.url)
  } catch (err) {
    console.warn('Share image re-render failed:', err)
    // Not shareSaveFailed: nothing was being saved here — the theme switch
    // only re-renders the preview, so the copy must name that action.
    pushToast(t('chat.toast.sharePreviewUpdateFailed'), { tone: 'danger' })
  } finally {
    shareSaving.value = false
  }
}

// Close the preview without leaving share mode: revoke the URL and restore
// focus. While share mode is still active the header Share button is unmounted
// (v-if="!shareMode"), so focus returns to the share banner — the mode's anchor
// and where startShareMode put it; only once the mode has ended does the entry
// button exist to receive focus.
function closeSharePreview() {
  const preview = sharePreview.value
  if (preview) URL.revokeObjectURL(preview.url)
  sharePreview.value = null
  nextTick(() => {
    if (shareMode.value) shareBannerRef.value?.focus()
    else chatHeaderActionsRef.value?.focusAction('share')
  })
}

// The export composable owns all filename composition and slugging (it is
// CJK-aware). Hand it the raw human title and nothing else — pre-mangling here
// (e.g. stripping non-ASCII) would erase Chinese titles before the slugger sees
// them, and pre-composing a filename only forced the composable to take it back
// apart.
function shareTitle(): string {
  return currentChatTitle.value
}

/* ── Streaming ─────────────────────────────────────────────────────── */

function scrollToBottom() {
  nextTick(() => {
    // A stream/event may request a follow while the reader is at the live edge,
    // then the reader can scroll up before Vue applies this next-tick callback.
    // Re-check here so that queued automatic scrolls never override that choice.
    if (threadRef.value && autoScroll.value) {
      threadRef.value.scrollTop = threadRef.value.scrollHeight
    }
  })
}

function onThreadScroll() {
  if (!threadRef.value) return
  const gap = threadRef.value.scrollHeight - threadRef.value.scrollTop - threadRef.value.clientHeight
  historyNavigationScrollLock.updateFromScroll(gap)
}

function onHistoryNavigate() {
  cancelAnchorStabilization()
  historyNavigationScrollLock.start()
}

function onHistoryNavigateEnd() {
  historyNavigationScrollLock.finish()
  // Reconcile once at the final position: scroll events emitted during the
  // smooth transition were intentionally ignored while the lock was active.
  onThreadScroll()
}

// Show the jump-to-latest affordance whenever the reader has scrolled up off the
// live edge (autoScroll releases at gap >= 60) and there is content to return to.
// Re-pinning autoScroll lets the stream resume following the bottom.
const showJumpToLatest = computed(() => !autoScroll.value && messages.value.length > 0)
function jumpToLatest() {
  cancelAnchorStabilization()
  historyNavigationScrollLock.finish()
  autoScroll.value = true
  scrollToBottom()
}

/* ── Tool calls ────────────────────────────────────────────────────── */

function showToolResultModal(content: string, title = t('chat.toolResult'), context?: ToolResultContext) {
  toolResultModal.value = { open: true, title, content, context }
}

/* ── Attachments ───────────────────────────────────────────────────── */

function dragEventHasFiles(e: DragEvent): boolean {
  const types = Array.from(e.dataTransfer?.types || [])
  return types.includes('Files')
}

function onChatDragEnter(e: DragEvent) {
  if (!dragEventHasFiles(e)) return
  e.preventDefault()
  threadDragDepth.value += 1
  threadDragOver.value = true
}

function onChatDragOver(e: DragEvent) {
  if (!dragEventHasFiles(e)) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
  threadDragOver.value = true
}

function onChatDragLeave(e: DragEvent) {
  if (!dragEventHasFiles(e)) return
  threadDragDepth.value = Math.max(0, threadDragDepth.value - 1)
  if (threadDragDepth.value === 0) {
    threadDragOver.value = false
  }
}

function onChatDrop(e: DragEvent) {
  e.preventDefault()
  threadDragDepth.value = 0
  threadDragOver.value = false
  if (!dragEventHasFiles(e)) return
  const files = Array.from(e.dataTransfer?.files || [])
  if (files.length === 0) return
  void addAttachments(files)
  composerRef.value?.focusTextarea()
}

/* ── Textarea ──────────────────────────────────────────────────────── */

function autoResizeTextarea() {
  composerRef.value?.resizeTextarea()
}

/* ── Clipboard paste ───────────────────────────────────────────────── */

function onDocumentPaste(e: ClipboardEvent) {
  // Pastes aimed at another editable surface (clarify/approval inputs, the
  // command palette) or at an open dialog keep their default behavior — only
  // composer-bound pastes claim clipboard files, mirroring onDocumentKeydown.
  if (!shouldCaptureFilePaste(e.target, {
    composerTextareaFocused: composerRef.value?.isTextareaFocused() ?? false,
    dialogLayerOpen: hasOpenDialogLayer(),
  })) return
  const files = collectClipboardFiles(e.clipboardData)
  if (files.length === 0) return
  void addAttachments(files)
  // File managers and screenshot tools put both the file and its name/path as
  // text on the clipboard; once we have attached the files, suppress the
  // default paste so that text is not also dumped into the composer (and then
  // sent to the agent). Plain-text pastes with no file fall through unchanged.
  e.preventDefault()
}

/* ── Document keydown (ESC) ────────────────────────────────────────── */

function onDocumentKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (e.defaultPrevented) return
  if (hasOpenDialogLayer()) return

  // The share preview modal owns Escape while it is open: it closes only the
  // preview (share mode stays active) via its own handler, so bail here and let
  // it run rather than tearing down the whole share mode underneath it.
  if (sharePreview.value) return

  if (shareMode.value) {
    e.preventDefault()
    endShareMode()
    return
  }

  const target = e.target
  const editableTarget = target instanceof HTMLInputElement
    || target instanceof HTMLSelectElement
    || (target instanceof HTMLTextAreaElement && !composerRef.value?.isTextareaFocused())
    || (target instanceof HTMLElement && target.isContentEditable)
  if (editableTarget) return

  if (canStop.value) {
    e.preventDefault()
    onStop()
    return
  }

  if (pendingQueue.value.length > 0 && !composerRef.value?.isTextareaFocused()) {
    e.preventDefault()
    popAllPendingIntoComposer()
  }
}

/* ── Lifecycle ─────────────────────────────────────────────────────── */

// One-shot composer prefill carried in history state (the Sessions Hub task
// input navigates here with it). Consumed on draft entry so reload or
// back/forward does not re-apply the text.
function consumeDraftPrefill() {
  const state = window.history.state as Record<string, unknown> | null
  const prefill = typeof state?.prefill === 'string' ? state.prefill : ''
  if (!prefill) return
  inputText.value = prefill
  landingPrefilled.value = true
  // A Sessions Hub "Start task" hand-off also asks the draft to send the
  // prefill in one step; the actual flush waits for the subscription in onMounted.
  if (state?.autosend === true) pendingAutoSend.value = prefill
  try {
    window.history.replaceState({ ...window.history.state, prefill: undefined, autosend: undefined }, '')
  } catch { /* ignore */ }
}

// Reset to a clean draft for the agent requested by the draft route. The
// provisional key stays out of the URL and storage until the first send.
function enterDraft() {
  landingPrefilled.value = false
  const agentId = draftAgentId()
  const isFreshDraft = pendingSessionIntent.value === 'new_chat'
    && messages.value.length === 0
    && !isStreaming.value
    && agentIdFromSessionKey(sessionKey.value) === agentId
  if (!isFreshDraft) startDraftSession(agentId)
  consumeDraftPrefill()
  if (isDesktopViewport.value) composerRef.value?.focusTextarea()
}

onMounted(async () => {
  chatViewDisposed = false
  // Initialize session key. Without an explicit ?session= the view opens as a
  // draft instead of restoring a previous session.
  const initialSession = resolveInitialSession()
  sessionKey.value = initialSession.sessionKey
  if (initialSession.draft) {
    pendingSessionIntent.value = 'new_chat'
    if (!isDraftRoute() || hasLegacyNewChatQuery()) goToDraft({ replace: true })
    consumeDraftPrefill()
  } else {
    persistSession(sessionKey.value, { updateRoute: false, source: 'chatView.initialSession' })
  }

  // Load elevated mode
  loadElevatedMode()

  // Register event handlers before sessions.messages.subscribe can replay
  // buffered events. Session hydration and unrelated feature configuration
  // then start together, so a slow config/model RPC cannot hold long history
  // behind it.
  unsubs.push(chatRpcSubscriptions.subscribe())
  unsubs.push(chatApprovals.subscribe())
  unsubs.push(metaRuns.subscribe())
  const sessionSubscription = subscribeSession()
  if (!initialSession.draft) loadHistory()
  void loadFeatureToggles().then(() => {
    if (!chatViewDisposed) unsubs.push(bindFeatureRefresh(scheduleHistorySync))
  })
  loadSlashCommands()

  // Composer resize observer
  const composerEl = composerRef.value?.composerElement()
  if (composerEl) {
    composerResizeObserver = new ResizeObserver(() => {
      const h = composerRef.value?.composerElement()?.getBoundingClientRect().height || 0
      document.documentElement.style.setProperty('--composer-h', h + 'px')
    })
    composerResizeObserver.observe(composerEl)
  }

  // Focus textarea on desktop
  if (isDesktopViewport.value) {
    composerRef.value?.focusTextarea()
  }

  // Sessions Hub "Start task" hand-off: send the prefilled draft in one step.
  // Wait for the subscription first so the first turn streams into this view
  // rather than being missed before sessions.messages.subscribe registers.
  if (pendingAutoSend.value) {
    const text = pendingAutoSend.value
    pendingAutoSend.value = ''
    await sessionSubscription
    sendComposerText(text)
  }
})

onUnmounted(() => {
  chatViewDisposed = true
  unsubs.forEach(fn => fn())
  unsubs = []
  cleanupPendingQueue()
  cleanupHistory()
  cleanupStream()
  cleanupCompaction()
  cleanupVoiceInput()
  chatApprovals.cleanup()
  metaRuns.cleanup()
  if (composerResizeObserver) { composerResizeObserver.disconnect(); composerResizeObserver = null }
  document.documentElement.style.removeProperty('--composer-h')
  // Drop any live share-preview object URL so the blob can be reclaimed.
  if (sharePreview.value) {
    URL.revokeObjectURL(sharePreview.value.url)
    sharePreview.value = null
  }
  unsubscribeSession()
})

useDocumentEvent('paste', onDocumentPaste)
useDocumentEvent('keydown', onDocumentKeydown)

// Watch for route changes
watch(() => route.query.session, (newSession) => {
  if (newSession && typeof newSession === 'string') {
    recordSessionNavigationDiag('route.query.session', {
      from: sessionKey.value,
      to: newSession,
      routeSession: newSession,
    })
    switchToSession(newSession)
  }
})

// Entering the draft route resets to a clean draft for the requested agent.
watch(() => [route.path, route.query.agent], () => {
  if (isDraftRoute()) enterDraft()
})

// Legacy ?newChat=1 / ?new=1 links land on the draft route, then the params disappear.
watch(() => [route.query.newChat, route.query.new], () => {
  if (hasLegacyNewChatQuery()) goToDraft({ replace: true })
})

// A draft materializes its session key in the URL only when the first message
// actually goes out.
watch(pendingSessionIntent, (intent, previous) => {
  if (previous !== 'new_chat' || intent !== null) return
  if (!isDraftRoute()) return
  persistSession(sessionKey.value, { source: 'chatView.draftMaterialized' })
})

watch(sessionKey, () => {
  pendingForkBeforeMessageId.value = null
  if (shareMode.value) endShareMode()
  deliverablesOpen.value = false
  metaRunsHistoryOpen.value = false
})

watch(shareableMessageCount, (count) => {
  if (count === 0 && shareMode.value) endShareMode()
})

// Router-led turns hold the live answer/activity reveal back for [MIN,MAX] ms,
// then mount a block of content at once. Re-pin the thread on that reveal so it
// lands at the bottom instead of below the fold.
watch(answerRevealOpen, (open) => {
  if (open && autoScroll.value) scrollToBottom()
})

// An approval/clarify interrupt is a user-blocking control, not answer content,
// so it must not sit behind the router-lead reveal window. With the fold
// authoritative (default), the gated activity is the only interrupt surface,
// so reveal immediately when a live interrupt part appears — otherwise the card
// can stay invisible for up to the MAX backstop when no router decision lands.
watch(() => liveInterruptParts.value.length, (n, prev) => {
  if (n > (prev ?? 0)) revealNow()
})
</script>

<style scoped src="../styles/chat-view.css"></style>

<style scoped>
/* No shared sr-only utility exists in this repo (each component scopes its
   own), so these persistent announcers clip out here: zero visual footprint,
   still exposed to assistive tech. The session announcer intentionally sits
   outside the aria-busy conversation region. */
.chat-turn-settled-announcer,
.chat-session-load-announcer {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
