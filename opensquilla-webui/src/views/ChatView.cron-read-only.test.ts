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
    expect(chatViewSource).toContain('idempotentReplayBlockedReason: liveSendBlockedReason')
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
