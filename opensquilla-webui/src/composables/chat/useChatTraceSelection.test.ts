import { effectScope, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import type { ChatMessage, ChatRunStatus } from '@/types/chat'
import { useChatTraceSelection } from './useChatTraceSelection'

function harness() {
  const scope = effectScope()
  const sessionKey = ref('agent:main:webchat:example')
  const messages = ref<ChatMessage[]>([])
  const runStatus = ref<ChatRunStatus>({ status: 'idle', label: '', task: null })
  const isStreaming = ref(false)
  const api = scope.run(() => useChatTraceSelection({ sessionKey, messages, runStatus, isStreaming }))!
  return { scope, sessionKey, messages, runStatus, isStreaming, ...api }
}

describe('chat trace selection', () => {
  it('opens another session in conversation and preserves the chosen view during streaming', () => {
    const h = harness()
    h.view.value = 'trace'
    h.sessionKey.value = 'agent:main:webchat:new-draft'
    expect(h.view.value).toBe('conversation')

    h.messages.value = [{ role: 'user', text: 'New request', ts: null, turnId: 'new-turn' }]
    h.runStatus.value = { status: 'running', label: '', task: { turn_id: 'new-turn' } }
    h.isStreaming.value = true
    h.messages.value.push({ role: 'assistant', text: 'Partial answer', ts: null })
    expect(h.view.value).toBe('conversation')
    expect(h.turnId.value).toBe('new-turn')

    h.view.value = 'trace'
    h.messages.value[1]!.text = 'Continuing answer'
    expect(h.view.value).toBe('trace')
    h.view.value = 'conversation'
    h.messages.value[1]!.turnId = 'new-turn'
    h.runStatus.value = { status: 'idle', label: '', task: null }
    h.isStreaming.value = false
    expect(h.view.value).toBe('conversation')
    h.scope.stop()
  })

  it('follows an accepted live turn before its transcript arrives, and retains it after completion', () => {
    const h = harness()
    h.messages.value = [{ role: 'user', text: 'Earlier request', ts: null, turnId: 'earlier' }]
    h.runStatus.value = { status: 'running', label: '', task: { task_id: 'current' } }
    h.isStreaming.value = true
    expect(h.turnId.value).toBe('current')
    expect(h.selectedRunning.value).toBe(true)
    h.messages.value.push({ role: 'user', text: 'Current request', ts: null, turnId: 'current' })
    h.messages.value.push({ role: 'assistant', text: 'Done', ts: null, turnId: 'current' })
    h.runStatus.value = { status: 'idle', label: '', task: null }
    h.isStreaming.value = false
    expect(h.turnId.value).toBe('current')
    expect(h.selectedRunning.value).toBe(false)
    expect(h.turns.value).toHaveLength(2)
    expect(h.turns.value[1]?.preview).toBe('Current request')
    h.scope.stop()
  })

  it('keeps a historical selection while another turn runs, and resets it across sessions', () => {
    const h = harness()
    h.messages.value = [{ role: 'user', text: 'Previous request', ts: null, turnId: 'previous' }]
    h.selectedTurn.value = 'previous'
    h.runStatus.value = { status: 'running', label: '', task: { turn_id: 'next' } }
    expect(h.turnId.value).toBe('previous')
    expect(h.selectedRunning.value).toBe(false)
    h.selectedTurn.value = ''
    expect(h.turnId.value).toBe('next')
    h.selectedTurn.value = 'previous'
    h.sessionKey.value = 'agent:main:webchat:other'
    h.messages.value = []
    h.runStatus.value = { status: 'idle', label: '', task: null }
    expect(h.selectedTurn.value).toBe('')
    expect(h.turnId.value).toBe('')
    h.scope.stop()
  })

  it('uses persisted turn identity and does not mistake a message id for a trace', () => {
    const h = harness()
    h.messages.value = [
      { role: 'user', text: 'Legacy message', ts: null, messageId: 'message-only' },
      { role: 'assistant', text: 'Answer', ts: null, turnId: 'persisted-turn' },
    ]
    expect(h.turns.value.map(turn => turn.id)).toEqual(['persisted-turn'])
    expect(h.turnId.value).toBe('persisted-turn')
    h.scope.stop()
  })

  it('keeps the accepted turn live as assistant output is appended without task metadata', () => {
    const h = harness()
    h.messages.value = [{ role: 'user', text: 'Current request', ts: null, turnId: 'accepted' }]
    h.isStreaming.value = true
    h.runStatus.value = { status: 'running', label: '', task: null }
    expect(h.turnId.value).toBe('accepted')
    h.messages.value.push({ role: 'assistant', text: 'Partial', ts: null })
    expect(h.turnId.value).toBe('accepted')
    expect(h.selectedRunning.value).toBe(true)
    h.messages.value[1].text = 'Partial output continues'
    expect(h.turnId.value).toBe('accepted')
    h.messages.value[1].turnId = 'accepted'
    h.runStatus.value = { status: 'idle', label: '', task: null }
    h.isStreaming.value = false
    expect(h.turnId.value).toBe('accepted')
    expect(h.selectedRunning.value).toBe(false)
    h.scope.stop()
  })

  it('does not show the previous trace as live while a new input awaits its turn id', () => {
    const h = harness()
    h.messages.value = [
      { role: 'assistant', text: 'Earlier answer', ts: null, turnId: 'previous' },
      { role: 'user', text: 'New request', ts: null, clientId: 'optimistic' },
    ]
    h.runStatus.value = { status: 'running', label: '', task: { status: 'running' } }
    h.isStreaming.value = true
    expect(h.turnId.value).toBe('')
    expect(h.selectedRunning.value).toBe(false)
    h.messages.value.push({ role: 'assistant', text: 'Preparing', ts: null })
    expect(h.turnId.value).toBe('')
    h.messages.value[1]!.turnId = 'accepted'
    expect(h.turnId.value).toBe('accepted')
    expect(h.selectedRunning.value).toBe(true)
    h.scope.stop()
  })
})
