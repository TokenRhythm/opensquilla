import type { ChatMessage, ChatTurnOutcome } from '@/types/chat'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'
import { localizedChatErrorMessage } from '@/utils/chat/errors'

/** Error visibility depends on the turn result, never on diagnostic persistence. */
export function hasTerminalErrorNotice(outcome: ChatTurnOutcome): boolean {
  const status = outcome.status.toLowerCase()
  if (['succeeded', 'completed', 'cancelled', 'canceled'].includes(status)) return false
  return ['failed', 'timeout', 'abandoned', 'interrupted', 'partial', 'budgetlimited', 'blocked'].includes(status)
    || ['failed', 'partial', 'budgetlimited', 'blocked', 'interrupted'].includes(outcome.kind?.toLowerCase() || '')
}

/** One terminal notice per explicit turn, shared by live and paginated history. */
export function dedupeTerminalErrorNotices(messages: ChatMessage[]): ChatMessage[] {
  const result: ChatMessage[] = []
  const byTurn = new Map<string, number>()
  for (const rawMessage of messages) {
    const message = rawMessage.role === 'error' && rawMessage.terminalNotice
      ? { ...rawMessage, text: localizedChatErrorMessage(
          rawMessage.errorCode || rawMessage.turnOutcome?.errorClass, '',
          rawMessage.turnOutcome?.replaySafe === true, rawMessage.turnOutcome?.failureKind,
          rawMessage.turnOutcome?.status, {
            reason: rawMessage.turnOutcome?.reason,
            cancellationSource: rawMessage.turnOutcome?.cancellationSource,
            outcomeKind: rawMessage.turnOutcome?.kind,
          },
        ) }
      : rawMessage
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
    // Rich stream receipts can add classification/reference evidence, but
    // cannot replace the scheduler's status or terminal reason.
    const lifecycle = [preferred, other].find(message => message.turnOutcome?.statusSource === 'task')
      ?? [preferred, other].find(message => ['timeout', 'abandoned', 'cancelled'].includes(message.turnOutcome?.status ?? ''))
    if (turnOutcome && lifecycle?.turnOutcome) {
      const authority = lifecycle.turnOutcome
      turnOutcome.status = authority.status
      if (authority.reason !== undefined) turnOutcome.reason = authority.reason
      if (authority.kind !== undefined) turnOutcome.kind = authority.kind
      turnOutcome.statusSource = authority.statusSource
      if (authority.terminalMessage !== undefined) turnOutcome.terminalMessage = authority.terminalMessage
    }
    result[index] = {
      ...other, ...preferred, turnOutcome,
      text: localizedChatErrorMessage(
        preferred.errorCode || turnOutcome?.errorClass, '', turnOutcome?.replaySafe === true, turnOutcome?.failureKind,
        turnOutcome?.status,
        { reason: turnOutcome?.reason, cancellationSource: turnOutcome?.cancellationSource, outcomeKind: turnOutcome?.kind },
      ),
    }
  }
  return result
}
