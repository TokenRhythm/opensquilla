import { describe, expect, it } from 'vitest'

import { chatMessageKey } from './messageIdentity'
import type { ChatRenderedMessage } from '@/types/chat'

describe('chatMessageKey', () => {
  it('keeps the optimistic key after canonical history assigns a message id', () => {
    const before = {
      clientId: 'local-assistant',
      id: 'assistant-0',
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 0,
    } as ChatRenderedMessage
    const after = {
      ...before,
      messageId: 'server-assistant',
    }

    expect(chatMessageKey(before, 0)).toBe('local-assistant')
    expect(chatMessageKey(after, 0)).toBe('local-assistant')
  })
})
