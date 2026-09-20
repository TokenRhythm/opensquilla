// @vitest-environment happy-dom
import { createApp, defineComponent, h, nextTick, onUnmounted, ref } from 'vue'
import { createMemoryHistory, createRouter, RouterView, useRoute } from 'vue-router'
import { afterEach, describe, expect, it } from 'vitest'
import { SettingsBackgroundRoute, useSettingsRouteOverlay } from './settingsRouteOverlay'

const cleanups: Array<() => void> = []
afterEach(() => { for (const cleanup of cleanups.splice(0)) cleanup() })

async function harness(initial = '/chat/new?agent=research&project=project-a') {
  let mounts = 0
  let unmounts = 0
  const draft = {
    text: 'Unsent instruction',
    attachments: [new File(['source'], 'notes.txt')],
    selection: { model: 'model-a', provider: 'provider-a' },
    mode: 'off',
  }
  let activeDraft: typeof draft | undefined
  const Chat = defineComponent({
    setup() {
      mounts += 1
      const route = useRoute()
      activeDraft = ref(draft).value
      onUnmounted(() => { unmounts += 1 })
      return () => h('div', { class: 'chat', 'data-path': route.fullPath }, activeDraft!.text)
    },
  })
  const Settings = defineComponent({
    setup() {
      const route = useRoute()
      return () => h('div', { class: 'settings', 'data-section': route.params.section }, 'Settings')
    },
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/chat', component: Chat, meta: { viewKey: 'chat' } },
      { path: '/chat/new', component: Chat, meta: { viewKey: 'chat' } },
      { path: '/settings', name: 'settings', component: Settings },
      { path: '/settings/:section', name: 'settings-section', component: Settings },
      { path: '/usage', component: { render: () => h('div', 'Usage') } },
    ],
  })
  await router.push(initial)
  const Host = defineComponent({
    setup() {
      const { backgroundRoute, contentRoute } = useSettingsRouteOverlay(router)
      return () => h('main', [
        h(SettingsBackgroundRoute, { route: contentRoute.value }, {
          default: () => h(RouterView, { route: contentRoute.value }, {
            default: ({ Component, route }: any) => Component && h(Component, { key: route.meta.viewKey || route.name }),
          }),
        }),
        backgroundRoute.value ? h(RouterView) : null,
      ])
    },
  })
  const node = document.createElement('div')
  document.body.append(node)
  const app = createApp(Host).use(router)
  app.mount(node)
  await nextTick()
  cleanups.push(() => { app.unmount(); node.remove() })
  return { router, node, draft, currentDraft: () => activeDraft, mounts: () => mounts, unmounts: () => unmounts }
}

describe('Settings over a live chat', () => {
  it('preserves the exact composer instance and route through Settings section changes and close', async () => {
    const h = await harness()
    const before = h.currentDraft()
    const file = before!.attachments[0]
    const original = h.router.currentRoute.value.fullPath
    await h.router.push('/settings/modelStrategy')
    expect(h.mounts()).toBe(1)
    expect(h.unmounts()).toBe(0)
    expect(h.node.querySelector('.chat')?.getAttribute('data-path')).toBe(original)
    expect(h.node.querySelector('.settings')?.getAttribute('data-section')).toBe('modelStrategy')
    await h.router.replace('/settings/provider')
    expect(h.node.querySelector('.settings')?.getAttribute('data-section')).toBe('provider')
    expect(h.currentDraft()).toBe(before)
    expect(h.currentDraft()!.attachments[0]).toBe(file)
    await h.router.push(original)
    expect(h.node.querySelector('.settings')).toBeNull()
    expect(h.currentDraft()).toBe(before)
    expect(h.currentDraft()).toMatchObject({ text: 'Unsent instruction', selection: h.draft.selection, mode: 'off' })
    expect(h.mounts()).toBe(1)
  })

  it('keeps a model-only empty draft and exits Settings through browser Back', async () => {
    const h = await harness()
    h.currentDraft()!.text = ''
    h.currentDraft()!.attachments = []
    const before = h.currentDraft()
    await h.router.push('/settings/modelStrategy')
    const back = new Promise<void>(resolve => {
      const stop = h.router.afterEach(() => { stop(); resolve() })
    })
    h.router.back()
    await back
    await nextTick()
    expect(h.currentDraft()).toBe(before)
    expect(h.currentDraft()!.selection).toEqual(h.draft.selection)
    expect(h.node.querySelector('.settings')).toBeNull()
    expect(h.mounts()).toBe(1)
  })

  it('closes the overlay and exposes the newly selected chat URL without restoring the old draft URL', async () => {
    const h = await harness()
    await h.router.push('/settings/modelStrategy')
    await h.router.push('/chat?session=agent:main:webchat:other')
    expect(h.node.querySelector('.settings')).toBeNull()
    expect(h.node.querySelector('.chat')?.getAttribute('data-path')).toBe('/chat?session=agent:main:webchat:other')
    expect(h.mounts()).toBe(1)
    await h.router.push('/usage')
    expect(h.node.querySelector('.chat')).toBeNull()
    expect(h.unmounts()).toBe(1)
  })

  it.each(['/settings/modelStrategy', '/usage'])('does not manufacture a background chat for %s', async initial => {
    const h = await harness(initial)
    await h.router.push('/settings/provider')
    expect(h.mounts()).toBe(0)
    expect(h.node.querySelectorAll('.settings')).toHaveLength(1)
    expect(h.node.querySelector('.chat')).toBeNull()
  })
})
