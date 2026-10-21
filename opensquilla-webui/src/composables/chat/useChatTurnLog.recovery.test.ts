import { describe, expect, it } from 'vitest'
import { useChatTurnLog } from './useChatTurnLog'

function turnLog() {
  return useChatTurnLog({ renderMarkdown: text => text, toolCallGroups: () => [] })
}

describe('provider call rollback', () => {
  it('restores the ordered successful prefix and keeps completed tools and artifacts', () => {
    const log = turnLog()
    log.beginProviderCall('successful')
    log.appendFrame({ kind: 'thinking', text: 'earlier reasoning', at: 1, blockId: 'r1' })
    log.appendFrame({ kind: 'text', text: 'earlier answer', presentation: 'answer' })
    log.appendFrame({ kind: 'tool-start', toolId: 't1', name: 'read_file', input: '{}', at: 2 })
    log.appendFrame({ kind: 'tool-result', toolId: 't1', name: 'read_file', input: '{}', result: 'kept result', isError: false, at: 3 })
    log.appendFrame({ kind: 'artifact', artifact: { id: 'a1', name: 'kept artifact' } })
    log.beginProviderCall('failed')
    const successfulPrefix = log.foldedTurn.value.parts.map(part => part.type)
    log.appendFrame({ kind: 'thinking', text: 'failed reasoning', at: 4, blockId: 'r2' })
    log.appendFrame({ kind: 'text', text: 'failed answer', presentation: 'answer' })
    log.appendFrame({ kind: 'tool-start', toolId: 't2', name: 'write_file', input: '{}', at: 5 })
    expect(log.rollbackProviderCall('failed')).toBe(true)
    expect(log.foldedTurn.value.parts.map(part => part.type)).toEqual(successfulPrefix)
    expect(log.foldedTurn.value.rawText).toBe('earlier answer')
    expect(log.foldedTurn.value.thinkingText).toContain('earlier reasoning')
    expect(log.foldedTurn.value.thinkingText).not.toContain('failed reasoning')
    expect(log.peekToolCall('t1')?.result).toBe('kept result')
    expect(log.peekToolCall('t2')).toBeNull()
    expect(log.foldedTurn.value.artifacts.map(artifact => artifact.id)).toEqual(['a1'])
    log.beginProviderCall('replacement')
    log.appendFrame({ kind: 'text', text: ' replacement' })
    expect(log.foldedTurn.value.rawText).toBe('earlier answer replacement')
  })

  it('does not let adjacent coalesced text mutate the saved prefix', () => {
    const log = turnLog()
    log.appendFrame({ kind: 'text', text: 'complete' })
    log.beginProviderCall('failed')
    log.appendFrame({ kind: 'text', text: ' discarded' })
    log.appendFrame({ kind: 'text', text: ' tail' })
    expect(log.rollbackProviderCall('failed')).toBe(true)
    expect(log.peekRawText()).toBe('complete')
  })

  it('requires canonical recovery after a steer moved the failed call across messages', () => {
    const log = turnLog()
    log.beginProviderCall('failed')
    log.appendFrame({ kind: 'text', text: 'partial before steer' })
    log.checkpointText()
    log.beginProviderCall('failed')
    log.appendFrame({ kind: 'text', text: 'partial after steer' })
    expect(log.rollbackProviderCall('failed')).toBe(false)
    expect(log.peekRawText()).toBe('partial after steer')
  })
})
