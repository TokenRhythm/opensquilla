import { describe, expect, it } from 'vitest'
import { conversationSlashActionAllowed, MAIN_CONVERSATION_POLICY, SIDE_CONVERSATION_POLICY } from './conversationPolicy'

describe('conversation command policy', () => {
  it.each(['/fork', 'sessions.fork', 'fork_session', '/plan', 'plans.setMode', '/goal', 'goal.set',
    '/new', 'new_chat', '/reset', 'sessions.reset', '/side', '/btw', 'workbench.openSideChat'])('denies %s only in the temporary host', action => {
    expect(conversationSlashActionAllowed(MAIN_CONVERSATION_POLICY, action)).toBe(true)
    expect(conversationSlashActionAllowed(SIDE_CONVERSATION_POLICY, action)).toBe(false)
  })
  it.each(['/compact', 'sessions.contextCompact', '/usage', 'usage.status'])('retains ordinary %s capability', action => {
    expect(conversationSlashActionAllowed(SIDE_CONVERSATION_POLICY, action)).toBe(true)
  })
})
