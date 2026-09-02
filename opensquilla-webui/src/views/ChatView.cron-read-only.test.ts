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
    expect(chatViewSource).toContain('v-if="isCronSession"')
    expect(chatViewSource).toContain("{{ t('chat.cronSessionReadOnly') }}")
    expect(chatViewSource).toContain('<template v-else>')
    expect(chatViewSource).toContain(
      "chatRootRef.value?.querySelector<HTMLElement>('.chat-composer-dock')",
    )
    expect(chatViewSource).toContain(
      "'chat--composer-floating': composerFxEnabled && !isNewChatLanding && !isCronSession",
    )
    expect(chatViewSource).toContain(
      "'chat--plan-questionnaire-open': Boolean(dockedPlanQuestionnaire) && !isCronSession",
    )
    expect(chatViewSource).toMatch(
      /'chat--composer-collapsed':[\s\S]*&& !isNewChatLanding\s*&& !isCronSession/,
    )
  })

  it('uses the existing send gate while preserving the replay-specific gate', () => {
    expect(chatViewSource).toContain("isCronSession.value ? t('chat.cronSessionReadOnly') : null")
    expect(chatViewSource).toContain('sessionInteractivityBlockedReason.value')
    expect(chatViewSource).toContain('sessionInteractivityBlockedReason,')
    expect(chatViewSource).toContain('idempotentReplayBlockedReason: liveSendBlockedReason')
    expect(chatViewSource).toContain(':message-actions-available="!isCronSession"')
    expect(chatViewSource).toContain('canMutateMessages: () => !isCronSession.value')
    expect(chatViewSource.match(/:turn-actions-disabled="isCronSession"/g)).toHaveLength(2)
    expect(chatViewSource).toContain('turnActionsBlocked: () => isCronSession.value')
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

    expect(implementCurrent).toContain('if (isCronSession.value || liveSendBlockedReason.value) return')
    expect(implementCurrent.indexOf('isCronSession.value')).toBeLessThan(
      implementCurrent.indexOf('chatPlans.implement'),
    )
    expect(implementNew).toContain('if (isCronSession.value || liveSendBlockedReason.value) return')
    expect(implementNew.indexOf('isCronSession.value')).toBeLessThan(
      implementNew.indexOf('chatPlans.implement'),
    )
    expect(replan).toContain('if (isCronSession.value) return')
    expect(replan.indexOf('isCronSession.value')).toBeLessThan(
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
    expect(disabledState).toContain('isCronSession.value')
  })

  it('guards fork before creating another session', () => {
    const fork = sourceBetween('async function forkConversation(', 'async function resumeSandbox(')

    expect(fork).toContain('if (isCronSession.value) return')
    expect(fork.indexOf('isCronSession.value')).toBeLessThan(
      fork.indexOf('sessionLifecycle.fork'),
    )
  })

  it('does not prepare hidden attachments from drag, drop, or paste', () => {
    const dragEnter = sourceBetween('function onChatDragEnter(', 'function onChatDragOver(')
    const dragOver = sourceBetween('function onChatDragOver(', 'function onChatDragLeave(')
    const drop = sourceBetween('function onChatDrop(', '/* ── Textarea')
    const paste = sourceBetween('function onDocumentPaste(', '/* ── Document keydown')

    expect(dragEnter).toContain('isCronSession.value || replanActive.value')
    expect(dragEnter).toContain("e.dataTransfer.dropEffect = 'none'")
    expect(dragOver).toContain('isCronSession.value || replanActive.value')
    expect(dragOver).toContain("e.dataTransfer.dropEffect = 'none'")
    expect(chatViewSource).toContain('v-if="threadDragOver && !isCronSession"')
    expect(chatViewSource).toContain('watch(isCronSession, readOnly => {')
    expect(drop).toContain('if (isCronSession.value)')
    expect(drop.indexOf('if (isCronSession.value)')).toBeLessThan(drop.indexOf('addAttachments(files)'))
    expect(paste).toContain('if (isCronSession.value) return')
    expect(paste.indexOf('if (isCronSession.value) return')).toBeLessThan(
      paste.indexOf('collectClipboardFiles'),
    )
  })
})
