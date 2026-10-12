import { getCurrentScope, onScopeDispose, watch, type Ref } from 'vue'
import type { ChatRenderedMessage } from '@/types/chat'
import { downloadText } from '@/utils/browser'
import { artifactMeta, artifactName } from '@/utils/chat/artifacts'
import { sanitizeAssistantPresentationText } from '@/utils/chat/silentSentinels'
import { resolveAssistantAnswer } from '@/utils/chat/assistantActivity'
import { turnOutcomePresentation } from '@/utils/chat/turnOutcome'
import { messageContentIdentity, needsCompleteMessageText, restoreHistoryTimelineText, type ReadMessageText } from '@/utils/chat/historyMessageContent'

export interface UseChatMarkdownExportOptions {
  messages: Readonly<Ref<ChatRenderedMessage[]>>
  currentTitle: Readonly<Ref<string>>
  aiGeneratedLabel: Readonly<Ref<string>>
  sessionIdentity?: () => string
  readMessageText?: ReadMessageText
}

export interface BuildChatMarkdownOptions {
  messages: readonly ChatRenderedMessage[]
  title: string
  exportedAt: string
  aiGeneratedLabel: string
}

function markdownFilename(title: string): string {
  const slug = String(title || 'chat')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 36) || 'chat'
  return `opensquilla-chat-${slug}-${new Date().toISOString().slice(0, 10)}.md`
}

function markdownEscape(text: string): string {
  return String(text || '').replace(/\r\n/g, '\n').trim()
}

function subagentCompletionMarkdown(text: string): string {
  try {
    const parsed = JSON.parse(text)
    if (!parsed || parsed.type !== 'subagent_completion') return markdownEscape(text)
    const child = String(parsed.child_session_key || 'subagent')
    const status = String(parsed.status || 'finished')
    const reason = parsed.terminal_reason ? ` (${parsed.terminal_reason})` : ''
    const resultText = markdownEscape(parsed.result?.text || '')
    const lines = [`Subagent ${child} completed with status ${status}${reason}.`]
    if (resultText) lines.push('', 'Result:', resultText)
    return lines.join('\n')
  } catch {
    return markdownEscape(text)
  }
}

function assistantMarkdownText(message: ChatRenderedMessage): string {
  const outcome = turnOutcomePresentation(message.turnOutcome)
  const lifecycle = outcome === 'stopped' || outcome === 'interrupted' || message.interrupted
    ? 'interrupted' as const
    : outcome === 'timeout' || outcome === 'failed' || message.terminalFailure
      ? 'failed' as const
      : message.isStreaming
        ? 'working' as const
        : 'settled' as const
  return resolveAssistantAnswer(message, message.timelineItems ?? [], lifecycle).text
}

export function buildChatMarkdown(options: BuildChatMarkdownOptions): string {
  const lines: string[] = [
    `# ${options.title || 'OpenSquilla chat'}`,
    '',
    `Exported: ${options.exportedAt}`,
    '',
  ]
  for (const message of options.messages) {
    if (message.isRouterStrip) {
      const winner = message.gridCells?.[message.winnerIdx ?? -1]
      const selectedModel = String(message.routerSelectedModel || '').trim()
      if (selectedModel || winner) {
        lines.push(`> Router selected ${selectedModel || winner?.model || winner?.displayName || winner?.tier}`)
      }
      continue
    }
    if (!['user', 'assistant', 'system', 'subagent', 'error'].includes(message.displayRole || message.role)) continue
    lines.push(`## ${message.roleLabel || message.displayRole || message.role}`)
    if (message.timeStr) lines.push(`_${message.timeStr}_`)
    const presentationText = message.displayRole === 'assistant'
      ? sanitizeAssistantPresentationText(assistantMarkdownText(message), {
          inputMode: message.turnInputMode,
          runKind: message.turnRunKind,
        })
      : message.text
    if (presentationText) {
      const body = message.displayRole === 'subagent'
        ? subagentCompletionMarkdown(presentationText)
        : markdownEscape(presentationText)
      if (body) lines.push('', body)
    }
    if (message.artifacts?.length) {
      lines.push('', 'Artifacts:')
      for (const artifact of message.artifacts) {
        const meta = artifactMeta(artifact)
        lines.push(`- ${artifactName(artifact)}${meta ? ` (${meta})` : ''}`)
      }
    }
    lines.push('')
  }
  lines.push('---', '', `> ${markdownEscape(options.aiGeneratedLabel)}`, '')
  return lines.join('\n')
}

export function useChatMarkdownExport(options: UseChatMarkdownExportOptions) {
  let pending: AbortController | null = null
  function cancelExport() {
    pending?.abort()
    pending = null
  }
  if (options.sessionIdentity) watch(options.sessionIdentity, cancelExport, { flush: 'sync' })
  if (getCurrentScope()) onScopeDispose(cancelExport)

  async function exportMarkdown(): Promise<boolean> {
    cancelExport()
    const controller = new AbortController()
    pending = controller
    const transcript = options.messages.value
    const identities = transcript.map(messageContentIdentity)
    const session = options.sessionIdentity?.()
    const title = options.currentTitle.value
    const label = options.aiGeneratedLabel.value
    const current = () => !controller.signal.aborted && pending === controller
      && session === options.sessionIdentity?.() && transcript === options.messages.value
      && transcript.length === identities.length
      && transcript.every((message, index) => messageContentIdentity(message) === identities[index])
    try {
      const complete: ChatRenderedMessage[] = []
      for (const message of transcript) {
        if (!message.isRouterStrip
          && ['user', 'assistant', 'system', 'subagent', 'error'].includes(message.displayRole || message.role)
          && needsCompleteMessageText(message)) {
          if (!options.readMessageText) throw new Error('Complete history content is unavailable')
          const text = await options.readMessageText({ ...message, contentRef: message.contentRef && { ...message.contentRef } }, controller.signal)
          if (!current()) return false
          // Assistant answer selection uses timeline presentation boundaries.
          // Replacing only message.text would still export their old previews.
          const timelineItems = message.contentSlice ? undefined : restoreHistoryTimelineText(message, text)
          if (!message.contentSlice && message.historyPayloadPreview?.textUtf16Lengths
            && message.timelineItems?.some(item => item.type === 'text') && !timelineItems) {
            throw new Error('Complete history timeline is unavailable')
          }
          complete.push({ ...message, text, timelineItems: timelineItems ?? message.timelineItems })
        } else complete.push(message)
      }
      if (!current()) return false
      const markdown = buildChatMarkdown({
        title,
        exportedAt: new Date().toISOString(),
        messages: complete,
        aiGeneratedLabel: label,
      })
      downloadText(markdownFilename(title), 'text/markdown;charset=utf-8', markdown)
      return true
    } catch (error) {
      if (!controller.signal.aborted) console.warn('Markdown export failed:', error instanceof Error ? error.message : String(error))
      return false
    } finally {
      if (pending === controller) pending = null
    }
  }

  return {
    exportMarkdown,
  }
}
