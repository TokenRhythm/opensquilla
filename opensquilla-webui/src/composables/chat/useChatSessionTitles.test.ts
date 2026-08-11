import { describe, expect, it } from 'vitest'

import {
  buildChatSessionTitles,
  isSensibleChatTitle,
  looksLikeRawSessionId,
  resolveChatHeaderTitle,
  type ChatHeaderMessage,
} from './useChatSessionTitles'

const labels = {
  newChat: 'Localized new chat',
  chatWithSuffix: (suffix: string) => `Localized chat ${suffix}`,
}
const stripTimePrefix = (text: string) => text.replace(/^\d{2}:\d{2} /, '')
const userMessage = (text: string): ChatHeaderMessage => ({ role: 'user', text })

describe('chat session title validation', () => {
  it('rejects raw session identifiers without rejecting human titles', () => {
    expect(looksLikeRawSessionId('agent:main:webchat:a1b2c3d4')).toBe(true)
    expect(looksLikeRawSessionId('550e8400-e29b-41d4-a716-446655440000')).toBe(true)
    expect(looksLikeRawSessionId('cron:midnight')).toBe(true)
    expect(looksLikeRawSessionId('QA Browser Session')).toBe(false)
  })

  it('requires a non-empty, human-readable title', () => {
    expect(isSensibleChatTitle('QA Browser Session')).toBe(true)
    expect(isSensibleChatTitle('   ')).toBe(false)
    expect(isSensibleChatTitle('agent:main:webchat:a1b2c3d4')).toBe(false)
  })
})

describe('buildChatSessionTitles', () => {
  it('uses the canonical session-list title when no rename is pending', () => {
    expect(buildChatSessionTitles(
      [{ key: 'agent:main:webchat:abc', title: 'Stored title' }],
      {},
    )).toEqual({ 'agent:main:webchat:abc': 'Stored title' })
  })

  it('applies an optimistic rename over the session-list title', () => {
    expect(buildChatSessionTitles(
      [{ key: 'agent:main:webchat:abc', title: 'Old title' }],
      { 'agent:main:webchat:abc': 'QA Browser Session' },
    )).toEqual({ 'agent:main:webchat:abc': 'QA Browser Session' })
  })
})

describe('resolveChatHeaderTitle', () => {
  it('prefers a renamed session title over the first user message', () => {
    expect(resolveChatHeaderTitle(
      'agent:main:webchat:abc',
      { 'agent:main:webchat:abc': 'QA Browser Session' },
      [userMessage('Reply with QA_OK and do not use tools.')],
      stripTimePrefix,
      labels,
    )).toBe('QA Browser Session')
  })

  it('truncates a long stored title to the existing header limit', () => {
    const title = 'A session title that exceeds the chat header display width'
    expect(resolveChatHeaderTitle(
      'agent:main:webchat:abc',
      { 'agent:main:webchat:abc': title },
      [],
      stripTimePrefix,
      labels,
    )).toBe(`${title.slice(0, 28)}…`)
  })

  it('falls back to the first user message for missing or raw titles', () => {
    expect(resolveChatHeaderTitle(
      'agent:main:webchat:abc',
      { 'agent:main:webchat:abc': 'agent:main:webchat:abc' },
      [userMessage('09:15  First   user message')],
      stripTimePrefix,
      labels,
    )).toBe('First user message')
  })

  it('preserves localized empty-session fallbacks', () => {
    expect(resolveChatHeaderTitle('', {}, [], stripTimePrefix, labels))
      .toBe('Localized new chat')
    expect(resolveChatHeaderTitle(
      'agent:main:webchat:sandbox',
      {},
      [],
      stripTimePrefix,
      labels,
    )).toBe('Localized chat sandbox')
  })
})
