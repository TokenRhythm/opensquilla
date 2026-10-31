// @vitest-environment happy-dom

import { createApp, h, KeepAlive, nextTick, type VNode } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { OBSERVABILITY_KEY, type Observability } from '@/modules/observability'
import { SESSION_DIRECTORY_KEY, type SessionDirectory } from '@/modules/sessionDirectory'
import { usageSnapshot, usageTotals } from '@/testing/usage.test-helper'
import UsageView from './UsageView.vue'

const mounted: Array<ReturnType<typeof createApp>> = []

beforeEach(() => {
  i18n.global.locale.value = 'en'
  localStorage.clear()
  localStorage.setItem('opensquilla-usage-range', '14')
})

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  localStorage.clear()
  vi.useRealTimers()
})

async function settle() {
  await Promise.resolve()
  await Promise.resolve()
  await nextTick()
}

async function mountUsage(path: string) {
  const usage = vi.fn<Observability['usage']>().mockImplementation(async range => usageSnapshot({
    asOfMs: Date.now(),
    totals: usageTotals({ input: 120, output: 30 }),
    range: { preset: range === 'today' ? 'today' : `last_${range}_calendar_days`, fromMs: 0, toMs: 100 },
  }))
  const directory: SessionDirectory = {
    listPage: vi.fn().mockResolvedValue({ items: [], hasMore: false, nextCursor: null }),
    count: vi.fn().mockResolvedValue({ value: 0, exact: true }),
    resolve: vi.fn().mockResolvedValue({ key: 'synthetic-session', id: 'synthetic-session' }),
    search: vi.fn().mockResolvedValue({ sessions: [], messages: [] }),
  }
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/usage', name: 'usage', component: UsageView },
      { path: '/chat', name: 'chat', component: { render: () => h('div', 'Chat') } },
    ],
  })
  await router.push(path)
  const app = createApp({
    render: () => h(RouterView, null, {
      default: ({ Component }: { Component: VNode }) => h(KeepAlive, null, { default: () => Component }),
    }),
  })
  app.use(router).use(i18n)
  app.provide(OBSERVABILITY_KEY, {
    usage,
    tailLogs: vi.fn(),
    updateNotice: vi.fn(),
    downloadSupportBundle: vi.fn(),
  } satisfies Observability)
  app.provide(SESSION_DIRECTORY_KEY, directory)
  const root = document.createElement('div')
  document.body.appendChild(root)
  app.mount(root)
  mounted.push(app)
  await settle()
  return { router, root, usage }
}

function rangeButtons(root: HTMLElement) {
  return root.querySelectorAll('.usage-chart [role="group"]')[1]!
    .querySelectorAll<HTMLButtonElement>('button')
}

describe('Usage page range links', () => {
  it('loads today exactly once when opening a direct menu link', async () => {
    const { root, usage } = await mountUsage('/usage?range=today')
    expect(usage.mock.calls.map(call => call[0])).toEqual(['today'])
    expect(rangeButtons(root)[1]!.getAttribute('aria-pressed')).toBe('true')
    expect(localStorage.getItem('opensquilla-usage-range')).toBe('today')
  })

  it('reselects today when returning to a cached Usage view from the menu', async () => {
    const { router, root, usage } = await mountUsage('/usage')
    const firstView = root.querySelector('.usage-stage')
    await router.push('/chat')
    await settle()
    await router.push('/usage?range=today')
    await settle()
    expect(root.querySelector('.usage-stage')).toBe(firstView)
    expect(usage.mock.calls.map(call => call[0])).toEqual(['14', 'today'])
    expect(rangeButtons(root)[1]!.getAttribute('aria-pressed')).toBe('true')
  })

  it('handles active view links and resumes persisted filters after a manual choice', async () => {
    const { router, root, usage } = await mountUsage('/usage')
    await router.push('/usage?range=today&other=preserved')
    await settle()
    expect(usage.mock.calls.map(call => call[0])).toEqual(['14', 'today'])

    const replace = vi.spyOn(router, 'replace')
    rangeButtons(root)[2]!.click()
    await replace.mock.results[0]!.value
    await settle()
    expect(router.currentRoute.value.query).toEqual({ other: 'preserved' })
    expect(localStorage.getItem('opensquilla-usage-range')).toBe('7')
    expect(usage.mock.calls.map(call => call[0])).toEqual(['14', 'today', '7'])

    await router.push('/chat')
    await router.push('/usage')
    await settle()
    expect(usage.mock.calls[usage.mock.calls.length - 1]![0]).toBe('7')
    await router.push('/usage?range=today')
    await settle()
    expect(usage.mock.calls[usage.mock.calls.length - 1]![0]).toBe('today')
  })

  it.each(['unsupported', 'today&range=7'])('ignores invalid range query %s', async query => {
    const { usage } = await mountUsage(`/usage?range=${query}`)
    expect(usage.mock.calls.map(call => call[0])).toEqual(['14'])
  })

  it('restores today as a normal saved filter without requiring a query', async () => {
    localStorage.setItem('opensquilla-usage-range', 'today')
    const { usage } = await mountUsage('/usage')
    expect(usage.mock.calls.map(call => call[0])).toEqual(['today'])
  })

  it('does not fall back to yesterday on a failed Today refresh after midnight', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 0, 1, 23, 59))
    const { root, usage } = await mountUsage('/usage?range=today')
    expect(root.textContent).toContain('150')
    usage.mockRejectedValueOnce(new Error('synthetic gateway unavailable'))
    vi.setSystemTime(new Date(2026, 0, 2, 0, 1))
    root.querySelector<HTMLButtonElement>('button[title="Refresh"]')!.click()
    await settle()
    expect(usage.mock.calls[1]![1]?.cachedSnapshot).toBeNull()
    expect(root.textContent).not.toContain('150')
    expect(root.textContent).toContain('synthetic gateway unavailable')
  })
})
