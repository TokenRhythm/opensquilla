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
    expect(chatViewSource).toContain('v-if="turnActionsBlocked"')
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
    expect(chatViewSource).toContain(':message-actions-available="!turnActionsBlocked"')
    expect(chatViewSource).toContain('canMutateMessages: () => !turnActionsBlocked.value')
    expect(chatViewSource.match(/:turn-actions-disabled="turnActionsBlocked"/g)).toHaveLength(3)
    expect(chatViewSource).toContain('turnActionsBlocked: () => turnActionsBlocked.value')
    expect(chatViewSource).toContain('<MetaSkillSetupCard')
    expect(chatViewSource).toContain(':turn-actions-disabled="turnActionsBlocked"')
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
