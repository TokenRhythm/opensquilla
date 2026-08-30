import { describe, expect, it, vi } from 'vitest'

import { createV4TurnCommands } from './turnCommandsV4'
import type { TurnCommandsTransport } from './turnCommandsV4'

describe('v4 TurnCommands Adapter', () => {
  it('maps semantic admission to chat.send without changing the payload', async () => {
    const request = vi.fn(async <T = unknown>() => (
      { sessionKey: 'agent:main:test', task_id: 'task-1' } as T
    )) as TurnCommandsTransport['request']
    const commands = createV4TurnCommands({ request, supports: () => true })
    const params = {
      message: 'hello',
      sessionKey: 'agent:main:test',
      clientRequestId: 'request-1',
      queueMode: 'followup',
    }

    await expect(commands.send({ kind: 'new-turn', params })).resolves.toEqual({
      sessionKey: 'agent:main:test',
      task_id: 'task-1',
    })
    expect(request).toHaveBeenCalledWith('chat.send', params)
  })

  it('selects the durable pending-input endpoint only for staged admission', async () => {
    const request = vi.fn(async <T = unknown>() => ({ accepted: true } as T)) as
      TurnCommandsTransport['request']
    const commands = createV4TurnCommands({ request, supports: () => true })
    const params = {
      key: 'agent:main:test',
      pendingInputId: 'pending-1',
      clientRequestId: 'request-1',
      requestFingerprint: 'fingerprint-1',
    }

    await commands.send({ kind: 'pending-input', params })
    expect(request).toHaveBeenCalledWith(
      'sessions.pending_inputs.dispatch',
      params,
    )
  })

  it('keeps abort scoped to the semantic request and chooses pending steer by identity', async () => {
    const request = vi.fn(async <T = unknown>() => (
      { accepted: true, aborted: true } as T
    )) as TurnCommandsTransport['request']
    const commands = createV4TurnCommands({
      request,
      supports: method => method !== 'sessions.pending_inputs.steer',
    })
    await commands.cancel({
      sessionKey: 'agent:main:test',
      taskId: 'task-1',
      scope: 'task',
      source: 'webui_stop',
    })
    await commands.steer({
      key: 'agent:main:test',
      message: 'adjust',
      expected_turn_id: 'turn-1',
      client_request_id: 'request-2',
      client_message_id: 'message-2',
      pendingInputId: 'pending-1',
    })
    expect(request).toHaveBeenNthCalledWith(
      1,
      'chat.abort',
      {
        sessionKey: 'agent:main:test',
        taskId: 'task-1',
        scope: 'task',
        source: 'webui_stop',
      },
    )
    expect(request).toHaveBeenNthCalledWith(
      2,
      'sessions.pending_inputs.steer',
      expect.objectContaining({ pendingInputId: 'pending-1' }),
    )
    expect(commands.supports('same-turn-steer')).toBe(true)
    expect(commands.supports('durable-steer')).toBe(false)
  })
})
