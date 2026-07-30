import type { Ref } from 'vue'
import type { ChatRenderedMessage } from '@/types/chat'
import { downloadText } from '@/utils/browser'
import { artifactMeta, artifactName } from '@/utils/chat/artifacts'

export interface UseChatMarkdownExportOptions {
  messages: Readonly<Ref<ChatRenderedMessage[]>>
  currentTitle: Readonly<Ref<string>>
  aiGeneratedLabel: Readonly<Ref<string>>
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
      if (winner) lines.push(`> Router selected ${winner.displayName || winner.model || winner.tier}`)
      continue
    }
    if (!['user', 'assistant', 'system', 'subagent', 'error'].includes(message.displayRole || message.role)) continue
    lines.push(`## ${message.roleLabel || message.displayRole || message.role}`)
    if (message.timeStr) lines.push(`_${message.timeStr}_`)
    if (message.text) {
      const body = message.displayRole === 'subagent'
        ? subagentCompletionMarkdown(message.text)
        : markdownEscape(message.text)
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
  function exportMarkdown() {
    const markdown = buildChatMarkdown({
      title: options.currentTitle.value,
      exportedAt: new Date().toISOString(),
      messages: options.messages.value,
      aiGeneratedLabel: options.aiGeneratedLabel.value,
    })
    downloadText(markdownFilename(options.currentTitle.value), 'text/markdown;charset=utf-8', markdown)
  }

  return {
    exportMarkdown,
  }
}
