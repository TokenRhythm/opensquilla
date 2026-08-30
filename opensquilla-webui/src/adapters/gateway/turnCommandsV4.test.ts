import { describe, expect, it, vi } from 'vitest'

import { CHAT_ABORT_METHOD } from '@/contracts/generated/v4/chatAbort'
import { CHAT_SEND_METHOD } from '@/contracts/generated/v4/chatSend'
import { SESSIONS_PENDING_INPUTS_DISPATCH_METHOD } from '@/contracts/generated/v4/pendingInputsDispatch'
import { SESSIONS_PENDING_INPUTS_STEER_METHOD } from '@/contracts/generated/v4/pendingInputsSteer'
import { SESSIONS_STEER_V2_METHOD } from '@/contracts/generated/v4/sessionsSteerV2'

import {
  createV4TurnCommands,
  TurnCommandContractError,
  toWireSendParams,
} from './turnCommandsV4'
import type { TurnCommandsTransport } from './turnCommandsV4'

describe('v4 TurnCommands Adapter', () => {
  it('maps semantic admission to chat.send without changing the payload', async () => {
    const request = vi.fn(async <T = unknown>() => (
      {
        session_key: 'agent:main:test',
        message_id: 'message-1',
        user_message_id: 'user-1',
        client_message_id: 'client-1',
        task_id: 'task-1',
        task_status: 'queued',
        unknown_extension: { preserved: true },
      } as T
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
      messageId: 'message-1',
      userMessageId: 'user-1',
      clientMessageId: 'client-1',
      taskId: 'task-1',
      taskStatus: 'queued',
      metadata: { unknown_extension: { preserved: true } },
    })
    expect(request).toHaveBeenCalledWith(CHAT_SEND_METHOD, params)
  })

  it('projects canonical send fields to the v4 source alias at the adapter boundary', () => {
    const params = toWireSendParams({
      message: 'hello',
      sessionKey: 'agent:main:test',
      clientRequestId: 'request-1',
      clientMessageId: 'message-1',
      promptAnnotationIds: ['annotation-1'],
      documentContext: { documentId: 'doc-1', headRevisionId: 'rev-1' },
      source: { elevated: 'operator', runMode: 'safe' },
      intent: 'new_chat',
      workspaceId: 'workspace-1',
      collaborationMode: 'plan',
      initialRoutingMode: 'router',
      forkBeforeMessageId: 'message-0',
      displayText: 'display',
      attachments: [{ type: 'text/plain', mime: 'text/plain', name: 'note.txt' }],
      queueMode: 'followup',
      extension: { preserved: true },
    })

    expect(params).toEqual({
      message: 'hello',
      sessionKey: 'agent:main:test',
      clientRequestId: 'request-1',
      clientMessageId: 'message-1',
      promptAnnotationIds: ['annotation-1'],
      documentContext: { documentId: 'doc-1', headRevisionId: 'rev-1' },
      _source: { elevated: 'operator', runMode: 'safe' },
      intent: 'new_chat',
      workspaceId: 'workspace-1',
      collaborationMode: 'plan',
      initialRoutingMode: 'router',
      forkBeforeMessageId: 'message-0',
      displayText: 'display',
      attachments: [{ type: 'text/plain', mime: 'text/plain', name: 'note.txt' }],
      queueMode: 'followup',
      extension: { preserved: true },
    })
    expect(params).not.toHaveProperty('source')
  })

  it('keeps a legacy handoff source when no canonical source is present', () => {
    expect(toWireSendParams({
      message: 'replay',
      sessionKey: 'agent:main:test',
      _source: { runMode: 'full' },
    })).toMatchObject({
      message: 'replay',
      sessionKey: 'agent:main:test',
      _source: { runMode: 'full' },
    })
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
      SESSIONS_PENDING_INPUTS_DISPATCH_METHOD,
      params,
    )
  })

  it('keeps abort scoped to the semantic request and chooses pending steer by identity', async () => {
    const request = vi.fn(async <T = unknown>() => (
      { accepted: true, aborted: true } as T
    )) as TurnCommandsTransport['request']
    const commands = createV4TurnCommands({
      request,
      supports: method => method !== SESSIONS_PENDING_INPUTS_STEER_METHOD,
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
      expectedTurnId: 'turn-1',
      clientRequestId: 'request-2',
      clientMessageId: 'message-2',
      pendingInputId: 'pending-1',
    })
    expect(request).toHaveBeenNthCalledWith(
      1,
      CHAT_ABORT_METHOD,
      {
        sessionKey: 'agent:main:test',
        taskId: 'task-1',
        scope: 'task',
        source: 'webui_stop',
      },
    )
    expect(request).toHaveBeenNthCalledWith(
      2,
      SESSIONS_PENDING_INPUTS_STEER_METHOD,
      {
        key: 'agent:main:test',
        message: 'adjust',
        expected_turn_id: 'turn-1',
        client_request_id: 'request-2',
        client_message_id: 'message-2',
        pendingInputId: 'pending-1',
      },
    )
    expect(commands.supports('same-turn-steer')).toBe(true)
    expect(commands.supports('durable-steer')).toBe(false)
  })

  it('rejects a response that violates the generated result Contract', async () => {
    const request = vi.fn(async () => null) as TurnCommandsTransport['request']
    const commands = createV4TurnCommands({ request })

    await expect(commands.send({
      kind: 'new-turn',
      params: { message: 'hello', sessionKey: 'agent:main:test' },
    })).rejects.toMatchObject({
      name: 'TurnCommandContractError',
      method: CHAT_SEND_METHOD,
    })
    await expect(commands.cancel({ sessionKey: 'agent:main:test' }))
      .rejects.toBeInstanceOf(TurnCommandContractError)
  })

  it('forwards legacy malformed params so the Gateway retains its error semantics', async () => {
    const response = { ok: true, instant_accept: true }
    const request = vi.fn(async <T = unknown>() => response as T) as
      TurnCommandsTransport['request']
    const commands = createV4TurnCommands({ request })
    const malformed = { message: 42, sessionKey: 'agent:main:test' } as never

    await expect(commands.send({ kind: 'new-turn', params: malformed }))
      .resolves.toEqual({
        ok: true,
        instantAccept: true,
      })
    expect(request).toHaveBeenCalledWith(CHAT_SEND_METHOD, malformed)
  })

  it('validates all steer result variants while preserving method selection', async () => {
    const request = vi.fn(async <T = unknown>() => ({
      status: 'accepted',
      accepted: true,
      session_key: 'agent:main:test',
      expected_turn_id: 'turn-1',
      client_request_id: 'request-1',
      client_message_id: 'message-1',
      user_message_id: 'user-message-1',
      turn_id: 'turn-1',
      disposition: 'steering',
      fallback_safe: true,
      unknown_extension: 'kept',
    } as T)) as TurnCommandsTransport['request']
    const commands = createV4TurnCommands({ request })

    await expect(commands.steer({
      key: 'agent:main:test',
      message: 'adjust',
      expectedTurnId: 'turn-1',
      clientRequestId: 'request-1',
      clientMessageId: 'message-1',
    })).resolves.toMatchObject({
      accepted: true,
      sessionKey: 'agent:main:test',
      expectedTurnId: 'turn-1',
      clientRequestId: 'request-1',
      clientMessageId: 'message-1',
      userMessageId: 'user-message-1',
      turnId: 'turn-1',
      fallbackSafe: true,
      metadata: { unknown_extension: 'kept' },
    })
    await expect(commands.steer({
      key: 'agent:main:test',
      message: 'queued adjustment',
      expectedTurnId: 'turn-1',
      clientRequestId: 'request-2',
      clientMessageId: 'message-2',
      pendingInputId: 'pending-1',
      requestFingerprint: 'fingerprint-1',
      expectedRevision: 1,
    })).resolves.toMatchObject({ accepted: true })
    expect(request).toHaveBeenNthCalledWith(1, SESSIONS_STEER_V2_METHOD, expect.any(Object))
    expect(request).toHaveBeenNthCalledWith(2, SESSIONS_PENDING_INPUTS_STEER_METHOD, expect.any(Object))
  })
})
