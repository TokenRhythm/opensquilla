import type { ChatRenderedMessage } from '@/types/chat'
import { chatErrorPresentation } from '@/utils/chat/chatErrorPresentation'

type ResumeMessage = Pick<ChatRenderedMessage, 'displayRole' | 'turnId' | 'turnOutcome' | 'errorCode' | 'isStreaming'>

export interface SandboxResumeContext {
  sessionKey: string
  taskId: string
  taskStatus: string
  connectionAvailable: boolean
  busy: boolean
  shareMode: boolean
  forkPreview: boolean
}

export interface SandboxResumeIdentity {
  sessionKey: string
  turnId: string
  epoch: number
  viewEpoch: number
}

export function sandboxResumeMessageTurnId(message: ResumeMessage): string {
  const outcome = message.turnOutcome
  const turnId = message.turnId || outcome?.turnId || ''
  if (!turnId || message.displayRole !== 'error' || outcome?.status !== 'failed') return ''
  if (message.turnId && outcome.turnId && message.turnId !== outcome.turnId) return ''
  if (outcome.taskId && outcome.taskId !== turnId) return ''
  if (message.errorCode && outcome.errorClass && message.errorCode !== outcome.errorClass) return ''
  const presentation = chatErrorPresentation({
    code: message.errorCode || outcome.errorClass,
    reason: outcome.reason,
    failureKind: outcome.failureKind,
    terminalStatus: outcome.status,
    outcomeKind: outcome.kind,
    cancellationSource: outcome.cancellationSource,
  })
  return presentation.action === 'resume-sandbox' ? turnId : ''
}

/** A session-addressed recovery must belong to its latest authoritative failed turn. */
export function currentSandboxResumeTurnId(
  messages: readonly ResumeMessage[],
  context: SandboxResumeContext,
): string {
  if (!context.sessionKey || !context.taskId || context.taskStatus !== 'failed'
    || !context.connectionAvailable || context.busy || context.shareMode || context.forkPreview) return ''
  let candidate = ''
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message.isStreaming) return ''
    if (!['user', 'assistant', 'error'].includes(message.displayRole)) continue
    const turnId = message.turnId || message.turnOutcome?.turnId || ''
    // An unbound user row may be a new send whose acceptance has not arrived.
    if (!turnId && message.displayRole === 'user') return ''
    if (turnId && turnId !== context.taskId) return candidate
    if (!candidate) candidate = sandboxResumeMessageTurnId(message)
    if (turnId === context.taskId && message.displayRole === 'user') return candidate
  }
  return candidate
}

/** Includes the view epoch so leaving and returning to the same session is stale. */
export function isCurrentSandboxResume(
  captured: SandboxResumeIdentity,
  current: SandboxResumeIdentity,
): boolean {
  return Boolean(captured.sessionKey && captured.turnId
    && captured.sessionKey === current.sessionKey
    && captured.turnId === current.turnId
    && captured.epoch === current.epoch
    && captured.viewEpoch === current.viewEpoch)
}
