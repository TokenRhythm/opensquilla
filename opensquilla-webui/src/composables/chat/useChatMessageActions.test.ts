// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref, toRaw } from 'vue'

import { useChatMessageActions, type UseChatMessageActionsOptions } from './useChatMessageActions'
import { useChatTextRendering } from './useChatTextRendering'
import { useLocalPathDraft } from './useLocalPathDraft'
import { useChatAttachments } from './useChatAttachments'
import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'
import { snapshotAttachment } from '@/utils/chat/attachments'
import type {
  Attachment,
  ChatMessage,
  ChatRenderedMessage,
  ChatTurnOutcome,
  DisplayAttachment,
} from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'
import { sliceMessageContent, type ReadMessageText } from '@/utils/chat/historyMessageContent'
import { interleaveHistoryModelCallSegments, interleaveHydratedHistoryModelCallSegments } from '@/utils/chat/historyModelCallSegments'

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
    pendingAttachments: ref<Attachment[]>([]),
    isStreaming: ref(false),
    sanitizeCopyText,
    stripTimePrefix: text => text,
    autoResizeTextarea: vi.fn(),
    sendRegeneratedInput: vi.fn(async () => true),
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
  it('restores message reference chips for edit, and the prior draft on cancellation', () => {
    const path = 'C:\\资料\\book.pdf'
    const draftPath = 'C:\\draft.html'
    const { options } = makeOptions([{ role: 'user', text: `Read\n${path}`, ts: null,
      messageId: 'durable', localPathReferences: [path] }])
    const draft = useLocalPathDraft()
    draft.restoreInput(`Draft\n${draftPath}`, [draftPath])
    options.inputText = draft.inputText
    options.localPathReferences = draft.localPaths
    options.restoreInput = draft.restoreInput
    const api = useChatMessageActions(options)
    api.editMessage(renderedMessage({ sourceIndex: 0 }))
    expect(draft.composerText.value).toBe('Read')
    expect(draft.localPaths.value).toEqual([path])
    expect(draft.inputText.value).toBe(`Read\n${path}`)
    expect(api.cancelEdit()).toBe(true)
    expect(draft.composerText.value).toBe('Draft')
    expect(draft.localPaths.value).toEqual([draftPath])
  })

  it('regenerates complete canonical text while retaining explicit reference chips', async () => {
    const path = 'C:\\book.pdf'
    const { options } = makeOptions([
      { role: 'user', text: path, ts: null, messageId: 'durable', localPathReferences: [path] },
      { role: 'assistant', text: 'done', ts: null },
    ])
    const draft = useLocalPathDraft()
    options.inputText = draft.inputText
    options.localPathReferences = draft.localPaths
    options.restoreInput = draft.restoreInput
    const api = useChatMessageActions(options)
    expect(await api.regenerateMessage(renderedMessage({ role: 'assistant', sourceIndex: 1 }))).toBe(true)
    await nextTick()
    expect(draft.composerText.value).toBe('')
    expect(draft.inputText.value).toBe('')
    expect(draft.localPaths.value).toEqual([])
    expect(options.sendRegeneratedInput).toHaveBeenCalledWith(expect.objectContaining({
      text: path, localPathReferences: [path], attachments: [], forkBeforeMessageId: 'durable',
    }), expect.any(Function))
  })

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

  it('puts the transcript and the draft back when the edit is cancelled', () => {
    // #1372: entering edit mode empties the transcript on the first click.
    // Without a way back, Escape cleared the composer and left the empty state
    // on screen, which reads as the conversation having been deleted.
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
      { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
      { role: 'assistant', text: 'ack B', ts: null, messageId: 'msg-b1' },
    ])
    options.inputText.value = 'half-written draft'

    api.editMessage(renderedMessage({
      role: 'user',
      displayRole: 'user',
      sourceIndex: 2,
      messageId: 'msg-B',
      text: 'B',
    }))
    expect(options.messages.value.map(message => message.text)).toEqual(['A', 'ack A'])

    expect(api.cancelEdit()).toBe(true)

    expect(options.messages.value.map(message => message.text)).toEqual([
      'A', 'ack A', 'B', 'ack B',
    ])
    // The draft the edit overwrote is part of what was lost, so it comes back
    // too rather than the composer being left holding the edited message.
    expect(options.inputText.value).toBe('half-written draft')
    expect(pendingForkBeforeMessageId.value).toBeNull()
  })

  it('reports nothing to cancel when no edit is in flight', () => {
    const { api, options } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
    ])
    options.inputText.value = 'just a draft'

    // Escape distinguishes the two: a false here is what lets it fall through
    // to clearing the composer instead of swallowing the key.
    expect(api.cancelEdit()).toBe(false)
    expect(options.inputText.value).toBe('just a draft')
    expect(options.messages.value.map(message => message.text)).toEqual(['A'])
  })

  it('cancels only once, so a later Escape cannot resurrect the transcript', () => {
    const { api, options } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
      { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
    ])

    api.editMessage(renderedMessage({
      role: 'user',
      displayRole: 'user',
      sourceIndex: 2,
      messageId: 'msg-B',
      text: 'B',
    }))
    expect(api.cancelEdit()).toBe(true)
    options.messages.value = [{ role: 'user', text: 'sent since', ts: null, messageId: 'msg-C' }]

    expect(api.cancelEdit()).toBe(false)
    expect(options.messages.value.map(message => message.text)).toEqual(['sent since'])
  })

  it('does not restore while the fork id has been consumed', () => {
    // A send consumes the fork id before admission. Cancellation must remain
    // unavailable until a rejected send restores that id.
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
      { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
    ])

    api.editMessage(renderedMessage({
      role: 'user',
      displayRole: 'user',
      sourceIndex: 2,
      messageId: 'msg-B',
      text: 'B',
    }))
    pendingForkBeforeMessageId.value = null

    expect(api.cancelEdit()).toBe(false)
    expect(options.messages.value.map(message => message.text)).toEqual(['A', 'ack A'])
  })

  it('restores the original transcript and draft after consecutive unsubmitted edits', () => {
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
      { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
      { role: 'assistant', text: 'ack B', ts: null, messageId: 'msg-b1' },
    ])

    options.inputText.value = 'half-written draft'

    api.editMessage(renderedMessage({
      role: 'user', displayRole: 'user', sourceIndex: 2, messageId: 'msg-B', text: 'B',
    }))
    api.editMessage(renderedMessage({
      role: 'user', displayRole: 'user', sourceIndex: 0, messageId: 'msg-A', text: 'A',
    }))

    expect(pendingForkBeforeMessageId.value).toBe('msg-A')
    expect(api.cancelEdit()).toBe(true)
    expect(options.messages.value.map(message => message.text)).toEqual([
      'A', 'ack A', 'B', 'ack B',
    ])
    expect(options.inputText.value).toBe('half-written draft')
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(api.cancelEdit()).toBe(false)
  })

  it.each([null, 'msg-other'])('keeps the restore point while the fork id is %s', (forkId) => {
    // Escape consults `cancelEdit` on every press now, so a press made while
    // the frame is unusable must not spend the undo: the fork id can come back
    // (a rejected send restores its own attempt), and the user still has only
    // this one exit.
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
      { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
    ])

    api.editMessage(renderedMessage({
      role: 'user', displayRole: 'user', sourceIndex: 2, messageId: 'msg-B', text: 'B',
    }))
    pendingForkBeforeMessageId.value = forkId
    expect(api.cancelEdit()).toBe(false)

    pendingForkBeforeMessageId.value = 'msg-B'
    expect(api.cancelEdit()).toBe(true)
    expect(options.messages.value.map(message => message.text)).toEqual(['A', 'ack A', 'B'])
  })

  it('leaves composer text the edit did not put there', () => {
    // Between the edit and the Escape the composer can pick up text that is
    // not the edited message — a pending item popped back with Alt+ArrowUp, a
    // draft recovered from a rejected send. That text is the user's, and
    // restoring the pre-edit draft over it would destroy it.
    const { api, options } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null, messageId: 'msg-a1' },
      { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
    ])
    options.inputText.value = 'older draft'

    api.editMessage(renderedMessage({
      role: 'user', displayRole: 'user', sourceIndex: 2, messageId: 'msg-B', text: 'B',
    }))
    expect(options.inputText.value).toBe('B')
    options.inputText.value = 'popped off the queue'

    expect(api.cancelEdit()).toBe(true)
    expect(options.messages.value.map(message => message.text)).toEqual(['A', 'ack A', 'B'])
    expect(options.inputText.value).toBe('popped off the queue')
  })

  it.each(['', 'replacement draft'])(
    'preserves newer composer content after consecutive edits: %j',
    replacement => {
      const { api, options } = makeOptions([
        { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
        { role: 'assistant', text: 'ack A', ts: null },
        { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
      ])
      options.inputText.value = 'original draft'
      api.editMessage(renderedMessage({ sourceIndex: 2, messageId: 'msg-B' }))
      api.editMessage(renderedMessage({ sourceIndex: 0, messageId: 'msg-A' }))
      options.inputText.value = replacement

      expect(api.cancelEdit()).toBe(true)
      expect(options.messages.value.map(message => message.text)).toEqual(['A', 'ack A', 'B'])
      expect(options.inputText.value).toBe(replacement)
    },
  )

  it('keeps cancellation unavailable during streaming without spending the restore point', () => {
    const { api, options } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
    ])
    api.editMessage(renderedMessage({ sourceIndex: 0, messageId: 'msg-A' }))
    options.isStreaming.value = true

    expect(api.cancelEdit()).toBe(false)
    expect(options.messages.value).toEqual([])
    options.isStreaming.value = false
    expect(api.cancelEdit()).toBe(true)
    expect(options.messages.value[0]?.messageId).toBe('msg-A')
  })

  it('discards the old restore point when leaving a session', () => {
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
    ])
    api.editMessage(renderedMessage({ sourceIndex: 0, messageId: 'msg-A' }))
    api.discardEditRestorePoint()
    options.messages.value = [{ role: 'user', text: 'another session', ts: null }]
    options.inputText.value = 'another draft'
    pendingForkBeforeMessageId.value = 'msg-A'

    expect(api.cancelEdit()).toBe(false)
    expect(options.messages.value[0]?.text).toBe('another session')
    expect(options.inputText.value).toBe('another draft')
  })

  it('starts a new restore point after the previous edit has been sent', () => {
    const { api, options, pendingForkBeforeMessageId } = makeOptions([
      { role: 'user', text: 'A', ts: null, messageId: 'msg-A' },
      { role: 'assistant', text: 'ack A', ts: null },
      { role: 'user', text: 'B', ts: null, messageId: 'msg-B' },
    ])
    api.editMessage(renderedMessage({ sourceIndex: 2, messageId: 'msg-B' }))
    pendingForkBeforeMessageId.value = null
    const submitted: ChatMessage[] = [
      ...options.messages.value,
      { role: 'user', text: 'revised B', ts: null, messageId: 'msg-revised-B' },
    ]
    options.messages.value = submitted
    options.inputText.value = 'new draft'
    api.editMessage(renderedMessage({ sourceIndex: 0, messageId: 'msg-A' }))

    expect(api.cancelEdit()).toBe(true)
    expect(options.messages.value).toEqual(submitted)
    expect(options.inputText.value).toBe('new draft')
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

    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.messages.value.map(message => message.text)).toEqual(['A', 'ack A', 'B', 'ack B', 'C'])
    expect(options.inputText.value).toBe('')
    expect(options.sendRegeneratedInput).toHaveBeenCalledWith(expect.objectContaining({ text: 'B', forkBeforeMessageId: 'msg-B' }), expect.any(Function))
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
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
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
      expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
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
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
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
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
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
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
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
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
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
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
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
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
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
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
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
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
    expect(options.notifyMessagePending).toHaveBeenCalledOnce()

    options.messages.value[0]!.messageId = 'msg-now-durable'
    const secondAccepted = await api.regenerateMessage(rendered)
    await nextTick()

    expect(firstAccepted).toBe(false)
    expect(secondAccepted).toBe(true)
    expect(pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendRegeneratedInput).toHaveBeenCalledWith(expect.objectContaining({ forkBeforeMessageId: 'msg-now-durable' }), expect.any(Function))
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

    expect(options.sendRegeneratedInput).toHaveBeenCalledOnce()
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

  it('copies only the explicit final answer after a failed tool', async () => {
    const { api } = makeOptions([], text => text, () => 'AI generated')

    await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text: 'Working note.\n\nFinal answer.',
      terminalFailure: true,
      turnOutcome: {
        turnId: 'turn-copy-failed-tool',
        status: 'failed',
      },
      timelineItems: [
        {
          type: 'text',
          key: 'work',
          html: 'Working note.',
          rawText: 'Working note.',
          presentation: 'intermediate',
        },
        {
          type: 'tool-group',
          key: 'failed-tool',
          group: {
            groupId: 'failed-tool',
            operationKey: 'web.search',
            label: 'Search',
            iconName: 'search',
            calls: [{
              toolId: 'failed-tool',
              renderKey: 'failed-tool',
              name: 'web_search',
              displayName: 'Search',
              inputRaw: '{}',
              inputPreview: '{}',
              isRunning: false,
              status: 'error',
              isError: true,
              result: 'network error',
              resultPreview: 'network error',
              isOpen: false,
            }],
            secondary: '',
            isRunning: false,
            isError: true,
            status: 'error',
          },
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

  it('does not copy explicit intermediate-only activity as an answer', async () => {
    const { api } = makeOptions([], text => text, () => 'AI generated')

    const copied = await api.copyMessage(renderedMessage({
      role: 'assistant',
      displayRole: 'assistant',
      text: 'Work narration.',
      timelineItems: [
        {
          type: 'text',
          key: 'work',
          html: 'Work narration.',
          rawText: 'Work narration.',
          presentation: 'intermediate',
        },
        {
          type: 'tool-group',
          key: 'finish',
          group: {
            groupId: 'finish',
            operationKey: 'file.read',
            label: 'Read',
            iconName: 'edit',
            calls: [{
              toolId: 'finish',
              renderKey: 'finish',
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
      ],
    }))

    expect(copied).toBe(false)
    expect(copyTextWithFallback).not.toHaveBeenCalled()
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


describe('complete historical message actions', () => {
  const body = '0123456789'.repeat(2048) + 'EXPECTED_FINAL_TAIL'
  function fixture() {
    const { options } = makeOptions([
      { role: 'user', ts: 1, messageId: 'long-user', text: body.slice(0, 16384), previewComplete: false,
        contentRef: { version: 1, sessionKey: 'session-a', sessionId: 'physical-a', messageId: 'long-user',
          source: 'active', view: 'display', revision: 'revision-a' } },
      { role: 'assistant', ts: 2, messageId: 'answer', text: 'answer' },
    ])
    const session = ref('session-a:0')
    options.sessionIdentity = () => session.value
    options.inputText.value = 'prior draft'
    const read = vi.fn<ReadMessageText>(async () => body)
    options.readMessageText = read
    const api = useChatMessageActions(options)
    return { api, options, read, session,
      user: renderedMessage({ sourceIndex: 0, messageId: 'long-user' }),
      assistant: renderedMessage({ role: 'assistant', displayRole: 'assistant', sourceIndex: 1, messageId: 'answer' }) }
  }

  it('edits the complete durable body and cancellation restores the transcript and draft', async () => {
    const { api, options, read, user } = fixture()
    const transcript = options.messages.value
    const pending = api.editMessage(user)
    expect(options.inputText.value).toBe('prior draft')
    expect(options.messages.value).toBe(transcript)
    expect(await pending).toBe(true)
    expect(read.mock.calls[0]?.[0]).toMatchObject({ contentRef: { sessionId: 'physical-a', revision: 'revision-a' } })
    expect(options.inputText.value).toBe(body)
    expect(api.cancelEdit()).toBe(true)
    expect(options.messages.value).toBe(transcript)
    expect(options.inputText.value).toBe('prior draft')
  })

  it('restores the full historical body and its attachments together before editing', async () => {
    const { api, options, user } = fixture()
    options.messages.value[0]!.attachments = [displayAttachment('staged')]
    const attachments: Attachment[] = [{ kind: 'inline', local_id: -21, name: 'restored.txt', mime: 'text/plain', data: 'cmVxdWVzdA==' }]
    options.readAttachments = vi.fn(async () => attachments)
    expect(await api.editMessage(user)).toBe(true)
    expect(options.inputText.value).toBe(body)
    expect(options.pendingAttachments!.value).toEqual(attachments)
    expect(options.readAttachments).toHaveBeenCalledWith(expect.objectContaining({ text: body }), expect.any(AbortSignal))
  })

  it('regenerates with the full user body, even when the rendered answer has already hydrated', async () => {
    const { api, options, assistant } = fixture()
    expect(await api.regenerateMessage(assistant)).toBe(true)
    await nextTick()
    expect(options.inputText.value).toBe('prior draft')
    expect(options.sendRegeneratedInput).toHaveBeenCalledWith(expect.objectContaining({ text: body }), expect.any(Function))
    expect(options.pendingForkBeforeMessageId.value).toBeNull()
  })

  it.each(['edit', 'regenerate'] as const)('does not use a preview when %s cannot read the body', async operation => {
    const { api, options, read, user, assistant } = fixture()
    read.mockRejectedValue(new Error('incomplete range'))
    const transcript = options.messages.value
    expect(await (operation === 'edit' ? api.editMessage(user) : api.regenerateMessage(assistant))).toBe(false)
    expect(options.messages.value).toBe(transcript)
    expect(options.inputText.value).toBe('prior draft')
    expect(options.pendingForkBeforeMessageId.value).toBeNull()
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
    expect(options.sendUsageBarrierReplay).not.toHaveBeenCalled()
  })

  it.each(['cancel', 'session', 'epoch', 'revision', 'draft', 'attachment-draft', 'source-attachment', 'streaming', 'discard'] as const)(
    'discards a late complete edit after %s changes', async change => {
      const { api, options, read, user, session } = fixture()
      let resolve!: (text: string) => void
      read.mockImplementation(() => new Promise(done => { resolve = done }))
      const transcript = options.messages.value
      const request = api.editMessage(user)
      if (change === 'cancel') expect(api.cancelEdit()).toBe(true)
      if (change === 'session') { session.value = 'session-b:0'; session.value = 'session-a:0' }
      if (change === 'epoch') session.value = 'session-a:1'
      if (change === 'revision') options.messages.value[0]!.contentRef!.revision = 'new-revision'
      if (change === 'draft') options.inputText.value = 'new draft'
      if (change === 'attachment-draft') options.pendingAttachments!.value.push({ kind: 'inline', local_id: 20, name: 'new.txt', mime: 'text/plain', data: 'bmV3' })
      if (change === 'source-attachment') options.messages.value[0]!.attachments = [displayAttachment('inline')]
      if (change === 'streaming') options.isStreaming.value = true
      if (change === 'discard') api.discardEditRestorePoint()
      resolve(body)
      expect(await request).toBe(false)
      expect(options.messages.value).toBe(transcript)
      expect(options.inputText.value).toBe(change === 'draft' ? 'new draft' : 'prior draft')
      expect(options.pendingForkBeforeMessageId.value).toBeNull()
      expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
    },
  )

  it('supersedes an in-flight edit with a newer regenerate without applying the older read', async () => {
    const { api, options, read, user, assistant } = fixture()
    let resolve!: (text: string) => void
    read.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const edit = api.editMessage(user)
    expect(await api.regenerateMessage(assistant)).toBe(true)
    resolve(body + 'STALE')
    expect(await edit).toBe(false)
    await nextTick()
    expect(options.inputText.value).toBe('prior draft')
    expect(options.sendRegeneratedInput).toHaveBeenCalledWith(expect.objectContaining({ text: body }), expect.any(Function))
  })

  it('does not send a queued synchronous regenerate after navigating away and back', async () => {
    const { api, options, assistant, session } = fixture()
    options.messages.value[0]!.previewComplete = true
    options.messages.value[0]!.text = body
    const regeneration = api.regenerateMessage(assistant)
    session.value = 'session-b:0'
    session.value = 'session-a:0'
    expect(await regeneration).toBe(false)
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
  })

  it('reads the full body before a proven usage-barrier replay', async () => {
    const { api, options, assistant } = fixture()
    const outcome = safeUsageOutcome('turn', 'long-user')
    options.messages.value[0]!.turnId = 'turn'
    options.messages.value[1]!.turnOutcome = outcome
    assistant.turnOutcome = outcome
    expect(await api.regenerateMessage(assistant)).toBe(true)
    expect(options.sendUsageBarrierReplay).toHaveBeenCalledWith({ text: body, forkBeforeMessageId: 'long-user' })
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
  })
})

describe('complete historical message copy', () => {
  const body = '0123456789'.repeat(2048) + '🙂EXPECTED_FINAL_TAIL'
  const scopes: ReturnType<typeof effectScope>[] = []
  afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))

  function fixture(role: 'user' | 'assistant' = 'user') {
    const source: ChatMessage = {
      role, ts: 1, messageId: 'long-message', text: body.slice(0, 16384), previewComplete: false,
      contentRef: { version: 1, sessionKey: 'session-a', sessionId: 'physical-a', messageId: 'long-message',
        source: 'active', view: 'display', revision: 'revision-a' },
    }
    const { sanitizeCopyText, stripTimePrefix } = useChatTextRendering()
    const { options } = makeOptions([source], sanitizeCopyText, () => 'AI generated')
    options.stripTimePrefix = stripTimePrefix
    const session = ref('session-a:0')
    options.sessionIdentity = () => session.value
    const read = vi.fn<ReadMessageText>(async () => body)
    options.readMessageText = read
    const scope = effectScope()
    scopes.push(scope)
    const api = scope.run(() => useChatMessageActions(options))!
    const message = renderedMessage({ ...options.messages.value[0], displayRole: role, sourceIndex: 0 })
    return { api, options, read, session, scope, message }
  }

  function deferRead(read: ReturnType<typeof fixture>['read']) {
    let resolve!: (text: string) => void
    const result = new Promise<string>(done => { resolve = done })
    read.mockImplementation(() => result)
    return resolve
  }

  it.each(['user', 'assistant'] as const)('waits for the full %s body before copying or reporting success', async role => {
    const { api, read, message } = fixture(role)
    const resolve = deferRead(read)
    let settled = false
    const pending = api.copyMessage(message).then(result => { settled = true; return result })
    await nextTick()
    expect(copyTextWithFallback).not.toHaveBeenCalled()
    expect(settled).toBe(false)
    expect(read).toHaveBeenCalledOnce()
    resolve(body)
    expect(await pending).toBe(true)
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith(
      role === 'assistant' ? `${body}\n\nAI generated` : body,
    )
    expect(message.text).toBe(body.slice(0, 16384))
  })

  it.each(['missing reader', 'unavailable content', 'incomplete range'])('does not copy a preview after %s', async failure => {
    const { api, options, read, message } = fixture()
    if (failure === 'missing reader') options.readMessageText = undefined
    else read.mockRejectedValue(new Error(failure))
    expect(await api.copyMessage(message)).toBe(false)
    expect(copyTextWithFallback).not.toHaveBeenCalled()
  })

  it.each(['session', 'epoch', 'revision', 'replacement', 'dispose'] as const)(
    'discards a late copy after %s changes', async change => {
      const { api, options, read, message, session, scope } = fixture()
      const resolve = deferRead(read)
      const pending = api.copyMessage(message)
      if (change === 'session') { session.value = 'session-b:0'; session.value = 'session-a:0' }
      if (change === 'epoch') session.value = 'session-a:1'
      if (change === 'revision') {
        options.messages.value[0]!.contentRef = { ...options.messages.value[0]!.contentRef!, revision: 'revision-b' }
      }
      if (change === 'replacement') options.messages.value[0] = { role: 'user', ts: 2, messageId: 'other', text: 'other' }
      if (change === 'dispose') scope.stop()
      resolve(body)
      expect(await pending).toBe(false)
      expect(copyTextWithFallback).not.toHaveBeenCalled()
    },
  )

  it('lets a newer complete copy supersede an older pending read', async () => {
    const { api, read, message } = fixture()
    const resolve = deferRead(read)
    const pending = api.copyMessage(message)
    expect(await api.copyMessage(renderedMessage({ text: 'Newer copy', previewComplete: true }))).toBe(true)
    resolve(body)
    expect(await pending).toBe(false)
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith('Newer copy')
    expect(read.mock.calls[0]![1].aborted).toBe(true)
  })

  it('keeps complete short messages on the immediate clipboard path', async () => {
    const { api, read, message } = fixture()
    message.previewComplete = true
    message.text = 'Complete short message'
    expect(await api.copyMessage(message)).toBe(true)
    expect(read).not.toHaveBeenCalled()
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith(message.text)
  })

  it('does not treat composer edits or streaming as a copy cancellation', async () => {
    const { api, options, read, message } = fixture()
    const resolve = deferRead(read)
    const pending = api.copyMessage(message)
    options.inputText.value = 'Keep this unrelated draft'
    options.isStreaming.value = true
    resolve(body)
    expect(await pending).toBe(true)
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith(body)
    expect(options.inputText.value).toBe('Keep this unrelated draft')
    expect(options.sendRegeneratedInput).not.toHaveBeenCalled()
  })

  it('restores full timeline text before choosing and sanitizing the assistant answer', async () => {
    const { api, read, message } = fixture('assistant')
    const note = 'Working note.'
    const answer = `<details>Keep literal details.</details>\n${body}`
    const full = note + answer
    read.mockResolvedValue(full)
    message.text = full.slice(0, 16384)
    message.historyPayloadPreview = { textUtf16Lengths: [note.length, answer.length] }
    message.timelineItems = [
      { type: 'text', key: 'note', rawText: note, html: note, presentation: 'intermediate' },
      { type: 'text', key: 'answer', rawText: answer.slice(0, 64), html: '', presentation: 'answer' },
    ]
    expect(await api.copyMessage(message)).toBe(true)
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith(`${answer}\n\nAI generated`)
    expect(message.timelineItems[1]).toMatchObject({ rawText: answer.slice(0, 64) })
  })

  it('retains the rendered content slice when asking the shared reader for text', async () => {
    const { api, read, message } = fixture('assistant')
    message.contentSlice = { startCodepoint: 12, endCodepoint: 16 }
    read.mockResolvedValue('😀正文🙂')
    expect(await api.copyMessage(message)).toBe(true)
    expect(read.mock.calls[0]?.[0]).toMatchObject({
      contentSlice: { startCodepoint: 12, endCodepoint: 16 },
      contentRef: { sessionId: 'physical-a', messageId: 'long-message', revision: 'revision-a', view: 'display' },
    })
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith('😀正文🙂\n\nAI generated')
  })

  function steerFixture() {
    const base = fixture('assistant')
    const chunks = ['P'.repeat(16390) + '😀', 'Middle🙂', 'Tail𐐀']
    const full = chunks.join('')
    const prefixEnd = Array.from(chunks[0]!).length
    const middleEnd = prefixEnd + Array.from(chunks[1]!).length
    const assistant: ChatMessage = {
      ...base.options.messages.value[0]!, text: full.slice(0, 16384), turnId: 'turn',
      usage: { model_call_segments: [
        { model_call_id: 'call-a', iteration: 1, start_codepoint: prefixEnd, end_codepoint: middleEnd },
        { model_call_id: 'call-b', iteration: 2, start_codepoint: middleEnd, end_codepoint: Array.from(full).length },
      ] },
    }
    const steers: ChatMessage[] = [
      { role: 'user', ts: 1, text: 'First steer', turnId: 'turn', inputDisposition: 'applied',
        steerModelCallId: 'call-a', steerAppliedIteration: 1 },
      { role: 'user', ts: 2, text: 'Second steer', turnId: 'turn', inputDisposition: 'applied',
        steerModelCallId: 'call-b', steerAppliedIteration: 2 },
    ]
    base.options.messages.value = [...steers, assistant]
    function reproject() {
      const aggregate = { ...assistant, contentRef: { ...assistant.contentRef! } }
      base.options.messages.value = interleaveHydratedHistoryModelCallSegments([
        { role: 'user', ts: 0, text: 'Earlier history loaded' }, ...steers, aggregate,
      ], aggregate, full)
    }
    return { ...base, chunks, full, reproject }
  }

  it.each(['aggregate', 'prefix', 'middle', 'suffix'] as const)(
    'copies the clicked %s scope while the same body is reprojected', async scope => {
      const { api, options, read, chunks, full, reproject } = steerFixture()
      if (scope !== 'aggregate') reproject()
      const assistants = options.messages.value.flatMap((row, index) => row.role === 'assistant'
        ? [renderedMessage({ ...row, displayRole: 'assistant', sourceIndex: index })] : [])
      const segmentIndex = scope === 'middle' ? 1 : scope === 'suffix' ? 2 : 0
      const clicked = assistants[segmentIndex]!
      const clickedSlice = clicked.contentSlice && { ...clicked.contentSlice }
      const resolve = deferRead(read)
      const pending = api.copyMessage(clicked)
      reproject()
      expect(options.messages.value.filter(row => row.role === 'assistant')).toHaveLength(3)
      expect(options.messages.value.find(row => row.messageId === 'long-message')!.contentSlice).toBeDefined()
      expect(copyTextWithFallback).not.toHaveBeenCalled()
      const expected = scope === 'aggregate' ? full : chunks[segmentIndex]!
      expect(read.mock.calls[0]![0].contentSlice).toEqual(clickedSlice)
      resolve(sliceMessageContent(read.mock.calls[0]![0], full))
      expect(await pending).toBe(true)
      expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith(`${expected}\n\nAI generated`)
    },
  )

  it.each(['owner', 'turn', 'revision', 'session', 'physical session', 'source', 'view', 'length', 'digest', 'content revision'] as const)(
    'rejects the pending aggregate copy when reprojection changes its %s identity', async change => {
      const { api, options, read, full, reproject } = steerFixture()
      const clicked = renderedMessage({ ...options.messages.value[2]!, displayRole: 'assistant', sourceIndex: 2 })
      const resolve = deferRead(read)
      const pending = api.copyMessage(clicked)
      reproject()
      const canonical = options.messages.value.find(row => row.messageId === 'long-message')!
      if (change === 'owner') canonical.messageId = 'another-owner'
      if (change === 'turn') canonical.turnId = 'another-turn'
      if (change === 'revision') canonical.contentRef!.revision = 'revision-b'
      if (change === 'session') canonical.contentRef!.sessionKey = 'another-session'
      if (change === 'physical session') canonical.contentRef!.sessionId = 'another-physical-session'
      if (change === 'source') canonical.contentRef!.source = 'compacted'
      if (change === 'view') canonical.contentRef!.view = 'raw'
      if (change === 'length') canonical.contentRef!.byteLength = full.length + 1
      if (change === 'digest') canonical.contentRef!.sha256 = 'another-digest'
      if (change === 'content revision') canonical.contentRevision = 'another-content-revision'
      resolve(full)
      expect(await pending).toBe(false)
      expect(copyTextWithFallback).not.toHaveBeenCalled()
    },
  )

  it('copies only each real Steer slice and fences a synthetic prefix by its source index', async () => {
    const { api, options, read } = fixture('assistant')
    const chunks = ['P'.repeat(16390) + '😀', 'Middle🙂', 'Tail𐐀']
    const full = chunks.join('')
    const prefixEnd = Array.from(chunks[0]!).length
    const middleEnd = prefixEnd + Array.from(chunks[1]!).length
    const assistant = options.messages.value[0]!
    options.messages.value = interleaveHistoryModelCallSegments([
      { role: 'user', ts: 1, text: 'First steer', turnId: 'turn', inputDisposition: 'applied',
        steerModelCallId: 'call-a', steerAppliedIteration: 1 },
      { role: 'user', ts: 2, text: 'Second steer', turnId: 'turn', inputDisposition: 'applied',
        steerModelCallId: 'call-b', steerAppliedIteration: 2 },
      { ...assistant, text: full, turnId: 'turn', usage: { model_call_segments: [
        { model_call_id: 'call-a', iteration: 1, start_codepoint: prefixEnd, end_codepoint: middleEnd },
        { model_call_id: 'call-b', iteration: 2, start_codepoint: middleEnd, end_codepoint: Array.from(full).length },
      ] } },
    ])
    const segments = options.messages.value.flatMap((row, index) => {
      if (row.role !== 'assistant') return []
      row.text = row.text!.slice(0, 4)
      return [renderedMessage({ ...row, displayRole: 'assistant', sourceIndex: index,
        historyPayloadPreview: { textUtf16Lengths: [full.length] },
        timelineItems: [{ type: 'text', key: 'aggregate', rawText: full, html: '', presentation: 'answer' }],
      })]
    })
    expect(segments).toHaveLength(3)
    expect(segments[0]!.messageId).toBeUndefined()
    expect(segments[0]!.clientId).toContain('history-model-call-segment:')
    expect(segments[2]!.messageId).toBe('long-message')
    read.mockImplementation(async message => sliceMessageContent(message, full))
    for (const [index, segment] of segments.entries()) {
      expect(await api.copyMessage(segment)).toBe(true)
      expect(copyTextWithFallback).toHaveBeenLastCalledWith(`${chunks[index]}\n\nAI generated`)
    }
    const resolve = deferRead(read)
    const pending = api.copyMessage(segments[0]!)
    const source = options.messages.value[segments[0]!.sourceIndex!]!
    source.contentRef = { ...source.contentRef!, revision: 'replacement-revision' }
    resolve(chunks[0]!)
    expect(await pending).toBe(false)
    expect(copyTextWithFallback).toHaveBeenCalledTimes(3)
  })
})

describe('editing original attachments', () => {
  const originalAttachment = (name = 'original.png'): DisplayAttachment => ({
    kind: 'file', displayId: name, renderKey: name, name, mime: 'image/png', sha256_ref: 'a'.repeat(64),
  })
  const recoveredAttachment = (name = 'original.png', id = -1): Attachment => ({
    kind: 'inline', local_id: id, name, mime: 'image/png', data: btoa(name),
    file: new File([name], name, { type: 'image/png' }),
  })
  function imageEdit(text = '', count = 1) {
    const originals = Array.from({ length: count }, (_, index) => originalAttachment(`original-${index}.png`))
    const recovered = originals.map((attachment, index) => recoveredAttachment(attachment.name, -index - 1))
    const h = makeOptions([
      { role: 'user', text, ts: null, messageId: 'original-user', attachments: originals },
      { role: 'assistant', text: 'original answer', ts: null, messageId: 'original-answer' },
    ])
    h.options.readAttachments = vi.fn(async () => recovered)
    h.options.notifyAttachmentRecoveryFailed = vi.fn()
    const clicked = renderedMessage({ sourceIndex: 0, messageId: 'original-user' })
    return { ...h, recovered, clicked }
  }
  function deferred<T>() {
    let resolve!: (value: T) => void
    const promise = new Promise<T>(yes => { resolve = yes })
    return { promise, resolve }
  }

  it.each([['', 1], ['describe these', 1], ['', 3], ['compare these', 3]] as const)(
    'restores complete original input for text %j and %i images', async (text, count) => {
      const h = imageEdit(text, count)
      const transcript = h.options.messages.value
      const result = h.api.editMessage(h.clicked)
      expect(h.options.messages.value).toBe(transcript)
      expect(await result).toBe(true)
      expect(h.options.inputText.value).toBe(text)
      expect(h.options.pendingAttachments!.value).toEqual(h.recovered)
      expect(h.options.messages.value).toEqual([])
      expect(h.pendingForkBeforeMessageId.value).toBe('original-user')
      expect(h.options.focusComposer).toHaveBeenCalledOnce()
    },
  )

  it('restores the existing attachment draft when cancelling the edit', async () => {
    const h = imageEdit()
    const draft = recoveredAttachment('draft.png', 12)
    h.options.inputText.value = 'existing draft'
    h.options.pendingAttachments!.value = [draft]
    const transcript = h.options.messages.value
    await h.api.editMessage(h.clicked)
    expect(h.api.cancelEdit()).toBe(true)
    expect(h.options.messages.value).toBe(transcript)
    expect(h.options.inputText.value).toBe('existing draft')
    expect(h.options.pendingAttachments!.value).toEqual([draft])
    expect(toRaw(h.options.pendingAttachments!.value[0]!.file)).toBe(draft.file)
    expect(h.pendingForkBeforeMessageId.value).toBeNull()
  })

  it('clears unrelated attachments for a text-only edit and restores them on cancel', () => {
    const h = makeOptions([{ role: 'user', text: 'text only', ts: null, messageId: 'text' }])
    const draft = recoveredAttachment('draft.png', 12)
    h.options.pendingAttachments!.value = [draft]
    expect(h.api.editMessage(renderedMessage({ sourceIndex: 0 }))).toBe(true)
    expect(h.options.pendingAttachments!.value).toEqual([])
    expect(h.api.cancelEdit()).toBe(true)
    expect(h.options.pendingAttachments!.value).toEqual([draft])
  })

  it.each(['append', 'remove', 'replace-file', 'workspace-change'])(
    'does not overwrite newer attachment changes on cancel: %s', async (change) => {
      const h = imageEdit()
      h.options.pendingAttachments!.value = [recoveredAttachment('old-draft.png', 12)]
      await h.api.editMessage(h.clicked)
      if (change === 'append') h.options.pendingAttachments!.value.push(recoveredAttachment('new.png', 13))
      if (change === 'remove') h.options.pendingAttachments!.value = []
      if (change === 'replace-file') h.options.pendingAttachments!.value[0]!.file = new File(['different'], 'original-0.png')
      if (change === 'workspace-change') h.options.pendingAttachments!.value[0]!.workspaceFile = {
        workspaceId: 'current', relativePath: 'new.png', name: 'new.png', mime: 'image/png',
      }
      const newer = h.options.pendingAttachments!.value
      expect(h.api.cancelEdit()).toBe(true)
      expect(h.options.pendingAttachments!.value).toBe(newer)
      expect(h.options.messages.value).toHaveLength(2)
    },
  )

  it('restores the first draft after consecutive unsubmitted attachment edits', async () => {
    const h = imageEdit('earlier')
    h.options.messages.value.push(
      { role: 'user', text: 'later', ts: null, messageId: 'later-user', attachments: [originalAttachment('later.png')] },
      { role: 'assistant', text: 'later answer', ts: null },
    )
    const firstDraft = recoveredAttachment('first-draft.png', 12)
    h.options.pendingAttachments!.value = [firstDraft]
    await h.api.editMessage(renderedMessage({ sourceIndex: 2, messageId: 'later-user' }))
    await h.api.editMessage(h.clicked)
    expect(h.api.cancelEdit()).toBe(true)
    expect(h.options.messages.value).toHaveLength(4)
    expect(h.options.pendingAttachments!.value).toEqual([firstDraft])
  })

  it('recognizes sender-cloned recovery attachments after a rejected edit send', async () => {
    const h = imageEdit()
    const oldDraft = recoveredAttachment('draft.png', 12)
    h.options.pendingAttachments!.value = [oldDraft]
    await h.api.editMessage(h.clicked)
    h.pendingForkBeforeMessageId.value = null
    h.options.pendingAttachments!.value = []
    expect(h.api.cancelEdit()).toBe(false)
    h.pendingForkBeforeMessageId.value = 'original-user'
    h.options.pendingAttachments!.value = h.recovered.map(attachment => ({ ...attachment }))
    expect(h.api.cancelEdit()).toBe(true)
    expect(h.options.pendingAttachments!.value).toEqual([oldDraft])
  })

  function stagedEdit() {
    const h = imageEdit('original prompt')
    const oldDraft = recoveredAttachment('draft.png', 12)
    const staged: Attachment = {
      local_id: -1, name: 'original.png', mime: 'image/png', size: 8,
      file: new File(['original'], 'original.png', { type: 'image/png' }),
      kind: 'staged', file_uuid: 'expired', expires_at: 1, ttl_seconds: 3600,
    }
    h.options.inputText.value = 'original draft'
    h.options.pendingAttachments!.value = [oldDraft]
    h.options.readAttachments = vi.fn(async () => [staged])
    return { ...h, oldDraft, staged }
  }

  it('restores the prior attachment draft after staging refresh and rejected-send recovery', async () => {
    const h = stagedEdit()
    await h.api.editMessage(h.clicked)
    const unused = async (): Promise<never> => { throw new Error('Unexpected content operation') }
    const uploadAttachment = vi.fn(async () => ({ fileUuid: 'refreshed', expiresAt: Date.now() / 1000 + 3600, ttlSeconds: 3600 }))
    const content: ArtifactContentAccess = {
      fetchArtifact: unused, openArtifact: unused, openArtifactBlob: unused,
      clearPreviewStorage: unused, fetchAttachment: unused, uploadAttachment,
    }
    const intake = useChatAttachments(content)
    // Ordinary sends prepare an independent payload, then restore that payload
    // if admission rejects it. Refresh must not count as a user's image edit.
    const payload = h.options.pendingAttachments!.value.map(snapshotAttachment)
    expect(await intake.prepareAttachmentsForSend({ attachments: payload })).toBe(true)
    expect(uploadAttachment).toHaveBeenCalledOnce()
    expect(payload[0]!.file_uuid).toBe('refreshed')
    h.options.pendingAttachments!.value = []
    h.pendingForkBeforeMessageId.value = null
    h.options.pendingAttachments!.value = payload
    h.pendingForkBeforeMessageId.value = 'original-user'
    expect(h.api.cancelEdit()).toBe(true)
    expect(h.options.pendingAttachments!.value).toEqual([h.oldDraft])
    expect(h.options.inputText.value).toBe('original draft')
  })

  it.each(['file', 'id', 'name', 'mime', 'data', 'kind', 'missing-file'] as const)(
    'does not discard a staged attachment changed beyond its upload receipt: %s', async change => {
      const h = stagedEdit()
      await h.api.editMessage(h.clicked)
      const changed: Attachment = { ...h.staged, file_uuid: 'refreshed', expires_at: 99999 }
      if (change === 'file') changed.file = new File(['different'], 'original.png', { type: 'image/png' })
      if (change === 'id') changed.local_id = 27
      if (change === 'name') changed.name = 'new-name.png'
      if (change === 'mime') changed.mime = 'image/jpeg'
      if (change === 'data') changed.data = btoa('new image bytes')
      if (change === 'kind') changed.kind = 'failed'
      if (change === 'missing-file') changed.file = undefined
      h.options.pendingAttachments!.value = [changed]
      const current = h.options.pendingAttachments!.value
      expect(h.api.cancelEdit()).toBe(true)
      expect(h.options.pendingAttachments!.value).toBe(current)
    },
  )

  it('still invalidates a pending edit if the existing draft upload receipt changes', async () => {
    const h = imageEdit()
    const read = deferred<Attachment[]>()
    h.options.readAttachments = vi.fn(() => read.promise)
    h.options.pendingAttachments!.value = [{ ...recoveredAttachment('draft.png', 12), kind: 'staged', file_uuid: 'old' }]
    const transcript = h.options.messages.value
    const pending = h.api.editMessage(h.clicked)
    h.options.pendingAttachments!.value[0]!.file_uuid = 'refreshed'
    read.resolve(h.recovered)
    expect(await pending).toBe(false)
    expect(h.options.messages.value).toBe(transcript)
    expect(h.options.pendingAttachments!.value[0]!.file_uuid).toBe('refreshed')
  })

  it.each(['reject', 'incomplete', 'missing-reader', 'missing-composer'])(
    'keeps the transcript and draft intact if recovery fails: %s', async (failure) => {
      const h = imageEdit()
      h.options.inputText.value = 'existing draft'
      h.options.pendingAttachments!.value = [recoveredAttachment('draft.png', 12)]
      const transcript = h.options.messages.value
      const attachments = h.options.pendingAttachments!.value
      if (failure === 'reject') h.options.readAttachments = vi.fn(async () => { throw new Error('expired') })
      if (failure === 'incomplete') h.options.readAttachments = vi.fn(async () => [])
      if (failure === 'missing-reader') h.options.readAttachments = undefined
      if (failure === 'missing-composer') h.options.pendingAttachments = undefined
      expect(await h.api.editMessage(h.clicked)).toBe(false)
      expect(h.options.messages.value).toBe(transcript)
      expect(h.options.inputText.value).toBe('existing draft')
      if (h.options.pendingAttachments) expect(h.options.pendingAttachments.value).toBe(attachments)
      expect(h.pendingForkBeforeMessageId.value).toBeNull()
      expect(h.options.notifyAttachmentRecoveryFailed).toHaveBeenCalledOnce()
      expect(h.options.focusComposer).not.toHaveBeenCalled()
    },
  )

  it.each(['cancel', 'draft-text', 'draft-attachments', 'source-attachments', 'streaming', 'navigate', 'dispose', 'new-edit'])(
    'ignores a stale original attachment read after %s', async (change) => {
      const h = imageEdit()
      h.options.messages.value.push({ role: 'user', text: 'other message', ts: null, messageId: 'other' })
      const read = deferred<Attachment[]>()
      h.options.readAttachments = vi.fn(() => read.promise)
      const session = ref('original-session')
      h.options.sessionIdentity = () => session.value
      const scope = effectScope()
      const actions = scope.run(() => useChatMessageActions(h.options))!
      const result = actions.editMessage(h.clicked)
      const transcript = h.options.messages.value
      expect(h.pendingForkBeforeMessageId.value).toBeNull()
      if (change === 'cancel') expect(actions.cancelEdit()).toBe(true)
      if (change === 'draft-text') h.options.inputText.value = 'newer draft'
      if (change === 'draft-attachments') h.options.pendingAttachments!.value.push(recoveredAttachment('new.png', 99))
      if (change === 'source-attachments') h.options.messages.value[0]!.attachments![0]!.sha256_ref = 'b'.repeat(64)
      if (change === 'streaming') h.options.isStreaming.value = true
      if (change === 'navigate') session.value = 'another-session'
      if (change === 'dispose') scope.stop()
      if (change === 'new-edit') expect(actions.editMessage(renderedMessage({ sourceIndex: 2 }))).toBe(true)
      const draftBeforeCompletion = h.options.inputText.value
      const attachmentsBeforeCompletion = h.options.pendingAttachments!.value
      const messagesBeforeCompletion = h.options.messages.value
      read.resolve(h.recovered)
      expect(await result).toBe(false)
      expect(h.options.inputText.value).toBe(draftBeforeCompletion)
      expect(h.options.pendingAttachments!.value).toBe(attachmentsBeforeCompletion)
      expect(h.options.messages.value).toBe(messagesBeforeCompletion)
      if (change !== 'new-edit') expect(h.options.messages.value).toBe(transcript)
      expect(h.options.notifyAttachmentRecoveryFailed).not.toHaveBeenCalled()
      scope.stop()
    },
  )

  it('does not switch away from an attachment draft whose intake is unfinished', () => {
    const h = imageEdit()
    h.options.attachmentWorkBusy = () => true
    h.options.pendingAttachments!.value = [{ kind: 'uploading', local_id: 12, name: 'draft.png', mime: 'image/png' }]
    expect(h.api.editMessage(h.clicked)).toBe(false)
    expect(h.options.messages.value).toHaveLength(2)
    expect(h.options.pendingAttachments!.value[0]!.kind).toBe('uploading')
    expect(h.options.readAttachments).not.toHaveBeenCalled()
    expect(h.options.notifyDeliveryBlocked).toHaveBeenCalledOnce()
  })

  it('does not commit an edit if attachment intake starts while recovering history', async () => {
    const h = imageEdit()
    const read = deferred<Attachment[]>()
    let busy = false
    h.options.attachmentWorkBusy = () => busy
    h.options.readAttachments = vi.fn(() => read.promise)
    const result = h.api.editMessage(h.clicked)
    busy = true
    read.resolve(h.recovered)
    expect(await result).toBe(false)
    expect(h.options.messages.value).toHaveLength(2)
    expect(h.pendingForkBeforeMessageId.value).toBeNull()
    expect(h.options.notifyDeliveryBlocked).toHaveBeenCalledOnce()
  })
})
