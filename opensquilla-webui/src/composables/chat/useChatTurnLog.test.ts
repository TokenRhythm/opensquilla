// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'

import { useChatTurnLog } from './useChatTurnLog'

function createTurnLog() {
  return useChatTurnLog({
    renderMarkdown: text => text,
    toolCallGroups: () => [],
  })
}

describe('useChatTurnLog publication', () => {
  it('does not retain a diagnostic frame snapshot in production reducer mode', () => {
    const log = createTurnLog()
    log.useReducer.value = true
    log.appendFrame({ kind: 'text', text: 'prefix', presentation: 'answer' })
    log.publish()
    log.appendFrame({ kind: 'text', text: '-suffix', presentation: 'answer' })
    log.publish()

    expect(log.events.value).toEqual([])
    expect(log.foldedTurn.value.rawText).toBe('prefix-suffix')
  })

  it('keeps immutable diagnostic frames for shadow and rollback modes', () => {
    const log = createTurnLog()
    log.useReducer.value = 'shadow'
    log.appendFrame({ kind: 'text', text: 'shadow', presentation: 'answer' })
    log.publish()

    expect(log.events.value).toHaveLength(1)
    expect(log.events.value[0]).toMatchObject({ kind: 'text', text: 'shadow' })
  })
})
