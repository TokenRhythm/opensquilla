import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { Agent } from '@/types/agents'
import { useAgentDrawer } from './useAgentDrawer'

function deferredBoolean() {
  let resolve!: (value: boolean) => void
  const promise = new Promise<boolean>((res) => { resolve = res })
  return { promise, resolve }
}

const agents = ref<Agent[]>([{
  id: 'writer',
  name: 'Writer',
  description: 'Original',
  type: 'custom',
}])

describe('useAgentDrawer dirty close ownership', () => {
  it('closes a clean drawer without confirmation', async () => {
    const confirmDiscard = vi.fn(async () => false)
    const drawer = useAgentDrawer(agents, confirmDiscard)
    drawer.openDrawer('edit', 'writer')

    expect(await drawer.requestCloseDrawer()).toBe(true)
    expect(drawer.drawerOpen.value).toBe(false)
    expect(confirmDiscard).not.toHaveBeenCalled()
  })

  it('deduplicates repeated dirty close requests and keeps the drawer on cancel', async () => {
    const decision = deferredBoolean()
    const confirmDiscard = vi.fn(() => decision.promise)
    const drawer = useAgentDrawer(agents, confirmDiscard)
    drawer.openDrawer('edit', 'writer')
    drawer.form.value.description = 'Changed'

    const first = drawer.requestCloseDrawer()
    const repeated = drawer.requestCloseDrawer()
    expect(repeated).toBe(first)
    expect(confirmDiscard).toHaveBeenCalledTimes(1)

    decision.resolve(false)
    expect(await first).toBe(false)
    expect(drawer.drawerOpen.value).toBe(true)
  })

  it('closes after a confirmed dirty request', async () => {
    const confirmDiscard = vi.fn(async () => true)
    const drawer = useAgentDrawer(agents, confirmDiscard)
    drawer.openDrawer('edit', 'writer')
    drawer.form.value.name = 'New writer'

    expect(await drawer.requestCloseDrawer()).toBe(true)
    expect(confirmDiscard).toHaveBeenCalledTimes(1)
    expect(drawer.drawerOpen.value).toBe(false)
  })

  it('keeps Cancel as a return to view mode instead of closing the drawer', async () => {
    const confirmDiscard = vi.fn(async () => true)
    const drawer = useAgentDrawer(agents, confirmDiscard)
    drawer.openDrawer('edit', 'writer')
    drawer.form.value.name = 'New writer'

    drawer.onCancelEdit()
    await nextTick()
    await Promise.resolve()

    expect(drawer.drawerMode.value).toBe('view')
    expect(drawer.drawerOpen.value).toBe(true)
  })
})
