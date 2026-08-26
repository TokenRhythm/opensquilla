// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'

import { useChatMessageActions, type UseChatMessageActionsOptions } from './useChatMessageActions'
import { useChatTextRendering } from './useChatTextRendering'
import type { ChatMessage, ChatRenderedMessage } from '@/types/chat'
import type { ChatRoutingControl } from '@/types/rpc'
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
  sanitizeCopyText: (text: string) => string = text => text,
  aiGeneratedLabel?: () => string,
  overrides: Partial<UseChatMessageActionsOptions> = {},
) {
  const pendingForkBeforeMessageId = ref<string | null>(null)
  const pendingRoutingControl = ref<ChatRoutingControl | null>(null)
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
    pendingRoutingControl,
    aiGeneratedLabel,
    notifyMessagePending: vi.fn(),
    notifyBranchBusy: vi.fn(),
    notifyAttachmentBranchUnsupported: vi.fn(),
    ...overrides,
  }
  return {
    api: useChatMessageActions(options),
    options,
    pendingForkBeforeMessageId,
    pendingRoutingControl,
  }
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

  it('attaches a one-shot redo control only in four-tier mapping mode', async () => {
    const fixed = makeOptions(
      [
        { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
        { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
      ],
      text => text,
      undefined,
      { isFourTierMapping: () => true },
    )

    fixed.api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'msg-a1',
    }))
    await nextTick()

    expect(fixed.pendingRoutingControl.value).toEqual({
      mode: 'four_tier_mapping',
      intent: 'redo',
      redoOfMessageId: 'msg-A',
    })

    const legacy = makeOptions(
      [
        { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
        { role: 'assistant', text: 'ack B', ts: null, messageId: 'msg-b1' },
      ],
      text => text,
      undefined,
      { isFourTierMapping: () => false },
    )
    legacy.pendingRoutingControl.value = {
      mode: 'four_tier_mapping',
      intent: 'redo',
      redoOfMessageId: 'stale',
    }

    legacy.api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'msg-b1',
    }))
    await nextTick()

    expect(legacy.pendingRoutingControl.value).toBeNull()
  })

  it('keeps four-tier mapping branch state unchanged when work becomes busy', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(
      messages,
      text => text,
      undefined,
      {
        isFourTierMapping: () => true,
        isBranchActionBlocked: () => true,
      },
    )

    api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'msg-a1',
    }))

    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
    expect(options.notifyBranchBusy).toHaveBeenCalledOnce()
  })

  it('keeps the legacy streaming refusal free of four-tier mapping feedback', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
    ]
    const { api, options } = makeOptions(
      messages,
      text => text,
      undefined,
      {
        isStreaming: ref(true),
        isFourTierMapping: () => false,
      },
    )

    api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'msg-a1',
    }))

    expect(options.messages.value).toEqual(messages)
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
    expect(options.notifyBranchBusy).not.toHaveBeenCalled()
  })

  it('fails closed on four-tier mapping attachment branches without changing legacy behavior', async () => {
    const attached: ChatMessage[] = [
      {
        role: 'user',
        text: 'inspect image',
        ts: null,
        messageId: 'msg-image',
        attachments: [{
          kind: 'file',
          displayId: 'photo-1',
          renderKey: 'photo-1',
          name: 'photo.png',
          mime: 'image/png',
        }],
      },
      { role: 'assistant', text: 'answer', ts: null, messageId: 'msg-answer' },
    ]
    const fixed = makeOptions(
      attached,
      text => text,
      undefined,
      { isFourTierMapping: () => true },
    )

    fixed.api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'msg-answer',
    }))
    await nextTick()

    expect(fixed.options.messages.value).toEqual(attached)
    expect(fixed.options.sendCurrentInput).not.toHaveBeenCalled()
    expect(fixed.options.notifyAttachmentBranchUnsupported).toHaveBeenCalledOnce()

    const legacy = makeOptions(
      attached,
      text => text,
      undefined,
      { isFourTierMapping: () => false },
    )
    legacy.api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'msg-answer',
    }))
    await nextTick()

    expect(legacy.options.sendCurrentInput).toHaveBeenCalledOnce()
    expect(legacy.options.notifyAttachmentBranchUnsupported).not.toHaveBeenCalled()
  })

  it('does not add composer attachments to a four-tier mapping branch', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
    ]
    const { api, options, pendingForkBeforeMessageId } = makeOptions(
      messages,
      text => text,
      undefined,
      {
        isFourTierMapping: () => true,
        hasPendingBranchAttachments: () => true,
      },
    )

    api.regenerateMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      sourceIndex: 1,
      messageId: 'msg-a1',
    }))

    expect(options.messages.value).toEqual(messages)
    expect(options.inputText.value).toBe('')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendCurrentInput).not.toHaveBeenCalled()
    expect(options.notifyAttachmentBranchUnsupported).toHaveBeenCalledOnce()

    const edited = makeOptions(
      messages,
      text => text,
      undefined,
      {
        isFourTierMapping: () => true,
        hasPendingBranchAttachments: () => true,
      },
    )
    edited.api.editMessage(renderedMessage({
      role: 'user',
      displayRole: 'user',
      sourceIndex: 0,
      messageId: 'msg-A',
    }))

    expect(edited.options.messages.value).toEqual(messages)
    expect(edited.options.inputText.value).toBe('')
    expect(edited.pendingForkBeforeMessageId.value).toBeNull()
    expect(edited.options.focusComposer).not.toHaveBeenCalled()
    expect(edited.options.notifyAttachmentBranchUnsupported).toHaveBeenCalledOnce()
  })

  it('always clears redo control when editing a four-tier mapping user turn', () => {
    const harness = makeOptions(
      [{ role: 'user', text: 'A', ts: null, messageId: 'msg-A' }],
      text => text,
      undefined,
      { isFourTierMapping: () => true },
    )
    harness.pendingRoutingControl.value = {
      mode: 'four_tier_mapping',
      intent: 'redo',
      redoOfMessageId: 'msg-A',
    }

    harness.api.editMessage(renderedMessage({
      role: 'user',
      displayRole: 'user',
      sourceIndex: 0,
      messageId: 'msg-A',
      text: 'A',
    }))

    expect(harness.pendingRoutingControl.value).toBeNull()
    expect(harness.pendingForkBeforeMessageId.value).toBe('msg-A')
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
})
