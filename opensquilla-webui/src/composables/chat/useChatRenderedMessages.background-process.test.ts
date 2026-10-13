import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { ChatMessage, RawToolCallPayload } from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'
import {
  stripBackgroundProcessNotice,
  stripBackgroundProcessNoticeSegments,
} from '@/utils/chat/backgroundProcessNotice'
import { useChatRenderedMessages } from './useChatRenderedMessages'
import { useChatMessageActions } from './useChatMessageActions'
import { buildChatMarkdown } from './useChatMarkdownExport'

vi.mock('@/utils/browser', () => ({
  copyTextWithFallback: vi.fn().mockResolvedValue(undefined),
  downloadText: vi.fn(),
}))

const notice = 'Background process status: exec_command (execution_id=test-execution). '
  + 'A running process was reported; no exit result was recorded in this turn.'
const answer = '网站已启动，打开 http://localhost:3001。'
const receipt: RawToolCallPayload = {
  type: 'tool_result', name: 'exec_command', tool_use_id: 'server-start',
  result: JSON.stringify({
    execution_id: 'test-execution',
    session: { session_id: 'test-execution', status: 'running', returncode: null },
  }),
  // Live commits retain the result but not the original background_running reason.
  execution_status: { status: 'success' }, is_error: false,
}

function render(messages: ChatMessage[]) {
  return useChatRenderedMessages({
    messages: ref(messages), sessionKey: ref('agent:main:webchat:notice-test'),
    routerSlots: ref([]), routerModels: ref({}), routerTierConfigs: ref({}),
    routerVisualEffectsEnabled: ref(false), routerVisualMode: ref('real_candidates'),
    renderMarkdown: text => `<p>${text}</p>`,
    stripGeneratedArtifactMarkers: text => text, stripTimePrefix: text => text,
    isSubagentCompletionMessage: () => false,
  }).renderedMessages.value
}

describe('background process notice presentation', () => {
  it.each(['explicit', 'persisted'] as const)(
    'hides the runtime footer in %s history, copy and export without changing the receipt',
    async (format) => {
      const source: ChatMessage = {
        role: 'assistant', text: `${answer}\n\n${notice}`, ts: null,
        tool_calls: [
          { type: 'tool_use', name: 'exec_command', tool_use_id: 'server-start',
            input: { command: 'node server.js', yield_time_ms: 0 } },
          receipt,
          ...(format === 'persisted' ? [
            { type: 'text', text: answer }, { type: 'text', text: notice },
          ] : []),
        ],
        ...(format === 'explicit' ? {
          timeline: [{ type: 'text', raw: `${answer}\n\n${notice}` }],
        } : {}),
      }
      const original = JSON.stringify(source)
      const [message] = render([source])
      expect(message!.text).toBe(answer)
      expect(message!.timelineItems?.filter(item => item.type === 'text')
        .map(item => item.rawText)).toEqual([answer])
      expect(message!.parts?.filter(part => part.type === 'text')
        .map(part => part.rawText)).toEqual([answer])
      expect(message!.toolCalls?.[0]?.result).toBe(receipt.result)
      expect(JSON.stringify(source)).toBe(original)

      const actions = useChatMessageActions({
        messages: ref([source]), inputText: ref(''), isStreaming: ref(false),
        sanitizeCopyText: text => text, stripTimePrefix: text => text,
        autoResizeTextarea: vi.fn(), sendCurrentInput: vi.fn(),
        sendUsageBarrierReplay: vi.fn(async () => true), focusComposer: vi.fn(),
        pendingForkBeforeMessageId: ref(null),
      })
      await actions.copyMessage(message!)
      expect(copyTextWithFallback).toHaveBeenLastCalledWith(answer)
      const exported = buildChatMarkdown({
        messages: [message!], title: 'Website', exportedAt: '2026-09-22',
        aiGeneratedLabel: 'AI generated',
      })
      expect(exported).toContain(answer)
      expect(exported).not.toContain('Background process status:')
    },
  )

  it('keeps the exact notice when the user quotes it', () => {
    expect(render([{ role: 'user', text: notice, ts: null }])[0]!.text).toBe(notice)
  })

  it.each([
    `The tool returned: ${notice}`,
    `> ${notice}`,
    `    ${notice}`,
    `Example:\n\n\`\`\`text\n${notice}\n\`\`\``,
    `Example:\n\n~~~\n${notice}`,
    `${notice}\n\nThis is an explanation of that message.`,
    notice.replace('test-execution', 'another-process'),
    'Error: the server could not start (EADDRINUSE).',
  ])('preserves documentation, other executions and errors: %s', (text) => {
    expect(stripBackgroundProcessNotice(text, [receipt])).toBe(text)
  })

  it('requires a matching non-error process receipt', () => {
    expect(stripBackgroundProcessNotice(notice, [])).toBe(notice)
    expect(stripBackgroundProcessNotice(notice, [{ ...receipt, is_error: true }])).toBe(notice)
    expect(stripBackgroundProcessNotice(notice, [{ ...receipt, result: 'unavailable' }])).toBe(notice)
    expect(stripBackgroundProcessNotice(notice, [{ ...receipt, name: 'web_search' }])).toBe(notice)
  })

  it('projects a standalone live footer while preserving fences across text segments', () => {
    expect(stripBackgroundProcessNoticeSegments([answer, `\n\n${notice}`], [receipt]))
      .toEqual([answer, ''])
    const example = ['Example:\n\n```text\n', notice]
    expect(stripBackgroundProcessNoticeSegments(example, [receipt])).toEqual(example)
    expect(stripBackgroundProcessNotice(`${answer}\r\n\r\n${notice}\r\n`, [receipt])).toBe(answer)
  })
})
