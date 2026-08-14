// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'

import { useChatMessageActions, type UseChatMessageActionsOptions } from './useChatMessageActions'
import { useChatTextRendering } from './useChatTextRendering'
import type { ChatMessage, ChatRenderedMessage } from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'

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

    api.regenerateMessage(renderedMessage({
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
  })

  it('preserves history, fork state, and the current draft when live delivery is unavailable', async () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)
    options.inputText.value = 'unrelated draft'
    options.canDeliver = () => false

    api.regenerateMessage(renderedMessage({
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

  it('does not regenerate as a parent send when the durable fork id is missing', async () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'still saving', ts: null, clientId: 'client-only' },
      { role: 'assistant', text: 'partial answer', ts: null, messageId: 'assistant-local' },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(messages)

    api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'assistant-local',
      text: 'partial answer',
    }))
    await nextTick()

    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
    expect(options.notifyMessagePending).toHaveBeenCalledOnce()
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
