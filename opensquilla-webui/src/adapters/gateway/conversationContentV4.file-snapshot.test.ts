import { describe, expect, it } from 'vitest'
import { projectConversationEvent } from './conversationContentV4'
import { decodeConversationEvent } from './conversationEventsV4'

function project(fileSnapshot: unknown, isError = false, eventName = 'session.event.tool_result') {
  return projectConversationEvent(decodeConversationEvent(eventName, {
    key: 'agent:main:test', task_id: 'task-1', stream_seq: 8, epoch: 2,
    id: 'write-1', name: 'write_file', result: 'Written',
    is_error: isError, file_snapshot: fileSnapshot,
  }))
}

describe('recorded file snapshot event contract', () => {
  it('retains complete UI content while dropping unknown snapshot properties', () => {
    const snapshot = { kind: 'write', path: 'index.html', content: '<html>'.repeat(4000) }
    const event = project({ ...snapshot, unknown: 'not display metadata' })
    expect(event).toMatchObject({ kind: 'known', semanticKind: 'tool-result',
      payload: { file_snapshot: snapshot } })
    if (event.kind === 'known' && event.semanticKind === 'tool-result') expect(event.payload.file_snapshot).toEqual(snapshot)
  })

  it('preserves an explicitly empty successful write', () => {
    expect(project({ kind: 'write', path: 'empty.txt', content: '' }))
      .toMatchObject({ payload: { file_snapshot: { kind: 'write', path: 'empty.txt', content: '' } } })
  })

  it('preserves a retained reference without treating its preview as the body', () => {
    const snapshot = { kind: 'edit', path: 'large.json', content: 'preview', sha256_ref: 'a'.repeat(64), bytes: 30_000_000 }
    expect(project(snapshot)).toMatchObject({ payload: { file_snapshot: snapshot } })
    expect(project({ kind: 'write', path: 'large.txt', content: 'partial', content_unavailable: true }))
      .toMatchObject({ payload: { file_snapshot: { content: '', content_unavailable: true } } })
  })

  it('keeps only an exact legacy locator and never promotes its excerpt to full content', () => {
    const legacy_ref = { message_id: 'message-1', tool_use_id: 'write-1', sha256: 'a'.repeat(64), original_chars: 16000 }
    expect(project({ kind: 'write', path: 'snake.html', content: 'excerpt', legacy_ref: { ...legacy_ref, replay: 'private' } }))
      .toMatchObject({ payload: { file_snapshot: { kind: 'write', path: 'snake.html', content: '', legacy_ref } } })
    for (const ref of [{ ...legacy_ref, message_id: '' }, { ...legacy_ref, sha256: 'bad' }, { ...legacy_ref, original_chars: -1 }]) {
      expect(project({ kind: 'write', path: 'snake.html', content: '', legacy_ref: ref }))
        .not.toHaveProperty('payload.file_snapshot')
    }
  })

  it.each([
    { kind: 'read', path: 'a.txt', content: 'not a mutation' },
    { kind: 'write', path: '', content: 'missing path' },
    { kind: 'write', path: 'a.txt' },
    { kind: 'edit', path: 'a.txt', content: null },
    ['write', 'a.txt', 'array'],
    { kind: 'write', path: 'a.txt', content: 'prefix', sha256_ref: '../other', bytes: 100 },
    { kind: 'write', path: 'a.txt', content: 'prefix', sha256_ref: 'a'.repeat(64) },
  ])('ignores invalid display data: %j', snapshot => {
    const event = project(snapshot)
    if (event.kind === 'known' && event.semanticKind === 'tool-result') expect(event.payload.file_snapshot).toBeUndefined()
    else throw new Error('Expected the valid tool result to remain visible')
  })

  it('does not accept an applied snapshot from an error or an unfinished invocation', () => {
    const snapshot = { kind: 'edit', path: 'a.txt', content: '-old\n+new' }
    for (const event of [project(snapshot, true), project(snapshot, false, 'session.event.tool_use_start')]) {
      if (event.kind === 'known' && (event.semanticKind === 'tool-result' || event.semanticKind === 'tool-use-started')) {
        expect(event.payload.file_snapshot).toBeUndefined()
      }
      else throw new Error('Expected a known tool event')
    }
  })
})
