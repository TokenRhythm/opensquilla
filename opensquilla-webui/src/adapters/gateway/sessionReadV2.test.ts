import { describe, expect, it, vi } from 'vitest'
import { requestV2SessionHistory } from './sessionReadV2'
import type { ContentRef } from '@/contracts/generated/v4/sessionsHistoryPageV2'

function result(reference: Partial<ContentRef> = {}) {
  return {
    session_id: 'sid', session_epoch: 2, projection_revision: 2,
    before_cursor: null, after_cursor: null, has_more_before: false,
    has_more_after: false, complete_for_requested_window: true,
    canonical_available: true, canonical_complete: true, history_scope: 'complete',
    compaction_summaries: [], turn_outcomes: [],
    items: [{
      message_id: 'mid', item_id: 'mid', order: '1', role: 'assistant', preview: 'preview',
      preview_complete: false,
      source_revision: 'legacy-v1:compacted:12:1000:2:16735', content_availability: 'ready',
      message: { message_id: 'mid', role: 'assistant', text: 'preview' },
      contents: [{ availability: 'ready', ref: {
        content_id: 'legacy:sid:mid', session_id: 'sid', session_epoch: 2,
        revision: 'legacy-v1:compacted:12:1000:2:16735', source: 'compacted',
        representation: 'text-utf8', byte_length: 1024 ** 3, sha256: 'a'.repeat(64),
        status: 'sealed', durable: true, expires_at: null, ...reference,
      } }],
    }],
  }
}

async function read(reference: Partial<ContentRef>) {
  return requestV2SessionHistory({
    generation: 1, request: vi.fn().mockResolvedValue(result(reference)),
  }, 'key', { direction: 'latest', limit: 10, signal: new AbortController().signal }, 1)
}

describe('v2 content read identity', () => {
  it.each(['display', 'raw'] as const)('only preserves a pending %s identity for semantic reads', async view => {
    const value = result()
    const item = value.items[0]!
    item.content_availability = 'preparing'
    item.contents = []
    const reference = { version: 1, sessionKey: 'key', sessionId: 'sid', messageId: 'mid',
      source: 'active', view, revision: 'legacy-v1:active:12:1000:2:pending' }
    Object.assign(item.message, { contentRef: reference, contentMetadataPending: true })
    const page = await requestV2SessionHistory({ generation: 1, request: vi.fn().mockResolvedValue(value) },
      'key', { direction: 'latest', limit: 10, signal: new AbortController().signal }, 1)
    expect(page.messages[0]?.contentRef).toEqual(view === 'display' ? reference : undefined)
    expect(page.messages[0]?.contentAvailability).toBe('preparing')
  })

  it.each(['raw', 'display'] as const)('preserves explicit %s view and storage coordinates', async view => {
    const page = await read({ view })
    expect(page.messages[0]?.contentRef).toEqual({
      version: 1, sessionKey: 'key', sessionId: 'sid', messageId: 'mid',
      byteLength: 1024 ** 3, sha256: 'a'.repeat(64),
      revision: 'legacy-v1:compacted:12:1000:2:16735', source: 'compacted', view,
    })
  })

  it('does not infer raw view from a digest when the wire omits view', async () => {
    const page = await read({})
    expect(page.messages[0]?.contentRef).not.toHaveProperty('view')
  })

  it.each([{ view: 'unsafe' }, { source: 'unsafe' }])('rejects unknown view/source in the generated validator', async invalid => {
    await expect(read(invalid as Partial<ContentRef>)).rejects.toThrow('violated its generated v4 Contract')
  })
})
