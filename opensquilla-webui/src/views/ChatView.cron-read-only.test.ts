import { describe, expect, it } from 'vitest'

import chatViewSource from './ChatView.vue?raw'

function sourceBetween(start: string, end: string): string {
  const startIndex = chatViewSource.indexOf(start)
  const endIndex = chatViewSource.indexOf(end, startIndex)
  if (startIndex < 0 || endIndex < 0) throw new Error(`Missing ChatView source boundary: ${start}`)
  return chatViewSource.slice(startIndex, endIndex)
}

describe('Cron session read-only presentation', () => {
  it('replaces the composer with a localized status without floating dock clearance', () => {
    expect(chatViewSource).toContain(
      'v-if="turnActionsBlocked && !exactReceiptReplayVisibleForCurrentSession"',
    )
    expect(chatViewSource).toContain("? 'chat.cronSessionReadOnly'")
    expect(chatViewSource).toContain("sessionPolicyPending ? 'chat.loadingSession' : 'chat.sessionReadOnly'")
    expect(chatViewSource).toContain('<template v-else>')
    expect(chatViewSource).toContain(
      "chatRootRef.value?.querySelector<HTMLElement>('.chat-composer-dock')",
    )
    expect(chatViewSource).toContain(
      "'chat--composer-floating': composerFxEnabled && !isNewChatLanding && !turnActionsBlocked",
    )
    expect(chatViewSource).toContain(
      "'chat--plan-questionnaire-open': Boolean(dockedPlanQuestionnaire) && !turnActionsBlocked",
    )
    expect(chatViewSource).toMatch(
      /'chat--composer-collapsed':[\s\S]*&& !isNewChatLanding\s*&& !turnActionsBlocked/,
    )
  })

  it('uses the existing send gate while preserving the replay-specific gate', () => {
    expect(chatViewSource).toContain("isCronSession.value\n    ? t('chat.cronSessionReadOnly')")
    expect(chatViewSource).toContain(
      "? t('chat.loadingSession')",
    )
    expect(chatViewSource).toContain("? t('chat.sessionReadOnly')")
    expect(chatViewSource).toContain('sessionInteractivityBlockedReason.value')
    expect(chatViewSource).toContain('sessionInteractivityBlockedReason,')
    expect(chatViewSource).toContain('idempotentReplayBlockedReason: liveSendBlockedReason')
    expect(chatViewSource).toContain('exactReceiptReplayPendingForCurrentSession,')
    expect(chatViewSource).toContain('isQueuedExactReceiptReplay,')
    expect(chatViewSource).toContain('const queueReceiptReplayItems = computed(')
    expect(chatViewSource).toContain(':immutable-retry-only="turnActionsBlocked"')
    expect(chatViewSource).toContain(
      'v-if="!turnActionsBlocked || exactReceiptReplayPendingForCurrentSession"',
    )
    expect(chatViewSource).toContain(':can-stop="!turnActionsBlocked && canStop"')
    expect(chatViewSource).toContain(
      'v-if="!turnActionsBlocked && executionDockRun"',
    )
    expect(chatViewSource).toContain(
      'v-if="!turnActionsBlocked && activeGoalRun"',
    )
    expect(chatViewSource).toContain(':fresh-input-disabled="turnActionsBlocked"')
    expect(chatViewSource).toContain('canMutate: () => !turnActionsBlocked.value')
    expect(chatViewSource).toContain(':message-actions-available="!turnActionsBlocked"')
    expect(chatViewSource).toContain('canMutateMessages: () => !turnActionsBlocked.value')
    expect(chatViewSource.match(/:turn-actions-disabled="turnActionsBlocked"/g)).toHaveLength(3)
    expect(chatViewSource).toContain('turnActionsBlocked: () => turnActionsBlocked.value')
    expect(chatViewSource).toContain('<MetaSkillSetupCard')
    expect(chatViewSource).toContain(':turn-actions-disabled="turnActionsBlocked"')
  })

  it('admits only an exact receipt replay through mutable composer gates', () => {
    const composerSend = sourceBetween(
      'async function onComposerSend()',
      'sendCurrentInput = onComposerSend',
    )
    const composerBlock = sourceBetween(
      'const composerSendBlockedMessage = computed(',
      'const sendButtonTitle = computed(',
    )

    expect(chatViewSource).toContain(
      ':session-routing-busy="modelRoutingSettingsBusy\n        && !exactReceiptReplayPendingForCurrentSession"',
    )
    expect(composerSend).toContain('if (composerSendBlockedMessage.value) return')
    expect(composerSend).toContain('if (exactReceiptReplayPendingForCurrentSession.value)')
    expect(composerSend).toContain('await dispatchCurrentInput()')
    expect(composerSend.indexOf('exactReceiptReplayPendingForCurrentSession.value')).toBeLessThan(
      composerSend.indexOf('modelRoutingSettingsBusy.value || planModeBusy.value'),
    )
    expect(composerSend.indexOf('await dispatchCurrentInput()')).toBeLessThan(
      composerSend.indexOf('goalDraftArmed.value'),
    )
    expect(composerBlock).toContain('if (forkBlock) return forkBlock')
    expect(composerBlock).toContain('if (exactReceiptReplayPendingForCurrentSession.value)')
    expect(composerBlock).toContain("return liveSendBlockedReason.value || ''")
  })

  it('routes ordinary queue retries through follow-up admission, not fresh Steer', () => {
    const pendingRetry = sourceBetween(
      'async function steerPendingMessage(',
      'const chatApprovals = useChatApprovals(',
    )

    expect(pendingRetry).toContain("['retryable', 'replay_pending'].includes")
    expect(pendingRetry).toContain('? await retryQueuedItem(item)')
    expect(pendingRetry).toContain(': await sendQueuedSteer(item)')
  })

  it('does not let Escape stop work or pop drafts while receipt replay is read-only', () => {
    const keydown = sourceBetween(
      'function onDocumentKeydown(',
      '/* ── Lifecycle',
    )
    const readOnlyGuard = keydown.indexOf('if (turnActionsBlocked.value) return')

    expect(readOnlyGuard).toBeGreaterThan(-1)
    expect(readOnlyGuard).toBeLessThan(keydown.indexOf('if (canStop.value)'))
    expect(readOnlyGuard).toBeLessThan(keydown.indexOf('popAllPendingIntoComposer()'))
  })

  it('uses authoritative directory interactivity with a pending direct-route gate', () => {
    const initialSession = sourceBetween(
      'const initialSession = resolveInitialSession',
      '// Load elevated mode',
    )
    expect(chatViewSource).toContain('useChatSessionInteractivity({')
    expect(chatViewSource).toContain('knownSessions: knownDirectorySessions')
    expect(chatViewSource).toContain('resolveEnabled: sessionPolicyResolutionEnabled')
    expect(chatViewSource).not.toContain('shouldResolve: key => readSessionFromUrl() === key')
    expect(chatViewSource).toContain('policyPending: sessionPolicyPending')
    expect(chatViewSource).toContain('policyUnavailable: sessionPolicyUnavailable')
    expect(chatViewSource).toContain('isNoninteractiveSession,')
    expect(chatViewSource).toContain('turnActionsBlocked,')
    expect(initialSession.indexOf("pendingSessionIntent.value = 'new_chat'")).toBeLessThan(
      initialSession.indexOf('sessionKey.value = initialSession.sessionKey'),
    )
  })

  it('guards current, new-task, and replan handlers before Plan mutations', () => {
    const implementCurrent = sourceBetween(
      'function implementCurrentPlan(',
      'function implementPlanInNewTask(',
    )
    const implementNew = sourceBetween(
      'function implementPlanInNewTask(',
      'function beginPlanRevision(',
    )
    const replan = sourceBetween('function beginPlanRevision(', 'function cancelPlanRevision(')

    expect(implementCurrent).toContain('if (turnActionsBlocked.value || liveSendBlockedReason.value) return')
    expect(implementCurrent.indexOf('turnActionsBlocked.value')).toBeLessThan(
      implementCurrent.indexOf('chatPlans.implement'),
    )
    expect(implementNew).toContain('if (turnActionsBlocked.value || liveSendBlockedReason.value) return')
    expect(implementNew.indexOf('turnActionsBlocked.value')).toBeLessThan(
      implementNew.indexOf('chatPlans.implement'),
    )
    expect(replan).toContain('if (turnActionsBlocked.value) return')
    expect(replan.indexOf('turnActionsBlocked.value')).toBeLessThan(
      replan.indexOf('chatPlans.beginReplan'),
    )
  })

  it('disables the standalone tail PlanCard as well as historical PlanCards', () => {
    const tailPlanCard = sourceBetween(
      '<PlanCard\n          v-if="currentPlan && !currentPlanInHistory"',
      '<!-- MetaSkill run cards:',
    )
    const disabledState = sourceBetween(
      'const planActionsDisabled = computed(',
      'const PLAN_RUN_TERMINAL_HOLD_MS',
    )

    expect(tailPlanCard).toContain(':disabled="planActionsDisabled"')
    expect(disabledState).toContain('turnActionsBlocked.value')
  })

  it('guards fork before creating another session', () => {
    const fork = sourceBetween('async function forkConversation(', 'async function resumeSandbox(')

    expect(fork).toContain('if (turnActionsBlocked.value) return')
    expect(fork.indexOf('turnActionsBlocked.value')).toBeLessThan(
      fork.indexOf('sessionLifecycle.fork'),
    )
  })

  it('does not prepare hidden attachments from drag, drop, or paste', () => {
    const dragEnter = sourceBetween('function onChatDragEnter(', 'function onChatDragOver(')
    const dragOver = sourceBetween('function onChatDragOver(', 'function onChatDragLeave(')
    const drop = sourceBetween('function onChatDrop(', '/* ── Textarea')
    const paste = sourceBetween('function onDocumentPaste(', '/* ── Document keydown')

    expect(dragEnter).toContain('turnActionsBlocked.value || replanActive.value')
    expect(dragEnter).toContain("e.dataTransfer.dropEffect = 'none'")
    expect(dragOver).toContain('turnActionsBlocked.value || replanActive.value')
    expect(dragOver).toContain("e.dataTransfer.dropEffect = 'none'")
    expect(chatViewSource).toContain('v-if="threadDragOver && !turnActionsBlocked"')
    expect(chatViewSource).toContain('watch(turnActionsBlocked, readOnly => {')
    expect(drop).toContain('if (turnActionsBlocked.value)')
    expect(drop.indexOf('if (turnActionsBlocked.value)')).toBeLessThan(drop.indexOf('addAttachments(files)'))
    expect(paste).toContain('if (turnActionsBlocked.value) return')
    expect(paste.indexOf('if (turnActionsBlocked.value) return')).toBeLessThan(
      paste.indexOf('collectClipboardFiles'),
    )
  })
})
