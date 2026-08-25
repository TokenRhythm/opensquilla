// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'

import { useChatMessageActions, type UseChatMessageActionsOptions } from './useChatMessageActions'
import { useChatTextRendering } from './useChatTextRendering'
import type {
  ChatMessage,
  ChatRenderedMessage,
  ChatTurnOutcome,
  DisplayAttachment,
} from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'

vi.mock('@/utils/browser', () => ({
  copyTextWithFallback: vi.fn().mockResolvedValue(undefined),
}))

function renderedMessage(overrides: Partial<ChatRenderedMessage>): ChatRenderedMessage {
  return {
    role: 'user',
    displayRole: 'user',
    roleLabel: 'User',
    text: '',
    timeStr: '',
    showHeader: false,
    ...overrides,
  }
}

function safeUsageOutcome(
  turnId: string,
  userMessageId = 'msg-user',
): ChatTurnOutcome {
  return {
    turnId,
    status: 'failed',
    errorClass: 'usage_accounting_busy',
    retryable: true,
    usageCallIndex: 1,
    noPriorProviderDispatch: true,
    replaySafe: true,
    userMessageId,
  }
}

function displayAttachment(kind: DisplayAttachment['kind']): DisplayAttachment {
  return {
    kind,
    displayId: `history:${kind}`,
    renderKey: `history:${kind}`,
    name: `${kind}.txt`,
    mime: 'text/plain',
    ...(kind === 'inline' ? { downloadData: 'cmVxdWVzdA==' } : {}),
    ...(kind === 'staged' ? { sha256_ref: 'a'.repeat(64) } : {}),
  }
}

function makeOptions(
  messages: ChatMessage[],
  sanitizeCopyText: (
    text: string,
    opts?: { assistantBoundary?: boolean },
  ) => string = text => text,
  aiGeneratedLabel?: () => string,
) {
  const pendingForkBeforeMessageId = ref<string | null>(null)
  const options: UseChatMessageActionsOptions = {
    messages: ref(messages),
    inputText: ref(''),
    isStreaming: ref(false),
    sanitizeCopyText,
    stripTimePrefix: text => text,
    autoResizeTextarea: vi.fn(),
    sendCurrentInput: vi.fn(),
    sendUsageBarrierReplay: vi.fn(async () => true),
    focusComposer: vi.fn(),
    pendingForkBeforeMessageId,
    aiGeneratedLabel,
    notifyMessagePending: vi.fn(),
    canDeliver: () => true,
    notifyDeliveryBlocked: vi.fn(),
  }
  return { api: useChatMessageActions(options), options, pendingForkBeforeMessageId }
}

beforeEach(() => {
  vi.mocked(copyTextWithFallback).mockClear()
})

describe('useChatMessageActions branching edits', () => {
  it('records the edited user message id before trimming local history', () => {
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
      { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
      { role: 'assistant', text: 'ack B', ts: null, messageId: 'msg-b1' },
    ])

    api.editMessage(renderedMessage({
      role: 'user',
      displayRole: 'user',
      sourceIndex: 2,
      messageId: 'msg-B',
      text: 'B',
    }))

    expect(pendingForkBeforeMessageId.value).toBe('msg-B')
    expect(options.messages.value.map(message => message.text)).toEqual(['A', 'ack A'])
    expect(options.inputText.value).toBe('B')
    expect(options.focusComposer).toHaveBeenCalledOnce()
  })

  it('records the previous user message id before regenerating', async () => {
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
      { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
      { role: 'assistant', text: 'ack B', ts: null, messageId: 'msg-b1' },
      { role: 'user', text: 'C', ts: null, messageId: 'msg-C' },
    ])

    const accepted = await api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 3,
      messageId: 'msg-b1',
      text: 'ack B',
    }))
    await nextTick()

    expect(pendingForkBeforeMessageId.value).toBe('msg-B')
    expect(options.messages.value.map(message => message.text)).toEqual(['A', 'ack A'])
    expect(options.inputText.value).toBe('B')
    expect(options.sendCurrentInput).toHaveBeenCalledOnce()
    expect(accepted).toBe(true)
  })

  it('dispatches usage replay directly even when Goal/Replan blocks the composer', async () => {
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      {
        role: 'user',
        text: 'bill this safely',
        ts: null,
        messageId: 'msg-user',
        turnId: 'turn-1',
      },
      {
        role: 'assistant',
        text: '',
        ts: null,
        messageId: 'terminal-activity:task-1',
        turnId: 'turn-1',
      },
      {
        role: 'error',
        text: 'Usage accounting temporarily unavailable.',
        ts: null,
        messageId: 'terminal-error:task-1',
        errorCode: 'usage_accounting_busy',
        turnId: 'turn-1',
      },
    ])
    options.inputText.value = 'unrelated draft'
    options.canDeliver = () => false

    const accepted = await api.regenerateMessage(renderedMessage({
      role: 'error',
      displayRole: 'error',
      sourceIndex: 2,
      messageId: 'terminal-error:task-1',
      errorCode: 'usage_accounting_busy',
      turnId: 'turn-1',
      turnOutcome: safeUsageOutcome('turn-1'),
      text: 'Usage accounting temporarily unavailable.',
    }))
    await nextTick()

    expect(options.sendUsageBarrierReplay).toHaveBeenCalledWith({
      text: 'bill this safely',
      forkBeforeMessageId: 'msg-user',
    })
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.messages.value).toHaveLength(3)
    expect(options.inputText.value).toBe('unrelated draft')
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
    expect(accepted).toBe(true)
  })

  it.each(['inline', 'staged', 'file'] as const)(
    'rejects programmatic whole-turn retry when the primary request has a %s display attachment',
    async (kind) => {
      const messages: ChatMessage[] = [
        {
          role: 'user',
          text: 'request with attachment',
          ts: null,
          messageId: 'msg-primary',
          turnId: 'turn-attachment',
          attachments: [displayAttachment(kind)],
        },
        {
          role: 'error',
          text: 'Usage accounting temporarily unavailable.',
          ts: null,
          messageId: 'terminal-error:attachment',
          errorCode: 'usage_accounting_busy',
          turnId: 'turn-attachment',
        },
      ]
      const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)
      options.inputText.value = 'unrelated draft'

      const accepted = api.regenerateMessage(renderedMessage({
        role: 'error',
        displayRole: 'error',
        sourceIndex: 1,
        messageId: 'terminal-error:attachment',
        errorCode: 'usage_accounting_busy',
        turnId: 'turn-attachment',
        turnOutcome: safeUsageOutcome('turn-attachment', 'msg-primary'),
        text: 'Usage accounting temporarily unavailable.',
      }))
      await nextTick()

      expect(accepted).toBe(false)
      expect(options.messages.value).toEqual(messages)
      expect(options.inputText.value).toBe('unrelated draft')
      expect(pendingForkBeforeMessageId.value).toBeNull()
      expect(options.sendUsageBarrierReplay).not.toHaveBeenCalled()
      expect(options.sendCurrentInput).not.toHaveBeenCalled()
    },
  )

  it('retries the authoritative primary user instead of a later same-turn steer', async () => {
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      {
        role: 'user',
        text: 'primary request',
        ts: null,
        messageId: 'msg-primary',
        turnId: 'turn-1',
      },
      {
        role: 'user',
        text: 'same-turn steer',
        ts: null,
        messageId: 'msg-steer',
        turnId: 'turn-1',
      },
      {
        role: 'error',
        text: 'Usage accounting temporarily unavailable.',
        ts: null,
        messageId: 'terminal-error:task-1',
        errorCode: 'usage_accounting_busy',
        turnId: 'turn-1',
      },
    ])

    const accepted = await api.regenerateMessage(renderedMessage({
      role: 'error',
      displayRole: 'error',
      sourceIndex: 2,
      messageId: 'terminal-error:task-1',
      errorCode: 'usage_accounting_busy',
      turnId: 'turn-1',
      turnOutcome: safeUsageOutcome('turn-1', 'msg-primary'),
      text: 'Usage accounting temporarily unavailable.',
    }))
    await nextTick()

    expect(accepted).toBe(true)
    expect(options.sendUsageBarrierReplay).toHaveBeenCalledWith({
      text: 'primary request',
      forkBeforeMessageId: 'msg-primary',
    })
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.messages.value).toHaveLength(3)
    expect(options.inputText.value).toBe('')
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
  })

  it('blocks an unsafe usage barrier on an assistant status-only bubble', async () => {
    const messages: ChatMessage[] = [
      {
        role: 'user',
        text: 'primary request',
        ts: null,
        messageId: 'msg-primary',
        turnId: 'turn-1',
      },
      {
        role: 'user',
        text: 'same-turn steer',
        ts: null,
        messageId: 'msg-steer',
        turnId: 'turn-1',
      },
      {
        role: 'assistant',
        text: '',
        ts: null,
        messageId: 'terminal-activity:task-1',
        turnId: 'turn-1',
      },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)

    const accepted = await api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 2,
      messageId: 'terminal-activity:task-1',
      turnId: 'turn-1',
      turnOutcome: {
        ...safeUsageOutcome('turn-1', 'msg-primary'),
        usageCallIndex: 2,
        noPriorProviderDispatch: false,
        replaySafe: false,
      },
      text: '',
    }))
    await nextTick()

    expect(accepted).toBe(false)
    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
  })

  it('blocks an assistant bubble when normalized error classes conflict around a barrier', () => {
    const messages: ChatMessage[] = [
      {
        role: 'user',
        text: 'primary request',
        ts: null,
        messageId: 'msg-primary',
        turnId: 'turn-1',
      },
      {
        role: 'user',
        text: 'same-turn steer',
        ts: null,
        messageId: 'msg-steer',
        turnId: 'turn-1',
      },
      {
        role: 'assistant',
        text: '',
        ts: null,
        messageId: 'terminal-activity:task-1',
        turnId: 'turn-1',
      },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)
    const turnOutcome = normalizeTurnOutcome({
      turn_id: 'turn-1',
      status: 'failed',
      error_class: 'provider_error',
      usage_call_index: 1,
      no_prior_provider_dispatch: true,
      replay_safe: true,
      user_message_id: 'msg-primary',
      outcome: {
        error_class: 'usage_accounting_busy',
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
        user_message_id: 'msg-primary',
      },
    })

    expect(turnOutcome).toMatchObject({
      errorClass: 'usage_accounting_busy',
      replaySafe: false,
    })
    const accepted = api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 2,
      messageId: 'terminal-activity:task-1',
      errorCode: 'provider_error',
      turnId: 'turn-1',
      turnOutcome,
      text: '',
    }))

    expect(accepted).toBe(false)
    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
  })

  it('retries the exact primary from a safe assistant status-only bubble', async () => {
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      {
        role: 'user',
        text: 'primary request',
        ts: null,
        messageId: 'msg-primary',
        turnId: 'turn-1',
      },
      {
        role: 'user',
        text: 'same-turn steer',
        ts: null,
        messageId: 'msg-steer',
        turnId: 'turn-1',
      },
      {
        role: 'assistant',
        text: '',
        ts: null,
        messageId: 'terminal-activity:task-1',
        turnId: 'turn-1',
      },
    ])

    const accepted = await api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 2,
      messageId: 'terminal-activity:task-1',
      turnId: 'turn-1',
      turnOutcome: safeUsageOutcome('turn-1', 'msg-primary'),
      text: '',
    }))
    await nextTick()

    expect(accepted).toBe(true)
    expect(options.sendUsageBarrierReplay).toHaveBeenCalledWith({
      text: 'primary request',
      forkBeforeMessageId: 'msg-primary',
    })
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.messages.value).toHaveLength(3)
    expect(options.inputText.value).toBe('')
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
  })

  it.each([
    [
      'the current page only has a previous-turn user',
      [
        {
          role: 'user',
          text: 'previous turn',
          ts: null,
          messageId: 'msg-old',
          turnId: 'turn-old',
        },
        {
          role: 'error',
          text: 'Usage accounting temporarily unavailable.',
          ts: null,
          messageId: 'terminal-error:task-new',
          errorCode: 'usage_accounting_busy',
          turnId: 'turn-new',
        },
      ] satisfies ChatMessage[],
      'turn-new',
    ],
    [
      'the error has no durable turn id',
      [
        {
          role: 'user',
          text: 'unknown turn',
          ts: null,
          messageId: 'msg-user',
          turnId: 'turn-known',
        },
        {
          role: 'error',
          text: 'Usage accounting temporarily unavailable.',
          ts: null,
          messageId: 'terminal-error:task-unknown',
          errorCode: 'usage_accounting_busy',
        },
      ] satisfies ChatMessage[],
      undefined,
    ],
    [
      'the same-turn user is not durable',
      [
        {
          role: 'user',
          text: 'previous durable turn',
          ts: null,
          messageId: 'msg-old',
          turnId: 'turn-old',
        },
        {
          role: 'user',
          text: 'same turn but pending',
          ts: null,
          clientId: 'client-new',
          turnId: 'turn-new',
        },
        {
          role: 'error',
          text: 'Usage accounting temporarily unavailable.',
          ts: null,
          messageId: 'terminal-error:task-new',
          errorCode: 'usage_accounting_busy',
          turnId: 'turn-new',
        },
      ] satisfies ChatMessage[],
      'turn-new',
    ],
  ])('fails closed when %s', async (_label, messages, turnId) => {
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)

    const accepted = api.regenerateMessage(renderedMessage({
      role: 'error',
      displayRole: 'error',
      sourceIndex: messages.length - 1,
      messageId: messages[messages.length - 1]?.messageId,
      errorCode: 'usage_accounting_busy',
      turnId,
      turnOutcome: safeUsageOutcome(turnId || '', 'missing-or-pending-user'),
      text: 'Usage accounting temporarily unavailable.',
    }))
    await nextTick()

    expect(accepted).toBe(false)
    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
  })

  it.each([
    ['missing proof', undefined],
    ['second call', {
      ...safeUsageOutcome('turn-safe'),
      usageCallIndex: 2,
    }],
    ['string call index', {
      ...safeUsageOutcome('turn-safe'),
      usageCallIndex: '1',
    } as unknown as ChatTurnOutcome],
    ['false no-prior proof', {
      ...safeUsageOutcome('turn-safe'),
      noPriorProviderDispatch: false,
    }],
    ['false replay-safe proof', {
      ...safeUsageOutcome('turn-safe'),
      replaySafe: false,
    }],
    ['missing primary user id', {
      ...safeUsageOutcome('turn-safe'),
      userMessageId: undefined,
    }],
    ['wrong primary user id', {
      ...safeUsageOutcome('turn-safe'),
      userMessageId: 'msg-steer',
    }],
    ['conflicting second-call proof', {
      ...safeUsageOutcome('turn-safe'),
      usageCallIndex: 2,
      noPriorProviderDispatch: true,
      replaySafe: true,
    }],
  ])('rejects programmatic usage retry with %s', async (_label, turnOutcome) => {
    const messages: ChatMessage[] = [
      {
        role: 'user',
        text: 'do not resend without proof',
        ts: null,
        messageId: 'msg-user',
        turnId: 'turn-safe',
      },
      {
        role: 'error',
        text: 'Usage accounting temporarily unavailable.',
        ts: null,
        messageId: 'terminal-error:task-safe',
        errorCode: 'usage_accounting_busy',
        turnId: 'turn-safe',
      },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)

    const accepted = api.regenerateMessage(renderedMessage({
      role: 'error',
      displayRole: 'error',
      sourceIndex: 1,
      messageId: 'terminal-error:task-safe',
      errorCode: 'usage_accounting_busy',
      turnId: 'turn-safe',
      turnOutcome,
      text: 'Usage accounting temporarily unavailable.',
    }))
    await nextTick()

    expect(accepted).toBe(false)
    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
  })

  it('preserves history, fork state, and the current draft when live delivery is unavailable', async () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)
    options.inputText.value = 'unrelated draft'
    options.canDeliver = () => false

    const accepted = api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'msg-a1',
      text: 'ack A',
    }))
    await nextTick()

    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('unrelated draft')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
    expect(options.notifyDeliveryBlocked).toHaveBeenCalledOnce()
    expect(accepted).toBe(false)
  })

  it('keeps an optimistic user row intact until its durable fork id arrives', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'still saving', ts: null, clientId: 'client-only' },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)

    api.editMessage(renderedMessage({
      role: 'user',
      displayRole: 'user',
      sourceIndex: 0,
      clientId: 'client-only',
      text: 'still saving',
    }))

    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.focusComposer).not.toHaveBeenCalled()
    // The refusal must be user-visible, not just a console trace: the button
    // otherwise looks dead when the chat.send ack was lost.
    expect(options.notifyMessagePending).toHaveBeenCalledOnce()
  })

  it('blocks edit while streaming with visible feedback instead of a silent no-op', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)
    options.isStreaming.value = true
    const notifyEditBlocked = vi.fn()
    options.notifyEditBlocked = notifyEditBlocked

    api.editMessage(renderedMessage({
      role: 'user',
      displayRole: 'user',
      sourceIndex: 0,
      messageId: 'msg-A',
      text: 'A',
    }))

    expect(options.messages.value.map(message => message.text)).toEqual(['A'])
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.focusComposer).not.toHaveBeenCalled()
    expect(notifyEditBlocked).toHaveBeenCalledOnce()
  })

  it('accepts retry after its durable fork id is bound later', async () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'still saving', ts: null, clientId: 'client-only' },
      { role: 'assistant', text: 'partial answer', ts: null, messageId: 'assistant-local' },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)

    const rendered = renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'assistant-local',
      text: 'partial answer',
    })
    const firstAccepted = api.regenerateMessage(rendered)
    await nextTick()

    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
    expect(options.notifyMessagePending).toHaveBeenCalledOnce()

    options.messages.value[0]!.messageId = 'msg-now-durable'
    const secondAccepted = api.regenerateMessage(rendered)
    await nextTick()

    expect(firstAccepted).toBe(false)
    expect(secondAccepted).toBe(true)
    expect(pendingForkBeforeMessageId.value).toBe('msg-now-durable')
    expect(options.sendCurrentInput).toHaveBeenCalledOnce()
  })

  it('regenerates and edits without pending feedback when ids are durable', async () => {
    const { api, options } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
    ])

    api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'msg-a1',
      text: 'ack A',
    }))
    await nextTick()

    expect(options.sendCurrentInput).toHaveBeenCalledOnce()
    expect(options.notifyMessagePending).not.toHaveBeenCalled()
  })
})

describe('useChatMessageActions protocol-shaped copy text', () => {
  it.each([
    'Document the literal `<tool_calls>` marker and keep this suffix.',
    '```xml\n<tool_calls><invoke name="demo"></invoke></tool_calls>\n```\nAfter the fence.',
    'Keep `<｜DSML｜tool_calls><｜DSML｜invoke name="demo">` and continue.',
    '<details><summary>View areas around line 10</summary>Visible note.</details>\n\nAfter details.',
  ])('copies the canonical assistant text: %s', async (text) => {
    const { sanitizeCopyText } = useChatTextRendering()
    const { api } = makeOptions(
      [],
      sanitizeCopyText,
      () => 'Content generated by AI, for reference only.',
    )

    const copied = await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text,
    }))

    expect(copied).toBe(true)
    expect(copyTextWithFallback).toHaveBeenCalledWith(
      `${text}\n\nContent generated by AI, for reference only.`,
    )
  })

  it('does not append the AI label when copying a user message', async () => {
    const { api } = makeOptions([], text => text, () => 'AI generated')

    await api.copyMessage(renderedMessage({ text: 'Keep my words unchanged.' }))

    expect(copyTextWithFallback).toHaveBeenCalledWith('Keep my words unchanged.')
  })

  it('copies the canonical projection without assistant boundary markers', async () => {
    const { sanitizeCopyText } = useChatTextRendering()
    const { api } = makeOptions([], sanitizeCopyText)

    await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text: 'NO_REPLY\nBeforeNO_REPLYAfter\nHEARTBEAT_OK',
      turnRunKind: 'goal',
      timelineItems: [
        { type: 'text', key: 'leading', html: '', rawText: 'NO_REPLY' },
        { type: 'text', key: 'before', html: '', rawText: 'Before' },
        { type: 'text', key: 'middle', html: '', rawText: 'NO_REPLY' },
        { type: 'text', key: 'after', html: '', rawText: 'After' },
        { type: 'text', key: 'trailing', html: '', rawText: 'HEARTBEAT_OK' },
      ],
    }))

    expect(copyTextWithFallback).toHaveBeenCalledWith('BeforeNO_REPLYAfter')
  })

  it('preserves a mixed sentinel-looking boundary when copying a direct-user answer', async () => {
    const { sanitizeCopyText } = useChatTextRendering()
    const { api } = makeOptions([], sanitizeCopyText)

    await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text: 'NO_REPLY\nLiteral explanation',
      turnInputMode: 'user',
      turnRunKind: 'default',
    }))

    expect(copyTextWithFallback).toHaveBeenCalledWith('NO_REPLY\nLiteral explanation')
  })

  it('copies the same terminal PlanRun delivery shown outside activity', async () => {
    const { api } = makeOptions(
      [],
      text => text,
      () => 'AI generated',
    )

    await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text: 'Working through files.\n\nImplementation complete.',
      timelineItems: [
        {
          type: 'text',
          key: 'work',
          html: 'Working through files.',
          rawText: 'Working through files.\n\n',
        },
        {
          type: 'tool-group',
          key: 'read',
          group: {
            groupId: 'read',
            operationKey: 'file.read',
            label: 'Read',
            iconName: 'edit',
            calls: [{
              toolId: 'read',
              renderKey: 'read',
              name: 'read_file',
              displayName: 'Read',
              inputRaw: '{"path":"README.md"}',
              inputPreview: 'README.md',
              isRunning: false,
              status: 'success',
              isError: false,
              result: 'ok',
              resultPreview: 'ok',
              isOpen: false,
            }],
            secondary: '',
            isRunning: false,
            isError: false,
            status: 'success',
          },
        },
        {
          type: 'text',
          key: 'delivery',
          html: 'Implementation complete.',
          rawText: 'Implementation complete.',
        },
        {
          type: 'tool-group',
          key: 'checkpoint',
          group: {
            groupId: 'checkpoint',
            operationKey: 'plan_run_checkpoint',
            label: 'Checkpoint',
            iconName: 'check',
            calls: [{
              toolId: 'checkpoint',
              renderKey: 'checkpoint',
              name: 'plan_run_checkpoint',
              displayName: 'Checkpoint',
              inputRaw: '{}',
              inputPreview: '',
              isRunning: false,
              status: 'success',
              isError: false,
              result: '{"plan_run":{"status":"completed"}}',
              resultPreview: 'completed',
              isOpen: false,
            }],
            secondary: '',
            isRunning: false,
            isError: false,
            status: 'success',
          },
        },
      ],
    }))

    expect(copyTextWithFallback).toHaveBeenCalledWith(
      'Implementation complete.\n\nAI generated',
    )
  })

  it('copies only the explicit final answer after intermediate commentary', async () => {
    const { api } = makeOptions([], text => text, () => 'AI generated')

    await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text: 'Working note.Final answer.',
      timelineItems: [
        {
          type: 'text',
          key: 'work',
          html: 'Working note.',
          rawText: 'Working note.',
          presentation: 'intermediate',
        },
        {
          type: 'text',
          key: 'answer',
          html: 'Final answer.',
          rawText: 'Final answer.',
          presentation: 'answer',
        },
      ],
    }))

    expect(copyTextWithFallback).toHaveBeenCalledWith('Final answer.\n\nAI generated')
  })

  it('copies the complete terminal Markdown answer from an ordinary tool transcript', async () => {
    const { api } = makeOptions([], text => text, () => 'AI generated')

    await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text: 'Checking.\n\nPreparing.\n\n---\n\n## Final answer',
      timelineItems: [
        { type: 'text', key: 'work', html: 'Checking.', rawText: 'Checking.' },
        {
          type: 'tool-group',
          key: 'request',
          group: {
            groupId: 'request',
            operationKey: 'web.read',
            label: 'Read',
            iconName: 'search',
            calls: [{
              toolId: 'request',
              renderKey: 'request',
              name: 'http_request',
              displayName: 'Request',
              inputRaw: '{}',
              inputPreview: '',
              isRunning: false,
              status: 'success',
              isError: false,
              result: 'ok',
              resultPreview: 'ok',
              isOpen: false,
            }],
            secondary: '',
            isRunning: false,
            isError: false,
            status: 'success',
          },
        },
        {
          type: 'text',
          key: 'terminal',
          html: 'Preparing.<hr><h2>Final answer</h2>',
          rawText: 'Preparing.\n\n---\n\n## Final answer',
        },
      ],
    }))

    expect(copyTextWithFallback).toHaveBeenCalledWith(
      'Preparing.\n\n---\n\n## Final answer\n\nAI generated',
    )
  })

  it('fails open to the complete visible transcript when the turn timed out', async () => {
    const { api } = makeOptions([], text => text, () => 'AI generated')

    await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text: 'Working.Partial delivery.',
      turnOutcome: { turnId: 'turn-timeout', status: 'timeout' },
      timelineItems: [
        { type: 'text', key: 'work', html: 'Working.', rawText: 'Working.' },
        {
          type: 'tool-group',
          key: 'request',
          group: {
            groupId: 'request',
            operationKey: 'web.read',
            label: 'Read',
            iconName: 'search',
            calls: [{
              toolId: 'request',
              renderKey: 'request',
              name: 'http_request',
              displayName: 'Request',
              inputRaw: '{}',
              inputPreview: '',
              isRunning: false,
              status: 'success',
              isError: false,
              result: 'ok',
              resultPreview: 'ok',
              isOpen: false,
            }],
            secondary: '',
            isRunning: false,
            isError: false,
            status: 'success',
          },
        },
        {
          type: 'text',
          key: 'partial',
          html: 'Partial delivery.',
          rawText: 'Partial delivery.',
        },
      ],
    }))

    expect(copyTextWithFallback).toHaveBeenCalledWith(
      'Working.Partial delivery.\n\nAI generated',
    )
  })

  it('does not add paragraph breaks when canonical text already owns spacing', async () => {
    const { api } = makeOptions([], text => text, () => 'AI generated')

    await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text: 'Working.\n\nPartial delivery.',
      timelineItems: [
        { type: 'text', key: 'work', html: 'Working.', rawText: 'Working.\n\n' },
        { type: 'text', key: 'partial', html: 'Partial delivery.', rawText: 'Partial delivery.' },
      ],
    }))

    expect(copyTextWithFallback).toHaveBeenCalledWith(
      'Working.\n\nPartial delivery.\n\nAI generated',
    )
  })
})
