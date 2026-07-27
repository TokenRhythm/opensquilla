// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import EmptyStateChips from './EmptyStateChips.vue'

vi.mock('@/composables/useRpc', () => ({
  useRpcCall: () => ({
    data: ref(null),
    loading: ref(false),
    error: ref(null),
    execute: vi.fn(),
  }),
}))

async function mountChips(props: {
  suppressed?: boolean
  metaSkills?: Array<{ value: string; description: string }>
  onPick?: (text: string) => void
} = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(EmptyStateChips, {
    agentId: 'main',
    ...props,
  })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('EmptyStateChips', () => {
  it('hides both task and meta-skill actions while suppressed', async () => {
    const { app, el } = await mountChips({
      suppressed: true,
      metaSkills: [{ value: 'meta-paper-write', description: 'Draft a paper' }],
    })

    expect(el.querySelector('.empty-state__chips')).toBeNull()
    expect(el.querySelector('.empty-state__meta')).toBeNull()
    expect(el.querySelector('.empty-state__greeting')).not.toBeNull()
    app.unmount()
  })

  it('emits both ordinary and meta-skill choices when available', async () => {
    const onPick = vi.fn()
    const { app, el } = await mountChips({
      metaSkills: [{ value: 'meta-paper-write', description: 'Draft a paper' }],
      onPick,
    })

    el.querySelector<HTMLButtonElement>('.empty-state__chip')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    el.querySelector<HTMLButtonElement>('.empty-state__meta-chip')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()

    expect(onPick).toHaveBeenCalledTimes(2)
    expect(onPick).toHaveBeenLastCalledWith('/meta meta-paper-write')
    app.unmount()
  })
})
