// @vitest-environment happy-dom
import { createApp, defineComponent, onUnmounted } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import type { ChatRenderedMessage } from '@/types/chat'
import {
  createForkTransitionLifetime,
  forkRpcRequest,
  forkNavigationPhase,
  forkRouteHandoffAction,
  snapshotForkPreviewMessages,
  validatedForkChildKey,
} from './forkTransition'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function row(
  id: string,
  displayRole: 'user' | 'assistant',
  turnId: string,
): ChatRenderedMessage {
  return {
    id,
    messageId: id,
    turnKey: `turn:${turnId}`,
    role: displayRole,
    displayRole,
    roleLabel: displayRole,
    text: id,
    timeStr: '',
    showHeader: false,
    turnOutcome: { turnId, status: 'completed' },
  }
}

describe('snapshotForkPreviewMessages', () => {
  it('keeps the assistant answer when the outcome is attached to every row in the turn', () => {
    const source = [
      row('user-old', 'user', 'turn-old'),
      row('assistant-old', 'assistant', 'turn-old'),
      row('user-new', 'user', 'turn-new'),
      row('assistant-new', 'assistant', 'turn-new'),
    ]

    const preview = snapshotForkPreviewMessages(source, 'turn-old')

    expect(preview.map(message => message.messageId)).toEqual([
      'user-old',
      'assistant-old',
    ])
    expect(preview[1]).not.toBe(source[1])
  })

  it('uses the dedicated through-turn method and exact payload', () => {
    expect(forkRpcRequest('parent-key', 'turn-7')).toEqual({
      method: 'sessions.forkThroughTurn',
      params: { key: 'parent-key', throughTurnId: 'turn-7' },
    })
    expect(forkRpcRequest('parent-key')).toEqual({
      method: 'sessions.fork',
      params: { key: 'parent-key' },
    })
  })

  it('requires a matching through-turn response echo before accepting the child', () => {
    expect(validatedForkChildKey({
      key: 'child-key',
      forkMode: 'through_turn',
      throughTurnId: 'turn-7',
    }, 'turn-7')).toBe('child-key')
    expect(() => validatedForkChildKey({
      key: 'child-key',
      forkMode: 'full',
      throughTurnId: 'turn-7',
    }, 'turn-7')).toThrow(/confirm the requested turn boundary/)
    expect(() => validatedForkChildKey({
      key: 'child-key',
      forkMode: 'through_turn',
      throughTurnId: 'turn-other',
    }, 'turn-7')).toThrow(/confirm the requested turn boundary/)
  })

  it('keeps the preview in returning state until the parent target is ready', () => {
    expect(forkNavigationPhase('parent-key', 'parent-key')).toBe('returning')
    expect(forkNavigationPhase('child-key', 'parent-key')).toBe('opening')
  })

  it('converts native Back to the parent into a returning hand-off', () => {
    const transition = {
      parentKey: 'parent-key',
      targetKey: 'child-key',
      phase: 'opening' as const,
    }
    expect(forkRouteHandoffAction('parent-key', transition)).toBe('returning')
    expect(forkRouteHandoffAction('child-key', transition)).toBe('keep')
    expect(forkRouteHandoffAction('other-key', transition)).toBe('clear')
    expect(forkRouteHandoffAction('parent-key', {
      ...transition,
      targetKey: 'parent-key',
      phase: 'creating',
    })).toBe('clear')
  })
})

describe('fork transition lifetime', () => {
  it('drops a deferred RPC continuation after its mounted owner unmounts', async () => {
    const lifetime = createForkTransitionLifetime()
    const rpc = deferred<{ key: string }>()
    const mutateState = vi.fn()
    const routerPush = vi.fn()
    const pushToast = vi.fn()
    let generation = 0

    const Owner = defineComponent({
      setup() {
        generation = lifetime.begin()
        onUnmounted(() => lifetime.dispose())
        void rpc.promise.then(() => {
          if (!lifetime.isCurrent(generation)) return
          mutateState()
          routerPush()
          pushToast()
        })
        return () => null
      },
    })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(Owner)
    app.mount(host)

    app.unmount()
    rpc.resolve({ key: 'child-key' })
    await rpc.promise
    await Promise.resolve()

    expect(lifetime.isCurrent(generation)).toBe(false)
    expect(mutateState).not.toHaveBeenCalled()
    expect(routerPush).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
    host.remove()
  })
})
