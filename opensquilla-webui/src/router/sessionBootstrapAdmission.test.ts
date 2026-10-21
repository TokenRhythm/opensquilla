// @vitest-environment happy-dom
import { createApp, defineComponent, h, nextTick, onErrorCaptured, onScopeDispose, ref, watch, type Component } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, describe, expect, it } from 'vitest'
import { ScriptTarget, transpileModule } from '@typescript/typescript6'
import { SettingsBackgroundRoute, useSettingsRouteOverlay } from './settingsRouteOverlay'
import { createAppAutomaticRpc } from '@/utils/appAutomaticRpc'
import {
  claimSessionBootstrapAdmission, clearPrimedSessionBootstrapAdmission,
  optionalSessionRpcAllowed, primeSessionBootstrapAdmission,
  registerSessionBootstrapAdmissionOwner,
} from '@/composables/chat/sessionBootstrapAdmission'
import routerSource from './index.ts?raw'
import chatSource from '@/views/ChatView.vue?raw'

function codeBetween(source: string, start: string, end?: string) {
  const begin = source.indexOf(start)
  const finish = end ? source.indexOf(end, begin) : source.length
  if (begin < 0 || finish < begin) throw new Error(`Source boundary changed: ${start}`)
  return transpileModule(source.slice(begin, finish), {
    compilerOptions: { target: ScriptTarget.ES2022 },
  }).outputText
}

// Run the production guard and ChatView ownership wiring, keeping unrelated
// chat services out of this router/overlay lifecycle regression.
const guards = codeBetween(routerSource, 'function isChatRoutePath', '// Capture the leaving route')
  + codeBetween(routerSource, 'router.afterEach(')
const ownership = codeBetween(chatSource, 'let releaseOptionalRpcAdmission:', 'let optionalRpcAdmissionGeneration')
function claimViewAdmission(): () => void {
  return new Function('claimSessionBootstrapAdmission', 'registerSessionBootstrapAdmissionOwner', 'onScopeDispose',
    `${ownership}; return releaseOptionalRpcAdmission`)(
    claimSessionBootstrapAdmission, registerSessionBootstrapAdmissionOwner, onScopeDispose,
  )
}

const cleanups: Array<() => void> = []
afterEach(() => {
  cleanups.splice(0).reverse().forEach(cleanup => cleanup())
  clearPrimedSessionBootstrapAdmission()
  expect(optionalSessionRpcAllowed.value).toBe(true)
})

async function harness(initial = '/chat?session=A', loadChat?: () => Promise<Component>) {
  let mounts = 0, reads = 0
  const Chat = defineComponent({ setup() {
    mounts++
    const release = claimViewAdmission()
    void Promise.resolve().then(release)
    return () => h('div', { class: 'chat' }, 'Chat')
  } })
  const Settings = defineComponent({ render: () => h('div', 'Settings') })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/chat', component: loadChat || (() => Promise.resolve(Chat)), meta: { viewKey: 'chat' } },
    { path: '/chat/new', component: Chat, meta: { viewKey: 'chat' } },
    { path: '/settings', name: 'settings', component: Settings },
    { path: '/usage', component: { render: () => h('div', 'Usage') } },
  ] })
  new Function('router', 'primeSessionBootstrapAdmission', 'clearPrimedSessionBootstrapAdmission', 'routeTitle', 'saveLastRoute', guards)(
    router, primeSessionBootstrapAdmission, clearPrimedSessionBootstrapAdmission, () => '', () => {},
  )
  await router.push(initial)
  const Host = defineComponent({ setup() {
    const { backgroundRoute, contentRoute } = useSettingsRouteOverlay(router)
    return () => h('main', [h(SettingsBackgroundRoute, { route: contentRoute.value }, {
      default: () => h(RouterView, { route: contentRoute.value }, {
        default: ({ Component, route }: any) => Component && h(Component, { key: route.meta.viewKey || route.name }),
      }),
    }), backgroundRoute.value ? h(RouterView) : null])
  } })
  const el = document.createElement('div'); document.body.append(el)
  const app = createApp(Host).use(router); app.mount(el)
  cleanups.push(() => { app.unmount(); el.remove() })
  await nextTick(); await nextTick()
  const auto = createAppAutomaticRpc({ available: () => true, admitted: () => optionalSessionRpcAllowed.value,
    resumeDirectory: async () => {}, subscribeCron: () => {}, loadAgents: async () => {},
    loadSidebar: async () => { reads++; return 'applied' }, cancelSidebar: () => {},
  })
  const stop = watch(optionalSessionRpcAllowed, () => { void auto.admissionChanged() }, { flush: 'sync' })
  cleanups.push(() => { stop(); auto.dispose() })
  await auto.mount()
  return { router, Chat, auto, mounts: () => mounts, reads: () => reads }
}

describe('chat admission across Settings overlay navigation', () => {
  it.each(['/chat?session=A', '/chat?session=B', '/chat/new'])('keeps directory refresh admitted when returning to %s', async target => {
    const h = await harness()
    await h.router.push('/settings')
    await h.router.push(target)
    await nextTick(); await nextTick()
    expect(h.mounts()).toBe(1)
    expect(optionalSessionRpcAllowed.value).toBe(true)
    const before = h.reads()
    await h.auto.load()
    expect(h.reads()).toBe(before + 1)
  })

  it('keeps browser Back admitted and releases ownership when the view unmounts', async () => {
    const h = await harness()
    await h.router.push('/settings')
    const back = new Promise<void>(resolve => {
      const stop = h.router.afterEach(() => { stop(); resolve() })
    })
    h.router.back(); await back; await nextTick()
    expect(h.mounts()).toBe(1)
    expect(optionalSessionRpcAllowed.value).toBe(true)
    await h.router.push('/usage'); await nextTick()
    primeSessionBootstrapAdmission()
    expect(optionalSessionRpcAllowed.value).toBe(false)
    clearPrimedSessionBootstrapAdmission()
    await h.router.push('/chat?session=B'); await nextTick(); await nextTick()
    expect(h.mounts()).toBe(2)
    expect(optionalSessionRpcAllowed.value).toBe(true)
  })

  it('holds a cold Settings-to-chat entry until lazy setup claims it', async () => {
    let resolve!: (component: Component) => void
    let started!: () => void
    const lazy = new Promise<Component>(done => { resolve = done })
    const chunkRequested = new Promise<void>(done => { started = done })
    const h = await harness('/settings', () => { started(); return lazy })
    const navigation = h.router.push('/chat?session=A')
    await chunkRequested
    expect(h.mounts()).toBe(0)
    expect(optionalSessionRpcAllowed.value).toBe(false)
    resolve(h.Chat); await navigation; await nextTick(); await nextTick()
    expect(h.mounts()).toBe(1)
    expect(optionalSessionRpcAllowed.value).toBe(true)
  })

  it('releases cold-entry priming on aborted navigation and lazy-load failure', async () => {
    const h = await harness('/settings', async () => { throw new Error('Chunk unavailable') })
    const abort = h.router.beforeEach(to => {
      if (to.path !== '/chat') return
      expect(optionalSessionRpcAllowed.value).toBe(false)
      return false
    })
    await h.router.push('/chat')
    expect(optionalSessionRpcAllowed.value).toBe(true)
    abort()
    await expect(h.router.push('/chat')).rejects.toThrow('Chunk unavailable')
    expect(optionalSessionRpcAllowed.value).toBe(true)
  })

  it('disposes ownership and the initial hold when setup fails inside an error boundary', async () => {
    primeSessionBootstrapAdmission()
    const FailedChat = defineComponent({ setup() {
      claimViewAdmission()
      throw new Error('Failed setup')
    }, render: () => null })
    const Host = defineComponent({ setup() {
      const failed = ref(false)
      onErrorCaptured(() => { failed.value = true; return false })
      return () => failed.value ? h('div', 'Error') : h(FailedChat)
    } })
    const el = document.createElement('div')
    const app = createApp(Host); app.mount(el)
    cleanups.push(() => app.unmount())
    await nextTick(); await nextTick()
    expect(optionalSessionRpcAllowed.value).toBe(true)
    primeSessionBootstrapAdmission()
    expect(optionalSessionRpcAllowed.value).toBe(false)
  })
})
