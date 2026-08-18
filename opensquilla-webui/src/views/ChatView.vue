<template>
  <div
    ref="chatRootRef"
    class="chat"
    :class="{
      'chat--new-landing': isNewChatLanding,
      'chat--meta-setup': Boolean(setupState),
      'chat--drag-over': threadDragOver,
      'chat--plan-questionnaire-open': Boolean(dockedPlanQuestionnaire),
      'chat--composer-floating': composerFxEnabled && !isNewChatLanding,
      'chat--composer-collapsed': composerCollapsed && composerFxEnabled && !isNewChatLanding,
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
          v-if="forkTransition"
          class="chat-fork-transition-overlay"
          data-testid="chat-fork-transition-overlay"
        >
          <div
            class="chat-fork-transition-status"
            :class="{ 'chat-fork-transition-status--error': forkTransition.phase === 'error' }"
            :role="forkTransition.phase === 'error' ? 'alert' : 'status'"
            :aria-live="forkTransition.phase === 'error' ? 'assertive' : 'polite'"
            aria-atomic="true"
            data-testid="chat-fork-transition-status"
          >
            <span
              v-if="forkTransition.phase !== 'error'"
              class="chat-fork-transition-status__spinner"
              aria-hidden="true"
            />
            <Icon v-else name="info" :size="15" aria-hidden="true" />
            <span class="chat-fork-transition-status__copy">{{ forkTransition.phase === 'error'
              ? t('chat.forkOpenFailed')
              : forkTransition.phase === 'creating'
                ? t('chat.forkCreating')
                : forkTransition.phase === 'returning'
                  ? t('chat.forkReturning')
                  : t('chat.forkOpening') }}</span>
            <template v-if="forkTransition.phase === 'error'">
              <button
                type="button"
                class="btn btn--ghost chat-fork-transition-status__action"
                data-testid="chat-fork-retry"
                @click="retryForkTransition"
              >{{ t('chat.reloadSession') }}</button>
              <button
                type="button"
                class="btn btn--ghost chat-fork-transition-status__action"
                data-testid="chat-fork-return"
                @click="returnToForkParent"
              >{{ t('chat.forkReturnOriginal') }}</button>
            </template>
          </div>
        </div>
        <div
          ref="threadRef"
          class="chat-thread"
          role="region"
          tabindex="0"
          :aria-label="t('chat.conversation')"
          :aria-busy="isStreaming || forkInFlight"
          @scroll="onThreadScroll"
          @wheel.passive="onThreadWheel"
          @touchmove.passive="markThreadScrollIntent('either')"
          @pointerdown="markThreadScrollIntent('either')"
          @pointermove="onThreadPointerMove"
          @keydown="onThreadScrollKeydown"
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
          v-if="!forkTransition && visibleHistoryRecoveryState"
          :key="`${sessionKey}:history`"
          :state="visibleHistoryRecoveryState"
          @retry="retryHistory"
        />
        <ChatSessionRecoveryStatus
          v-if="!forkTransition && liveRecoveryState"
          :key="`${sessionKey}:live`"
          :state="liveRecoveryState"
          @retry="retryLive"
        />
        <div
          v-if="!forkTransition && showConfirmedEmptySession"
          class="chat-empty"
        >
          {{ t('chat.noMessagesYet') }}
        </div>
        <HistoryLoadSentinel
          v-if="!isNewChatLanding && !forkTransition"
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

        <div
          class="chat-message-surface"
          :class="{ 'chat-message-surface--preview': forkTransition }"
          :inert="forkTransition ? true : undefined"
          :aria-hidden="forkTransition ? 'true' : undefined"
          :data-preview-session="forkTransition?.parentKey"
        >
        <ChatMessageList
          ref="messageListRef"
          :messages="forkTransition?.previewMessages || visibleRenderedMessages"
          :session-key="forkTransition?.parentKey || sessionKey"
          :scroll-container="threadRef"
          :virtualization-disabled="Boolean(forkTransition)"
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
          :follow-live-edge="autoScroll"
          :goal="currentGoalRun"
          :goal-elapsed="goalLastElapsed"
          @fork-conversation="forkConversation"
          @edit-message="editMessage"
          @regenerate-message="handleRegenerateMessage"
          @toggle-share-message="toggleShareMessage"
          @download-artifact="downloadArtifact"
          @open-artifact="openArtifact"
          @toggle-tool-group="toggleToolGroup"
          @toggle-tool-item="toggleToolItem"
          @show-tool-result="showToolResultModal"
          @open-session="switchToSession"
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
        </div>

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
        <!-- Durable goal outcome line at the transcript tail: once a goal
             reaches a terminal state the ribbon above the composer fades,
             but the conversation keeps a small "Goal complete · 6m 52s"
             record where the work ended. -->
        <GoalOutcomeNotice
          v-if="goalOutcomeGoal && !goalOutcomeHasMessageAnchor"
          :goal="goalOutcomeGoal"
          :elapsed="goalLastElapsed"
        />
        <PlanCard
          v-if="currentPlan && !currentPlanInHistory"
          :plan="currentPlan"
          :disabled="planActionsDisabled"
          :pending-action="planCardPendingAction"
          @implement-current="implementCurrentPlan"
          @implement-new="implementPlanInNewTask"
          @replan="beginPlanRevision"
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
              lifecycle="working"
              :step-count="executionDockRun?.status === 'running' ? 0 : liveActivityStepCount"
              :failure-count="liveActivityFailureCount"
              :phase-label="liveActivityPhaseLabel"
              :elapsed-label="streamTurnElapsed"
              :stale="streamActivityStale"
            >
              <ReasoningTimeline
                v-if="liveReasoningBlocks.length"
                :blocks="liveReasoningBlocks"
                :collapse-active="liveReasoningCollapseActive"
                pace-bursts
                nested
                @reveal-complete="completeReasoningPresentation"
              />

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
              <StreamingTextPart
                :raw-text="liveAnswerPart.rawText"
                :render-markdown="renderMarkdown"
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
        <div ref="bottomSentinelRef" class="chat-bottom-sentinel" aria-hidden="true" />
        </div>
        <ConversationMinimap
          v-if="!isNewChatLanding && !shareMode && !forkTransition"
          :messages="renderedMessages"
          :scroll-container="threadRef"
          :ensure-message-visible="messageListRef?.ensureMessageVisible"
          :release-ensured-message="messageListRef?.releaseEnsuredMessage"
          :message-offset="messageListRef?.messageOffset"
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
         execution surfaces reuse this dock across multiple turns. -->
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
    <!-- Long-running goal progress lives in the same dock as plan execution so
         the active objective stays visible above the composer across turns. -->
    <Transition name="goal-run-dock">
      <div v-if="activeGoalRun" ref="goalRunDockRef" class="goal-run-dock">
        <GoalRibbon
          :goal="activeGoalRun"
          :elapsed="goalElapsed"
          :busy="goalBusy"
          :plan-mode-active="initialCollaborationMode === 'plan'"
          :connection-takeover-available="goalConnectionTakeoverAvailable"
          :reattaching="goalReattaching"
          @edit="editGoalFromRibbon"
          @pause="pauseGoal"
          @resume="resumeGoal"
          @takeover="takeOverGoalConnection"
          @clear="clearGoal"
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
        <span
          v-if="cmd.metaStatus === 'needs_setup'"
          class="chat-slash-status"
        >{{ t('chat.metaRuns.needsSetup') }}</span>
        <span class="chat-slash-desc" :title="cmd.desc">{{ cmd.desc }}</span>
      </div>
    </div>

    <PendingQueue
      :items="pendingQueue"
      :max-pending="maxPending"
      :reorder-enabled="canReorderPendingQueue"
      :reorder-pending="pendingQueueReorderPending"
      :image-blocked-message="queuedImageSendBlockedMessage"
      :steer-available="sameTurnSteerAvailable"
      :durable-steer-available="rpc.supportsMethod('sessions.pending_inputs.steer')"
      :steer-unavailable-message="sameTurnSteerUnavailableMessage"
      @clear="clearPendingQueue"
      @edit="editPendingMessage"
      @remove="removePendingChip"
      @reorder="reorderPendingItem"
      @reorder-end="endPendingReorder"
      @reorder-start="beginPendingReorder"
      @steer="steerPendingMessage"
    />

    <div
      v-if="dockedPlanQuestionnaire"
      class="plan-questionnaire-dock"
      @wheel="handlePlanQuestionnaireWheel"
    >
      <ClarifyCard
        :request="dockedPlanQuestionnaire"
        :submitted="clarifySubmitted"
        :busy="clarifyBusy"
        :error="clarifyError"
        :docked="true"
        @submit="submitClarify"
      />
    </div>

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
      :input-disabled="Boolean(dockedPlanQuestionnaire) || Boolean(forkTransition)"
      :run-mode="runMode"
      :allowed-run-modes="composerAllowedRunModes"
      :safe-setup-available="composerSafeSetupAvailable"
      :run-mode-locked="runModeLocked"
      :run-mode-lock-message="t('chat.composer.runModeLocked')"
      :model-routing-mode="modelRoutingMode"
      :model-routing-settings-busy="modelRoutingSettingsBusy"
      :coding-mode-enabled="codingModeEnabled"
      :coding-mode-settings-busy="codingModeSettingsBusy"
      :goal-draft-armed="goalDraftArmed"
      :goal-mode-available="goalUiAvailable"
      :goal-mode-busy="goalBusy || planModeBusy || replanActive"
      :goal-mode-existing="goalComposerExisting"
      :add-menu-avoid-element="goalRunDockRef"
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
      :prompt-cache-keepalive-available="promptCacheKeepaliveAvailable"
      :prompt-cache-keepalive-session-ready="promptCacheKeepaliveSessionReady"
      :prompt-cache-keepalive-status="promptCacheKeepaliveStatus"
      :collapsed="composerCollapsed && composerFxEnabled && !isNewChatLanding"
      :floating="composerFxEnabled && !isNewChatLanding"
      @expand="expandComposer"
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
      @arm-goal="void activateGoalComposerMode()"
      @disarm-goal="disarmGoalMode"
      @cancel-replan="cancelPlanRevision"
      @voice-input="onVoiceInput"
      @voice-setup="onVoiceSetup"
      @export-markdown="exportMarkdown"
      @send="onComposerSend"
      @stop="onComposerStop"
      @choose-project="openProjectPicker"
      @close-project="closeProjectDraft"
      @open-prompt-cache-keepalive="promptCacheKeepaliveOpen = true"
      @refresh-prompt-cache-keepalive="void refreshPromptCacheKeepaliveStatus()"
    />
    <SandboxSetupDialog
      :open="composerSandboxSetupOpen"
      :pending="sandboxSetupPending"
      :outcome="sandboxSetupOutcome"
      @cancel="cancelComposerSandboxSetup"
      @background="runComposerSandboxSetupInBackground"
      @confirm="void confirmComposerSandboxSetup()"
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

    <PromptCacheKeepaliveDialog
      v-if="promptCacheKeepaliveAvailable"
      :open="promptCacheKeepaliveOpen"
      :session-key="sessionKey"
      @close="promptCacheKeepaliveOpen = false"
      @saved="onPromptCacheKeepaliveSaved"
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
import { storeToRefs } from 'pinia'
import { useRpcStore } from '@/stores/rpc'
import { useRpcCall } from '@/composables/useRpc'
import { useAppStore } from '@/stores/app'
import { useSandboxSetupStore } from '@/stores/sandboxSetup'
import { useWorkbenchStore } from '@/workbench/store'
import { usePlatform } from '@/platform'
import ApprovalCard from '@/components/chat/ApprovalCard.vue'
import ActivityDisclosure from '@/components/chat/ActivityDisclosure.vue'
import AssistantActivityTimeline from '@/components/chat/AssistantActivityTimeline.vue'
import ChatArtifactList from '@/components/chat/ChatArtifactList.vue'
import PromptCacheKeepaliveDialog from '@/components/chat/PromptCacheKeepaliveDialog.vue'
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
import StreamingTextPart from '@/components/chat/parts/StreamingTextPart.vue'
import ReasoningTimeline from '@/components/chat/ReasoningTimeline.vue'
import MetaPreflightCard from '@/components/chat/MetaPreflightCard.vue'
import MetaRibbon from '@/components/chat/MetaRibbon.vue'
import MetaSkillSetupCard from '@/components/chat/MetaSkillSetupCard.vue'
import GoalRibbon from '@/components/chat/GoalRibbon.vue'
import GoalOutcomeNotice from '@/components/chat/GoalOutcomeNotice.vue'
import PendingQueue from '@/components/chat/PendingQueue.vue'
import PlanCard from '@/components/chat/PlanCard.vue'
import PlanRunRibbon from '@/components/chat/PlanRunRibbon.vue'
import RouterFxStrip from '@/components/chat/RouterFxStrip.vue'
import SharePreviewModal from '@/components/chat/SharePreviewModal.vue'
import SandboxSetupDialog from '@/components/sandbox/SandboxSetupDialog.vue'
import ToolResultModal from '@/components/chat/ToolResultModal.vue'
import Icon from '@/components/Icon.vue'
import HistoryLoadSentinel from '@/components/HistoryLoadSentinel.vue'
import type { ChatMessageListVirtualizer } from '@/utils/chat/variableMessageWindow'
import { useChatApprovals } from '@/composables/chat/useChatApprovals'
import { useChatAttachments } from '@/composables/chat/useChatAttachments'
import { useChatCompaction } from '@/composables/chat/useChatCompaction'
import { useChatComposerShortcuts } from '@/composables/chat/useChatComposerShortcuts'
import { useChatRouteHeaderBridge } from '@/composables/chat/useChatRouteHeaderBridge'
import {
  goalHasRenderedTerminalAnchor,
  goalStatusIsTerminal,
  type GoalSetAcceptedPayload,
  useChatGoals,
} from '@/composables/chat/useChatGoals'
import { useChatDraftPersistence } from '@/composables/chat/useChatDraftPersistence'
import { useChatElevatedMode } from '@/composables/chat/useChatElevatedMode'
import { useChatFeatureToggles } from '@/composables/chat/useChatFeatureToggles'
import { useChatHistory } from '@/composables/chat/useChatHistory'
import { useChatMarkdownExport } from '@/composables/chat/useChatMarkdownExport'
import { useChatMessageActions } from '@/composables/chat/useChatMessageActions'
import {
  resolveChatHeaderTitle,
  useChatSessionTitles,
} from '@/composables/chat/useChatSessionTitles'
import {
  createChatMetaDraftRecovery,
  listServerMetaDrafts,
  queryServerMetaDrafts,
} from '@/composables/chat/useChatMetaDraftRecovery'
import {
  useChatPendingQueue,
  type PendingQueueOwnerContext,
} from '@/composables/chat/useChatPendingQueue'
import { useChatShareExport } from '@/composables/chat/useChatShareExport'
import type { ShareExportTheme } from '@/composables/chat/useChatShareExport'
import { useMediaQuery } from '@/composables/chat/useMediaQuery'
import {
  fmtTok,
  useChatRenderedMessages,
} from '@/composables/chat/useChatRenderedMessages'
import { useChatRouterDecisionRuntime } from '@/composables/chat/useChatRouterDecisionRuntime'
import { useChatAnswerReveal } from '@/composables/chat/useChatAnswerReveal'
import { useChatRpcEventHandlers } from '@/composables/chat/useChatRpcEventHandlers'
import { useChatRpcSubscriptions } from '@/composables/chat/useChatRpcSubscriptions'
import { useChatSend, type ChatSendOutcome } from '@/composables/chat/useChatSend'
import { useChatSteerDelivery } from '@/composables/chat/useChatSteerDelivery'
import { useChatTaskOwnership } from '@/composables/chat/useChatTaskOwnership'
import {
  composerRunModeSelectionAction,
  effectiveComposerRunMode,
} from '@/composables/chat/composerRunMode'
import { useSandboxSetupRecovery } from '@/composables/chat/useSandboxSetupRecovery'
import { useChatStallWatchdog } from '@/composables/chat/useChatStallWatchdog'
import { useArtifactImageLightbox } from '@/composables/chat/useArtifactImageLightbox'
import { useMetaRuns } from '@/composables/chat/useMetaRuns'
import { useMetaSkillSetup } from '@/composables/chat/useMetaSkillSetup'
import { useChatPlans } from '@/composables/chat/useChatPlans'
import { runStatusLabelText as sessionRunStatusLabelText } from '@/composables/useSessions'
import {
  shouldCanonicalizeInitialDraftRoute,
  useChatSessionRoute,
} from '@/composables/chat/useChatSessionRoute'
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
  runModeWriteRpcCallOptions,
  sandboxSetupRpcCallOptions,
} from '@/composables/chat/sessionBootstrapAdmission'
import { useChatSessionRuntime } from '@/composables/chat/useChatSessionRuntime'
import { useChatSessionSubscription } from '@/composables/chat/useChatSessionSubscription'
import {
  useChatSlashCommands,
  type DurableMetaDraft,
} from '@/composables/chat/useChatSlashCommands'
import { useChatStream } from '@/composables/chat/useChatStream'
import { useComposerFloatingPreference } from '@/composables/useComposerFloatingPreference'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import { useChatUsageWidget } from '@/composables/chat/useChatUsageWidget'
import { useSessionArtifacts } from '@/composables/chat/useSessionArtifacts'
import { useVoiceInput } from '@/composables/chat/useVoiceInput'
import { navigateMetaSetupProviderSettings } from '@/composables/chat/metaSetupProviderNavigation'
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
  ChatMaintenanceEvent,
  ChatMessage,
  ChatPendingItem,
  ChatRenderedMessage,
  ChatRunStatus,
  ChatRunStatusSource,
  ChatRunStatusState,
  ChatSteerCapability,
  ChatStreamTimelineItem,
  ChatToolCall,
  DisplayAttachment,
  HiddenControlDispatchResult,
  ToolResultContext,
} from '@/types/chat'
import {
  createForkTransitionLifetime,
  forkNavigationPhase,
  forkRouteHandoffAction,
  forkRpcRequest,
  snapshotForkPreviewMessages,
  validatedForkChildKey,
  type ForkRpcResponse,
} from '@/utils/chat/forkTransition'
import {
  steerUnavailableReason,
  type SteerUnavailableReason,
} from '@/utils/chat/steerAvailability'
import type {
  ArtifactPayload,
  MetaDraftDiscardResponse,
  SessionEventPayload,
  SessionMessagesSnapshotResponse,
  SessionMessagesSubscribeResponse,
} from '@/types/rpc'
import type { ModelRoutingMode } from '@/types/modelRouting'
import {
  isRecognizedSandboxRunMode,
  normalizeSandboxRunMode,
  type SandboxRunMode,
} from '@/types/sandbox'
import type { ChatPart, InterruptViewState } from '@/types/parts'
import type { ReasoningBlock } from '@/types/turnlog'
import type {
  PromptCacheKeepaliveStatus,
  PromptCacheKeepaliveStatusUpdate,
} from '@/types/promptCacheKeepalive'
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
import {
  persistDeferredMetaDraft,
  takeDeferredMetaDrafts,
} from '@/utils/chat/metaDraftOutbox'
import { listPendingMetaDiscards } from '@/utils/chat/metaDiscardOutbox'
import { createHistoryNavigationScrollLock } from '@/utils/chat/historyNavigationScrollLock'
import {
  captureElementScrollAnchor,
  captureVisibleTextScrollAnchor,
  createScrollHandoffGuard,
  restoreElementScrollAnchor,
  restoreTextScrollAnchor,
} from '@/utils/chat/scrollAnchor'
import {
  createComposerRetractionController,
  type ComposerScrollIntent,
} from '@/utils/chat/composerRetraction'
import {
  FINISHED_STREAM_TASK_ID,
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
import {
  projectSessionCreationRouterPresentation,
} from '@/utils/chat/sessionCreationRouterPresentation'
import { createPendingInputWal } from '@/utils/chat/pendingInputWal'
import { agentIdFromSessionKey } from '@/utils/chat/sessionKeys'
import { shouldDisableLandingSuggestions } from '@/utils/chat/landingSuggestions'
import { handoffPlanQuestionnaireWheel } from '@/utils/chat/planQuestionnaireWheel'
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
  canCollapse: () => boolean
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
const sandboxSetupStore = useSandboxSetupStore()
const {
  ensuring: sandboxSetupPending,
  outcome: sandboxSetupOutcome,
} = storeToRefs(sandboxSetupStore)
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
const pendingAutoSendSessionKey = ref('')

/* ── DOM refs ──────────────────────────────────────────────────────── */

const chatRootRef = ref<HTMLElement | null>(null)
const threadRef = ref<HTMLElement | null>(null)
const goalRunDockRef = ref<HTMLElement | null>(null)
const messageListRef = ref<ChatMessageListVirtualizer | null>(null)
const bottomSentinelRef = ref<HTMLElement | null>(null)
let bottomIntersectionObserver: IntersectionObserver | null = null
const composerRef = ref<ChatComposerHandle | null>(null)
/* Floating-composer retract: a pure controller accumulates slow user travel
   while ignoring scrollTop changes caused by history, minimap, and layout. */
const composerRetraction = createComposerRetractionController()
const composerCollapsed = ref(false)
let pendingComposerScrollIntent: ComposerScrollIntent = null
let composerScrollIntentTimer: number | null = null

// Settings → Appearance "Floating composer" toggle. Off: the composer docks in
// the normal layout and never retracts; on (default): it floats over the
// transcript and collapses to a single line while scrolling up.
const { enabled: composerFxEnabled } = useComposerFloatingPreference()
/* ── State ─────────────────────────────────────────────────────────── */

const sessionKey = ref('')

function clearPendingComposerScrollIntent() {
  pendingComposerScrollIntent = null
  if (composerScrollIntentTimer !== null) {
    window.clearTimeout(composerScrollIntentTimer)
    composerScrollIntentTimer = null
  }
}

function resetComposerRetraction() {
  clearPendingComposerScrollIntent()
  composerCollapsed.value = composerRetraction.reset()
}

function expandComposer() {
  clearPendingComposerScrollIntent()
  composerCollapsed.value = composerRetraction.expand(threadRef.value?.scrollTop ?? null)
}

function markThreadScrollIntent(intent: Exclude<ComposerScrollIntent, null>) {
  pendingComposerScrollIntent = intent
  if (composerScrollIntentTimer !== null) window.clearTimeout(composerScrollIntentTimer)
  // Wheel scrolling can land one frame after its input event. A short token
  // covers that browser scheduling gap; direction matching still rejects a
  // history-prepend correction that moves opposite to the gesture.
  composerScrollIntentTimer = window.setTimeout(() => {
    pendingComposerScrollIntent = null
    composerScrollIntentTimer = null
  }, 120)
}

function currentThreadScrollIntent(): ComposerScrollIntent {
  return pendingComposerScrollIntent
}

watch(composerFxEnabled, resetComposerRetraction, { flush: 'sync' })
watch(sessionKey, resetComposerRetraction, { flush: 'sync' })
const promptCacheKeepaliveOpen = ref(false)
const promptCacheKeepaliveStatus = ref<PromptCacheKeepaliveStatus | null>(null)
const promptCacheKeepaliveAvailable = computed(() => (
  rpc.supportsMethod('sessions.promptCacheKeepalive.status')
  && rpc.supportsMethod('sessions.promptCacheKeepalive.set')
))
const workbenchEnabled = computed(() => appStore.features.artifactWorkbench === true)
const inputText = ref('')
const composerRevision = ref(0)
const aborted = ref(false)
const autoScroll = ref(true)
const historyNavigationScrollLock = createHistoryNavigationScrollLock(autoScroll)
const composing = ref(false)
const messages = ref<Message[]>([])

type ForkTransitionPhase = 'creating' | 'opening' | 'returning' | 'error'

interface ForkTransitionState {
  generation: number
  parentKey: string
  childKey: string
  targetKey: string
  throughTurnId?: string
  phase: ForkTransitionPhase
  errorReason?: 'navigation' | 'history' | 'live'
  /** Render-only snapshot; never becomes the child session's canonical messages. */
  previewMessages: ChatRenderedMessage[]
}

const forkTransition = ref<ForkTransitionState | null>(null)
const forkInFlight = computed(() => (
  forkTransition.value !== null && forkTransition.value.phase !== 'error'
))
const forkTransitionLifetime = createForkTransitionLifetime()

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
  writeCallOptions: runModeWriteRpcCallOptions,
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
const requestedRunMode = computed<SandboxRunMode>(
  () => activeRunModeLock.value ?? globalRunMode.value,
)

const sandboxSetupRecovery = useSandboxSetupRecovery({
  rpc: {
    call: (method, params) =>
      rpc.call(method, params, sandboxSetupRpcCallOptions),
    waitForConnection: () => rpc.waitForConnection(10_000),
  },
  connectionState: computed(() => rpc.state),
  runMode: requestedRunMode,
  autoRefresh: false,
  onUnavailable: async (status) => {
    await platform.settings.reportSandboxUnavailable?.({
      state: status.state,
      ...(status.message ? { message: status.message } : {}),
    })
  },
})
const {
  status: sandboxSetupStatus,
} = sandboxSetupRecovery
const runMode = computed<SandboxRunMode>(() => effectiveComposerRunMode(
  globalRunMode.value,
  sandboxSetupStatus.value,
  activeRunModeLock.value,
  sandboxSetupRecovery.resolved.value,
))
const composerAllowedRunModes = computed<SandboxRunMode[]>(() => {
  if (!sandboxSetupRecovery.resolved.value) {
    return allowedRunModes.value.filter((mode) => mode !== 'safe')
  }
  const status = sandboxSetupStatus.value
  if (
    status !== null
    && status.state !== 'ready'
  ) {
    return allowedRunModes.value.filter((mode) => mode !== 'safe')
  }
  return allowedRunModes.value
})
const composerSafeSetupAvailable = computed(() => sandboxSetupRecovery.canSetup.value)
const composerSandboxSetupOpen = ref(false)

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
const acceptanceStopPending = ref(false)
const acceptanceRecoveryPending = ref(false)
const taskOwnership = useChatTaskOwnership()
let bindActiveStreamTask = (taskId: string) => { activeStreamTaskId.value = taskId }
let restoreLiveTurnSnapshot = (_snapshot: SessionMessagesSnapshotResponse) => {}

// Pending session intent
const pendingSessionIntent = ref<string | null>(null)
const pendingForkBeforeMessageId = ref<string | null>(null)
const freshTaskDraft = useFreshTaskDraft()
const promptCacheKeepaliveSessionReady = computed(() => pendingSessionIntent.value === null)

async function refreshPromptCacheKeepaliveStatus() {
  const key = sessionKey.value
  if (
    !key
    || !promptCacheKeepaliveAvailable.value
    || !promptCacheKeepaliveSessionReady.value
  ) return
  try {
    const next = await rpc.call<PromptCacheKeepaliveStatus>(
      'sessions.promptCacheKeepalive.status',
      { key },
    )
    if (sessionKey.value === key) promptCacheKeepaliveStatus.value = next
  } catch {
    // The settings dialog owns actionable RPC errors. Menu refresh is best effort.
  }
}

function onPromptCacheKeepaliveSaved(update: PromptCacheKeepaliveStatusUpdate) {
  if (update.sessionKey === sessionKey.value) {
    promptCacheKeepaliveStatus.value = update.status
  }
}

watch(sessionKey, () => {
  promptCacheKeepaliveStatus.value = null
})

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
  streamPhaseElapsed,
  streamTurnElapsed,
  streamToolElapsedText,
  streamIdleTimeoutMs,
  thinkingVisible,
  thinkingText,
  startStreaming,
  reconcileStreamTaskClock,
  resetStreamForRouterReplay,
  resetLiveTurnState: resetStreamLiveTurnState,
  resetStreamIdleTimer,
  setStreamConnectionAvailable,
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
  completeReasoningPresentation,
} = chatStream
watch(
  () => rpc.state,
  state => setStreamConnectionAvailable(state === 'connected'),
  { immediate: true },
)
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
let sendUsageBarrierReplay: (payload: {
  text: string
  forkBeforeMessageId: string
}) => Promise<boolean> = async () => false
// Late-bound: dispatchHiddenSend is created below (useChatSend) but the /meta
// slash handler (useChatSlashCommands, created earlier) needs it at call time.
let dispatchHiddenForMeta: (
  providerText: string,
  displayText: string,
  clientRequestId?: string,
  targetSessionKey?: string,
) => Promise<HiddenControlDispatchResult> = (
  _providerText,
  _displayText,
  clientRequestId = '',
  targetSessionKey = '',
) => (
  Promise.resolve({
    status: 'rejected',
    reason: 'invalid_request',
    clientRequestId,
    sessionKey: targetSessionKey || sessionKey.value,
  })
)
let dispatchPlanComposerPrompt: (prompt: string, composerText: string) => void = () => {}
let isCompactInFlightForCurrentSession: () => boolean = () => false
let isQueuedDeliveryBlocked: () => boolean = () => false
let isLiveDeliveryBlocked: () => boolean = () => true
let dispatchQueuedHiddenControl: (
  item: ChatPendingItem,
  ownerSessionKey: string,
) => Promise<ChatSendOutcome> = async () => 'not_sent'
let dispatchQueuedItem: (
  item: ChatPendingItem,
  ownerSessionKey?: string,
) => Promise<ChatSendOutcome> = async () => 'not_sent'
const pendingQueueOwnerContext = ref<PendingQueueOwnerContext | null>(null)
let handleHiddenControlDispatchResult: (result: HiddenControlDispatchResult) => void = () => {}
let discardHiddenControlOutbox: (sessionKey: string, clientRequestId: string) => boolean = () => false
let forgetHiddenControlOutbox: (sessionKey: string, clientRequestId: string) => void = () => {}
let disarmGoalDraftForMetaRestore: () => void = () => {}
const pendingInputWal = createPendingInputWal()
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
    || taskOwnership.hasAuthoritativeWork.value
    || acceptanceStopPending.value
    || acceptanceRecoveryPending.value
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
  pendingInputWal,
  rpc,
  supportsMethod: method => rpc.supportsMethod(method),
  connectionState: computed(() => rpc.state),
  prepareAttachmentsForSend,
  onPendingPersistenceError: reason => {
    const message = reason === 'order_conflict'
      ? 'Queue order changed in another tab. The server order was restored.'
      : reason === 'attachments_unsupported'
      ? 'Queued attachments are not supported yet. Your draft was kept.'
      : reason === 'wal_failed'
        ? 'Could not save the queued message locally. Your draft was kept.'
        : 'The queued message is still saved locally and will retry after reconnecting.'
    pushToast(message, {
      tone: ['server_rejected', 'order_conflict'].includes(reason) ? 'warn' : 'danger',
    })
  },
  dispatchHiddenControl: (item, ownerSessionKey) =>
    dispatchQueuedHiddenControl(item, ownerSessionKey),
  onHiddenControlDispatchResult: (result) => {
    if (result.reason === 'discarded') {
      const discardPersisted = discardHiddenControlOutbox(
        result.sessionKey,
        result.clientRequestId,
      )
      if (!discardPersisted) {
        pushToast(t('chat.metaRuns.cancelNotSaved'), { tone: 'danger' })
        return false
      }
    }
    handleHiddenControlDispatchResult(result)
    return true
  },
  dispatchPendingItem: (item, ownerSessionKey) =>
    dispatchQueuedItem(item, ownerSessionKey),
})
const {
  pendingQueue,
  canQueueMore,
  canReorder: canReorderPendingQueue,
  isReordering: pendingQueueReorderPending,
  busySendMode,
  maxPending,
  enqueuePendingPayload,
  enqueuePendingInput,
  enqueueRecoveredInput,
  enqueueHiddenControl,
  enqueuePendingSteerAttempt,
  removePendingChip,
  beginPendingDelivery,
  settlePendingDelivery,
  cancelDurableItem,
  clearPendingQueue,
  switchPendingQueue,
  adoptPendingQueue,
  recoverPendingQueueHandoff,
  failPendingQueueHandoff,
  editPendingItem,
  popPendingTail,
  popAllPendingIntoComposer,
  beginPendingReorder,
  reorderPendingItem,
  endPendingReorder,
  schedulePendingDrainAfterTerminal,
  flushDeferredPendingDrain,
  cleanup: cleanupPendingQueue,
} = chatPendingQueue
watch(attachmentWorkBusy, (busy) => {
  if (!busy) flushDeferredPendingDrain()
})

function restoreMetaLaunchDraft(launchText: string, targetSessionKey: string): void {
  const restored = String(launchText || '').trim()
  const target = String(targetSessionKey || '').trim()
  if (!restored || !target) return
  if (target !== sessionKey.value) {
    if (!persistDeferredMetaDraft({ sessionKey: target, launchText: restored })) {
      pushToast(t('chat.metaRuns.couldNotRunSkill', { skill: restored.split(/\s+/, 3)[1] || 'MetaSkill' }), {
        tone: 'danger',
      })
    }
    return
  }

  // A restored /meta launch is an ordinary slash draft, never a Goal
  // objective. Resolve that precedence before inspecting or queueing text.
  disarmGoalDraftForMetaRestore()
  const currentDraft = inputText.value.trim()
  if (!currentDraft) {
    inputText.value = restored
    autoResizeTextarea()
    nextTick(() => composerRef.value?.focusTextarea())
    return
  }
  if (currentDraft === restored) return
  if (!enqueueRecoveredInput(restored)) {
    // Preserve the newer composer verbatim. A durable deferred copy is safer
    // than concatenating two independently sendable requests into one turn.
    persistDeferredMetaDraft({ sessionKey: target, launchText: restored })
  }
}

function restoreDeferredMetaDrafts(
  targetSessionKey: string,
  skipLaunchTexts: ReadonlySet<string> = new Set(),
): void {
  if (sessionKey.value !== targetSessionKey) return
  for (const launchText of takeDeferredMetaDrafts(targetSessionKey)) {
    if (skipLaunchTexts.has(launchText)) continue
    restoreMetaLaunchDraft(launchText, targetSessionKey)
  }
}

const chatCompaction = useChatCompaction({
  sessionKey,
  schedulePendingDrainAfterTerminal,
  popAllPendingIntoComposer,
})
const {
  compactStatus,
  getCompactionPlacement,
  setCompactInFlight,
  hideCompactStatus,
  showCompactStatus,
  showCompactionToast,
  cleanup: cleanupCompaction,
} = chatCompaction
isCompactInFlightForCurrentSession = chatCompaction.isCompactInFlightForCurrentSession

function transcriptCompactionState(status: string): ChatMaintenanceEvent['state'] {
  if (status === 'skipped') return 'skipped'
  if (status === 'stale') return 'stale'
  if (status === 'cancelled') return 'cancelled'
  if (['failed', 'error', 'timed_out'].includes(status)) return 'failed'
  if (['completed', 'emergency_ephemeral'].includes(status)) return 'completed'
  return 'running'
}

// Standalone compaction has no assistant turn to own it. Anchor its lifecycle
// in the transcript at first observation and update that same row by id, so a
// later user turn cannot make the maintenance boundary drift down the page.
watch(compactStatus, (status) => {
  const compactionId = String(status.compactionId || '').trim()
  if (!status.visible || !compactionId) return
  const maintenance: ChatMaintenanceEvent = {
    kind: 'context_compaction',
    compactionId,
    source: status.source || 'manual',
    state: transcriptCompactionState(status.status),
    durability: status.durability || '',
    ...(status.detail ? { detail: status.detail } : {}),
    ...(status.reason ? { reason: status.reason } : {}),
  }
  const index = messages.value.findIndex(message => (
    message.role === 'maintenance'
    && message.maintenance?.kind === 'context_compaction'
    && message.maintenance.compactionId === compactionId
  ))
  if (index >= 0) {
    const previous = messages.value[index]!
    messages.value.splice(index, 1, { ...previous, maintenance })
    return
  }
  messages.value.push({
    role: 'maintenance',
    text: '',
    ts: Date.now(),
    clientId: `live-maintenance:context-compaction:${compactionId}`,
    maintenance,
  })
}, { flush: 'sync' })

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
  readSessionFromUrl,
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
  timeTranslator: t,
})
const { renderedMessages } = chatRenderedMessages
const sessionCreationRouterPresentation = computed(() => (
  projectSessionCreationRouterPresentation(renderedMessages.value, isStreaming.value)
))
const visibleRenderedMessages = computed(() => sessionCreationRouterPresentation.value.messages)

function shouldRenderRouterStrip(_message: ChatRenderedMessage): boolean {
  // Always surface the router strip — the live ensemble strip is the primary
  // surface for the synthesizing process and no longer defers to activity.
  return true
}

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
  historySessionKey,
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

const steerDelivery = useChatSteerDelivery({
  messages,
  pendingQueue,
  checkpointForUserMessage: turnId => chatStream.checkpointForUserMessage?.(turnId),
  scheduleHistorySync,
  removePendingItem: item => settlePendingDelivery(item, 'accepted'),
  restoreSteerIntoComposer: text => appendComposerText(text),
  onProjected: () => {
    autoScroll.value = true
    scrollToBottom()
  },
})

// The durable artifact index fills gaps left by the bounded/compacted message
// history. History and the in-flight ArtifactEvent stream remain live fallback
// sources for mixed-version gateways and list-refresh races.
const chatSessionArtifacts = useSessionArtifacts({
  rpc,
  sessionKey,
  messages,
  streamArtifacts,
})
const {
  artifacts: sessionArtifacts,
  load: loadSessionArtifacts,
  loadAfterReconnect: loadSessionArtifactsAfterReconnect,
  reset: resetSessionArtifacts,
  cleanup: cleanupSessionArtifacts,
} = chatSessionArtifacts

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
  sendUsageBarrierReplay: payload => sendUsageBarrierReplay(payload),
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
  notifyEditBlocked: () => pushToast(t('chat.pending.editWhileStreaming'), { tone: 'info' }),
})
const {
  copyMessage,
  regenerateMessage,
  editMessage,
} = chatMessageActions

async function handleRegenerateMessage(
  message: ChatRenderedMessage,
  settle?: (accepted: boolean) => void,
) {
  const accepted = await regenerateMessage(message)
  settle?.(accepted)
}

let applyPendingUserInputSnapshot: typeof chatPlans.applyBootstrap = () => {}
let applyGoalSnapshot: (snapshot: SessionMessagesSubscribeResponse) => void = () => {}
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
  taskOwnership,
  ownershipHydrationRequired: () => pendingSessionIntent.value !== 'new_chat',
  acceptanceStopPending,
  sessionRunStatus,
  startStreaming,
  reconcileStreamTaskClock,
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
    if (isRecognizedSandboxRunMode(lock.runMode)) {
      activeRunModeLock.value = normalizeSandboxRunMode(lock.runMode)
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
    applyGoalSnapshot(snapshot)
    applyPendingUserInputSnapshot(snapshot)
  },
})
const {
  subscribeSession,
  retrySessionMetadata,
  unsubscribeSession,
  cancelActiveSubscription,
  streamGeneration,
  observeStreamGeneration,
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
watch(
  livePhase,
  phase => appStore.setChatLivePhase(phase),
  { immediate: true },
)
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
  || taskOwnership.hasAuthoritativeWork.value
  || acceptanceStopPending.value
  || acceptanceRecoveryPending.value
  || activeTaskGroups.value.size > 0
  || isCompactInFlightForCurrentSession()
  || ['queued', 'running', 'approval_pending'].includes(runStatus.value.status)
  || activePlanRun.value?.status === 'queued'
  || activePlanRun.value?.status === 'running'
  || pendingQueueOwnerContext.value?.sessionKey === sessionKey.value
))
const canStop = computed(() => (
  !isSessionHydrating.value
  && taskOwnership.hydrationResolved.value
  && !taskOwnership.stopRequestedTaskId.value
  && !acceptanceStopPending.value
  && !acceptanceRecoveryPending.value
  && (
    Boolean(taskOwnership.stopTargetTaskId.value)
    || activeStreamTaskId.value === PENDING_STREAM_TASK_ID
    || Boolean(
      activeStreamTaskId.value
      && ![
        FINISHED_STREAM_TASK_ID,
        STOPPED_STREAM_TASK_ID,
      ].includes(activeStreamTaskId.value),
    )
    || isCompactInFlightForCurrentSession()
    || activeTaskGroups.value.size > 0
    || activePlanRun.value?.status === 'queued'
    || activePlanRun.value?.status === 'running'
    || pendingQueueOwnerContext.value?.sessionKey === sessionKey.value
  )
))
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
  taskOwnership,
  activeStreamTaskId,
  activeStreamSessionKey,
  acceptanceStopPending,
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
  rebindDraftSession,
} = chatSessionRuntime
switchToPlanSession = switchToSession

async function switchToSession(nextSessionKey: string) {
  if (nextSessionKey !== sessionKey.value) {
    activeProjectWorkspace.beginSessionResolution(nextSessionKey)
  }
  const outcome = await switchRuntimeToSession(nextSessionKey)
  if (outcome?.authoritative) {
    await handleAuthoritativeSessionSubscription(nextSessionKey)
  }
  return outcome
}

const metaSkillSetup = useMetaSkillSetup({
  rpc,
  currentSessionKey: sessionKey,
  dispatchHidden: (providerText: string, displayText: string, clientRequestId?: string) => (
    dispatchHiddenForMeta(providerText, displayText, clientRequestId)
  ),
  autoRestore: false,
  restoreDraft: restoreMetaLaunchDraft,
  discardDraft: async (draftSessionKey: string, clientRequestId: string) => {
    const result = await rpc.call<MetaDraftDiscardResponse>('meta.drafts.discard', {
      sessionKey: draftSessionKey,
      clientRequestId,
    })
    if (result?.accepted === true) {
      forgetHiddenControlOutbox(draftSessionKey, clientRequestId)
      return 'accepted'
    }
    if (result?.discarded !== true) return 'unconfirmed'
    // Only after the server confirms atomic discard may the setup flow restore
    // plain composer text. Remove the matching browser hidden-control copy too,
    // otherwise a later session restore could replay the old stable id beside
    // the newly restored composer request.
    forgetHiddenControlOutbox(draftSessionKey, clientRequestId)
    return 'discarded'
  },
  onDraftAlreadyAccepted: () => {
    pushToast(t('chat.metaRuns.cancelAlreadyAccepted'), { tone: 'info', duration: 7000 })
  },
  forgetHiddenControl: (draftSessionKey: string, clientRequestId: string) => {
    forgetHiddenControlOutbox(draftSessionKey, clientRequestId)
  },
})
const {
  setupState,
  requestSetup: requestMetaSetup,
  confirmSetup,
  beginProviderHandoff,
  cancelProviderHandoff,
  retrySetup,
  cancelSetup,
  restoreSetupJob: restoreMetaSetupJob,
  handleHiddenDispatchResult,
} = metaSkillSetup
handleHiddenControlDispatchResult = handleHiddenDispatchResult
const metaSetupProviderNavigationPending = ref(false)

function projectAcceptedGoalMessage({
  objective,
  clientMessageId,
  response,
}: GoalSetAcceptedPayload): void {
  // The callback may settle after a navigation. Never project one session's
  // accepted transcript row into another session.
  if (response.sessionKey !== sessionKey.value) return

  const messageId = String(
    response.userMessageId || response.goal?.sourceMessageId || '',
  ).trim()
  if (!messageId) {
    // Older/malformed responses cannot safely anchor a local row. Re-read the
    // authoritative transcript instead of inventing an identity.
    scheduleHistorySync()
    return
  }

  const taskId = String(response.taskId || '').trim()
  const createdAt = Number(response.goal?.createdAt)
  const timestamp: Message['ts'] = Number.isFinite(createdAt) && createdAt > 0
    ? createdAt
    : new Date().toISOString()
  let index = messages.value.findIndex(message => message.messageId === messageId)
  if (index < 0) {
    index = messages.value.findIndex(message => message.clientId === clientMessageId)
  }

  if (index >= 0) {
    const current = messages.value[index]!
    messages.value.splice(index, 1, {
      ...current,
      role: 'user',
      text: current.text || objective,
      ts: current.ts ?? timestamp,
      clientId: current.clientId || clientMessageId,
      messageId,
      ...(taskId ? { turnId: current.turnId || taskId } : {}),
    })
  } else {
    messages.value.push({
      role: 'user',
      text: objective,
      ts: timestamp,
      clientId: clientMessageId,
      messageId,
      ...(taskId ? { turnId: taskId } : {}),
    })
  }

  autoScroll.value = true
  scrollToBottom()
  scheduleHistorySync()
}

const chatGoals = useChatGoals({
  rpc,
  sessionKey,
  currentEpoch,
  streamGeneration,
  ensureSessionKey: async () => {
    // A goal needs a durable session before it can be registered. On the
    // new-chat landing the client already owns a provisional key, including
    // on the bare /chat route. The durable boundary is the pending intent,
    // not the route shape: ordinary first sends consume the same intent only
    // after their atomic acceptance. Materialize Goal sessions explicitly,
    // then switch and subscribe before goals.set.
    if (sessionKey.value && pendingSessionIntent.value !== 'new_chat') {
      return sessionKey.value
    }
    const sourceKey = sessionKey.value
    const sourceIntent = pendingSessionIntent.value
    const workspaceId = pendingWorkspaceId.value
    const created = await rpc.call<{ key?: string }>('sessions.create', {
      agentId: agentIdFromSessionKey(sourceKey),
      kind: 'webchat',
      ...(workspaceId ? { workspaceId } : {}),
    })
    const key = String(created?.key || '').trim()
    if (!key) throw new Error('failed to create a session for the goal')
    // Creating the durable row may outlive this draft. Never let its completion
    // navigate the operator away from the session/project they chose meanwhile.
    if (
      sessionKey.value !== sourceKey
      || pendingSessionIntent.value !== sourceIntent
      || pendingWorkspaceId.value !== workspaceId
    ) return ''
    if (workspaceId) freshTaskDraft.bindMaterializedProjectTask(key, workspaceId)
    await switchToSession(key)
    return key
  },
  ensureSubscribed: async key => {
    if (key !== sessionKey.value) return false
    if (livePhase.value === 'ready') return true
    const outcome = await subscribeSession()
    return outcome.authoritative
  },
  onSetAccepted: projectAcceptedGoalMessage,
  notify: message => pushToast(message, { duration: 6000 }),
})
applyGoalSnapshot = snapshot => { chatGoals.applyHydration(snapshot) }
const {
  draftArmed: goalDraftArmed,
  goal: currentGoalRun,
  activeGoal: activeGoalRun,
  lastGoal: lastGoalRun,
  busy: goalBusy,
  connectionTakeoverAvailable: goalConnectionTakeoverAvailable,
  reattaching: goalReattaching,
  elapsed: goalElapsed,
  lastGoalElapsed: goalLastElapsed,
  arm: armGoalMode,
  disarm: disarmGoalMode,
  startGoal,
  edit: editGoal,
  pause: pauseGoal,
  resume: resumeGoal,
  takeOverConnection: takeOverGoalConnection,
  clear: clearGoalMutation,
  status: goalStatus,
} = chatGoals
disarmGoalDraftForMetaRestore = disarmGoalMode

async function editGoalFromRibbon(
  objective: string,
  settle?: (accepted: boolean) => void,
) {
  let accepted = false
  try {
    accepted = await editGoal(objective)
    if (accepted) {
      pushToast(t('chat.goal.editNextTurn'), { tone: 'info', duration: 6000 })
    }
    return accepted
  } finally {
    settle?.(accepted)
  }
}

async function clearGoal() {
  const requestedSessionKey = sessionKey.value
  const requestedGoal = currentGoalRun.value
  if (!requestedGoal || goalBusy.value) return false
  const requestedGoalIdentity = {
    goalId: requestedGoal.goalId,
    sessionId: requestedGoal.sessionId,
    epoch: requestedGoal.epoch,
  }
  const approved = await confirm({
    title: t('chat.goal.removeConfirmTitle'),
    body: t('chat.goal.removeConfirmBody'),
    primaryLabel: t('chat.goal.removeConfirmPrimary'),
    primaryClass: 'btn--danger',
  })
  if (!approved) return false
  const current = currentGoalRun.value
  if (
    sessionKey.value !== requestedSessionKey
    || !current
    || current.goalId !== requestedGoalIdentity.goalId
    || current.sessionId !== requestedGoalIdentity.sessionId
    || current.epoch !== requestedGoalIdentity.epoch
  ) return false
  return clearGoalMutation()
}

// The transcript-tail outcome line only renders terminal goals; active and
// paused goals stay on the ribbon above the composer.
const goalOutcomeGoal = computed(() => lastGoalRun.value)
// Settlement follows transcript persistence in the terminal lifecycle, so a
// normal completed Goal binds directly to its final assistant row. If that row
// does not exist (for example, a legacy snapshot or failed final summary), keep
// the compatible transcript-tail outcome instead of hiding it indefinitely.
const goalOutcomeHasMessageAnchor = computed(() => (
  goalHasRenderedTerminalAnchor(goalOutcomeGoal.value, renderedMessages.value)
))

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
    resetSessionArtifacts()
    chatPlans.reset()
    chatGoals.reset()
  },
  setCompactInFlight,
  showCompactStatus,
  showCompactionToast,
  notify: (message: string) => pushToast(message, { duration: 6000 }),
  dispatchHidden: (
    providerText: string,
    displayText: string,
    clientRequestId?: string,
    targetSessionKey?: string,
  ) => dispatchHiddenForMeta(
    providerText,
    displayText,
    clientRequestId,
    targetSessionKey,
  ),
  restoreDraft: restoreMetaLaunchDraft,
  requestMetaSetup,
  dispatchPlanPrompt: (prompt: string, composerText: string) => {
    dispatchPlanComposerPrompt(prompt, composerText)
  },
  activatePlanMode: activatePlanComposerMode,
  planModeAvailable: () => planUiAvailable.value,
  codingModeEnabled,
  setCodingModeEnabled,
  armGoal: activateGoalComposerMode,
  startGoal,
  goalStatus,
  goalEdit: editGoal,
  goalPause: pauseGoal,
  goalResume: resumeGoal,
  goalClear: clearGoal,
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
  classifySlashCommand,
  executeSlashCommand,
  restoreDurableMetaDrafts: restoreServerMetaDrafts,
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

const activeSteerCapability = computed<ChatSteerCapability | null>(() => {
  const task = runStatus.value.task
  return task?.steer_capability || task?.steerCapability || null
})

const chatSend = useChatSend({
  rpc,
  supportsMethod: method => rpc.supportsMethod(method),
  activeSteerCapability,
  inputText,
  messages,
  sessionKey,
  pendingQueueOwnerContext,
  hasPendingQueueWork: () => pendingQueue.value.length > 0,
  pendingInputWal,
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
    // The provisional draft bootstrap can finish before the Gateway creates
    // the first durable session. Re-register immediately after acceptance so
    // buffered text, tool, and reasoning frames replay into the first turn.
    const bootstrap = startSessionBootstrap({ includeHistory: false, force: true })
    void bootstrap.live.then(outcome => {
      if (outcome.authoritative && sessionKey.value === key) {
        void handleAuthoritativeSessionSubscription(key)
      }
    })
  },
  aborted,
  activeStreamTaskId,
  activeStreamSessionKey,
  taskOwnership,
  acceptanceStopPending,
  acceptanceRecoveryPending,
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
  recoverPendingQueueHandoff,
  failPendingQueueHandoff,
  scheduleHistorySync,
  schedulePendingDrainAfterTerminal,
  flushDeferredPendingDrain,
  bindActiveStreamTask: taskId => bindActiveStreamTask(taskId),
  isCompactInFlightForCurrentSession,
  hasPendingAttachmentWork,
  prepareAttachmentsForSend,
  enqueuePendingInput,
  enqueuePendingPayload,
  cancelDurablePendingItem: cancelDurableItem,
  enqueueHiddenControl,
  enqueuePendingSteerAttempt,
  steerDelivery,
  restoreSteerIntoComposer: text => appendComposerText(text),
  popAllPendingIntoComposer,
  reconcileTaskOwnership: () => retrySessionMetadata(),
  classifySlashCommand,
  executeSlashCommand,
  closeSlashMenu,
  autoResizeTextarea,
  scrollToBottom,
})
const {
  onSend: dispatchCurrentInput,
  onStop,
  sendQueuedSteer,
  sendQueuedFollowup,
  sendUsageBarrierReplay: dispatchUsageBarrierReplay,
  dispatchComposerPrompt,
  dispatchHiddenSend,
  dispatchQueuedHiddenSend,
  discardHiddenControl,
  forgetHiddenControl,
  flushPendingMetaDiscards,
  restoreHiddenControls,
  sendHiddenMetaPreflightConfirmation,
  recoverResponseHandoffs,
} = chatSend
sendUsageBarrierReplay = dispatchUsageBarrierReplay
void recoverResponseHandoffs()
watch(
  [() => rpc.state, sessionKey],
  ([state]) => {
    if (state === 'connected') void recoverResponseHandoffs()
  },
)
async function onSend(
  sendOptions?: Parameters<typeof dispatchCurrentInput>[0],
): Promise<void> {
  markProvisionalDraftUsed()
  if (pendingAutoSendSessionKey.value === sessionKey.value) {
    pendingAutoSend.value = ''
    pendingAutoSendSessionKey.value = ''
  }
  await dispatchCurrentInput(sendOptions)
}
sendCurrentInput = onSend
dispatchHiddenForMeta = dispatchHiddenSend
discardHiddenControlOutbox = discardHiddenControl
forgetHiddenControlOutbox = forgetHiddenControl

async function restoreDurableMetaControls(
  targetSessionKey: string,
  prefetchedServerDrafts?: DurableMetaDraft[],
  isCurrent: () => boolean = () => true,
): Promise<void> {
  // Setup owns a matching cancellation tombstone so it can clear its recovery
  // checkpoint without ever re-entering launch. Queue-only tombstones are then
  // retried here before any server draft is considered.
  const pendingDiscardIds = new Set(
    listPendingMetaDiscards(targetSessionKey).map(item => item.clientRequestId),
  )
  await restoreMetaSetupJob(targetSessionKey)
  if (!isCurrent()) return
  const setupDiscardRequestId = setupState.value?.retryMode === 'discard'
    ? setupState.value.resumeRequestId || ''
    : ''
  const flushedDiscardIds = await flushPendingMetaDiscards(
    targetSessionKey,
    setupDiscardRequestId ? [setupDiscardRequestId] : [],
  )
  if (!isCurrent()) return
  for (const requestId of flushedDiscardIds) {
    pendingDiscardIds.add(requestId)
  }
  const serverDrafts = (prefetchedServerDrafts
    ?? await listServerMetaDrafts(rpc, { sessionKey: targetSessionKey }))
    .filter(draft => !pendingDiscardIds.has(draft.clientRequestId))
  if (!isCurrent()) return
  restoreDeferredMetaDrafts(
    targetSessionKey,
    new Set(serverDrafts.map(draft => draft.launchText)),
  )
  const activeSetupRequestId = setupState.value?.sessionKey === targetSessionKey
    ? setupState.value.resumeRequestId || setupState.value.providerHandoff?.clientRequestId || ''
    : ''
  const matchingServerDrafts = serverDrafts.filter(
    draft => draft.sessionKey === targetSessionKey,
  )
  const setupHandledRequestIds = activeSetupRequestId
    ? matchingServerDrafts
        .filter(draft => draft.clientRequestId === activeSetupRequestId)
        .map(draft => draft.clientRequestId)
    : []
  const attemptedServerRequestIds = await restoreServerMetaDrafts(
    matchingServerDrafts.filter(
      draft => draft.clientRequestId !== activeSetupRequestId,
    ),
    isCurrent,
  )
  if (!isCurrent()) return
  await restoreHiddenControls(
    targetSessionKey,
    [...setupHandledRequestIds, ...attemptedServerRequestIds],
    isCurrent,
  )
}

function flushPendingAutoSend(targetSessionKey: string): boolean {
  if (
    !pendingAutoSend.value
    || pendingAutoSendSessionKey.value !== targetSessionKey
    || sessionKey.value !== targetSessionKey
  ) {
    return false
  }
  const text = pendingAutoSend.value
  pendingAutoSend.value = ''
  pendingAutoSendSessionKey.value = ''
  // The handoff is no longer automatic once the user edits its prefill while
  // waiting for an authoritative reconnect.
  if (inputText.value !== text) return false
  sendComposerText(text)
  return true
}

async function handleAuthoritativeSessionSubscription(
  targetSessionKey: string,
  prefetchedServerDrafts?: DurableMetaDraft[],
): Promise<void> {
  const attempt = ++durableRecoveryGeneration
  const isCurrent = () => (
    chatViewActive
    && attempt === durableRecoveryGeneration
    && sessionKey.value === targetSessionKey
  )
  if (!isCurrent()) return
  // Ordinary Sessions Hub handoffs must never wait behind optional Meta
  // recovery. Durable controls remain persisted for the next reconnect.
  if (flushPendingAutoSend(targetSessionKey)) return
  await Promise.all([
    metaRuns.hydrateRecovery(),
    restoreDurableMetaControls(targetSessionKey, prefetchedServerDrafts, isCurrent),
  ])
}

function isPristineDraftForRecovery(expectedSessionKey: string, agentId: string): boolean {
  return !provisionalDraftUsed
    && sessionKey.value === expectedSessionKey
    && isDraftRoute()
    && draftAgentId() === agentId
    && agentIdFromSessionKey(expectedSessionKey) === agentId
    && pendingSessionIntent.value === 'new_chat'
    && messages.value.length === 0
    && inputText.value.length === 0
    && pendingAttachments.value.length === 0
    && pendingQueue.value.length === 0
    && pendingAutoSend.value.length === 0
    && !isStreaming.value
    && setupState.value?.sessionKey !== expectedSessionKey
}

const metaDraftRecovery = createChatMetaDraftRecovery({
  currentSessionKey: () => sessionKey.value,
  listDrafts: query => queryServerMetaDrafts(rpc, query),
  isPristineDraft: isPristineDraftForRecovery,
  rebindDraftSession,
  onAuthoritativeSubscription: handleAuthoritativeSessionSubscription,
})

let provisionalDraftUsed = false
let durableRecoveryGeneration = 0

function markProvisionalDraftUsed(): void {
  if (provisionalDraftUsed) return
  provisionalDraftUsed = true
  metaDraftRecovery.invalidate()
}
const sameTurnSteerAvailable = computed(() => (
  isStreaming.value
  && chatSend.supportsSameTurnSteer()
))

function steerUnavailableReasonMessage(reason: SteerUnavailableReason): string {
  switch (reason) {
    case 'gatewayUnsupported':
      return t('chat.pending.steerUnavailable.gatewayUnsupported')
    case 'ensemble':
      return t('chat.pending.steerUnavailable.ensemble')
    case 'taskType':
      return t('chat.pending.steerUnavailable.taskType')
    case 'queueOnly':
      return t('chat.pending.steerUnavailable.queueOnly')
    case 'noActiveTurn':
      return t('chat.pending.steerUnavailable.noActiveTurn')
    case 'turnClosing':
      return t('chat.pending.steerUnavailable.turnClosing')
    case 'capabilityPending':
      return t('chat.pending.steerUnavailable.capabilityPending')
    case 'taskMismatch':
      return t('chat.pending.steerUnavailable.taskMismatch')
    case 'textUnsupported':
      return t('chat.pending.steerUnavailable.textUnsupported')
    default:
      return t('chat.pending.steerUnavailable.generic')
  }
}

const sameTurnSteerUnavailableMessage = computed(() => {
  if (sameTurnSteerAvailable.value) return ''
  const reason = steerUnavailableReason({
    isStreaming: isStreaming.value,
    methodAvailable: rpc.supportsMethod('sessions.steer.v2'),
    modelRoutingMode: modelRoutingMode.value,
    capability: activeSteerCapability.value,
    activeTaskId: activeStreamTaskId.value,
  })
  return reason ? steerUnavailableReasonMessage(reason) : ''
})

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
  // Goal draft mode: the composer text is the durable objective and the set
  // mutation atomically accepts its first ordinary user turn.
  if (goalDraftArmed.value) {
    const goalText = inputText.value.trim()
    if (!goalText) return
    const started = await startGoal(goalText)
    if (!started) return
    disarmGoalMode()
    inputText.value = ''
    autoResizeTextarea()
    return
  }
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
dispatchQueuedHiddenControl = dispatchQueuedHiddenSend
dispatchQueuedItem = sendQueuedFollowup

function editPendingMessage(pendingUiId: string) {
  if (!editPendingItem(pendingUiId)) return
  nextTick(() => composerRef.value?.focusTextarea())
}

const pendingSteerClicks = new WeakSet<ChatPendingItem>()

async function steerPendingMessage(pendingUiId: string) {
  const candidate = pendingQueue.value.find(item => item.pendingUiId === pendingUiId)
  if (
    candidate?.steerAttempt
    && (
      candidate.steerAttempt.phase === 'submitting'
      || pendingSteerClicks.has(candidate)
    )
  ) return
  const item = candidate?.steerAttempt
    ? candidate
    : beginPendingDelivery(pendingUiId, candidate?.hiddenControl === true)
  if (!item) return
  if (candidate?.steerAttempt) pendingSteerClicks.add(candidate)

  let outcome: ChatSendOutcome = 'retryable_failure'
  try {
    outcome = item.hiddenControl
      ? await dispatchQueuedHiddenSend(item, item.ownerSessionKey || sessionKey.value)
      : await sendQueuedSteer(item)
  } finally {
    settlePendingDelivery(item, outcome)
    pendingSteerClicks.delete(item)
  }
}

const chatApprovals = useChatApprovals({
  rpc,
  sessionKey,
  runStatus,
  stream: { isStreaming, appendInterruptFrame, ensureInterruptBubble },
  interruptState,
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

const dockedPlanQuestionnaire = computed(() => (
  pendingClarify.value?.presentation === 'plan_questionnaire_v1'
    ? pendingClarify.value
    : null
))

function handlePlanQuestionnaireWheel(event: WheelEvent) {
  handoffPlanQuestionnaireWheel(event, threadRef.value)
}

const rpcEventHandlers = useChatRpcEventHandlers({
  sessionKey,
  currentEpoch,
  lastStreamSeq,
  observeStreamGeneration,
  activeTaskGroups,
  taskOwnership,
  activeStreamTaskId,
  aborted,
  messages,
  pendingQueue,
  steerDelivery,
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
  getCompactionPlacement: id => getCompactionPlacement(id) || undefined,
  showWarningToast: message => pushToast(message || t('chat.warning.default'), { tone: 'warn', duration: 5000 }),
  scheduleHistorySync,
  schedulePendingDrainAfterTerminal,
  popAllPendingIntoComposer,
  restoreSteerIntoComposer: text => appendComposerText(text),
  saveWidgetState,
  onSessionSubscribed: () => {
    if (isDraftRoute()) metaDraftRecovery.retry(draftAgentId())
    return handleAuthoritativeSessionSubscription(sessionKey.value)
  },
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
const liveTimelineSplit = computed(() => splitLiveAssistantTimeline(liveTimelineItems.value, {
  keepToolTurnTextInActivity: true,
}))
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
  {
    // The shared activity tick advances both the current phase duration and
    // the stable turn-level header without requiring provider wire traffic.
    void streamPhaseElapsed.value
    return projectAssistantActivityTimeline(liveActivityTimelineItems.value, {
      lifecycle: liveAnswerPart.value ? 'answering' : 'working',
      statusHistory: liveActivityStatusHistory.value,
      endedAt: Date.now(),
    })
  },
)
const liveActivityPhaseLabel = computed(() => {
  return String(t('chat.activity.lifecycle.working'))
})
const liveCurrentPhaseCode = computed(() => [...liveActivityProjection.value.statusSteps]
  .reverse()
  .find(step => step.isCurrent && step.category !== 'maintenance')
  ?.label.code)
const liveReasoningCollapseActive = computed(() =>
  liveCurrentPhaseCode.value === 'chat.activity.lifecycle.answering',
)
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
const liveReasoningBlocks = computed<ReasoningBlock[]>(() => {
  if (foldLiveTurnMode.value === true) {
    return foldedTurn.value.reasoningBlocks.filter(block => block.text)
  }
  if (!liveThinkingText.value) return []
  const seconds = Number.parseInt(streamThinkingElapsedText.value, 10)
  const elapsed = Number.isFinite(seconds) ? seconds : 0
  return [{
    id: 'legacy-live-reasoning',
    index: 0,
    text: liveThinkingText.value,
    status: 'streaming',
    startedAt: Date.now() - elapsed * 1000,
    contentKind: 'reasoning',
  }]
})
// No clamp and no raw status count: the header chip must agree with the
// visible body, which renders clusters plus only the semantic status steps.
// A text-only turn therefore counts 0 and the disclosure's stepCount > 0
// gate hides the chip instead of claiming "step 1" over an empty body.
const liveActivityStepCount = computed(() =>
  liveActivityProjection.value.activityClusters.length
    + liveActivityProjection.value.statusSteps.filter(isSemanticActivityStatusStep).length
    + liveReasoningBlocks.value.length,
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

function preserveTerminalAnswerAnchor() {
  const container = threadRef.value
  if (!container || autoScroll.value) return
  const liveAnswer = container.querySelector<HTMLElement>('.live-answer')
  const elementAnchor = captureElementScrollAnchor(container, liveAnswer)
  const textAnchor = captureVisibleTextScrollAnchor(container, liveAnswer)
  if (!elementAnchor && !textAnchor) return

  const ownerSessionKey = sessionKey.value
  const guard = createScrollHandoffGuard(container)
  const previousRows = Array.from(
    container.querySelectorAll<HTMLElement>('.chat-message-list__row'),
  )
  const previousLastRow = previousRows[previousRows.length - 1] ?? null
  let frameCount = 0
  const finish = () => guard.dispose()
  const restore = () => {
    if (
      sessionKey.value !== ownerSessionKey
      || isStreaming.value
      || autoScroll.value
      || threadRef.value !== container
      || guard.isCancelled()
      || guard.positionChangedBeyondTolerance()
    ) {
      finish()
      return
    }
    const rows = Array.from(
      container.querySelectorAll<HTMLElement>('.chat-message-list__row'),
    )
    const lastRow = rows[rows.length - 1] ?? null
    const replacement = lastRow && lastRow !== previousLastRow
      ? lastRow.querySelector<HTMLElement>('.assistant-answer, .msg-ai-text')
      : null
    const restored = restoreTextScrollAnchor(textAnchor, replacement)
      || restoreElementScrollAnchor(elementAnchor, replacement)
    if (restored) guard.acceptCurrentPosition()
    frameCount += 1
    // The terminal row, variable-height cache, and Markdown decorators settle
    // on adjacent frames. Re-apply the same visual offset after each phase;
    // user intent or a new stream/session cancels the handoff above.
    if (frameCount < 3 && (!restored || frameCount < 2)) {
      window.requestAnimationFrame(restore)
    } else {
      finish()
    }
  }
  void nextTick(() => window.requestAnimationFrame(restore))
}

watch(isStreaming, (streaming, wasStreaming) => {
  if (streaming) turnSettledAnnouncement.value = ''
  else if (wasStreaming) {
    preserveTerminalAnswerAnchor()
    turnSettledAnnouncement.value = String(t('chat.activity.lifecycle.settled'))
  }
}, { flush: 'pre' })

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
  observeStreamGeneration,
  sendHiddenConfirmation: sendHiddenMetaPreflightConfirmation,
  sendHiddenReplay: (providerText: string, displayText: string) => (
    dispatchHiddenForMeta(providerText, displayText)
  ),
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
let composerDockResizeObserver: ResizeObserver | null = null
let composerDockPinFrame: number | null = null
let lastComposerDockHeight = -1

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

watch(isNewChatLanding, resetComposerRetraction, { flush: 'sync' })

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
  if (dockedPlanQuestionnaire.value) return t('chat.clarify.answerPlanQuestionnaire')
  if (replanActive.value) return t('chat.plan.revisePromptPlaceholder')
  if (goalDraftArmed.value) return t('chat.goal.placeholder')
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
const goalUiAvailable = computed(() =>
  rpc.supportsMethod('goals.set')
  && rpc.supportsMethod('goals.capabilities'),
)
const goalComposerExisting = computed(() => (
  currentGoalRun.value !== null
  && !goalStatusIsTerminal(currentGoalRun.value.status)
))
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
  (forkTransition.value
    ? t(
        forkTransition.value.phase === 'error'
          ? 'chat.forkOpenFailed'
          : forkTransition.value.phase === 'creating'
            ? 'chat.forkCreating'
            : forkTransition.value.phase === 'returning'
              ? 'chat.forkReturning'
              : 'chat.forkOpening',
      )
    : '')
  || modelImageSendBlockedMessage.value
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

async function activatePlanComposerMode(): Promise<boolean> {
  const accepted = await chatPlans.setMode('plan')
  if (accepted) disarmGoalMode()
  return accepted
}

async function activateGoalComposerMode(): Promise<boolean> {
  if (
    !goalUiAvailable.value
    || goalComposerExisting.value
    || goalBusy.value
    || planModeBusy.value
    || replanActive.value
  ) return false
  if (collaboration.value.mode === 'plan') {
    const accepted = await chatPlans.setMode('default')
    if (!accepted) return false
  }
  armGoalMode()
  return true
}

function setCollaborationMode(mode: CollaborationMode) {
  if (mode === 'plan') {
    void activatePlanComposerMode()
    return
  }
  void chatPlans.setMode(mode)
}

const sessionTitles = useChatSessionTitles()
const currentChatTitle = computed(() => {
  return resolveChatHeaderTitle(
    sessionKey.value,
    sessionTitles.value,
    messages.value,
    stripTimePrefix,
    {
      newChat: t('chat.newChat'),
      chatWithSuffix: suffix => t('chat.chatWithSuffix', { suffix }),
    },
  )
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

function reportRunModePersistenceError(cause: unknown): void {
  const detail = cause instanceof Error ? cause.message : String(cause)
  console.warn('Failed to persist sandbox run mode:', detail)
  pushToast(detail, { tone: 'danger' })
}

async function persistComposerRunMode(mode: SandboxRunMode): Promise<void> {
  await setGlobalRunMode(mode)
  void sandboxSetupRecovery.refresh()
}

async function setComposerRunMode(mode: SandboxRunMode): Promise<void> {
  if (runModeLocked.value) return
  sandboxSetupStore.noteRunModeSelection(mode)
  const action = composerRunModeSelectionAction(
    mode,
    sandboxSetupStatus.value,
    composerSafeSetupAvailable.value,
    sandboxSetupRecovery.resolved.value,
  )
  if (action === 'ignore') return
  if (action === 'setup') {
    sandboxSetupStore.resetOutcome()
    composerSandboxSetupOpen.value = true
    return
  }
  try {
    await persistComposerRunMode(mode)
  } catch (cause) {
    reportRunModePersistenceError(cause)
  }
}

function cancelComposerSandboxSetup(): void {
  if (sandboxSetupPending.value) return
  composerSandboxSetupOpen.value = false
}

async function confirmComposerSandboxSetup(): Promise<void> {
  if (sandboxSetupPending.value) return
  const ready = await sandboxSetupStore.startSafeSetup()
  if (ready) {
    composerSandboxSetupOpen.value = false
    await refreshRunModePreference()
    await sandboxSetupRecovery.refresh()
  }
}

function runComposerSandboxSetupInBackground(): void {
  composerSandboxSetupOpen.value = false
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

async function openMetaSetupProviderSettings(providerId: string) {
  if (metaSetupProviderNavigationPending.value) return
  metaSetupProviderNavigationPending.value = true
  try {
    const opened = await navigateMetaSetupProviderSettings({
      providerId,
      sessionKey: setupState.value?.sessionKey || '',
      currentRouteSession: route.query.session,
      router,
      beginHandoff: beginProviderHandoff,
      cancelHandoff: cancelProviderHandoff,
      materializeSession: (handoffSessionKey) => {
        persistSession(handoffSessionKey, {
          updateRoute: false,
          source: 'chatView.metaSetupProviderHandoff',
        })
      },
    })
    if (!opened) {
      pushToast(t('chat.metaSetup.providerNavigationFailed'), { tone: 'danger' })
    }
  } finally {
    metaSetupProviderNavigationPending.value = false
  }
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

const sessionWorkbenchArtifacts = computed(() =>
  sessionArtifacts.value.filter(artifactUsesWorkbenchPreview),
)

const headerDeliverableCount = computed(() => sessionArtifacts.value.length)

const deliverablesOpen = ref(false)

function focusHeaderAction(
  action: 'deliverables' | 'share' | 'copy-session-key',
) {
  void nextTick(() => chatRouteHeaderRegistration.focusAction(action))
}

function openDeliverables() {
  if (sessionArtifacts.value.length === 0) return
  const allArtifactsUseWorkbench = sessionWorkbenchArtifacts.value.length
    === sessionArtifacts.value.length
  if (workbenchEnabled.value && allArtifactsUseWorkbench) {
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
  // Generated images are also inline media. Route every visual artifact to
  // the authenticated lightbox before the inline-focus fallback so clicking
  // either the thumbnail or its open affordance actually previews it.
  if (artifactCategory(artifact) === 'visual' && sessionKey.value) {
    artifactImageLightbox.open({
      artifact,
      navigationArtifacts: sessionArtifacts.value,
      sessionKey: sessionKey.value,
    })
    return true
  }
  if (
    isInlineMediaArtifact(artifact)
    || artifactWorkbenchPreviewKind(artifact) === 'unsupported'
  ) {
    return focusInlineDeliverable(artifact)
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

function clearForkTransition(generation?: number) {
  if (
    generation !== undefined
    && forkTransition.value?.generation !== generation
  ) return
  const clearedGeneration = generation ?? forkTransition.value?.generation
  forkTransition.value = null
  forkTransitionLifetime.invalidate(clearedGeneration)
}

function isForkTransitionActive(generation: number): boolean {
  return Boolean(
    chatViewActive
    && !chatViewDisposed
    && forkTransitionLifetime.isCurrent(generation)
    && forkTransition.value?.generation === generation
  )
}

function failForkTransition(
  generation: number,
  reason: NonNullable<ForkTransitionState['errorReason']>,
  error: unknown,
) {
  if (!isForkTransitionActive(generation)) return
  const transition = forkTransition.value!
  console.warn('Fork child hand-off failed:', error instanceof Error ? error.message : error)
  const firstFailure = transition.phase !== 'error'
  forkTransition.value = {
    ...transition,
    phase: 'error',
    errorReason: reason,
  }
  if (firstFailure) pushToast(t('chat.toast.forkOpenFailed'), { tone: 'warn' })
}

async function retryForkTransition() {
  const transition = forkTransition.value
  if (
    !transition?.targetKey
    || transition.phase !== 'error'
    || !isForkTransitionActive(transition.generation)
  ) return
  const retryPhase = forkNavigationPhase(transition.targetKey, transition.parentKey)
  forkTransition.value = {
    ...transition,
    phase: retryPhase,
    errorReason: undefined,
  }
  try {
    if (
      sessionKey.value !== transition.targetKey
      || readSessionFromUrl() !== transition.targetKey
    ) {
      const navigationFailure = await router.push({
        path: '/chat',
        query: { session: transition.targetKey },
      })
      if (!isForkTransitionActive(transition.generation)) return
      if (navigationFailure && readSessionFromUrl() !== transition.targetKey) {
        throw navigationFailure
      }
      return
    }
    if (livePhase.value === 'degraded') void retryLive()
    void retryHistory()
  } catch (error) {
    failForkTransition(transition.generation, 'navigation', error)
  }
}

async function returnToForkParent() {
  const transition = forkTransition.value
  if (!transition || !isForkTransitionActive(transition.generation)) return
  if (
    sessionKey.value === transition.parentKey
    && readSessionFromUrl() === transition.parentKey
  ) {
    if (
      transition.targetKey === transition.parentKey
      && historySessionKey.value === transition.parentKey
      && historyState.value.initialLoadStatus !== 'ready'
    ) {
      if (transition.phase === 'error') {
        await retryForkTransition()
        if (!isForkTransitionActive(transition.generation)) return
      }
      return
    }
    clearForkTransition(transition.generation)
    return
  }
  forkTransition.value = {
    ...transition,
    targetKey: transition.parentKey,
    phase: 'returning',
    errorReason: undefined,
  }
  try {
    const navigationFailure = await router.push({
      path: '/chat',
      query: { session: transition.parentKey },
    })
    if (!isForkTransitionActive(transition.generation)) return
    if (navigationFailure && readSessionFromUrl() !== transition.parentKey) {
      throw navigationFailure
    }
  } catch (error) {
    failForkTransition(transition.generation, 'navigation', error)
  }
}

async function forkConversation(throughTurnId?: string) {
  const parentKey = sessionKey.value
  if (!parentKey || forkTransition.value) return
  if (pendingSessionIntent.value === 'new_chat' || isStreaming.value) return
  const normalizedTurnId = throughTurnId?.trim() || undefined
  const generation = forkTransitionLifetime.begin()
  if (!generation) return
  forkTransition.value = {
    generation,
    parentKey,
    childKey: '',
    targetKey: parentKey,
    ...(normalizedTurnId ? { throughTurnId: normalizedTurnId } : {}),
    phase: 'creating',
    previewMessages: snapshotForkPreviewMessages(renderedMessages.value, normalizedTurnId),
  }
  try {
    const request = forkRpcRequest(parentKey, normalizedTurnId)
    const res = await rpc.call<ForkRpcResponse>(request.method, request.params)
    if (!isForkTransitionActive(generation)) return
    const childKey = validatedForkChildKey(res, normalizedTurnId)
    if (sessionKey.value !== parentKey) {
      clearForkTransition(generation)
      return
    }
    forkTransition.value = {
      ...forkTransition.value,
      childKey,
      targetKey: childKey,
      phase: 'opening',
    }
    const navigationFailure = await router.push({
      path: '/chat',
      query: { session: childKey },
    })
    if (!isForkTransitionActive(generation)) return
    if (navigationFailure && readSessionFromUrl() !== childKey) {
      throw navigationFailure
    }
  } catch (err) {
    if (!isForkTransitionActive(generation)) return
    const childCreated = Boolean(forkTransition.value?.childKey)
    if (childCreated) {
      failForkTransition(generation, 'navigation', err)
    } else if (forkTransition.value?.generation === generation) {
      console.warn('Fork failed:', err)
      clearForkTransition(generation)
      pushToast(t('chat.toast.forkFailed'), { tone: 'danger' })
    }
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

// App owns the header component. This view registers one stable set of refs and
// commands; draft materialization only changes those refs and never rebuilds
// the header subtree. The owner token makes delayed teardown harmless.
const chatRouteHeader = useChatRouteHeaderBridge()
const chatRouteHeaderRegistration = chatRouteHeader.register({
  visible: computed(() => !isNewChatLanding.value),
  title: currentChatTitle,
  copyState: sessionCopyState,
  copyIcon: sessionCopyIcon,
  copyLiveText: sessionCopyLiveText,
  deliverableCount: headerDeliverableCount,
  shareMode,
  shareableMessageCount,
}, {
  openDeliverables,
  startShare: startShareMode,
  copySessionKey: onSessionCopyClick,
  restoreComposerFocus: () => composerRef.value?.focusTextarea(),
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
    else chatRouteHeaderRegistration.focusAction('share')
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
    if (threadRef.value && bottomSentinelRef.value && autoScroll.value) {
      // The floating composer is represented by bottom padding after the
      // sentinel. scrollIntoView() aligns the sentinel but leaves that padding
      // below the viewport, so the live answer remains hidden under the dock
      // and the geometric bottom gap equals the composer height. Scroll the
      // container itself to its true maximum instead.
      threadRef.value.scrollTop = threadRef.value.scrollHeight
    }
  })
}

function onThreadScroll() {
  const el = threadRef.value
  if (!el) return
  const gap = el.scrollHeight - el.scrollTop - el.clientHeight
  // Virtualized row measurement and other layout corrections can move the
  // bottom sentinel without any reader gesture. Only release live-edge follow
  // when the reader expressed scroll intent; arriving back at the bottom may
  // always re-enable it.
  const intent = currentThreadScrollIntent()
  if (gap < 60 || intent !== null) historyNavigationScrollLock.updateFromScroll(gap)
  if (isNewChatLanding.value || !composerFxEnabled.value) {
    resetComposerRetraction()
    return
  }
  const wasCollapsed = composerCollapsed.value
  composerCollapsed.value = composerRetraction.observe({
    scrollTop: el.scrollTop,
    bottomGap: gap,
    intent: currentThreadScrollIntent(),
    canCollapse: !slashOpen.value && (composerRef.value?.canCollapse() ?? true),
    navigationLocked: historyNavigationScrollLock.locked,
  })
  if (composerCollapsed.value !== wasCollapsed) clearPendingComposerScrollIntent()
}

function onThreadWheel(event: WheelEvent) {
  if (event.deltaY === 0) return
  markThreadScrollIntent(event.deltaY < 0 ? 'up' : 'down')
}

function onThreadPointerMove(event: PointerEvent) {
  if (event.buttons !== 0 || event.pointerType === 'touch') {
    markThreadScrollIntent('either')
  }
}

function onThreadScrollKeydown(event: KeyboardEvent) {
  if (event.target !== event.currentTarget) return
  const up = event.key === 'ArrowUp'
    || event.key === 'PageUp'
    || event.key === 'Home'
    || (event.key === ' ' && event.shiftKey)
  const down = event.key === 'ArrowDown'
    || event.key === 'PageDown'
    || event.key === 'End'
    || (event.key === ' ' && !event.shiftKey)
  if (up || down) markThreadScrollIntent(up ? 'up' : 'down')
}

function syncComposerRetractionFromThread() {
  const el = threadRef.value
  if (!el) return
  clearPendingComposerScrollIntent()
  const gap = el.scrollHeight - el.scrollTop - el.clientHeight
  historyNavigationScrollLock.updateFromScroll(gap)
  composerCollapsed.value = composerRetraction.observe({
    scrollTop: el.scrollTop,
    bottomGap: gap,
    intent: null,
    canCollapse: !slashOpen.value && (composerRef.value?.canCollapse() ?? true),
    navigationLocked: false,
  })
}

function onHistoryNavigate() {
  cancelAnchorStabilization()
  syncComposerRetractionFromThread()
  historyNavigationScrollLock.start()
}

function onHistoryNavigateEnd() {
  historyNavigationScrollLock.finish()
  // Smooth-scroll frames and the final arrival are navigation, not transcript
  // browsing gestures. Establish a baseline without toggling the composer.
  syncComposerRetractionFromThread()
  if (autoScroll.value) expandComposer()
}

// Show the jump-to-latest affordance whenever the reader has scrolled up off the
// live edge (autoScroll releases at gap >= 60) and there is content to return to.
// Re-pinning autoScroll lets the stream resume following the bottom.
const showJumpToLatest = computed(() => !autoScroll.value && messages.value.length > 0)
function jumpToLatest() {
  cancelAnchorStabilization()
  historyNavigationScrollLock.finish()
  expandComposer()
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
  if (state?.autosend === true) {
    pendingAutoSend.value = prefill
    pendingAutoSendSessionKey.value = sessionKey.value
  }
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
  provisionalDraftUsed = false
  const agentId = draftAgentId()
  const isFreshDraft = pendingSessionIntent.value === 'new_chat'
    && messages.value.length === 0
    && !isStreaming.value
    && agentIdFromSessionKey(sessionKey.value) === agentId
  if (!isFreshDraft) startDraftSession(agentId)
  consumeDraftPrefill()
  if (isDesktopViewport.value) composerRef.value?.focusTextarea()
}

let chatViewActive = false

onMounted(async () => {
  chatViewActive = true
  chatViewDisposed = false
  if (
    typeof IntersectionObserver !== 'undefined'
    && threadRef.value
    && bottomSentinelRef.value
  ) {
    bottomIntersectionObserver = new IntersectionObserver((entries) => {
      if (entries.some(entry => entry.isIntersecting) && !historyNavigationScrollLock.locked) {
        autoScroll.value = true
      }
    }, {
      root: threadRef.value,
      threshold: 1,
    })
    bottomIntersectionObserver.observe(bottomSentinelRef.value)
  }
  const initialRouteFullPath = route.fullPath
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

  // Provisional Meta draft discovery is detached from the critical bootstrap.
  // It may rebind only an untouched draft and never delays ordinary chat.
  if (initialSession.draft) metaDraftRecovery.start(draftAgentId())
  const initialMetaSessionKey = sessionKey.value
  void sessionBootstrap.live.then((outcome) => {
    if (
      outcome.authoritative
      && chatViewActive
      && sessionKey.value === initialMetaSessionKey
    ) {
      if (initialSession.draft) metaDraftRecovery.retry(draftAgentId())
      return Promise.all([
        metaRuns.hydrateRecovery(),
        restoreDurableMetaControls(initialMetaSessionKey),
      ])
    }
  }).catch((error: unknown) => {
    console.warn(
      'Initial Meta recovery failed:',
      error instanceof Error ? error.message : error,
    )
  })
  // The entire dock can grow through attachments, pending work, and textarea
  // autoresize. Publish its real height locally so the thread always reserves
  // exactly enough clearance for the floating surface.
  const composerDock = composerRef.value?.composerElement()?.parentElement ?? null
  if (composerDock && typeof ResizeObserver !== 'undefined') {
    const publishComposerDockHeight = () => {
      const height = Math.ceil(composerDock.getBoundingClientRect().height)
      if (height === lastComposerDockHeight) return
      // Chromium applies a ResizeObserver-driven custom property on the next
      // layout cycle. During expansion, reserve one measured growth step ahead
      // so the dock cannot outgrow the viewport clearance before that cycle.
      // During retraction the previously published (larger) height is safe.
      const growth = lastComposerDockHeight < 0
        ? 0
        : Math.max(0, height - lastComposerDockHeight)
      lastComposerDockHeight = height
      chatRootRef.value?.style.setProperty('--composer-dock-h', `${height + growth}px`)
      if (autoScroll.value && composerDockPinFrame === null) {
        composerDockPinFrame = requestAnimationFrame(() => {
          composerDockPinFrame = null
          const thread = threadRef.value
          if (thread && autoScroll.value) {
            thread.scrollTop = thread.scrollHeight
          }
        })
      }
    }
    composerDockResizeObserver = new ResizeObserver(publishComposerDockHeight)
    composerDockResizeObserver.observe(composerDock)
    publishComposerDockHeight()
  }

  // Focus textarea on desktop
  if (isDesktopViewport.value) {
    composerRef.value?.focusTextarea()
  }

  if (initialDraftProjectGeneration !== null) {
    const synced = await initialDraftProjectSync
    if (synced && shouldCanonicalizeInitialDraftRoute({
      disposed: chatViewDisposed,
      initialFullPath: initialRouteFullPath,
      currentFullPath: route.fullPath,
      currentPathIsDraft: isDraftRoute(),
      hasLegacyNewChatQuery: hasLegacyNewChatQuery(),
    })) {
      goToDraft({ replace: true })
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
  chatRouteHeaderRegistration.release()
  chatViewActive = false
  appStore.setChatLivePhase('idle')
  chatViewDisposed = true
  forkTransitionLifetime.dispose()
  forkTransition.value = null
  durableRecoveryGeneration += 1
  metaDraftRecovery.invalidate()
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
  cleanupSessionArtifacts()
  cleanupStream()
  cleanupCompaction()
  cleanupVoiceInput()
  chatApprovals.cleanup()
  metaRuns.cleanup()
  if (composerDockResizeObserver) {
    composerDockResizeObserver.disconnect()
    composerDockResizeObserver = null
  }
  bottomIntersectionObserver?.disconnect()
  bottomIntersectionObserver = null
  if (composerDockPinFrame !== null) {
    cancelAnimationFrame(composerDockPinFrame)
    composerDockPinFrame = null
  }
  clearPendingComposerScrollIntent()
  chatRootRef.value?.style.removeProperty('--composer-dock-h')
  // Drop any live share-preview object URL so the blob can be reclaimed.
  if (sharePreview.value) {
    URL.revokeObjectURL(sharePreview.value.url)
    sharePreview.value = null
  }
})

useDocumentEvent('paste', onDocumentPaste)
useDocumentEvent('keydown', onDocumentKeydown)

// Watch for route changes
watch(() => route.query.session, async (newSession) => {
  durableRecoveryGeneration += 1
  metaDraftRecovery.invalidate()
  const transition = forkTransition.value
  if (transition) {
    const handoffAction = forkRouteHandoffAction(newSession, transition)
    if (handoffAction === 'returning') {
      forkTransition.value = {
        ...transition,
        targetKey: transition.parentKey,
        phase: 'returning',
        errorReason: undefined,
      }
    } else if (handoffAction === 'clear') {
      clearForkTransition(transition.generation)
    }
  }
  if (newSession && typeof newSession === 'string') {
    recordSessionNavigationDiag('route.query.session', {
      from: sessionKey.value,
      to: newSession,
      routeSession: newSession,
    })
    await switchToSession(newSession)
  }
})

// A route switch briefly retains the parent's terminal history status. Require
// the history composable to bind to the child key before treating `ready` as
// hand-off completion, otherwise the preview would disappear one tick early.
watch(
  () => [
    sessionKey.value,
    historySessionKey.value,
    historyState.value.initialLoadStatus,
  ] as const,
  ([activeKey, loadedKey, status]) => {
    const transition = forkTransition.value
    if (
      !transition
      || transition.phase === 'creating'
      || !transition.targetKey
      || activeKey !== transition.targetKey
      || loadedKey !== transition.targetKey
    ) return
    if (status === 'ready') {
      clearForkTransition(transition.generation)
    } else if (status === 'error') {
      failForkTransition(
        transition.generation,
        'history',
        new Error('Child history failed to load'),
      )
    }
  },
)

watch(
  () => [sessionKey.value, livePhase.value] as const,
  ([activeKey, phase]) => {
    const transition = forkTransition.value
    if (
      transition?.phase !== 'creating'
      && transition?.targetKey === activeKey
      && phase === 'degraded'
    ) {
      failForkTransition(
        transition.generation,
        'live',
        new Error('Child live subscription unavailable'),
      )
    }
  },
)

// Entering the draft route resets to a clean draft for the requested agent.
watch(() => [route.path, route.query.agent, route.query.project], async () => {
  durableRecoveryGeneration += 1
  metaDraftRecovery.invalidate()
  const generation = draftProjectHydration.begin()
  if (!isDraftRoute()) return
  if (!await syncDraftProjectFromRoute(generation)) return
  enterDraft()
  metaDraftRecovery.start(draftAgentId())
})

watch(inputText, (value) => {
  if (value.length > 0) markProvisionalDraftUsed()
}, { flush: 'sync' })

watch(() => pendingAttachments.value.length, (count) => {
  if (count > 0) markProvisionalDraftUsed()
}, { flush: 'sync' })

watch(() => pendingQueue.value.length, (count) => {
  if (count > 0) markProvisionalDraftUsed()
}, { flush: 'sync' })

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
  // Retire any in-flight page walk and clear the old Session before starting
  // the new one, so a late response cannot leak deliverables across tabs/routes.
  resetSessionArtifacts()
  if (workbenchEnabled.value) workbenchStore.setSessionScope(sessionKey.value || null)
  if (shareMode.value) endShareMode()
  deliverablesOpen.value = false
  if (sessionKey.value && pendingSessionIntent.value !== 'new_chat') void loadSessionArtifacts()
})

// Hello refreshes method capabilities on reconnect. Retry the durable index
// for the current Session then; older gateways simply remain on history/live.
watch(() => rpc.state, (state, previous) => {
  if (
    state === 'connected'
    && previous !== 'connected'
    && sessionKey.value
    && pendingSessionIntent.value !== 'new_chat'
  ) {
    void loadSessionArtifactsAfterReconnect()
  }
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

.chat-bottom-sentinel {
  width: 100%;
  height: 1px;
  pointer-events: none;
}
</style>
