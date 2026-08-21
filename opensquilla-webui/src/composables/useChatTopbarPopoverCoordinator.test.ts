// @vitest-environment happy-dom
import {
  createApp,
  defineComponent,
  h,
  nextTick,
  ref,
  type App,
  type Ref,
} from 'vue'
import { afterEach, describe, expect, it } from 'vitest'

import {
  provideChatTopbarPopoverCoordinator,
  useChatTopbarPopoverCoordination,
  type ChatTopbarPopoverCoordinator,
  type ChatTopbarPopoverId,
} from './useChatTopbarPopoverCoordinator'

const POPOVER_IDS: readonly ChatTopbarPopoverId[] = [
  'session-actions',
  'system-status',
  'language',
  'theme',
  'bgm',
  'desktop-update',
]

interface MountedCoordinator {
  app: App
  controller: ChatTopbarPopoverCoordinator
  enabled: Ref<boolean>
  openById: Map<ChatTopbarPopoverId, Ref<boolean>>
  root: HTMLElement
  visibleIds: Ref<ChatTopbarPopoverId[]>
}

let mounted: MountedCoordinator | null = null

async function mountCoordinator(initiallyEnabled = true): Promise<MountedCoordinator> {
  const enabled = ref(initiallyEnabled)
  const openById = new Map<ChatTopbarPopoverId, Ref<boolean>>()
  const visibleIds = ref<ChatTopbarPopoverId[]>([...POPOVER_IDS])
  let controller: ChatTopbarPopoverCoordinator | null = null

  const Registration = defineComponent({
    props: {
      id: { type: String, required: true },
    },
    setup(props) {
      const id = props.id as ChatTopbarPopoverId
      const open = ref(false)
      openById.set(id, open)
      useChatTopbarPopoverCoordination(id, open)
      return () => h('button', {
        'data-registration': id,
        'aria-expanded': String(open.value),
        onClick: () => { open.value = !open.value },
      }, id)
    },
  })

  const Host = defineComponent({
    setup() {
      controller = provideChatTopbarPopoverCoordinator(enabled)
      return () => h('div', visibleIds.value.map(id => h(Registration, { id, key: id })))
    },
  })

  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp(Host)
  app.mount(root)
  await nextTick()
  mounted = { app, controller: controller!, enabled, openById, root, visibleIds }
  return mounted
}

function state(
  openById: Map<ChatTopbarPopoverId, Ref<boolean>>,
  id: ChatTopbarPopoverId,
): Ref<boolean> {
  const open = openById.get(id)
  if (!open) throw new Error(`Missing registration for ${id}`)
  return open
}

afterEach(() => {
  mounted?.app.unmount()
  mounted = null
  document.body.innerHTML = ''
})

describe('topbar popover coordinator', () => {
  it('keeps exactly the latest enabled popover open across every finite identity', async () => {
    const { controller, openById } = await mountCoordinator()
    for (const id of POPOVER_IDS) {
      state(openById, id).value = true
      expect(controller.activeId.value).toBe(id)
      expect(POPOVER_IDS.filter(candidate => state(openById, candidate).value))
        .toEqual([id])
    }
  })

  it('does not let a stale close clear the newly active owner', async () => {
    const { controller, openById, root } = await mountCoordinator()
    const language = root.querySelector<HTMLButtonElement>('[data-registration="language"]')!
    const bgm = root.querySelector<HTMLButtonElement>('[data-registration="bgm"]')!

    language.focus()
    language.click()
    bgm.focus()
    bgm.click()

    expect(state(openById, 'language').value).toBe(false)
    expect(state(openById, 'bgm').value).toBe(true)
    expect(controller.activeId.value).toBe('bgm')
    expect(document.activeElement).toBe(bgm)

    controller.deactivate('language')
    expect(controller.activeId.value).toBe('bgm')
  })

  it('leaves local popovers independent when coordination is disabled', async () => {
    const { controller, openById } = await mountCoordinator(false)
    state(openById, 'language').value = true
    state(openById, 'bgm').value = true
    await nextTick()

    expect(state(openById, 'language').value).toBe(true)
    expect(state(openById, 'bgm').value).toBe(true)
    expect(controller.activeId.value).toBeNull()
  })

  it('closes local carry-over when coordination enablement changes', async () => {
    const { controller, enabled, openById } = await mountCoordinator(false)
    state(openById, 'language').value = true
    enabled.value = true
    expect(state(openById, 'language').value).toBe(false)
    expect(controller.activeId.value).toBeNull()

    state(openById, 'theme').value = true
    expect(controller.activeId.value).toBe('theme')
    enabled.value = false
    expect(state(openById, 'theme').value).toBe(false)
    expect(controller.activeId.value).toBeNull()
    await nextTick()
  })

  it('compare-and-clears an active registration when its component unmounts', async () => {
    const { controller, openById, visibleIds } = await mountCoordinator()
    state(openById, 'system-status').value = true
    expect(controller.activeId.value).toBe('system-status')

    visibleIds.value = visibleIds.value.filter(id => id !== 'system-status')
    await nextTick()

    expect(controller.activeId.value).toBeNull()
  })

  it('preserves standalone local behavior when no coordinator is provided', async () => {
    const open = ref(false)
    const Standalone = defineComponent({
      setup() {
        useChatTopbarPopoverCoordination('language', open)
        return () => h('button', { onClick: () => { open.value = !open.value } }, 'toggle')
      },
    })
    const root = document.createElement('div')
    document.body.appendChild(root)
    const app = createApp(Standalone)
    app.mount(root)
    root.querySelector('button')?.click()
    await nextTick()

    expect(open.value).toBe(true)
    app.unmount()
  })
})
