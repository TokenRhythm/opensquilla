// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, type App } from 'vue'

import i18n from '@/i18n'
import ChatSessionSearch from './ChatSessionSearch.vue'

const mountedApps: App[] = []

interface MountOptions {
  open?: boolean
  query?: string
  active?: number
  total?: number
}

function mountSearch(options: MountOptions = {}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const emitted: Record<string, unknown[]> = {}
  const app = createApp({
    data: () => ({ ...options }),
    methods: {
      record(event: string) {
        return (payload?: unknown) => {
          emitted[event] = emitted[event] || []
          emitted[event].push(payload ?? true)
        }
      },
    },
    template: `
      <ChatSessionSearch
        :open="open"
        :query="query"
        :active="active ?? 0"
        :total="total ?? 0"
        @update:query="record('update:query')($event)"
        @next="record('next')()"
        @prev="record('prev')()"
        @close="record('close')()"
      />
    `,
    components: { ChatSessionSearch },
  })
  app.use(i18n)
  app.mount(host)
  mountedApps.push(app)
  return { host, emitted }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('ChatSessionSearch', () => {
  it('renders nothing while closed and focuses the input when opened', async () => {
    const first = mountSearch({ open: false, query: 'alpha', total: 2 })
    expect(first.host.querySelector('[data-testid="session-search"]')).toBeNull()

    const second = mountSearch({ open: true, query: 'alpha', total: 2 })
    const input = second.host.querySelector<HTMLInputElement>('[data-testid="session-search-input"]')!
    expect(input).toBeTruthy()
    await vi.waitFor(() => expect(document.activeElement).toBe(input))
    expect(input.value).toBe('alpha')
  })

  it('shows the localized match count and navigates on the controls', async () => {
    const { host, emitted } = mountSearch({ open: true, query: 'alpha', active: 1, total: 3 })
    expect(host.querySelector('[data-testid="session-search-count"]')?.textContent?.trim()).toBe('2/3')

    host.querySelector<HTMLButtonElement>('[data-testid="session-search-next"]')?.click()
    host.querySelector<HTMLButtonElement>('[data-testid="session-search-prev"]')?.click()
    host.querySelector<HTMLButtonElement>('[data-testid="session-search-close"]')?.click()

    expect(emitted.next).toHaveLength(1)
    expect(emitted.prev).toHaveLength(1)
    expect(emitted.close).toHaveLength(1)
  })

  it('reports no matches for an empty result set and disables stepping', () => {
    const { host, emitted } = mountSearch({ open: true, query: 'alpha', total: 0 })
    expect(host.querySelector('[data-testid="session-search-count"]')?.textContent?.trim()).toBe('No matches')

    host.querySelector<HTMLButtonElement>('[data-testid="session-search-next"]')?.click()
    host.querySelector<HTMLButtonElement>('[data-testid="session-search-prev"]')?.click()
    expect(emitted.next).toBeUndefined()
    expect(emitted.prev).toBeUndefined()
  })

  it('emits query updates, navigation on Enter, reverse on Shift+Enter, and closes on Escape', async () => {
    const { host, emitted } = mountSearch({ open: true, query: '', total: 2 })
    const input = host.querySelector<HTMLInputElement>('[data-testid="session-search-input"]')!

    input.value = 'needle'
    input.dispatchEvent(new Event('input'))
    expect(emitted['update:query']).toEqual(['needle'])

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(emitted.next).toHaveLength(1)
    expect(emitted.prev).toBeUndefined()

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true }))
    expect(emitted.prev).toHaveLength(1)

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(emitted.close).toHaveLength(1)
  })
})
