import { describe, expect, it } from 'vitest'

import {
  isSensibleChatTitle,
  looksLikeRawSessionId,
  resolveChatHeaderTitle,
  type ChatHeaderMessage,
} from './useChatSessionTitles'

const stripTimePrefix = (text: string) => String(text || '').replace(/^\d{2}:\d{2} /, '')

function userMessage(text: string): ChatHeaderMessage {
  return { role: 'user', text }
}

describe('looksLikeRawSessionId', () => {
  it('flags raw agent session keys and bare UUIDs', () => {
    expect(looksLikeRawSessionId('agent:main:webchat:a1b2c3d4')).toBe(true)
    expect(looksLikeRawSessionId('550e8400-e29b-41d4-a716-446655440000')).toBe(true)
    expect(looksLikeRawSessionId('cron:midnight')).toBe(true)
  })

  it('accepts human titles', () => {
    expect(looksLikeRawSessionId('QA Browser Session')).toBe(false)
    expect(looksLikeRawSessionId('agent review')).toBe(false)
  })
})

describe('isSensibleChatTitle', () => {
  it('accepts non-empty human titles', () => {
    expect(isSensibleChatTitle('QA Browser Session')).toBe(true)
    expect(isSensibleChatTitle('New chat')).toBe(true)
  })

  it('rejects empty and raw-key titles', () => {
    expect(isSensibleChatTitle('')).toBe(false)
    expect(isSensibleChatTitle('   ')).toBe(false)
    expect(isSensibleChatTitle('agent:main:webchat:a1b2c3d4')).toBe(false)
  })
})

describe('resolveChatHeaderTitle', () => {
  const titles: Record<string, string> = {}

  it('uses the stored session title (manual rename / derived) when present', () => {
    const sessionTitles = { 'agent:main:webchat:abc': 'QA Browser Session' }
    const result = resolveChatHeaderTitle(
      'agent:main:webchat:abc',
      sessionTitles,
      [userMessage('Reply with QA_OK and do not use tools.')],
      stripTimePrefix,
    )
    expect(result).toBe('QA Browser Session')
  })

  it('truncates a long stored title to the header width', () => {
    const long = 'A session title that far exceeds the header display width limit'
    const sessionTitles = { 'agent:main:webchat:abc': long }
    const result = resolveChatHeaderTitle('agent:main:webchat:abc', sessionTitles, [], stripTimePrefix)
    expect(result).toBe(long.slice(0, 28) + '…')
  })

  it('falls back to the first user message when the stored title is a raw key', () => {
    const sessionTitles = { 'agent:main:webchat:abc': 'agent:main:webchat:abc' }
    const result = resolveChatHeaderTitle(
      'agent:main:webchat:abc',
      sessionTitles,
      [userMessage('Reply with QA_OK')],
      stripTimePrefix,
    )
    expect(result).toBe('Reply with QA_OK')
  })

  it('falls back to the first user message when the session has no stored title', () => {
    const result = resolveChatHeaderTitle(
      'agent:main:webchat:abc',
      titles,
      [userMessage('14:07 Reply with QA_OK')],
      stripTimePrefix,
    )
    expect(result).toBe('Reply with QA_OK')
  })

  it('collapses whitespace and strips time prefixes in the message fallback', () => {
    const result = resolveChatHeaderTitle(
      'agent:main:webchat:abc',
      titles,
      [userMessage('09:15  Two  spaces   and more')],
      stripTimePrefix,
    )
    expect(result).toBe('Two spaces and more')
  })

  it('ignores tool-only turns (no user text) before the first user message', () => {
    const result = resolveChatHeaderTitle(
      'agent:main:webchat:abc',
      titles,
      [
        { role: 'assistant', text: 'Let me check.' },
        userMessage('The first user message'),
      ],
      stripTimePrefix,
    )
    expect(result).toBe('The first user message')
  })

  it('shows "New chat" for an empty session key', () => {
    expect(resolveChatHeaderTitle('', titles, [], stripTimePrefix)).toBe('New chat')
  })

  it('shows "New chat" for a default-suffix key with no messages', () => {
    expect(resolveChatHeaderTitle('agent:main:webchat:default', titles, [], stripTimePrefix)).toBe('New chat')
  })

  it('shows a suffix-based placeholder for a key with no messages or title', () => {
    expect(resolveChatHeaderTitle('agent:main:webchat:sandbox', titles, [], stripTimePrefix)).toBe('Chat sandbox')
  })
})
