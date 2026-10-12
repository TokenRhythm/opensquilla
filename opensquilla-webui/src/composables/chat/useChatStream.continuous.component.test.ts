// @vitest-environment happy-dom
import { createApp, h, nextTick, ref } from 'vue'
import { expect, it, vi } from 'vitest'
import type { ChatMessage } from '@/types/chat'
import StreamingTextPart from '@/components/chat/parts/StreamingTextPart.vue'
import { useChatStream } from './useChatStream'

it('renders newly arriving text past 1 MiB through the existing live message component', async () => {
  vi.useFakeTimers()
  const renderMarkdown = vi.fn((text: string) => text)
  const stream = useChatStream({
    messages: ref<ChatMessage[]>([]), lastHeaderRole: ref(''), aborted: ref(false), autoScroll: ref(false),
    applySessionRunState: vi.fn(), renderMarkdown,
    stripDirectiveTags: text => text, stripGeneratedArtifactMarkers: text => text,
    scrollToBottom: vi.fn(),
  })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ setup: () => () => h(StreamingTextPart, {
    rawText: stream.foldedTurn.value.rawText, renderMarkdown,
  }) })
  app.mount(host)
  try {
    const prefix = '0123456789abcdef'.repeat(65536)
    stream.appendDelta(prefix)
    vi.advanceTimersByTime(300)
    await nextTick()
    expect(host.textContent).toBe(prefix)
    stream.appendDelta('接着到达的正文🙂')
    vi.advanceTimersByTime(300)
    await nextTick()
    expect(host.textContent).toBe(prefix + '接着到达的正文🙂')
    expect(stream.streamPreviewComplete.value).toBe(true)
    expect(stream.isStreaming.value).toBe(true)
  } finally {
    stream.cleanup()
    app.unmount()
    host.remove()
    vi.useRealTimers()
  }
})
