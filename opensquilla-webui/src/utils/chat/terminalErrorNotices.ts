import type { ChatMessage } from '@/types/chat'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'
import { localizedChatErrorMessage } from '@/utils/chat/errors'

/** One terminal notice per explicit turn, shared by live and paginated history. */
export function dedupeTerminalErrorNotices(messages: ChatMessage[]): ChatMessage[] {
  const result: ChatMessage[] = []
  const byTurn = new Map<string, number>()
  for (const message of messages) {
    if (message.role !== 'error' || !message.terminalNotice || !message.turnId) {
      result.push(message)
      continue
    }
    const index = byTurn.get(message.turnId)
    if (index === undefined) {
      byTurn.set(message.turnId, result.length)
      result.push(message)
      continue
    }
    const previous = result[index]!
    // Prefer a real persisted row to a synthesized page-local notice. Keep the
    // first position, and merge metadata without making missing proof true.
    const preferPrevious = Boolean(previous.errorCode && !message.errorCode)
      || (previous.messageId && !previous.messageId.startsWith('terminal-error:')
        && (!message.messageId || message.messageId.startsWith('terminal-error:')))
    const preferred = preferPrevious ? previous : message
    const other = preferPrevious ? message : previous
    const turnOutcome = normalizeTurnOutcome({
      ...preferred.turnOutcome,
      turnId: message.turnId,
      ...(other.turnOutcome ? { outcome: other.turnOutcome } : {}),
    })
    result[index] = {
      ...other, ...preferred, turnOutcome,
      text: localizedChatErrorMessage(
        preferred.errorCode, preferred.text, turnOutcome?.replaySafe === true, turnOutcome?.failureKind,
      ),
    }
  }
  return result
}
