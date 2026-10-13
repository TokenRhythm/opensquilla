import { describe, expect, it } from 'vitest'
import { projectConversationEvent } from './conversationContentV4'
import { decodeConversationEvent } from './conversationEventsV4'

const wire = (patch: Record<string, unknown> = {}) => ({
  key: 'agent:main:test', task_id: 'task-1', turn_id: 'task-1', stream_seq: 8, epoch: 2,
  preview_id: 'p1', attempt_id: 'a1', tool_use_id: 'write-1', generation_epoch: 0,
  revision: 1, state: 'generating', tool_name: 'write_file', path: 'a.html', content: '<html>', truncated: false,
  ...patch,
})
const project = (patch?: Record<string, unknown>) => projectConversationEvent(
  decodeConversationEvent('session.event.tool_input_preview', wire(patch)),
)
describe('tool input preview wire projection', () => {
  it('projects the real wire contract as a typed bounded draft, never generic content', () => {
    const event = project({ content: 'prefix🚀' + 'x'.repeat(65_535) })
    expect(event).toMatchObject({ kind: 'known', semanticKind: 'tool-input-preview', payload: {
      task_id: 'task-1', generation_epoch: 0, toolInputPreview: { previewId: 'p1', attemptId: 'a1',
        toolUseId: 'write-1', state: 'generating', content: 'x'.repeat(65_535), truncated: true },
    } })
    expect(event).not.toHaveProperty('payload.content')
  })
  it.each([
    { tool_name: 'exec' }, { preview_id: '' }, { revision: 0 },
    { content: 123 }, { path: 'x'.repeat(4097) }, { state: 'running' },
  ])('rejects invalid draft fields: %j', patch => {
    expect(project(patch)).not.toHaveProperty('payload.toolInputPreview')
  })
  it('rejects a negative generation at the shared wire contract boundary', () => {
    expect(() => project({ generation_epoch: -1 })).toThrow('violated the v4 Contract')
  })
  it.each(['committed', 'discarded'])('keeps %s an explicit lifecycle fact', state => {
    expect(project({ state, content: '', revision: 2 })).toMatchObject({ payload: { toolInputPreview: { state, revision: 2 } } })
  })
})
