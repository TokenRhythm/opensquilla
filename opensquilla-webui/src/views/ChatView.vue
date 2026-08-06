<template>
  <div
    class="chat"
    :class="{
      'chat--new-landing': isNewChatLanding,
      'chat--meta-setup': Boolean(setupState),
      'chat--drag-over': threadDragOver,
      'chat--plan-questionnaire-open': Boolean(dockedPlanQuestionnaire),
    }"
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
          :is-streaming="isStreaming"
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

        <!-- Manual or turn-boundary compaction has no assistant turn to own
             it. Keep one quiet transcript maintenance row instead of a
             floating success card or a second task-like animation. -->
        <div
          v-if="compactStatus.visible && !compactStatus.compactionId"
          class="chat-compaction-event"
          :class="{
            'chat-compaction-event--running': compactStatus.isBusy,
            'chat-compaction-event--failed': compactStatus.tone === 'err',
          }"
          data-testid="compaction-event"
          :data-compaction-id="compactStatus.compactionId"
          :data-status="compactStatus.status"
          :data-source="compactStatus.source"
          :data-durability="compactStatus.durability"
          data-placement="turn-boundary"
          :role="compactStatus.tone === 'err' ? 'alert' : 'status'"
          :aria-live="compactStatus.tone === 'err' ? 'assertive' : 'polite'"
          aria-atomic="true"
        >
          <span class="chat-compaction-event__marker" aria-hidden="true" />
          <span class="chat-compaction-event__title">{{ compactStatus.message }}</span>
          <span v-if="compactStatus.detail" class="chat-compaction-event__detail">
            {{ compactStatus.detail }}
          </span>
        </div>

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

        <!-- Streaming AI message: activity stays open while the turn is live.
             Gateway-marked intermediate text remains in the transcript, while
             gateway-marked answer text streams below the activity boundary. -->
        <!-- No blanket aria-live here: the phase label inside ActivityDisclosure
             is the single live announcement point, so streaming DOM churn (tool
             rows, answer tokens) is not read out mutation-by-mutation. -->
        <div v-if="isStreaming && streamBubble && answerRevealOpen" class="msg-ai" data-history-role="assistant">
          <div class="msg-ai-main">
            <ActivityDisclosure
              default-open
              :lifecycle="liveAnswerPart ? 'answering' : 'working'"
              :step-count="executionDockRun?.status === 'running' ? 0 : liveActivityStepCount"
              :failure-count="liveActivityFailureCount"
              :phase-label="liveActivityPhaseLabel"
              :purpose-code="liveActivityPurposeCode ?? undefined"
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

            <!-- The gateway marks text as intermediate or answer. Only the
                 semantic answer span streams below the activity boundary; no
                 timeout or draft heuristic is involved. -->
            <div v-if="liveAnswerPart" class="live-answer">
              <TextPart
                :part="liveAnswerPart"
                :sources="[]"
              />
            </div>
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
            @deny="resolveApproval(entry, 'deny')"
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

    <MetaSkillSetupCard
      v-if="setupState"
      :state="setupState"
      :provider-navigation-pending="metaSetupProviderNavigationPending"
      @confirm="confirmSetup"
      @retry="retrySetup"
      @cancel="cancelSetup"
      @configure="openMetaSetupProviderSettings"
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
        <Icon name="chevronRight" :size="13" />
      </button>
    </Transition>
      <ChatComposer
        ref="composerRef"
        v-model="inputText"
        :attachments="pendingAttachments"
        :can-attach="canAttach"
        :can-stop="canStop"
        :can-run="canRun"
        :can-send="canSend"
        :can-continue="canContinue"
        :can-use-slash="canUseSlash"
        :can-show-slash="canShowSlash"
        :can-project="canProject"
        :can-show-project="canShowProject"
        :can-open-project-picker="canOpenProjectPicker"
        :can-cancel-project="canCancelProject"
        :can-bypass-content-delivery="canBypassContentDelivery"
        :run-mode="runMode"
        :run-mode-locked="runModeLocked"
        :run-mode-lock-message="runModeLockMessage"
        :pending-queue="pendingQueue"
        :pending-queue-state="pendingQueueState"
        :busy="composerBusy"
        :hint="composerHint"
        :placeholder="composerPlaceholder"
        :project-workspace="activeProjectWorkspace"
        :project-workspace-status="activeWorkspaceStatus"
        :project-workspace-error="activeWorkspaceError"
        :project-workspace-send-blocked-reason="activeWorkspaceSendBlockedReason"
        :project-workspace-send-blocked-detail="activeWorkspaceSendBlockedDetail"
        :project-workspace-send-blocked-retry="activeWorkspaceSendBlockedRetry"
        :error="composerError"
        :disabled="composerDisabled"
        :voice-input-supported="voiceInputSupported"
        :voice-input-active="voiceInputActive"
        :composer-revision="composerRevision"
        :is-streaming="isStreaming"
        :is-new-chat-landing="isNewChatLanding"
        @send="onComposerSend"
        @stop="onComposerStop"
        @continue="onComposerContinue"
        @attach="addAttachments"
        @detach="removeAttachment"
        @run-mode-toggle="onRunModeToggle"
        @open-project-picker="openProjectPicker"
        @close-project="closeProjectDraft"
        @pending-send="onPendingSend"
        @pending-pop-all="popAllPendingIntoComposer"
        @on-voice-start="onVoiceInputStart"
        @on-voice-stop="onVoiceInputStop"
        @on-voice-result="onVoiceInputResult"
        @on-voice-error="onVoiceInputError"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
// ... (script content unchanged - the only change is the template above)
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