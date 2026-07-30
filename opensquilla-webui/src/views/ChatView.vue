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
        :deliverable-count="headerDeliverableCount"
        :share-mode="shareMode"
        :shareable-message-count="shareableMessageCount"
        @open-deliverables="openDeliverables"
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
          :aria-busy="isStreaming"
          @scroll="onThreadScroll"
        >
        <template v-if="isNewChatLanding">
          <div class="chat-landing-brand" :aria-label="t('chat.newChatBrand')">
            <EmptyStateChips
              :key="landingAgentId"
              :agent-id="landingAgentId"
              :suppressed="landingSuggestionsHidden"
              :disabled="landingSuggestionsDisabled"
              @pick="applyLandingSuggestion"
            />
          </div>
        </template>
        <ChatSessionRecoveryStatus
          v-if="visibleHistoryRecoveryState"
          :key="`${sessionKey}:history`"
          :state="visibleHistoryRecoveryState"
          @retry="retryHistory"
        />
        <ChatSessionRecoveryStatus
          v-if="liveRecoveryState"
          :key="`${sessionKey}:live`"
          :state="liveRecoveryState"
          @retry="retryLive"
        />
        <div
          v-if="showConfirmedEmptySession"
          class="chat-empty"
        >
          {{ t('chat.noMessagesYet') }}
        </div>
        <HistoryLoadSentinel
          v-if="!isNewChatLanding"
          :scroll-container="threadRef"
          :has-more="historyState.hasMore"
          :loading="historyState.loadingEarlier"
          :blocked="historyState.loading"
          :error="historyState.loadEarlierError"
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
          :workbench-enabled="workbenchEnabled"
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
          :plan-action-pending="planCardPendingAction"
          :plan-actions-disabled="planActionsDisabled"
          @fork-conversation="forkConversation"
          @edit-message="editMessage"
          @regenerate-message="regenerateMessage"
          @toggle-share-message="toggleShareMessage"
          @download-artifact="downloadArtifact"
          @open-artifact="openArtifact"
          @toggle-tool-group="toggleToolGroup"
          @toggle-tool-item="toggleToolItem"
          @show-tool-result="showToolResultModal"
          @resolve-interrupt="resolveInterrupt"
          @extend-interrupt="extendInterrupt"
          @clarify-submit="submitClarify"
          @clarify-dismiss="dismissClarify"
          @resume-sandbox="resumeSandbox"
          @plan-implement-current="implementCurrentPlan"
          @plan-implement-new="implementPlanInNewTask"
          @plan-replan="beginPlanRevision"
        >
          <template #router-strip="{ message: msg }">
            <RouterFxStrip v-if="shouldRenderRouterStrip(msg)" :message="msg" />
          </template>
        </ChatMessageList>

        <PlanCard
          v-if="currentPlan && !currentPlanInHistory"
          :plan="currentPlan"
          :disabled="planActionsDisabled"
          :pending-action="planCardPendingAction"
          @implement-current="implementCurrentPlan"
          @implement-new="implementPlanInNewTask"
          @replan="beginPlanRevision"
        />

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
              :step-count="executionDockRun?.status === 'running' ? 0 : liveActivityStepCount"
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
                  liveActivityTimelineItems.length
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
              >
                <template #interrupt="{ part }">
                  <InterruptPart
                    v-if="part.resolution"
                    :part="part"
                    timeline
                    @resolve="resolveInterrupt"
                    @extend="extendInterrupt"
                    @clarify-submit="(fields, request) => submitClarify(fields, request)"
                    @clarify-dismiss="dismissClarify"
                  />
                </template>
              </AssistantActivityTimeline>
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

            <ChatArtifactList
              :artifacts="liveArtifacts"
              :navigation-artifacts="sessionArtifacts"
              :session-key="sessionKey"
              :auth-token="readAuthToken()"
              :prefer-workbench="workbenchEnabled"
              @download="downloadArtifact"
              @open="openArtifact"
            />

          </div>
        </div>

        <!-- Pending controls stay outside the collapsible activity timeline at
             the live edge. A resolution removes this card and leaves its compact
             outcome in chronological history. -->
        <InterruptPart
          v-for="part in livePendingInterruptParts"
          :key="part.key"
          :part="part"
          @resolve="resolveInterrupt"
          @extend="extendInterrupt"
          @clarify-submit="(fields, request) => submitClarify(fields, request)"
          @clarify-dismiss="dismissClarify"
        />

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
    <!-- Durable execution progress belongs to the work surface, not to the
         transcript. Keeping it immediately above the composer also lets a
         future goal driver reuse this dock across multiple turns. -->
    <Transition name="plan-run-dock">
      <div v-if="executionDockRun" class="plan-run-dock">
        <PlanRunRibbon
          :run="executionDockRun"
          :cancel-busy="planActionPending === 'cancel-run'"
          :disabled="planModeBusy || planActionPending !== null"
          @cancel="cancelActivePlanRun"
          @focus-return="focusComposerAfterPlanRun"
        />
      </div>
    </Transition>
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
        @click="completeSlashCmd(cmd)"
      >
        <span class="chat-slash-cmd">{{ cmd.cmd }}</span>
        <span class="chat-slash-desc" :title="cmd.desc">{{ cmd.desc }}</span>
      </div>
    </div>

    <PendingQueue
      :items="pendingQueue"
      :max-pending="maxPending"
      :image-blocked-message="queuedImageSendBlockedMessage"
      :steer-available="sameTurnSteerAvailable"
      @clear="clearPendingQueue"
      @edit="editPendingMessage"
      @remove="removePendingChip"
      @steer="steerPendingMessage"
    />

    <ChatComposer
      ref="composerRef"
      v-model="inputText"
      :attachments="pendingAttachments"
      :busy-send-mode="busySendMode"
      :has-send-content="composerHasSendContent"
      :is-streaming="isStreaming"
      :can-stop="canStop"
      :stop-targets-plan-run="composerStopsPlanRun"
      :is-new-landing="isNewChatLanding"
      :placeholder="composerPlaceholder"
      :send-button-title="sendButtonTitle"
      :send-blocked-message="composerSendBlockedMessage"
      :run-mode="runMode"
      :allowed-run-modes="allowedRunModes"
      :run-mode-locked="runModeLocked"
      :run-mode-lock-message="t('chat.composer.runModeLocked')"
      :model-routing-mode="modelRoutingMode"
      :model-routing-settings-busy="modelRoutingSettingsBusy"
      :coding-mode-enabled="codingModeEnabled"
      :coding-mode-settings-busy="codingModeSettingsBusy"
      :voice-busy="voiceBusy"
      :voice-recording="voiceRecording"
      :voice-ready="voiceReady"
      :project-workspace="activeWorkspace"
      :project-workspace-status="activeWorkspaceStatus"
      :project-status-message="activeProjectStatusMessage"
      :can-close-project="isDraftRoute() && pendingWorkspaceId !== null"
      :can-choose-project="rpc.canChooseProject"
      :plan-mode-available="planUiAvailable"
      :collaboration-mode="collaboration.mode"
      :plan-mode-busy="planModeBusy"
      :plan-mode-disabled="planActionPending !== null"
      :plan-mode-applies-next-turn="planModeAppliesNextTurn"
      :replan-active="replanActive"
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
      @set-coding-mode-enabled="setComposerCodingModeEnabled"
      @set-collaboration-mode="setCollaborationMode"
      @cancel-replan="cancelPlanRevision"
      @voice-input="onVoiceInput"
      @voice-setup="onVoiceSetup"
      @export-markdown="exportMarkdown"
      @send="onComposerSend"
      @stop="onComposerStop"
      @choose-project="openProjectPicker"
      @close-project="closeProjectDraft"
    />
    <ProjectWorkspacePickerDialog
      v-if="rpc.canChooseProject"
      :open="projectPickerOpen"
      :enabled="rpc.canChooseProject"
      :session-key="sessionKey"
      :initial-path="activeWorkspace?.path"
      @close="projectPickerOpen = false"
      @choose="chooseProjectPath"
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
import { useWorkbenchStore } from '@/workbench/store'
import { usePlatform } from '@/platform'
import ApprovalCard from '@/components/chat/ApprovalCard.vue'
import ActivityDisclosure from '@/components/chat/ActivityDisclosure.vue'
import AssistantActivityTimeline from '@/components/chat/AssistantActivityTimeline.vue'
import ChatArtifactList from '@/components/chat/ChatArtifactList.vue'
import ChatHeaderActions from '@/components/chat/ChatHeaderActions.vue'
import DeliverablesDrawer from '@/components/chat/DeliverablesDrawer.vue'
import ChatComposer from '@/components/chat/ChatComposer.vue'
import ProjectWorkspacePickerDialog from '@/components/ProjectWorkspacePickerDialog.vue'
import ChatMessageList from '@/components/chat/ChatMessageList.vue'
import ChatSessionRecoveryStatus from '@/components/chat/ChatSessionRecoveryStatus.vue'
import ChatStallNotice from '@/components/chat/ChatStallNotice.vue'
import ClarifyCard from '@/components/chat/ClarifyCard.vue'
import ConversationMinimap from '@/components/chat/ConversationMinimap.vue'
import EmptyStateChips from '@/components/chat/EmptyStateChips.vue'
import InterruptPart from '@/components/chat/parts/InterruptPart.vue'
import ReasoningPart from '@/components/chat/parts/ReasoningPart.vue'
import TextPart from '@/components/chat/parts/TextPart.vue'
import MetaPreflightCard from '@/components/chat/MetaPreflightCard.vue'
import MetaRibbon from '@/components/chat/MetaRibbon.vue'
import PendingQueue from '@/components/chat/PendingQueue.vue'
import PlanCard from '@/components/chat/PlanCard.vue'
import PlanRunRibbon from '@/components/chat/PlanRunRibbon.vue'
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
import { useChatRouterDecisionRuntime } from '@/composables/chat/useChatRouterDecisionRuntime'
import { useChatAnswerReveal } from '@/composables/chat/useChatAnswerReveal'
import { useChatRpcEventHandlers } from '@/composables/chat/useChatRpcEventHandlers'
import { useChatRpcSubscriptions } from '@/composables/chat/useChatRpcSubscriptions'
import { useChatSend, type ChatSendOutcome } from '@/composables/chat/useChatSend'
import { useSandboxSetupRecovery } from '@/composables/chat/useSandboxSetupRecovery'
import { useChatStallWatchdog } from '@/composables/chat/useChatStallWatchdog'
import { useArtifactImageLightbox } from '@/composables/chat/useArtifactImageLightbox'
import { useMetaRuns } from '@/composables/chat/useMetaRuns'
import { useChatPlans } from '@/composables/chat/useChatPlans'
import { runStatusLabelText as sessionRunStatusLabelText } from '@/composables/useSessions'
import { useChatSessionRoute } from '@/composables/chat/useChatSessionRoute'
import {
  useChatRunModePreference,
  type RunModePolicy,
} from '@/composables/chat/useChatRunModePreference'
import { useChatSessionBootstrap } from '@/composables/chat/useChatSessionBootstrap'
import { autoSendDraftIsUnchanged } from '@/composables/chat/sessionBootstrapContract'
import {
  acquireSessionBootstrapAdmission,
  claimSessionBootstrapAdmission,
  optionalSessionRpcCallOptions,
} from '@/composables/chat/sessionBootstrapAdmission'
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
import { useConfirm } from '@/composables/useConfirm'
import {
  useProjectWorkspaces,
  type ProjectWorkspaceItem,
} from '@/composables/useProjectWorkspaces'
import {
  createDraftProjectHydrationGuard,
  useActiveProjectWorkspace,
  type ActiveProjectWorkspaceSnapshot,
} from '@/composables/useActiveProjectWorkspace'
import { useFreshTaskDraft } from '@/composables/useFreshTaskDraft'
import type {
  Attachment,
  ChatMessage,
  ChatPendingItem,
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
  SessionMessagesSnapshotResponse,
} from '@/types/rpc'
import type { ModelRoutingMode } from '@/types/modelRouting'
import { isSandboxRunMode, type SandboxRunMode } from '@/types/sandbox'
import type { ChatPart, InterruptViewState } from '@/types/parts'
import type {
  CollaborationMode,
  PlanCardAction,
  PlanCardActionTarget,
  PlanRunSnapshot,
} from '@/types/plans'
import {
  artifactCategory,
  artifactDownloadUrl,
  isInlineMediaArtifact,
} from '@/utils/chat/artifacts'
import {
  artifactFromWorkbenchItem,
  createArtifactPreviewWorkbenchItem,
} from '@/workbench/artifactItems'
import {
  artifactUsesWorkbenchPreview,
  artifactWorkbenchPreviewKind,
} from '@/utils/workbench/artifactPreview'
import { focusArtifactInTranscript } from '@/utils/chat/artifactFocus'
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
import { shouldDisableLandingSuggestions } from '@/utils/chat/landingSuggestions'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
import {
  resolveChatHistoryRecoveryState,
  shouldShowConfirmedEmptySession,
  visibleChatHistoryRecoveryState,
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
// Setup runs before this view's/ancestor children's mounted hooks. Holding the
// admission gate here prevents global onboarding/workspace metadata calls from
// entering the serialized Gateway queue ahead of session recovery.
let releaseOptionalRpcAdmission: (() => void) | null =
  claimSessionBootstrapAdmission()
let optionalRpcAdmissionGeneration = 0
const appStore = useAppStore()
const workbenchStore = useWorkbenchStore()
const artifactImageLightbox = useArtifactImageLightbox()
const platform = usePlatform()
const router = useRouter()
const { t } = useI18n()
const { pushToast } = useToasts()
const { confirm } = useConfirm()
const projectWorkspaces = useProjectWorkspaces()
const activeProjectWorkspace = useActiveProjectWorkspace()
const draftProjectHydration = createDraftProjectHydrationGuard()
const {
  pendingWorkspaceId,
  boundWorkspaceId,
  activeWorkspace,
  status: activeWorkspaceStatus,
  sendBlockedReason: activeWorkspaceSendBlockedReason,
} = activeProjectWorkspace
const projectPickerOpen = ref(false)
let activeProjectValidationController: AbortController | null = null

function cancelActiveProjectValidation() {
  activeProjectValidationController?.abort()
  activeProjectValidationController = null
}

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
const workbenchEnabled = computed(() => appStore.features.artifactWorkbench === true)
const inputText = ref('')
const composerRevision = ref(0)
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
  runMode: globalRunMode,
  allowedRunModes,
  hydrateRunModePreference,
  setGlobalRunMode,
  applyRunModePreferenceChanged,
} = useChatRunModePreference({
  rpc,
  hydrateCallOptions: optionalSessionRpcCallOptions,
  runModePolicy: () => {
    const auth = rpc.auth as RpcAuthPayload | null
    return auth?.runModePolicy
  },
})
async function refreshRunModePreference() {
  try {
    await hydrateRunModePreference()
  } catch (cause) {
    console.warn(
      'Failed to hydrate global sandbox run mode:',
      cause instanceof Error ? cause.message : String(cause),
    )
  }
}
const activeRunModeLock = ref<SandboxRunMode | null>(null)
const runMode = computed<SandboxRunMode>(
  () => activeRunModeLock.value ?? globalRunMode.value,
)

const sandboxSetupRecovery = useSandboxSetupRecovery({
  rpc: {
    call: (method, params) =>
      rpc.call(method, params, optionalSessionRpcCallOptions),
  },
  connectionState: computed(() => rpc.state),
  runMode,
  autoRefresh: false,
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

async function refreshPostBootstrapMetadata() {
  await refreshRunModePreference()
  if (!chatViewDisposed && rpc.state === 'connected') {
    await sandboxSetupRecovery.refresh()
  }
}

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
let restoreLiveTurnSnapshot = (_snapshot: SessionMessagesSnapshotResponse) => {}

// Pending session intent
const pendingSessionIntent = ref<string | null>(null)
const pendingForkBeforeMessageId = ref<string | null>(null)
const freshTaskDraft = useFreshTaskDraft()

function activeSnapshot(workspace: ProjectWorkspaceItem): ActiveProjectWorkspaceSnapshot {
  return {
    id: workspace.id,
    name: workspace.name,
    path: workspace.path,
    available: workspace.available,
    removed: false,
    ...(workspace.availabilityReason
      ? { availabilityReason: workspace.availabilityReason }
      : {}),
  }
}
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
  runStatus,
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
  attachmentWorkBusy,
  onFileInputChange,
  addAttachments,
  removeAttachment,
  retryAttachment,
  hasPendingAttachmentWork,
  prepareAttachmentsForSend,
} = chatAttachments
watch(
  [inputText, pendingAttachments],
  () => {
    composerRevision.value += 1
  },
  { deep: true, flush: 'sync' },
)

let sendCurrentInput: () => void = () => {}
let sendAutomaticInput: () => void = () => {}
// Late-bound: dispatchHiddenSend is created below (useChatSend) but the /meta
// slash handler (useChatSlashCommands, created earlier) needs it at call time.
let dispatchHiddenForMeta: (providerText: string, displayText: string) => void = () => {}
let dispatchPlanComposerPrompt: (prompt: string, composerText: string) => void = () => {}
let isCompactInFlightForCurrentSession: () => boolean = () => false
let isQueuedDeliveryBlocked: () => boolean = () => false
let isLiveDeliveryBlocked: () => boolean = () => true
let dispatchHiddenControl: (
  providerText: string,
  displayText: string,
  ownerSessionKey?: string,
  queuedItem?: ChatPendingItem,
) => Promise<ChatSendOutcome> = async () => 'not_sent'
let dispatchQueuedItem: (
  item: ChatPendingItem,
  ownerSessionKey?: string,
) => Promise<ChatSendOutcome> = async () => 'not_sent'
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
    || isQueuedDeliveryBlocked()
    || isLiveDeliveryBlocked()
    || ['resolving', 'unavailable', 'removed', 'error'].includes(
      activeWorkspaceStatus.value,
    )
    || hasPendingAttachmentWork()
    || pendingQueueOwnerContext.value?.sessionKey === sessionKey.value
  ),
  autoResizeTextarea,
  sendCurrentInput: () => sendCurrentInput(),
  resetInputHistory: () => resetComposerInputHistory(),
  hasComposer: () => Boolean(composerRef.value),
  dispatchHiddenControl: (item, ownerSessionKey) =>
    dispatchHiddenControl(
      item.text,
      item.displayTextOverride || '',
      ownerSessionKey,
      item,
    ),
  dispatchPendingItem: (item, ownerSessionKey) =>
    dispatchQueuedItem(item, ownerSessionKey),
})
const {
  pendingQueue,
  canQueueMore,
  busySendMode,
  maxPending,
  enqueuePendingPayload,
  enqueuePendingInput,
  enqueueHiddenControl,
  enqueuePendingSteerRetry,
  removePendingChip,
  beginPendingDelivery,
  settlePendingDelivery,
  clearPendingQueue,
  switchPendingQueue,
  adoptPendingQueue,
  popPendingTail,
  popAllPendingIntoComposer,
  schedulePendingDrainAfterTerminal,
  flushDeferredPendingDrain,
  cleanup: cleanupPendingQueue,
} = chatPendingQueue
watch(attachmentWorkBusy, (busy) => {
  if (!busy) flushDeferredPendingDrain()
})

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
  readCallOptions: optionalSessionRpcCallOptions,
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
  readCallOptions: optionalSessionRpcCallOptions,
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
  bindFeatureRefresh,
} = chatFeatureToggles
isQueuedDeliveryBlocked = () => (
  modelRoutingSettingsBusy.value
  && hasSendableModelInputImageAttachment(pendingQueue.value[0]?.attachments || [])
)
watch(
  [modelRoutingMode, modelRoutingSettingsBusy],
  ([mode, busy], [previousMode, wasBusy]) => {
    const routingUnblocked = (
      (previousMode === 'llm_ensemble' && mode !== 'llm_ensemble')
      || (wasBusy && !busy)
    )
    if (!routingUnblocked || pendingQueue.value.length === 0) return
    schedulePendingDrainAfterTerminal()
    flushDeferredPendingDrain()
  },
)

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
  readProjectFromUrl,
  resolveInitialSession,
} = chatSessionRoute

let switchToPlanSession: (key: string) => void | Promise<unknown> = () => {}
let planMutationAccepted: () => void = () => {}
const chatPlans = useChatPlans({
  rpc,
  sessionKey,
  currentEpoch,
  isStreaming,
  inputText,
  createSessionKey,
  agentId: () => agentIdFromSessionKey(sessionKey.value),
  switchToSession: key => switchToPlanSession(key),
  focusComposer: () => composerRef.value?.focusTextarea(),
  notifyError: message => pushToast(
    t('chat.plan.actionFailed', { error: message }),
    { tone: 'danger', duration: 8000 },
  ),
  onMutationAccepted: () => planMutationAccepted(),
  isDraft: () => isDraftRoute() && pendingSessionIntent.value === 'new_chat',
})
const {
  collaboration,
  initialCollaborationMode,
  currentPlan,
  currentPlanRevisionId,
  activePlanRun,
  modeBusy: planModeBusy,
  modeAppliesNextTurn: planModeAppliesNextTurn,
  pendingAction: planActionPending,
  replanTarget,
  replanActive,
} = chatPlans

const renderSourceMessages = computed(() => messages.value)
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
  currentPlanRevisionId,
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
  const liveTurnKey = [...rendered]
    .reverse()
    .find(message => message.displayRole === 'user')
    ?.turnKey
  for (let i = rendered.length - 1; i >= 0; i--) {
    const msg = rendered[i]
    if (msg.isRouterStrip && (!liveTurnKey || msg.turnKey === liveTurnKey)) return null
    if (msg.displayRole === 'user' && msg.turnKey !== liveTurnKey) break
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
  retryHistory: retryHistoryRequest,
  scheduleHistorySync,
  cancelAnchorStabilization,
  cancelActiveHistory,
  cleanup: cleanupHistory,
} = chatHistory
planMutationAccepted = () => scheduleHistorySync()

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
const voiceCapability = useRpcCall<{ audioConfigured?: boolean }>(
  'onboarding.status',
  undefined,
  { callOptions: optionalSessionRpcCallOptions },
)
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
  canDeliver: () => !composerSendBlockedMessage.value,
  notifyDeliveryBlocked: () => {
    if (liveSendBlockedReason.value) {
      pushToast(liveSendBlockedReason.value, { tone: 'info' })
    }
  },
  notifyMessagePending: () => pushToast(t('chat.toast.messageStillSaving'), { tone: 'info' }),
})
const {
  copyMessage,
  regenerateMessage,
  editMessage,
} = chatMessageActions

let applyPendingUserInputSnapshot: typeof chatPlans.applyBootstrap = () => {}
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
  onLiveSnapshot: snapshot => restoreLiveTurnSnapshot(snapshot),
  onAuthoritativeIdle: () => {
    if (pendingQueueOwnerContext.value?.sessionKey !== sessionKey.value) {
      activeRunModeLock.value = null
    }
    const taskId = activeStreamTaskId.value
    if (
      taskId
      && taskId !== PENDING_STREAM_TASK_ID
      && taskId !== STOPPED_STREAM_TASK_ID
    ) {
      schedulePendingDrainAfterTerminal()
    }
  },
  onRunModeLock: lock => {
    if (lock.locked === false) return
    if (isSandboxRunMode(lock.runMode)) {
      activeRunModeLock.value = lock.runMode
    } else if (activeRunModeLock.value === null) {
      activeRunModeLock.value = globalRunMode.value
    }
  },
  beginSessionMetadataResolution: key =>
    pendingSessionIntent.value === 'new_chat'
      ? -1
      : activeProjectWorkspace.beginSessionResolution(key),
  onSessionMetadata: (key, generation, metadata) => {
    if (generation < 0) return
    activeProjectWorkspace.applySessionSnapshot(key, generation, metadata)
  },
  onSessionMetadataError: (key, generation) => {
    if (generation < 0) return
    activeProjectWorkspace.failSessionResolution(key, generation)
  },
  onSnapshot: snapshot => {
    chatPlans.applyBootstrap(snapshot)
    applyPendingUserInputSnapshot(snapshot)
  },
})
const {
  subscribeSession,
  retrySessionMetadata,
  unsubscribeSession,
  cancelActiveSubscription,
} = chatSessionSubscription
applySessionRunState = chatSessionSubscription.applySessionRunState

const chatSessionBootstrap = useChatSessionBootstrap({
  sessionKey,
  loadHistory: async (context, retry) => (
    retry
      ? await retryHistoryRequest(context)
      : await loadHistory({}, context)
  ),
  subscribeSession,
  cancelHistory: cancelActiveHistory,
  cancelSubscription: cancelActiveSubscription,
  unsubscribeSession,
})
const {
  livePhase,
  startSessionBootstrap: startSessionBootstrapCoordinator,
  cancelSessionBootstrap: cancelSessionBootstrapCoordinator,
  retryHistory: retryHistoryCoordinator,
  retryLive: retryLiveCoordinator,
  handleConnectionState: handleSessionConnectionStateCoordinator,
  isSessionBootstrapCurrent,
} = chatSessionBootstrap

function holdOptionalRpcAdmission() {
  if (!releaseOptionalRpcAdmission) {
    releaseOptionalRpcAdmission = acquireSessionBootstrapAdmission()
  }
  return ++optionalRpcAdmissionGeneration
}

function releaseOptionalRpcAdmissionAfter(
  promises: readonly Promise<unknown>[],
  admissionGeneration: number,
) {
  void Promise.allSettled(promises).then(() => {
    if (admissionGeneration !== optionalRpcAdmissionGeneration) return
    releaseOptionalRpcAdmission?.()
    releaseOptionalRpcAdmission = null
  })
}

function trackSessionBootstrapAdmission<T extends {
  criticalRequestsQueued: Promise<void>
}>(run: T): T {
  const admissionGeneration = holdOptionalRpcAdmission()
  releaseOptionalRpcAdmissionAfter(
    [run.criticalRequestsQueued],
    admissionGeneration,
  )
  return run
}

let postBootstrapMetadataStarted = false
function schedulePostBootstrapMetadata(
  run: {
    generation: number
    criticalRequestsQueued: Promise<void>
  },
  key: string,
) {
  if (postBootstrapMetadataStarted) return
  void run.criticalRequestsQueued.then(() => {
    if (
      postBootstrapMetadataStarted
      || chatViewDisposed
      || sessionKey.value !== key
      || !isSessionBootstrapCurrent(run.generation, key)
    ) return
    postBootstrapMetadataStarted = true
    void refreshPostBootstrapMetadata()
    void loadFeatureToggles().then(() => {
      if (!chatViewDisposed) unsubs.push(bindFeatureRefresh(scheduleHistorySync))
    })
    loadSlashCommands()
  })
}

function startSessionBootstrap(options?: {
  includeHistory?: boolean
  force?: boolean
}) {
  const key = sessionKey.value
  const run = trackSessionBootstrapAdmission(
    startSessionBootstrapCoordinator(options),
  )
  schedulePostBootstrapMetadata(run, key)
  return run
}

function retryHistory() {
  return retryHistoryCoordinator()
}

function retryLive() {
  return retryLiveCoordinator()
}

function cancelSessionBootstrap() {
  optionalRpcAdmissionGeneration += 1
  cancelSessionBootstrapCoordinator()
}

function handleSessionConnectionState(
  state: string,
  includeHistory = true,
) {
  const run = handleSessionConnectionStateCoordinator(state, includeHistory)
  if (
    run
    && (historyState.value.initialLoadStatus === 'loading'
      || livePhase.value === 'connecting')
  ) {
    return trackSessionBootstrapAdmission(run)
  }
  return run
}

const isSessionHydrating = computed(() => livePhase.value === 'connecting')
const liveSendBlockedReason = computed<string | null>(() => {
  if (!sessionKey.value || livePhase.value === 'ready') return null
  return t(
    livePhase.value === 'degraded'
      ? 'chat.liveSendBlockedDegraded'
      : 'chat.liveSendBlockedConnecting',
  )
})
isLiveDeliveryBlocked = () => Boolean(liveSendBlockedReason.value)
watch(livePhase, (phase, previousPhase) => {
  if (
    phase !== 'ready'
    || previousPhase === 'ready'
    || pendingQueue.value.length === 0
  ) return
  schedulePendingDrainAfterTerminal()
  flushDeferredPendingDrain()
})
watch(activeWorkspaceStatus, (status, previousStatus) => {
  if (
    status !== 'ready'
    || previousStatus === 'ready'
    || pendingQueue.value.length === 0
  ) return
  schedulePendingDrainAfterTerminal()
  flushDeferredPendingDrain()
})

const sessionHasActiveWork = computed(() => (
  isStreaming.value
  || activeTaskGroups.value.size > 0
  || ['queued', 'running', 'approval_pending'].includes(runStatus.value.status)
  || activePlanRun.value?.status === 'queued'
  || activePlanRun.value?.status === 'running'
  || pendingQueueOwnerContext.value?.sessionKey === sessionKey.value
))
const canStop = computed(() => !isSessionHydrating.value && sessionHasActiveWork.value)
const runModeLocked = computed(
  () => isSessionHydrating.value
    || sessionHasActiveWork.value
    || activeRunModeLock.value !== null,
)

watch(sessionHasActiveWork, active => {
  if (active && activeRunModeLock.value === null) {
    activeRunModeLock.value = globalRunMode.value
  } else if (!active && !isSessionHydrating.value) {
    activeRunModeLock.value = null
  }
}, { flush: 'sync' })

watch(sessionKey, () => {
  activeRunModeLock.value = null
})

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
  cancelSessionBootstrap: () => {
    // Retire draft-project work on the old socket before the next session's
    // coordinator can start, so its abort/reconnect cannot tear down B.
    draftProjectHydration.invalidate()
    cancelActiveProjectValidation()
    cancelSessionBootstrap()
  },
  startSessionBootstrap,
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
  resetDraftComposer: () => {
    inputText.value = ''
    pendingAttachments.value = []
    resetComposerInputHistory()
    autoResizeTextarea()
  },
})
const {
  resetCurrentSessionAfterSlash,
  startDraftSession,
  switchToSession: switchRuntimeToSession,
  adoptResponseSession,
} = chatSessionRuntime
switchToPlanSession = switchToSession

function switchToSession(nextSessionKey: string) {
  if (nextSessionKey !== sessionKey.value) {
    activeProjectWorkspace.beginSessionResolution(nextSessionKey)
  }
  return switchRuntimeToSession(nextSessionKey)
}

const chatSlashCommands = useChatSlashCommands({
  rpc,
  catalogCallOptions: optionalSessionRpcCallOptions,
  inputText,
  sessionKey,
  autoResizeTextarea,
  newSession: () => {
    freshTaskDraft.requestFreshTask('main')
    goToDraft({ agentId: 'main' })
  },
  resetCurrentSession: () => {
    resetCurrentSessionAfterSlash()
    chatPlans.reset()
  },
  setCompactInFlight,
  showCompactStatus,
  notify: (message: string) => pushToast(message, { duration: 6000 }),
  dispatchHidden: (providerText: string, displayText: string) => dispatchHiddenForMeta(providerText, displayText),
  dispatchPlanPrompt: (prompt: string, composerText: string) => {
    dispatchPlanComposerPrompt(prompt, composerText)
  },
  activatePlanMode: () => chatPlans.setMode('plan'),
  planModeAvailable: () => planUiAvailable.value,
  codingModeEnabled,
  setCodingModeEnabled,
})
const {
  slashOpen,
  slashIdx,
  filteredSlashCmds,
  loadSlashCommands,
  handleSlashInput,
  closeSlashMenu,
  completeSlashCmd,
  activateSlashCmd,
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
  completeSlashCmd,
  activateSlashCmd,
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
  supportsMethod: method => rpc.supportsMethod(method),
  activeSteerCapability: computed(() => {
    const task = runStatus.value.task
    return task?.steer_capability || task?.steerCapability || null
  }),
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
  composerRevision,
  pendingSessionIntent,
  pendingWorkspaceId,
  sendBlockedReason: liveSendBlockedReason,
  validateActiveProjectBeforeSend,
  acceptPendingWorkspaceBinding: activeProjectWorkspace.acceptPendingBinding,
  initialCollaborationMode,
  pendingForkBeforeMessageId,
  materializeDraftSession: key => {
    if (!isDraftRoute()) return
    const workspaceId = pendingWorkspaceId.value
    if (workspaceId) {
      freshTaskDraft.bindMaterializedProjectTask(key, workspaceId)
    }
    persistSession(key, { source: 'chatView.draftAccepted' })
  },
  aborted,
  activeStreamTaskId,
  activeStreamSessionKey,
  autoScroll,
  stream: chatStream,
  canStop: () => canStop.value,
  normalizeElevatedMode,
  adoptResponseSession: async (key, ownerRequestId) => {
    const sourceKey = sessionKey.value
    const workspaceId = freshTaskDraft.materializedWorkspaceBySession.value[sourceKey]
      || boundWorkspaceId.value
    if (workspaceId && key !== sourceKey) {
      freshTaskDraft.bindMaterializedProjectTask(key, workspaceId)
      freshTaskDraft.forgetMaterializedProjectTask(sourceKey)
    }
    return adoptResponseSession(key, ownerRequestId)
  },
  scheduleHistorySync,
  schedulePendingDrainAfterTerminal,
  flushDeferredPendingDrain,
  bindActiveStreamTask: taskId => bindActiveStreamTask(taskId),
  isCompactInFlightForCurrentSession,
  hasPendingAttachmentWork,
  prepareAttachmentsForSend,
  enqueuePendingInput,
  enqueuePendingPayload,
  enqueueHiddenControl,
  enqueuePendingSteerRetry,
  restoreSteerIntoComposer: text => appendComposerText(text),
  popAllPendingIntoComposer,
  executeSlashCommand,
  closeSlashMenu,
  autoResizeTextarea,
  scrollToBottom,
})
const {
  onSend,
  onStop,
  sendQueuedSteer,
  sendQueuedFollowup,
  dispatchComposerPrompt,
  dispatchHiddenSend,
  sendHiddenMetaPreflightConfirmation,
} = chatSend
const sameTurnSteerAvailable = computed(() => (
  isStreaming.value
  && chatSend.supportsSameTurnSteer()
))
const composerSameTurnSteerAvailable = computed(() => (
  sameTurnSteerAvailable.value
  && pendingAttachments.value.length === 0
  && !pendingSessionIntent.value
  && !pendingForkBeforeMessageId.value
))
watch(composerSameTurnSteerAvailable, (available) => {
  if (!available && busySendMode.value === 'steer') {
    busySendMode.value = 'queue'
  }
})

async function onComposerSend() {
  // All composer submission modes, including keyboard-driven plan revision,
  // share the same fail-closed delivery gate.
  if (composerSendBlockedMessage.value) return
  // Serialize an existing-session mode mutation before accepting another
  // composer turn, so the send cannot race the collaboration CAS update.
  if (planModeBusy.value) return
  const target = replanTarget.value
  if (!target) {
    onSend()
    return
  }
  const prompt = inputText.value.trim()
  if (!prompt) return
  const submittedRevision = composerRevision.value
  const accepted = await chatPlans.revise({ ...target, prompt })
  if (!accepted) return
  if (composerRevision.value === submittedRevision) {
    inputText.value = ''
    autoResizeTextarea()
  }
}

sendCurrentInput = onComposerSend
sendAutomaticInput = () => {
  void onSend({ cancelIfComposerChanged: true })
}
dispatchHiddenForMeta = dispatchHiddenSend
dispatchPlanComposerPrompt = (prompt, composerText) => {
  void dispatchComposerPrompt(prompt, composerText)
}
dispatchHiddenControl = dispatchHiddenSend
dispatchQueuedItem = sendQueuedFollowup

function takeVisiblePendingItem(index: number) {
  const item = pendingQueue.value[index]
  if (!item || item.hiddenControl || item.deliveryState) return null
  pendingQueue.value.splice(index, 1)
  return item
}

function editPendingMessage(index: number) {
  const item = takeVisiblePendingItem(index)
  if (!item) return
  inputText.value = [item.text, inputText.value].filter(text => text.trim()).join('\n')
  pendingAttachments.value = [...(item.attachments || []), ...pendingAttachments.value]
  pendingSessionIntent.value = item.intent || pendingSessionIntent.value
  autoResizeTextarea()
  nextTick(() => composerRef.value?.focusTextarea())
}

async function steerPendingMessage(index: number) {
  const candidate = pendingQueue.value[index]
  const item = beginPendingDelivery(index, candidate?.hiddenControl === true)
  if (!item) return

  let outcome: ChatSendOutcome = 'retryable_failure'
  try {
    outcome = item.hiddenControl
      ? await dispatchHiddenSend(
          item.text,
          item.displayTextOverride || '',
          item.ownerSessionKey || sessionKey.value,
          item,
        )
      : await sendQueuedSteer(item)
  } finally {
    settlePendingDelivery(item, outcome)
  }
}

// Deny notes are immutable queue payloads. They never borrow or clear the
// operator's editable composer, even if project validation or live recovery
// delays delivery.
function queueDenyFeedback(note: string) {
  const context = pendingQueueOwnerContext.value
  const owner = context?.sessionKey === sessionKey.value
    ? { ownerRequestId: context.ownerRequestId }
    : undefined
  const queued = enqueuePendingPayload({
    text: note,
    attachments: [],
    intent: null,
  }, owner)
  if (!queued) {
    inputText.value = [note, inputText.value].filter(text => text.trim()).join('\n')
    autoResizeTextarea()
    pushToast(t('chat.toast.queueFull'), { tone: 'info' })
    return
  }
  if (!isStreaming.value && !isCompactInFlightForCurrentSession()) {
    schedulePendingDrainAfterTerminal()
    flushDeferredPendingDrain()
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
  applyUserInputBootstrap,
} = chatApprovals
applyPendingUserInputSnapshot = applyUserInputBootstrap

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
  restoreSteerIntoComposer: text => appendComposerText(text),
  saveWidgetState,
  handleSessionConnectionState: state =>
    handleSessionConnectionState(state, !isDraftRoute()),
  loadCurrentSessionUsage,
  refreshRunModePreference: refreshPostBootstrapMetadata,
})
bindActiveStreamTask = rpcEventHandlers.bindActiveStreamTask
restoreLiveTurnSnapshot = rpcEventHandlers.restoreLiveTurnSnapshot
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
const livePendingInterruptParts = computed(() =>
  liveInterruptParts.value.filter(part => !part.resolution),
)

const visiblePendingInterruptKeys = computed(() => {
  const keys = new Set(livePendingInterruptParts.value.map(part => part.key))
  for (const message of renderedMessages.value) {
    for (const part of message.parts ?? []) {
      if (part.type === 'interrupt' && !part.resolution) keys.add(part.key)
    }
  }
  return [...keys]
})

async function focusPendingApprovalCard() {
  const request = appStore.approvalFocusRequest
  if (!request || request.sessionKey !== sessionKey.value) return

  await nextTick()
  if (
    appStore.approvalFocusRequest?.requestId !== request.requestId
    || request.sessionKey !== sessionKey.value
  ) return

  const card = [...(threadRef.value?.querySelectorAll<HTMLElement>('[data-approval-id]') ?? [])]
    .find(element => element.dataset.approvalId === request.approvalId)
  if (!card) return

  card.scrollIntoView({ behavior: 'smooth', block: 'center' })
  card.focus({ preventScroll: true })
  appStore.clearApprovalFocusRequest(request.requestId)
}

watch(
  [
    () => appStore.approvalFocusRequest?.requestId ?? 0,
    sessionKey,
    () => visiblePendingInterruptKeys.value.join('\u0000'),
  ],
  () => { void focusPendingApprovalCard() },
  { flush: 'post', immediate: true },
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

// Meta retries/replays and landing suggestions must never overwrite an
// operator-owned draft. Occupied composers keep the generated prompt as an
// immutable queue item; an empty but blocked composer stages it for explicit
// retry without pretending it was sent.
function sendComposerText(text: string) {
  const next = String(text || '')
  if (!next) return
  if (inputText.value.trim() || pendingAttachments.value.length > 0) {
    const context = pendingQueueOwnerContext.value
    const owner = context?.sessionKey === sessionKey.value
      ? { ownerRequestId: context.ownerRequestId }
      : undefined
    const queued = enqueuePendingPayload({
      text: next,
      attachments: [],
      intent: null,
    }, owner)
    if (!queued) {
      pushToast(t('chat.toast.queueFull'), { tone: 'info' })
      return
    }
    if (!isStreaming.value && !isCompactInFlightForCurrentSession()) {
      schedulePendingDrainAfterTerminal()
      flushDeferredPendingDrain()
    }
    return
  }
  inputText.value = next
  autoResizeTextarea()
  if (composerSendBlockedMessage.value) {
    composerRef.value?.focusTextarea()
    return
  }
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

const historyRecoveryState = computed(() => {
  return resolveChatHistoryRecoveryState({
    isDraftLanding: isNewChatLanding.value,
    initialHistoryStatus: historyState.value.initialLoadStatus,
    retrying: historyState.value.retrying,
    recoveryError: historyState.value.recoveryError,
  })
})

const visibleHistoryRecoveryState = computed(() => (
  visibleChatHistoryRecoveryState(historyRecoveryState.value)
))

const liveRecoveryState = computed(() => {
  if (livePhase.value === 'degraded') return 'live-degraded' as const
  if (
    livePhase.value === 'connecting'
    && historyRecoveryState.value === null
  ) {
    return 'live-connecting' as const
  }
  return null
})

const showConfirmedEmptySession = computed(() => shouldShowConfirmedEmptySession({
  isDraftLanding: isNewChatLanding.value,
  isStreaming: isStreaming.value,
  messageCount: messages.value.length,
  initialHistoryStatus: historyState.value.initialLoadStatus,
}))

const composerPlaceholder = computed(() => {
  if (replanActive.value) return t('chat.plan.revisePromptPlaceholder')
  if (collaboration.value.mode === 'plan') return t('chat.planMode.placeholder')
  if (isNewChatLanding.value) return t('chat.placeholderLanding')
  return isCompactViewport.value ? t('chat.placeholderCompact') : t('chat.placeholder')
})

const hasSendContent = computed(() => {
  return inputText.value.trim().length > 0 || pendingAttachments.value.some(isSendableAttachment)
})
const composerHasSendContent = computed(() =>
  replanActive.value ? inputText.value.trim().length > 0 : hasSendContent.value,
)

// A mixed-version gateway may know plans.setMode but not the atomic first-send
// contract. Hide Plan rather than claim a read-only turn that would run Default.
const planUiAvailable = computed(() =>
  rpc.supportsMethod('plans.setMode')
  && rpc.supportsMethod('plans.capabilities'),
)
const planCardPendingAction = computed<PlanCardAction | null>(() => {
  const action = planActionPending.value
  if (action === 'revise') return 'replan'
  return action === 'implement-current' || action === 'implement-new' || action === 'replan'
    ? action
    : null
})
const planActionsDisabled = computed(() =>
  isStreaming.value
  || planModeBusy.value
  || Boolean(liveSendBlockedReason.value)
  || planActionPending.value !== null
  || activePlanRun.value?.status === 'queued'
  || activePlanRun.value?.status === 'running',
)
const PLAN_RUN_TERMINAL_HOLD_MS = 2000
const executionDockRun = ref<PlanRunSnapshot | null>(null)
const composerStopsPlanRun = computed(() =>
  executionDockRun.value?.status === 'queued'
  || executionDockRun.value?.status === 'running',
)
let executionDockHideTimer: ReturnType<typeof setTimeout> | null = null

function clearExecutionDockHideTimer() {
  if (executionDockHideTimer === null) return
  clearTimeout(executionDockHideTimer)
  executionDockHideTimer = null
}

function syncExecutionDockRun() {
  const run = activePlanRun.value
  clearExecutionDockHideTimer()
  if (!run) {
    executionDockRun.value = null
    return
  }
  if (['queued', 'running', 'paused', 'blocked'].includes(run.status)) {
    executionDockRun.value = run
    return
  }
  if (executionDockRun.value?.runId !== run.runId) {
    executionDockRun.value = null
    return
  }
  executionDockRun.value = run
  executionDockHideTimer = setTimeout(() => {
    if (executionDockRun.value?.runId === run.runId) {
      executionDockRun.value = null
    }
    executionDockHideTimer = null
  }, PLAN_RUN_TERMINAL_HOLD_MS)
}

watch(
  () => [
    activePlanRun.value?.runId,
    activePlanRun.value?.status,
    activePlanRun.value?.stateRevision,
  ],
  syncExecutionDockRun,
  { immediate: true },
)
const currentPlanInHistory = computed(() => {
  const revisionId = currentPlan.value?.revisionId
  if (!revisionId) return false
  return renderedMessages.value.some(message =>
    message.planRevisions?.some(plan => plan.revisionId === revisionId),
  )
})

const landingSuggestionsHidden = computed(() => landingPrefilled.value)
const landingSuggestionsDisabled = computed(() => shouldDisableLandingSuggestions({
  landingPrefilled: landingPrefilled.value,
  composerText: inputText.value,
  attachmentCount: pendingAttachments.value.length,
}))

const queuedImageSendBlockedMessage = computed(() => {
  if (modelRoutingSettingsBusy.value) {
    return t('chat.composer.routingUpdateImageBlocked')
  }
  return modelRoutingMode.value === 'llm_ensemble'
    ? t('chat.composer.ensembleImageUnsupported')
    : ''
})

const modelImageSendBlockedMessage = computed(() => {
  return hasSendableModelInputImageAttachment(pendingAttachments.value)
    ? queuedImageSendBlockedMessage.value
    : ''
})

const activeProjectStatusMessage = computed(() => {
  switch (activeWorkspaceStatus.value) {
    case 'resolving':
      return t('workspaces.activeProjectResolving')
    case 'unavailable':
      return t('workspaces.activeProjectUnavailable')
    case 'removed':
      return t('workspaces.activeProjectRemoved')
    case 'unknown':
    case 'error':
      return t('workspaces.activeProjectBlocksSending')
    default:
      return ''
  }
})

const activeProjectComposerBlockMessage = computed(() => {
  switch (activeWorkspaceStatus.value) {
    case 'resolving':
    case 'unavailable':
    case 'removed':
      return activeProjectStatusMessage.value
    default:
      return ''
  }
})

const composerSendBlockedMessage = computed(() =>
  modelImageSendBlockedMessage.value
  || liveSendBlockedReason.value
  || activeProjectComposerBlockMessage.value,
)

const sendButtonTitle = computed(() => {
  if (replanActive.value) return t('chat.plan.reviseSend')
  if (composerSendBlockedMessage.value) return composerSendBlockedMessage.value
  if (isCompactInFlightForCurrentSession()) return t('chat.sendQueuesUntilCompaction')
  if (isStreaming.value) {
    return busySendMode.value === 'steer' && composerSameTurnSteerAvailable.value
      ? t('chat.sendSteers')
      : t('chat.sendQueues')
  }
  return t('chat.send')
})

function implementCurrentPlan(target: PlanCardActionTarget) {
  if (liveSendBlockedReason.value) return
  void chatPlans.implement(target, false)
}

function implementPlanInNewTask(target: PlanCardActionTarget) {
  if (liveSendBlockedReason.value) return
  void chatPlans.implement(target, true)
}

function beginPlanRevision(target: PlanCardActionTarget) {
  if (pendingAttachments.value.length > 0) {
    pushToast(t('chat.plan.attachmentsUnavailable'), { tone: 'warn' })
    return
  }
  chatPlans.beginReplan(target)
}

function cancelPlanRevision() {
  chatPlans.cancelReplan()
}

function cancelActivePlanRun() {
  void chatPlans.cancelRun()
}

function focusComposerAfterPlanRun() {
  composerRef.value?.focusTextarea()
}

function onComposerStop() {
  const run = executionDockRun.value
  if (run && (run.status === 'queued' || run.status === 'running')) {
    void chatPlans.cancelRun()
    return
  }
  onStop()
}

function setCollaborationMode(mode: CollaborationMode) {
  void chatPlans.setMode(mode)
}

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

async function setComposerRunMode(mode: SandboxRunMode) {
  if (runModeLocked.value) return
  try {
    await setGlobalRunMode(mode)
    void sandboxSetupRecovery.refresh()
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause)
    console.warn('Failed to persist sandbox run mode:', detail)
    pushToast(detail, { tone: 'danger' })
  }
}

async function setComposerModelRoutingMode(mode: ModelRoutingMode) {
  await setModelRoutingMode(mode)
  scheduleHistorySync()
}

async function setComposerCodingModeEnabled(enabled: boolean) {
  const updated = await setCodingModeEnabled(enabled)
  pushToast(t(
    updated
      ? (enabled ? 'chat.codingMode.enabled' : 'chat.codingMode.disabled')
      : 'chat.codingMode.updateFailed',
  ))
}

// A suggestion chip is an explicit task choice. Route it through the same
// composer-backed send path as every other message so routing, attachments,
// optimistic state, and recovery behavior stay identical.
function applyLandingSuggestion(text: string) {
  if (landingSuggestionsDisabled.value) return
  sendComposerText(text)
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

const sessionWorkbenchArtifacts = computed(() =>
  sessionArtifacts.value.filter(artifactUsesWorkbenchPreview),
)

const headerDeliverableCount = computed(() =>
  workbenchEnabled.value
    ? sessionWorkbenchArtifacts.value.length
    : sessionArtifacts.value.length,
)

const deliverablesOpen = ref(false)

function focusHeaderAction(
  action: 'deliverables' | 'share' | 'copy-session-key',
) {
  void nextTick(() => chatHeaderActionsRef.value?.focusAction(action))
}

function openDeliverables() {
  if (sessionArtifacts.value.length === 0) return
  if (workbenchEnabled.value) {
    const recentPreview = workbenchStore.findMostRecentItem(item => {
      if (
        item.kind !== 'artifact-preview'
        || item.scope.type !== 'session'
        || item.scope.id !== sessionKey.value
      ) return false
      const artifact = artifactFromWorkbenchItem(item)
      if (!artifact) return false
      return artifactUsesWorkbenchPreview(artifact)
    })
    if (recentPreview) {
      workbenchStore.activateItem(recentPreview.id)
      workbenchStore.setExpanded(true)
      return
    }

    for (let index = sessionWorkbenchArtifacts.value.length - 1; index >= 0; index -= 1) {
      const artifact = sessionWorkbenchArtifacts.value[index]
      if (!artifact) continue
      openArtifact(artifact)
      return
    }

    const latestArtifact = sessionArtifacts.value[sessionArtifacts.value.length - 1]
    if (latestArtifact && artifactCategory(latestArtifact) === 'visual') {
      openArtifact(latestArtifact)
      return
    }
    if (latestArtifact) focusInlineDeliverable(latestArtifact)
    return
  }
  deliverablesOpen.value = true
}

function focusInlineDeliverable(artifact: ArtifactPayload): boolean {
  const reduceMotion = typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  return focusArtifactInTranscript(
    threadRef.value,
    artifact,
    reduceMotion ? 'auto' : 'smooth',
  )
}

watch(sessionArtifacts, artifacts => {
  if (!sessionKey.value) return
  artifactImageLightbox.updateNavigation(artifacts, sessionKey.value)
  if (!workbenchEnabled.value) return
  for (const item of workbenchStore.items) {
    if (
      item.kind !== 'artifact-preview'
      || item.scope.type !== 'session'
      || item.scope.id !== sessionKey.value
    ) continue
    const artifact = artifactFromWorkbenchItem(item)
    if (!artifact) continue
    workbenchStore.updateItem(createArtifactPreviewWorkbenchItem({
      artifact,
      navigationArtifacts: artifacts,
      nativeHtml: item.hostKind === 'native-webcontents',
      sessionKey: sessionKey.value,
    }))
  }
})

function openArtifact(artifact: ArtifactPayload): boolean {
  if (
    isInlineMediaArtifact(artifact)
    || artifactWorkbenchPreviewKind(artifact) === 'unsupported'
  ) {
    return focusInlineDeliverable(artifact)
  }
  if (artifactCategory(artifact) === 'visual' && sessionKey.value) {
    artifactImageLightbox.open({
      artifact,
      navigationArtifacts: sessionArtifacts.value,
      sessionKey: sessionKey.value,
    })
    return true
  }
  if (!workbenchEnabled.value || !sessionKey.value) return false
  const opened = workbenchStore.openItem(createArtifactPreviewWorkbenchItem({
    artifact,
    navigationArtifacts: sessionArtifacts.value,
    nativeHtml: Boolean(
      platform.capabilities.hasNativeWorkbenchSurfaces
      && platform.workbench.native,
    ),
    sessionKey: sessionKey.value,
  }))
  if (!opened) {
    pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
  }
  return opened
}

function closeDeliverables() {
  deliverablesOpen.value = false
  focusHeaderAction('deliverables')
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
  if (replanActive.value) return
  threadDragDepth.value += 1
  threadDragOver.value = true
}

function onChatDragOver(e: DragEvent) {
  if (!dragEventHasFiles(e)) return
  e.preventDefault()
  if (replanActive.value) {
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'none'
    return
  }
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
  if (replanActive.value) {
    pushToast(t('chat.plan.attachmentsUnavailable'), { tone: 'warn' })
    return
  }
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
  if (replanActive.value) {
    e.preventDefault()
    pushToast(t('chat.plan.attachmentsUnavailable'), { tone: 'warn' })
    return
  }
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
    onComposerStop()
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

async function chooseProjectPath(path: string) {
  projectPickerOpen.value = false
  if (!rpc.canChooseProject) return
  const trusted = await confirm({
    title: t('workspaces.trustTitle'),
    body: t('workspaces.trustBody', { path }),
    primaryLabel: t('workspaces.trustConfirm'),
    primaryClass: 'btn--primary',
  })
  if (!trusted) return
  try {
    const workspace = await projectWorkspaces.openWorkspace(path)
    if (!workspace) return
    freshTaskDraft.requestFreshTask(draftAgentId(), workspace.id)
    goToDraft({
      agentId: draftAgentId(),
      projectId: workspace.id,
      replace: true,
    })
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause)
    pushToast(t('workspaces.openFailed', { error: detail }), { tone: 'warn' })
  }
}

function openProjectPicker() {
  if (!rpc.canChooseProject) return
  projectPickerOpen.value = true
}

function closeProjectDraft() {
  activeProjectWorkspace.clearDraft()
  freshTaskDraft.requestFreshTask(draftAgentId())
  goToDraft({
    agentId: draftAgentId(),
    projectId: null,
    replace: true,
  })
}

async function validateActiveProjectBeforeSend(): Promise<string | null> {
  const key = sessionKey.value
  const deadlineAt = Date.now() + 7_000
  cancelActiveProjectValidation()
  const controller = new AbortController()
  activeProjectValidationController = controller
  let workspaceId = boundWorkspaceId.value
  try {
    if (
      !workspaceId
      && activeWorkspaceStatus.value === 'error'
    ) {
      const recovered = await retrySessionMetadata({
        timeoutMs: Math.max(1, deadlineAt - Date.now()),
        signal: controller.signal,
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
      })
      if (!recovered) {
        if (!controller.signal.aborted && sessionKey.value === key) {
          pushToast(t('workspaces.activeProjectBlocksSending'), { tone: 'warn' })
        }
        return activeWorkspaceSendBlockedReason.value || 'error'
      }
      workspaceId = boundWorkspaceId.value
    }
    if (!workspaceId) return activeWorkspaceSendBlockedReason.value
    if (!rpc.canManageProjectWorkspaces) {
      return activeWorkspaceSendBlockedReason.value
    }
    const workspaces = await projectWorkspaces.loadWorkspaces({
      timeoutMs: Math.max(1, deadlineAt - Date.now()),
      signal: controller.signal,
      timeoutAction: 'reconnect',
      abortAction: 'reconnect',
    })
    if (sessionKey.value !== key || boundWorkspaceId.value !== workspaceId) {
      return activeWorkspaceSendBlockedReason.value || 'resolving'
    }
    const workspace = workspaces.find(item => item.id === workspaceId) || null
    activeProjectWorkspace.applyWorkspaceRefresh(
      workspace ? activeSnapshot(workspace) : null,
    )
  } catch {
    if (
      !controller.signal.aborted
      && sessionKey.value === key
      && boundWorkspaceId.value === workspaceId
    ) {
      activeProjectWorkspace.failWorkspaceRefresh()
    }
  } finally {
    if (activeProjectValidationController === controller) {
      activeProjectValidationController = null
    }
  }
  return activeWorkspaceSendBlockedReason.value
}

function draftProjectHydrationIsCurrent(
  generation: number,
  workspaceId: string | null,
): boolean {
  return draftProjectHydration.isCurrent(generation)
    && isDraftRoute()
    && readProjectFromUrl() === workspaceId
}

async function syncDraftProjectFromRoute(generation: number): Promise<boolean> {
  const deadlineAt = Date.now() + 7_000
  const workspaceId = readProjectFromUrl()
  if (!draftProjectHydrationIsCurrent(generation, workspaceId)) return false
  if (!workspaceId) {
    activeProjectWorkspace.clearDraft()
    return true
  }
  if (!rpc.canChooseProject) {
    activeProjectWorkspace.clearDraft()
    freshTaskDraft.requestFreshTask(draftAgentId())
    goToDraft({
      agentId: draftAgentId(),
      projectId: null,
      replace: true,
    })
    return true
  }
  const cached = projectWorkspaces.byId.value.get(workspaceId)
  if (cached) {
    activeProjectWorkspace.beginProjectDraft(activeSnapshot(cached))
    return true
  }
  activeProjectWorkspace.beginUnknownProjectDraft(workspaceId)
  const controller = draftProjectHydration.createController(generation)
  if (!controller) return false
  try {
    await rpc.waitForConnection(
      Math.max(1, deadlineAt - Date.now()),
      controller.signal,
      { timeoutAction: 'reconnect', abortAction: 'reconnect' },
    )
    if (!draftProjectHydrationIsCurrent(generation, workspaceId)) return false
    await projectWorkspaces.loadWorkspaces({
      timeoutMs: Math.max(1, deadlineAt - Date.now()),
      signal: controller.signal,
      timeoutAction: 'reconnect',
      abortAction: 'reconnect',
    })
    if (!draftProjectHydrationIsCurrent(generation, workspaceId)) return false
    const workspace = projectWorkspaces.byId.value.get(workspaceId)
    if (workspace) {
      activeProjectWorkspace.beginProjectDraft(activeSnapshot(workspace))
    } else {
      activeProjectWorkspace.beginUnknownProjectDraft(workspaceId)
    }
  } catch (cause) {
    if (!draftProjectHydrationIsCurrent(generation, workspaceId)) return false
    activeProjectWorkspace.failWorkspaceRefresh()
    const detail = cause instanceof Error ? cause.message : String(cause)
    pushToast(t('workspaces.loadFailed', { error: detail }), { tone: 'warn' })
  } finally {
    draftProjectHydration.complete(generation, controller)
  }
  return true
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
  let initialDraftProjectGeneration: number | null = null
  let initialAutoSendSnapshot: {
    text: string
    revision: number
    attachments: Attachment[]
  } | null = null
  if (initialSession.draft) {
    pendingSessionIntent.value = 'new_chat'
    initialDraftProjectGeneration = draftProjectHydration.begin()
    // Apply the hand-off before any asynchronous project/live work. A later
    // completion must never overwrite text the operator typed while waiting.
    consumeDraftPrefill()
    if (pendingAutoSend.value) {
      initialAutoSendSnapshot = {
        text: pendingAutoSend.value,
        revision: composerRevision.value,
        attachments: [...pendingAttachments.value],
      }
    }
  } else {
    activeProjectWorkspace.beginSessionResolution(initialSession.sessionKey)
    persistSession(sessionKey.value, { updateRoute: false, source: 'chatView.initialSession' })
  }

  // Load elevated mode
  loadElevatedMode()

  unsubs.push(rpc.on(
    'sandbox.run_mode.preference.changed',
    payload => applyRunModePreferenceChanged(payload),
  ))

  // Register event handlers before sessions.messages.subscribe can replay
  // buffered events, then start the two critical phases before any optional
  // config, usage, slash-command, or project-list RPC can enter the Gateway's
  // serialized dispatch queue.
  unsubs.push(chatRpcSubscriptions.subscribe())
  unsubs.push(chatApprovals.subscribe())
  unsubs.push(metaRuns.subscribe())
  unsubs.push(chatPlans.subscribe())
  const sessionBootstrap = startSessionBootstrap({
    includeHistory: !initialSession.draft,
  })
  const initialDraftProjectSync = initialDraftProjectGeneration === null
    ? Promise.resolve(true)
    : sessionBootstrap.live.then(() =>
        syncDraftProjectFromRoute(initialDraftProjectGeneration!),
      )
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

  if (initialDraftProjectGeneration !== null) {
    const synced = await initialDraftProjectSync
    if (synced) {
      if (!isDraftRoute() || hasLegacyNewChatQuery()) goToDraft({ replace: true })
    }
  }

  // Sessions Hub "Start task" hand-off: send the prefilled draft in one step.
  // Wait for the subscription first so the first turn streams into this view
  // rather than being missed before sessions.messages.subscribe registers.
  if (pendingAutoSend.value && initialAutoSendSnapshot) {
    const text = initialAutoSendSnapshot.text
    const autoSendSessionKey = sessionKey.value
    const autoSendGeneration = sessionBootstrap.generation
    const subscription = await sessionBootstrap.live
    pendingAutoSend.value = ''
    const composerUnchanged = autoSendDraftIsUnchanged(
      text,
      inputText.value,
      initialAutoSendSnapshot.attachments,
      pendingAttachments.value,
      initialAutoSendSnapshot.revision,
      composerRevision.value,
    )
    if (
      !chatViewDisposed
      && sessionKey.value === autoSendSessionKey
      && isSessionBootstrapCurrent(autoSendGeneration, autoSendSessionKey)
      && subscription.authoritative
      && livePhase.value === 'ready'
      && composerUnchanged
    ) {
      sendAutomaticInput()
    } else {
      // Fail closed: the Sessions Hub hand-off remains an editable draft. The
      // inline live-recovery state owns retry and explains why sending paused.
      composerRef.value?.focusTextarea()
    }
  }
})

onUnmounted(() => {
  chatViewDisposed = true
  draftProjectHydration.invalidate()
  cancelSessionBootstrap()
  releaseOptionalRpcAdmission?.()
  releaseOptionalRpcAdmission = null
  cancelActiveProjectValidation()
  clearExecutionDockHideTimer()
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
watch(() => [route.path, route.query.agent, route.query.project], async () => {
  const generation = draftProjectHydration.begin()
  if (!isDraftRoute()) return
  if (!await syncDraftProjectFromRoute(generation)) return
  enterDraft()
})

// Explicit new-task actions must reset even when navigation targets the exact
// draft URL already on screen (for example, clicking the same project pencil).
watch(freshTaskDraft.request, request => {
  if (!request) return
  draftProjectHydration.invalidate()
  landingPrefilled.value = false
  if (request.workspaceId && rpc.canChooseProject) {
    const workspace = projectWorkspaces.byId.value.get(request.workspaceId)
    if (workspace) {
      activeProjectWorkspace.beginProjectDraft(activeSnapshot(workspace))
    } else {
      activeProjectWorkspace.beginUnknownProjectDraft(request.workspaceId)
    }
  } else {
    activeProjectWorkspace.clearDraft()
  }
  startDraftSession(request.agentId)
  if (isDesktopViewport.value) composerRef.value?.focusTextarea()
})

watch(projectWorkspaces.workspaces, workspaces => {
  if (!rpc.canManageProjectWorkspaces) return
  const workspaceId = boundWorkspaceId.value
  if (!workspaceId) return
  const workspace = workspaces.find(item => item.id === workspaceId) || null
  activeProjectWorkspace.applyWorkspaceRefresh(
    workspace ? activeSnapshot(workspace) : null,
  )
})

watch(
  () => rpc.canChooseProject,
  allowed => {
    if (allowed) return
    projectPickerOpen.value = false
    if (!isDraftRoute() || !readProjectFromUrl()) return
    activeProjectWorkspace.clearDraft()
    freshTaskDraft.requestFreshTask(draftAgentId())
    goToDraft({
      agentId: draftAgentId(),
      projectId: null,
      replace: true,
    })
  },
)

// Legacy ?newChat=1 / ?new=1 links land on the draft route, then the params disappear.
watch(() => [route.query.newChat, route.query.new], () => {
  if (hasLegacyNewChatQuery()) goToDraft({ replace: true })
})

watch(sessionKey, () => {
  pendingForkBeforeMessageId.value = null
  if (workbenchEnabled.value) workbenchStore.setSessionScope(sessionKey.value || null)
  if (shareMode.value) endShareMode()
  deliverablesOpen.value = false
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

// An approval/clarify interrupt is a user-blocking control, not answer content.
// Reveal it immediately, re-pin the live edge, and keep it outside the
// collapsible activity surface so it cannot disappear while the backend waits.
watch(
  () => visiblePendingInterruptKeys.value,
  (keys, previousKeys = []) => {
    if (!keys.some(key => !previousKeys.includes(key))) return
    revealNow()
    autoScroll.value = true
    scrollToBottom()
  },
  { flush: 'post' },
)
</script>

<style scoped src="../styles/chat-view.css"></style>

<style scoped>
/* No shared sr-only utility exists in this repo (each component scopes its
   own), so the completion announcer's clip-out lives here: zero visual
   footprint, still exposed to assistive tech. */
.chat-turn-settled-announcer {
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
