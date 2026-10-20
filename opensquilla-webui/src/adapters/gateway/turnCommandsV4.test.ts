import { describe, expect, it, vi } from 'vitest'

import { CHAT_ABORT_METHOD } from '@/contracts/generated/v4/chatAbort'
import { CHAT_SEND_METHOD } from '@/contracts/generated/v4/chatSend'
import { CHAT_SEND_V2_METHOD } from '@/contracts/generated/v4/chatSendV2'
import { TURNS_RECEIPT_RESOLVE_METHOD } from '@/contracts/generated/v4/turnsReceiptResolve'
import { SESSIONS_PENDING_INPUTS_DISPATCH_METHOD } from '@/contracts/generated/v4/pendingInputsDispatch'
import { SESSIONS_PENDING_INPUTS_STEER_METHOD } from '@/contracts/generated/v4/pendingInputsSteer'
import { SESSIONS_STEER_V2_METHOD } from '@/contracts/generated/v4/sessionsSteerV2'
import { TURNS_RECEIPT_GET_METHOD } from '@/contracts/generated/v4/turnsReceiptGet'

import {
  createV4TurnCommands,
  TurnCommandContractError,
  toWireSendParams,
} from './turnCommandsV4'
import type { TurnCommandsTransport } from './turnCommandsV4'
import type { TurnReceiptRequest } from '@/modules/turnCommands'

describe('explicit local path display metadata', () => {
  it('copies references without replacing the complete provider text', () => {
    const paths = ['C:\\资料\\book.pdf']
    const text = `Summarize\n${paths[0]}`
    const wire = toWireSendParams({ message: text, sessionKey: 'session', localPathReferences: paths })
    expect(wire.message).toBe(text)
    expect(wire.localPathReferences).toEqual(paths)
    expect(wire.localPathReferences).not.toBe(paths)
  })

  it('drops mismatched metadata but preserves manually typed text', () => {
    const text = 'C:\\different.pdf'
    expect(toWireSendParams({ message: text, sessionKey: 'session', localPathReferences: ['C:\\book.pdf'] }))
      .toEqual({ message: text, sessionKey: 'session' })
  })
})

describe('read-only delivery receipt adapter', () => {
  const OLD_EPOCH = `v2.${'a'.repeat(32)}.100.125`
  const CURRENT_EPOCH = `v2.${'b'.repeat(32)}.100.125`
  const wireId = `tr2:${OLD_EPOCH}:request-1`
  const send: TurnReceiptRequest = { kind: 'send', request: { kind: 'new-turn', params: {
    message: 'frozen text', sessionKey: 'source', clientRequestId: 'request-1',
    attachments: [{ type: 'text/plain', mime: 'text/plain', name: 'note.txt', file_uuid: 'original-token' }],
    source: { runMode: 'safe' },
  } } }
  const found = () => ({ status: 'found', accepted: true, requestFingerprint: `sha256:${'a'.repeat(64)}`, receipt: {
    requestSessionKey: 'source', sessionKey: 'target', sessionId: 'incarnation-1',
    clientRequestId: 'request-1', messageId: 'message-1', sessionEpoch: 2,
    taskId: 'task-1', taskStatus: 'running',
  } })

  it('pins the versioned admission route and refuses downgrade without sending a legacy frame', async () => {
    let supported = true
    const request = vi.fn().mockResolvedValue({ ok: true, taskId: 'task-1', sessionKey: 'source', client_request_id: wireId })
    const commands = createV4TurnCommands({ request, supports: () => supported, policy: { turn_receipt_epoch: CURRENT_EPOCH } })
    const original = { kind: 'new-turn' as const, acceptanceProtocol: 'resolvable-v1' as const,
      acceptanceEpoch: OLD_EPOCH,
      params: { message: 'frozen', sessionKey: 'source', clientRequestId: 'request-1' } }
    expect(commands.supportsResolvableSend?.()).toBe(true)
    await expect(commands.send(original)).resolves.toMatchObject({ metadata: { client_request_id: 'request-1' } })
    expect(request.mock.calls[0]?.[0]).toBe(CHAT_SEND_V2_METHOD)
    expect(request.mock.calls[0]?.[1]).toEqual({ ...original.params, clientRequestId: wireId, acceptanceEpoch: OLD_EPOCH })
    supported = false
    await expect(commands.send(original)).rejects.toMatchObject({ failureCode: 'RESOLVABLE_SEND_UNSUPPORTED', accepted: null })
    expect(request).toHaveBeenCalledTimes(1)
  })

  it.each(['retired', 'accepted_unavailable'] as const)('validates the %s terminal identity and uses explicit resolution as a write', async status => {
    const result = { status, accepted: status !== 'retired', clientRequestId: wireId, requestSessionKey: 'source',
      requestFingerprint: `sha256:${'a'.repeat(64)}` }
    const request = vi.fn().mockResolvedValue(result)
    const commands = createV4TurnCommands({ request, supports: () => true, policy: { turn_receipt_epoch: CURRENT_EPOCH } })
    const query: TurnReceiptRequest = { kind: 'send', request: { kind: 'new-turn', acceptanceProtocol: 'resolvable-v1',
      acceptanceEpoch: OLD_EPOCH,
      params: { message: 'frozen', sessionKey: 'source', clientRequestId: 'request-1' } } }
    await expect(commands.resolveReceipt?.(query, { expectedGeneration: 7 })).resolves.toEqual({
      status: status === 'retired' ? 'retired' : 'accepted-unavailable',
    })
    expect(request).toHaveBeenCalledWith(TURNS_RECEIPT_RESOLVE_METHOD,
      { operation: CHAT_SEND_V2_METHOD, originalRequest: { ...query.request.params, clientRequestId: wireId, acceptanceEpoch: OLD_EPOCH } }, { expectedGeneration: 7 })
    request.mockResolvedValue({ ...result, clientRequestId: 'another-request' })
    await expect(commands.resolveReceipt?.(query)).rejects.toBeInstanceOf(TurnCommandContractError)
    request.mockResolvedValue(result)
    await expect(commands.lookupReceipt?.(send)).rejects.toBeInstanceOf(TurnCommandContractError)
  })

  it('never resolves a legacy missing receipt or downgrades a failed resolution to send', async () => {
    const request = vi.fn().mockRejectedValue(Object.assign(new Error('unsupported'), { code: 'METHOD_NOT_FOUND' }))
    const commands = createV4TurnCommands({ request, supports: () => true, policy: { turn_receipt_epoch: OLD_EPOCH } })
    await expect(commands.resolveReceipt?.(send)).resolves.toEqual({ status: 'unsupported' })
    expect(request).not.toHaveBeenCalled()
    await expect(commands.resolveReceipt?.({ kind: 'send', request: { kind: 'new-turn', acceptanceProtocol: 'resolvable-v1',
      acceptanceEpoch: OLD_EPOCH,
      params: { message: 'frozen', sessionKey: 'source', clientRequestId: 'request-1' } } })).resolves.toEqual({ status: 'unsupported' })
    expect(request.mock.calls.map(call => call[0])).toEqual([TURNS_RECEIPT_RESOLVE_METHOD])
  })

  it.each([undefined, null, '', ' ', 42, 'boot-1',
    `v3.${'a'.repeat(32)}.100.125`, `v2.${'a'.repeat(32)}.0100.125`,
    `v2.${'a'.repeat(32)}.100.126`, `v2.${'a'.repeat(32)}.9007199254740992.9007199254741017`,
  ])('does not advertise resolvable admission for an invalid Hello epoch %s', epoch => {
    const commands = createV4TurnCommands({ request: vi.fn(), supports: () => true, policy: { turn_receipt_epoch: epoch } })
    expect(commands.getReceiptEpoch?.()).toBeUndefined()
    expect(commands.supportsResolvableSend?.()).toBe(false)
  })

  it.each([OLD_EPOCH, undefined])('distinguishes closed admission from unknown history for saved epoch %s', acceptanceEpoch => {
    const request = vi.fn()
    let supported = true
    const policy: Record<string, unknown> = { turn_receipt_epoch: CURRENT_EPOCH }
    const commands = createV4TurnCommands({ request, supports: () => supported, policy })
    const query: TurnReceiptRequest = { kind: 'send', request: { kind: 'new-turn',
      acceptanceProtocol: 'resolvable-v1', acceptanceEpoch,
      params: { message: 'frozen', sessionKey: 'source', clientRequestId: 'request-1' } } }
    expect(commands.isAdmissionClosed?.(query)).toBe(true)
    expect(commands.isAdmissionClosed?.(send)).toBe(false)
    expect(commands.isAdmissionClosed?.({ kind: 'steer', request: {
      key: 'source', message: 'control', clientRequestId: 'steer-1',
      expectedTurnId: 'task-1', clientMessageId: 'message-steer-1',
    } })).toBe(false)
    expect(commands.isAdmissionClosed?.({ kind: 'send', request: { kind: 'pending-input',
      params: { key: 'source', pendingInputId: 'pending-1', clientRequestId: 'request-1', requestFingerprint: 'fingerprint-1' },
    } })).toBe(false)
    if (query.request.kind === 'new-turn') query.request.acceptanceEpoch = CURRENT_EPOCH
    expect(commands.isAdmissionClosed?.(query)).toBe(false)
    if (query.request.kind === 'new-turn') query.request.acceptanceEpoch = acceptanceEpoch
    for (const epoch of [undefined, null, '', 0]) {
      policy.turn_receipt_epoch = epoch
      expect(commands.isAdmissionClosed?.(query)).toBe(false)
    }
    policy.turn_receipt_epoch = CURRENT_EPOCH
    supported = false
    expect(commands.isAdmissionClosed?.(query)).toBe(false)
    expect(request).not.toHaveBeenCalled()
  })

  it.each([CHAT_SEND_V2_METHOD, TURNS_RECEIPT_GET_METHOD, TURNS_RECEIPT_RESOLVE_METHOD])(
    'does not infer closed admission when the current server lacks %s', missing => {
      const commands = createV4TurnCommands({ request: vi.fn(), supports: method => method !== missing,
        policy: { turn_receipt_epoch: CURRENT_EPOCH } })
      expect(commands.isAdmissionClosed?.({ kind: 'send', request: {
        kind: 'new-turn', acceptanceProtocol: 'resolvable-v1', acceptanceEpoch: OLD_EPOCH,
        params: { message: 'frozen', sessionKey: 'source', clientRequestId: 'request-1' },
      } })).toBe(false)
    },
  )

  it('never fills a saved versioned send epoch from a later Hello or composer extension', async () => {
    const request = vi.fn().mockResolvedValue({ ok: true, taskId: 'task-1', sessionKey: 'source' })
    const commands = createV4TurnCommands({ request, supports: () => true, policy: { turn_receipt_epoch: CURRENT_EPOCH } })
    await commands.send({ kind: 'new-turn', acceptanceProtocol: 'resolvable-v1', params: {
      message: 'frozen', sessionKey: 'source', clientRequestId: 'request-1', acceptanceEpoch: 'composer-epoch',
    } })
    expect(request).toHaveBeenCalledExactlyOnceWith(CHAT_SEND_V2_METHOD, {
      message: 'frozen', sessionKey: 'source', clientRequestId: 'request-1',
    })
  })

  it.each([
    { originalEpoch: undefined, currentEpoch: CURRENT_EPOCH },
    { originalEpoch: OLD_EPOCH, currentEpoch: undefined },
    { originalEpoch: 'boot-1', currentEpoch: CURRENT_EPOCH },
  ])('uses only read-only evidence when the epoch fence is missing: %j', async ({ originalEpoch, currentEpoch }) => {
    const request = vi.fn().mockResolvedValue({ status: 'not_found', accepted: null })
    const commands = createV4TurnCommands({ request, supports: () => true, policy: { turn_receipt_epoch: currentEpoch } })
    const query: TurnReceiptRequest = { kind: 'send', request: { kind: 'new-turn', acceptanceProtocol: 'resolvable-v1',
      ...(originalEpoch ? { acceptanceEpoch: originalEpoch } : {}),
      params: { message: 'frozen', sessionKey: 'source', clientRequestId: 'request-1' } } }
    await expect(commands.resolveReceipt?.(query)).resolves.toEqual({ status: 'not-found' })
    expect(request).toHaveBeenCalledExactlyOnceWith(TURNS_RECEIPT_GET_METHOD, {
      operation: CHAT_SEND_V2_METHOD, originalRequest: { ...query.request.params,
        ...(originalEpoch === OLD_EPOCH ? { clientRequestId: wireId } : {}),
        ...(originalEpoch ? { acceptanceEpoch: originalEpoch } : {}) },
    }, { recoveryClass: 'safe-read' })
    request.mockResolvedValue({ ...found(), receipt: { ...found().receipt,
      clientRequestId: originalEpoch === OLD_EPOCH ? wireId : 'request-1' } })
    await expect(commands.resolveReceipt?.(query)).resolves.toMatchObject({ status: 'found' })
    expect(request.mock.calls.every(call => call[0] === TURNS_RECEIPT_GET_METHOD)).toBe(true)
  })

  it('keeps an earlier epoch on read and resolution after restart without treating absence as retirement', async () => {
    const request = vi.fn().mockResolvedValue({ status: 'not_found', accepted: null })
    const commands = createV4TurnCommands({ request, supports: () => true, policy: { turn_receipt_epoch: CURRENT_EPOCH } })
    const query: TurnReceiptRequest = { kind: 'send', request: { kind: 'new-turn', acceptanceProtocol: 'resolvable-v1',
      acceptanceEpoch: OLD_EPOCH, params: { message: 'frozen', sessionKey: 'source', clientRequestId: 'request-1' } } }
    await expect(commands.lookupReceipt?.(query)).resolves.toEqual({ status: 'not-found' })
    await expect(commands.resolveReceipt?.(query)).resolves.toEqual({ status: 'not-found' })
    expect(request.mock.calls.map(call => call[0])).toEqual([TURNS_RECEIPT_GET_METHOD, TURNS_RECEIPT_RESOLVE_METHOD])
    for (const [, params] of request.mock.calls) {
      expect(params.originalRequest).toEqual({ ...query.request.params, clientRequestId: wireId, acceptanceEpoch: OLD_EPOCH })
    }
  })

  it('keeps overlapping server windows open without using the browser clock', () => {
    const policy = { turn_receipt_epoch: `v2.${'a'.repeat(32)}.124.149` }
    const commands = createV4TurnCommands({ request: vi.fn(), supports: () => true, policy })
    const query: TurnReceiptRequest = { kind: 'send', request: { kind: 'new-turn',
      acceptanceProtocol: 'resolvable-v1', acceptanceEpoch: OLD_EPOCH,
      params: { sessionKey: 'source', message: 'frozen', clientRequestId: 'request-1' } } }
    expect(commands.isAdmissionClosed?.(query)).toBe(false)
    policy.turn_receipt_epoch = `v2.${'a'.repeat(32)}.125.150`
    expect(commands.isAdmissionClosed?.(query)).toBe(true)
    expect(query.request).toMatchObject({ acceptanceEpoch: OLD_EPOCH })
  })

  it.each([
    {}, { client_request_id: 'request-1' },
    { client_request_id: wireId, clientRequestId: 'another-request' },
  ])('rejects an unbound versioned acknowledgement %j', async ids => {
    const request = vi.fn().mockResolvedValue({ ok: true, taskId: 'task-1', sessionKey: 'source', ...ids })
    const commands = createV4TurnCommands({ request, supports: () => true, policy: { turn_receipt_epoch: CURRENT_EPOCH } })
    await expect(commands.send({ kind: 'new-turn', acceptanceProtocol: 'resolvable-v1', acceptanceEpoch: OLD_EPOCH,
      params: { sessionKey: 'source', message: 'frozen', clientRequestId: 'request-1' },
    })).rejects.toBeInstanceOf(TurnCommandContractError)
  })

  it('refuses an oversized wire identity before transport without truncating the logical ID', async () => {
    const request = vi.fn()
    const commands = createV4TurnCommands({ request, supports: () => true })
    await expect(commands.send({ kind: 'new-turn', acceptanceProtocol: 'resolvable-v1', acceptanceEpoch: OLD_EPOCH,
      params: { sessionKey: 'source', message: 'frozen', clientRequestId: 'x'.repeat(256) },
    })).rejects.toBeInstanceOf(TurnCommandContractError)
    expect(request).not.toHaveBeenCalled()
  })

  it('looks up the frozen request as a fenced safe read and retains the authoritative target', async () => {
    const request = vi.fn().mockResolvedValue(found())
    const commands = createV4TurnCommands({ request, supports: method => method === TURNS_RECEIPT_GET_METHOD })
    const controller = new AbortController()
    expect(commands.supportsReceiptLookup?.()).toBe(true)
    await expect(commands.lookupReceipt?.(send, { signal: controller.signal, expectedGeneration: 7 })).resolves.toEqual({
      status: 'found', response: { ok: true, sessionKey: 'target', messageId: 'message-1', userMessageId: 'message-1',
        replayed: true, taskId: 'task-1', taskStatus: 'running',
        metadata: { requestFingerprint: `sha256:${'a'.repeat(64)}`, sessionId: 'incarnation-1', sessionEpoch: 2 } },
    })
    expect(request).toHaveBeenCalledExactlyOnceWith(TURNS_RECEIPT_GET_METHOD, {
      operation: CHAT_SEND_METHOD, originalRequest: {
        message: 'frozen text', sessionKey: 'source', clientRequestId: 'request-1',
        attachments: [{ type: 'text/plain', mime: 'text/plain', name: 'note.txt', file_uuid: 'original-token' }],
        _source: { runMode: 'safe' },
      },
    }, { signal: controller.signal, expectedGeneration: 7, recoveryClass: 'safe-read' })
  })

  it.each([false, undefined])('does not make any request to a Gateway without the advertised capability (%s)', async supported => {
    const request = vi.fn()
    const commands = createV4TurnCommands({ request, ...(supported !== undefined ? { supports: () => supported } : {}) })
    await expect(commands.lookupReceipt?.(send)).resolves.toEqual({ status: 'unsupported' })
    expect(request).not.toHaveBeenCalled()
  })

  it.each(['METHOD_NOT_FOUND', 'UNSUPPORTED'])('does not fall back to a send when %s is returned', async code => {
    const request = vi.fn().mockRejectedValue(Object.assign(new Error('unsupported'), { code }))
    const commands = createV4TurnCommands({ request, supports: () => true })
    await expect(commands.lookupReceipt?.(send)).resolves.toEqual({ status: 'unsupported' })
    expect(request).toHaveBeenCalledTimes(1)
    expect(request.mock.calls[0]?.[0]).toBe(TURNS_RECEIPT_GET_METHOD)
  })

  it('treats a missing or deleted receipt as unknown without admitting anything', async () => {
    const request = vi.fn().mockResolvedValue({ status: 'not_found', accepted: null })
    const commands = createV4TurnCommands({ request, supports: () => true })
    await expect(commands.lookupReceipt?.(send)).resolves.toEqual({ status: 'not-found' })
    expect(request).toHaveBeenCalledTimes(1)
  })

  it.each(['clientRequestId', 'requestSessionKey'] as const)('rejects a valid receipt for another %s', async field => {
    const value = found()
    value.receipt[field] = 'another'
    const commands = createV4TurnCommands({ request: vi.fn().mockResolvedValue(value), supports: () => true })
    await expect(commands.lookupReceipt?.(send)).rejects.toBeInstanceOf(TurnCommandContractError)
  })

  it('projects a durable Steer receipt without permitting fallback to a new turn', async () => {
    const value = found()
    const request = vi.fn().mockResolvedValue({ ...value, receipt: { ...value.receipt, steer: {
      key: 'target', session_key: 'target', session_id: 'incarnation-1', task_id: 'task-1',
      turn_id: 'task-1', client_request_id: 'request-1', client_message_id: 'client-1',
      user_message_id: 'message-1', surface_id: null, disposition: 'applied', status: 'accepted',
      accepted: true, replayed: true, revision: 3, fallback_safe: false,
    } } })
    const commands = createV4TurnCommands({ request, supports: () => true })
    await expect(commands.lookupReceipt?.({ kind: 'steer', request: {
      key: 'source', message: 'adjust', expectedTurnId: 'task-1', clientRequestId: 'request-1', clientMessageId: 'client-1',
    } })).resolves.toMatchObject({ status: 'found', response: {
      accepted: true, replayed: true, sessionKey: 'target', taskId: 'task-1',
      clientRequestId: 'request-1', disposition: 'applied', revision: 3, fallbackSafe: false,
    } })
  })

  it.each(['FINGERPRINT_CONFLICT', 'FORBIDDEN'])('propagates %s without allowing automatic submission', async code => {
    const request = vi.fn().mockRejectedValue(Object.assign(new Error('paused'), { code, accepted: null }))
    const commands = createV4TurnCommands({ request, supports: () => true })
    await expect(commands.lookupReceipt?.(send)).rejects.toMatchObject({ failureCode: code, accepted: null })
    expect(request).toHaveBeenCalledTimes(1)
  })

  it.each([
    [{ kind: 'send', request: { kind: 'pending-input', params: { key: 'source', pendingInputId: 'pending-1',
      clientRequestId: 'request-1', requestFingerprint: 'b'.repeat(64) } } }, SESSIONS_PENDING_INPUTS_DISPATCH_METHOD],
    [{ kind: 'steer', request: { key: 'source', message: 'adjust', expectedTurnId: 'turn-1',
      clientRequestId: 'request-1', clientMessageId: 'client-1' } }, SESSIONS_STEER_V2_METHOD],
    [{ kind: 'steer', request: { key: 'source', message: 'adjust', expectedTurnId: 'turn-1',
      clientRequestId: 'request-1', clientMessageId: 'client-1', pendingInputId: 'pending-1',
      requestFingerprint: 'b'.repeat(64) } }, SESSIONS_PENDING_INPUTS_STEER_METHOD],
  ] as const)('selects the original operation for %j', async (query, method) => {
    const request = vi.fn().mockResolvedValue({ status: 'not_found', accepted: null })
    const commands = createV4TurnCommands({ request, supports: () => true })
    await commands.lookupReceipt?.(query as TurnReceiptRequest)
    expect(request.mock.calls[0]?.[1]).toMatchObject({ operation: method })
    expect(request.mock.calls[0]?.[0]).toBe(TURNS_RECEIPT_GET_METHOD)
  })
})

describe('v4 TurnCommands Adapter', () => {
  it('fences an exact task Stop to the current generation and preserves an explicit older fence', async () => {
    const request = vi.fn().mockResolvedValue({ aborted: true })
    const commands = createV4TurnCommands({ request, generation: 7 })
    const stop = { sessionKey: 'session-1', taskId: 'task-1', scope: 'task' }
    await commands.cancel(stop)
    await commands.cancel(stop, { expectedGeneration: 6 })
    expect(request).toHaveBeenNthCalledWith(1, CHAT_ABORT_METHOD, stop, {
      expectedGeneration: 7, recoveryClass: 'task-control',
    })
    expect(request).toHaveBeenNthCalledWith(2, CHAT_ABORT_METHOD, stop, {
      expectedGeneration: 6, recoveryClass: 'task-control',
    })
  })

  it.each([
    { sessionKey: 'session-1' },
    { sessionKey: 'session-1', scope: 'task' },
    { sessionKey: 'session-1', taskId: 'task-1' },
    { sessionKey: 'session-1', taskId: ' ', scope: 'task' },
  ])('does not bypass recovery for an unscoped or unknown Stop %j', async stop => {
    const request = vi.fn().mockResolvedValue({ aborted: false })
    const commands = createV4TurnCommands({ request, generation: 7 })
    await commands.cancel(stop)
    expect(request).toHaveBeenCalledExactlyOnceWith(CHAT_ABORT_METHOD, stop)
  })

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

  it('preserves a narrow ordinary-turn command and rejects malformed descriptors', () => {
    const turnCommand = { name: 'project.init' as const, args: 'retain existing rules' }
    expect(toWireSendParams({ message: '/init retain existing rules', sessionKey: 's', turnCommand }))
      .toMatchObject({ turnCommand })
    expect(() => toWireSendParams({ message: '/init', sessionKey: 's',
      turnCommand: { name: 'anything', args: '' } as never }))
      .toThrow(expect.objectContaining({ failureCode: 'INVALID_REQUEST', accepted: false }))
  })

  it('validates a pinned first turn with selected skills, workspace references and imported files', async () => {
    const request = vi.fn(async <T>() => ({ sessionKey: 'agent:main:test' } as T)) as TurnCommandsTransport['request']
    const commands = createV4TurnCommands({ request, supports: () => true })
    const params = {
      message: 'edit the notes', sessionKey: 'agent:main:test',
      intent: 'new_chat', initialModel: 'model-a', initialProvider: 'provider-a', initialRoutingMode: 'direct' as const,
      selectedSkills: [{ name: 'tables', instanceId: 'skill:tables', digest: 'a'.repeat(64) }],
      requiredServices: ['desktop_browser'],
      workspaceFiles: [{ workspaceId: 'project-1', relativePath: 'docs/notes.md', name: 'notes.md', mime: 'text/markdown' }],
      attachments: [{ type: 'application/pdf', mime: 'application/pdf', name: 'original.pdf', file_uuid: 'fixture-file' }],
    }
    await commands.send({ kind: 'new-turn', params })
    expect(request).toHaveBeenCalledWith(CHAT_SEND_METHOD, params)
  })

  it.each(['documentContext', 'document_context', 'promptAnnotationIds', 'prompt_annotation_ids'])(
    'requires a fresh user decision before replaying retired %s input', key => {
      expect(() => toWireSendParams({
        message: 'old draft', sessionKey: 'session-1',
        [key]: key.toLowerCase().includes('context') ? { documentId: 'old-doc' } : ['old-annotation'],
      })).toThrow(expect.objectContaining({ failureCode: 'DOCUMENT_EDITING_RETIRED', accepted: false }))
    },
  )

  it('projects canonical send fields to the v4 source alias at the adapter boundary', () => {
    const params = toWireSendParams({
      message: 'hello',
      sessionKey: 'agent:main:test',
      clientRequestId: 'request-1',
      clientMessageId: 'message-1',
      pageContext: { resourceId: 'document:doc-1', annotations: [{ text: 'Make this larger', locatorHint: 'h1' }] },
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
      pageContext: { resourceId: 'document:doc-1', annotations: [{ text: 'Make this larger', locatorHint: 'h1' }] },
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


describe('initial model wire identity', () => {
  it('preserves legacy first-send recovery model and provider fields', () => {
    expect(toWireSendParams({
      message: 'hello', sessionKey: 'new-task', intent: 'new_chat',
      initial_model: 'model-a', initial_provider: 'openai',
    })).toEqual({
      message: 'hello', sessionKey: 'new-task', intent: 'new_chat',
      initial_model: 'model-a', initial_provider: 'openai',
    })
  })
  it('prefers explicit canonical selection over legacy recovery fields', () => {
    expect(toWireSendParams({
      message: 'hello', sessionKey: 'new-task', initialModel: 'model-b', initialProvider: 'anthropic',
      initial_model: 'model-a', initial_provider: 'openai',
    })).toEqual({
      message: 'hello', sessionKey: 'new-task', initialModel: 'model-b', initialProvider: 'anthropic',
    })
  })
})
