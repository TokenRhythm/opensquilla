import { describe, expect, it } from 'vitest'

import { chatMessageKey, stableClientUuid } from './messageIdentity'
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

describe('stableClientUuid', () => {
  it('returns a deterministic canonical UUIDv8 without merging distinct identities', async () => {
    const first = await stableClientUuid('usage-barrier\0session-a\0message-a')

    expect(first).toBe(await stableClientUuid('usage-barrier\0session-a\0message-a'))
    expect(first).not.toBe(await stableClientUuid('usage-barrier\0session-a\0message-b'))
    expect(first).not.toBe(await stableClientUuid('usage-barrier\0session-b\0message-a'))
    expect(first).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-8[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
  })
})
