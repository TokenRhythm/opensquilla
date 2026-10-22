// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  compile, computed, createApp, defineComponent, getCurrentInstance, h, nextTick,
  onBeforeUnmount, ref, toRef, type ComponentInternalInstance, type ComponentOptions,
} from 'vue'
import appSource from './App.vue?raw'
import ChatView from './views/ChatView.vue'
import { useChatStream } from './composables/chat/useChatStream'
import type { ChatMessage } from './types/chat'

function sourceBetween(start: string, end: string): string {
  const begin = appSource.indexOf(start)
  const finish = appSource.indexOf(end, begin + start.length)
  if (begin < 0 || finish < 0) throw new Error(`Missing App route boundary: ${start}`)
  return appSource.slice(begin, finish)
}

// Exercise App's actual router template and derived visibility, without starting
// unrelated Gateway services. The child uses ChatView's compiled Boolean props
// and the real stream scheduler, so a missing host binding changes DOM behavior.
const routeTemplate = sourceBetween('<router-view :route="settingsContentRoute"', '</router-view>') + '</router-view>'
const visibilitySource = [
  sourceBetween('const isChatRoute = computed(', '\n'),
  sourceBetween('const mainPaneHidden = computed(', '\n'),
].join('\n')

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

function mountRoute(keepAlive = false, transition = 'none', path = '/chat') {
  const settingsContentRoute = ref({ path, name: 'route', meta: { keepAlive, routeTransition: transition, viewKey: 'route' } })
  const workbenchSinglePane = ref(false)
  const workbenchLayoutMotion = ref(false)
  let stream!: ReturnType<typeof useChatStream>
  let child!: ComponentInternalInstance
  let mounts = 0
  let unmounts = 0
  const messages = ref<ChatMessage[]>([])
  const scroll = vi.fn()
  const Conversation = defineComponent({
    props: (ChatView as ComponentOptions).props,
    setup(props) {
      mounts++
      child = getCurrentInstance()!
      stream = useChatStream({
        presentationVisible: toRef(props as { surfaceVisible: boolean }, 'surfaceVisible'),
        messages, lastHeaderRole: ref(''), aborted: ref(false), autoScroll: ref(true),
        applySessionRunState: vi.fn(), renderMarkdown: text => text,
        stripDirectiveTags: text => text, stripGeneratedArtifactMarkers: text => text,
        scrollToBottom: scroll,
      })
      onBeforeUnmount(() => { unmounts++; stream.cleanup() })
      return () => h('span', stream.foldedTurn.value.rawText)
    },
  })
  const RouterView = defineComponent({
    props: ['route'],
    setup(props, { slots }) { return () => slots.default?.({ Component: Conversation, route: props.route }) },
  })
  const Shell = defineComponent({
    components: { RouterView },
    setup() {
      const visibility = new Function('computed', 'settingsContentRoute', 'workbenchSinglePane', 'workbenchLayoutMotion',
        `${visibilitySource}; return { isChatRoute, mainPaneHidden }`)(computed, settingsContentRoute, workbenchSinglePane, workbenchLayoutMotion)
      return { settingsContentRoute, ...visibility }
    },
    render: compile(routeTemplate),
  })
  const element = document.createElement('div')
  document.body.appendChild(element)
  const app = createApp(Shell)
  app.mount(element)
  apps.push(app)
  return { stream, element, messages, scroll, workbenchSinglePane, workbenchLayoutMotion,
    mounts: () => mounts, unmounts: () => unmounts, visible: () => child.props.surfaceVisible,
    suppliedProps: () => child.vnode.props }
}

async function paintFrame() {
  const pending = [...frames.values()]
  frames.clear()
  pending.forEach(callback => callback(performance.now()))
  await nextTick()
}

describe('App main conversation visibility forwarding', () => {
  it.each([
    [false, 'none'], [true, 'none'], [false, 'fade'], [true, 'fade'],
  ] as const)('retains the stream through visible, hidden and restored states (keepAlive=%s, transition=%s)', async (keepAlive, transition) => {
    const route = mountRoute(keepAlive, transition)
    route.stream.startStreaming()
    await nextTick()
    route.stream.appendDelta('visible')
    expect(frames.size).toBe(1)
    await paintFrame()
    expect(route.element.textContent).toBe('visible')

    route.workbenchSinglePane.value = true
    await nextTick()
    expect(route.visible()).toBe(false)
    route.scroll.mockClear()
    route.stream.appendDelta(' hidden')
    expect(frames.size).toBe(0)
    vi.advanceTimersByTime(249)
    await nextTick()
    expect(route.element.textContent).toBe('visible')
    vi.advanceTimersByTime(1)
    await nextTick()
    expect(route.element.textContent).toBe('visible hidden')
    expect(route.scroll).not.toHaveBeenCalled()
    expect(route.stream.isStreaming.value).toBe(true)

    route.stream.appendDelta(' pending')
    vi.advanceTimersByTime(50)
    route.workbenchSinglePane.value = false
    await nextTick()
    expect(route.visible()).toBe(true)
    expect(frames.size).toBe(1)
    await paintFrame()
    expect(route.element.textContent).toBe('visible hidden pending')
    expect(route.scroll).toHaveBeenCalled()
    const scrollCalls = route.scroll.mock.calls.length
    vi.advanceTimersByTime(250)
    expect(route.scroll).toHaveBeenCalledTimes(scrollCalls)
    expect(route.mounts()).toBe(1)
    expect(route.unmounts()).toBe(0)
    route.stream.endStreaming()
    expect(route.messages.value.some(message => message.text === 'visible hidden pending')).toBe(true)
  })

  it('keeps the outgoing main pane visible until its layout animation finishes', async () => {
    const route = mountRoute()
    route.workbenchLayoutMotion.value = true
    route.workbenchSinglePane.value = true
    await nextTick()
    expect(route.visible()).toBe(true)
    route.workbenchLayoutMotion.value = false
    await nextTick()
    expect(route.visible()).toBe(false)
    route.workbenchSinglePane.value = false
    await nextTick()
    expect(route.visible()).toBe(true)
  })

  it('does not pass chat presentation props into unrelated route components', async () => {
    const route = mountRoute(false, 'none', '/settings')
    route.workbenchSinglePane.value = true
    await nextTick()
    expect(route.suppliedProps()).not.toHaveProperty('surfaceVisible')
    expect(route.suppliedProps()).not.toHaveProperty('surface-visible')
  })
})
