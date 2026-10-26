// @vitest-environment happy-dom

import { createApp, defineComponent, h, nextTick, onBeforeUnmount, ref, toRef, type ComponentOptions, type Ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ChatView from './ChatView.vue'
import { useChatStream } from '@/composables/chat/useChatStream'
import type { ChatMessage } from '@/types/chat'

describe('ChatView stream visibility defaults', () => {
  const apps: ReturnType<typeof createApp>[] = []
  let frames: Map<number, FrameRequestCallback>
  let frameId: number

  beforeEach(() => {
    vi.useFakeTimers()
    frames = new Map()
    frameId = 0
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.set(++frameId, callback)
      return frameId
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  })

  afterEach(() => {
    apps.splice(0).forEach(app => app.unmount())
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  function mountStream(surfaceVisible?: Ref<boolean>) {
    let stream!: ReturnType<typeof useChatStream>
    const scroll = vi.fn()
    // Use ChatView's compiled runtime props so Vue performs the same Boolean
    // casting as route/workbench mounts, without unrelated session RPC setup.
    const Host = defineComponent({
      props: (ChatView as ComponentOptions).props,
      setup(props) {
        stream = useChatStream({
          presentationVisible: toRef(props as { surfaceVisible: boolean }, 'surfaceVisible'),
          messages: ref<ChatMessage[]>([]), lastHeaderRole: ref(''),
          aborted: ref(false), autoScroll: ref(true),
          applySessionRunState: vi.fn(), renderMarkdown: text => text,
          stripDirectiveTags: text => text, stripGeneratedArtifactMarkers: text => text,
          scrollToBottom: scroll,
        })
        onBeforeUnmount(() => stream.cleanup())
        return () => h('span', stream.foldedTurn.value.rawText)
      },
    })
    const element = document.createElement('div')
    document.body.appendChild(element)
    const app = createApp(() => h(Host, surfaceVisible ? { surfaceVisible: surfaceVisible.value } : {}))
    app.mount(element)
    apps.push(app)
    return { stream, scroll, element }
  }

  async function paintFrame() {
    const pending = [...frames.values()]
    frames.clear()
    pending.forEach(callback => callback(performance.now()))
    await nextTick()
  }

  it('publishes a normal route with omitted visibility on the next frame', async () => {
    const { stream, scroll, element } = mountStream()
    stream.startStreaming()
    await nextTick()
    stream.appendDelta('visible route')
    expect(frames.size).toBe(1)
    expect(element.textContent).toBe('')
    await paintFrame()
    expect(element.textContent).toBe('visible route')
    expect(scroll).toHaveBeenCalled()
  })

  it('keeps explicit hidden panels on the bounded fallback without scrolling', async () => {
    const { stream, scroll, element } = mountStream(ref(false))
    stream.startStreaming()
    await nextTick()
    stream.appendDelta('hidden panel')
    expect(frames.size).toBe(0)
    vi.advanceTimersByTime(249)
    await nextTick()
    expect(element.textContent).toBe('')
    vi.advanceTimersByTime(1)
    await nextTick()
    expect(element.textContent).toBe('hidden panel')
    expect(scroll).not.toHaveBeenCalled()
  })

  it('resumes a hidden panel on the frame clock without waiting for its old timer', async () => {
    const visible = ref(false)
    const { stream, element } = mountStream(visible)
    stream.startStreaming()
    await nextTick()
    stream.appendDelta('pending panel text')
    vi.advanceTimersByTime(50)
    visible.value = true
    await nextTick()
    expect(frames.size).toBe(1)
    await paintFrame()
    expect(element.textContent).toBe('pending panel text')
  })
})
