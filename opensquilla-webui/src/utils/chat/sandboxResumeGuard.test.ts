import { describe, expect, it } from 'vitest'
import type { ChatRenderedMessage } from '@/types/chat'
import {
  currentSandboxResumeTurnId,
  isCurrentSandboxResume,
  sandboxResumeMessageTurnId,
  type SandboxResumeContext,
} from './sandboxResumeGuard'

function paused(turnId = 'paused-turn'): ChatRenderedMessage {
  return {
    role: 'error', displayRole: 'error', roleLabel: 'Error', text: '',
    ts: null, timeStr: '', showHeader: true, turnId,
    errorCode: 'sandbox_threshold_exceeded',
    turnOutcome: { turnId, taskId: turnId, status: 'failed', kind: 'blocked' },
  }
}

const ready: SandboxResumeContext = {
  sessionKey: 'session-a', taskId: 'paused-turn', taskStatus: 'failed',
  connectionAvailable: true, busy: false, shareMode: false, forkPreview: false,
}

describe('sandbox resume ownership', () => {
  it('allows the latest paused failure while retaining earlier turns in history', () => {
    expect(currentSandboxResumeTurnId([paused('older-turn'), paused()], ready)).toBe('paused-turn')
  })

  it.each([
    { taskId: 'other-turn' }, { taskStatus: 'running' }, { taskStatus: 'cancelled' },
    { connectionAvailable: false }, { busy: true }, { shareMode: true },
    { forkPreview: true }, { sessionKey: '' },
  ])('fails closed when current state changes: %j', change => {
    expect(currentSandboxResumeTurnId([paused()], { ...ready, ...change })).toBe('')
  })

  it('rejects a stale error when a new user turn or stream has appeared', () => {
    const user = { ...paused(), role: 'user', displayRole: 'user' as const, turnId: undefined, turnOutcome: undefined }
    expect(currentSandboxResumeTurnId([paused(), user], ready)).toBe('')
    expect(currentSandboxResumeTurnId([paused(), { ...user, turnId: 'new-turn' }], ready)).toBe('')
    expect(currentSandboxResumeTurnId([paused(), { ...paused(), isStreaming: true }], ready)).toBe('')
  })

  it('rejects conflicting identity, code, and terminal lifecycle evidence', () => {
    const message = paused()
    expect(sandboxResumeMessageTurnId({ ...message, turnId: 'other-turn' })).toBe('')
    expect(sandboxResumeMessageTurnId({ ...message, turnOutcome: { ...message.turnOutcome!, taskId: 'other-task' } })).toBe('')
    expect(sandboxResumeMessageTurnId({ ...message, turnOutcome: { ...message.turnOutcome!, errorClass: 'provider_error' } })).toBe('')
    expect(sandboxResumeMessageTurnId({ ...message, turnOutcome: { ...message.turnOutcome!, status: 'timeout' } })).toBe('')
  })

  it('keeps a completion bound to the captured session, server epoch, view epoch and turn', () => {
    const captured = { sessionKey: 'session-a', epoch: 2, viewEpoch: 4, turnId: 'paused-turn' }
    const targets = [
      { ...captured, sessionKey: 'session-b' },
      { ...captured, epoch: 3 },
      { ...captured, viewEpoch: 6 },
      { ...captured, turnId: 'new-turn' },
      { ...captured, turnId: '' },
    ]
    expect(isCurrentSandboxResume(captured, { ...captured })).toBe(true)
    for (const current of targets) expect(isCurrentSandboxResume(captured, current)).toBe(false)
  })
})
