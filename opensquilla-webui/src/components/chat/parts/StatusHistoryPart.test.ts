// @vitest-environment happy-dom
import { createApp, h } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import StatusHistoryPart from './StatusHistoryPart.vue'
import statusHistoryPartSource from './StatusHistoryPart.vue?raw'
import type { StatusPart } from '@/types/parts'

const mountedApps: ReturnType<typeof createApp>[] = []

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        activitySteps: 'Activity · {count} step | Activity · {count} steps',
      },
    },
  },
})

function mount(entries: StatusPart[]) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(StatusHistoryPart, { entries }) })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

beforeEach(() => {
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('StatusHistoryPart disclosure', () => {
  it('always renders the collapsible details disclosure', () => {
    const host = mount([
      { action: 'route', label: 'Routing', at: 0 },
      { action: 'run', label: 'Running', at: 1500 },
    ])
    const fold = host.querySelector<HTMLDetailsElement>('details.status-history')
    expect(fold).not.toBeNull()
    expect(fold?.open).toBe(false)
    expect(fold?.querySelector('.status-history__summary')?.textContent)
      .toContain('Activity · 2 steps')
    expect(host.querySelectorAll('.status-history__row')).toHaveLength(2)
  })

  it('has no embedded/static rendering mode', () => {
    // The static section variant was unreachable dead code; the component is
    // a plain <details> fold with no mode switch.
    expect(statusHistoryPartSource).not.toContain('embedded')
    expect(statusHistoryPartSource).not.toContain('--static')
  })

  it('shows the time spent in each phase as the gap to the next entry', () => {
    const host = mount([
      { action: 'a', label: 'A', at: 0 },
      { action: 'b', label: 'B', at: 500 },
      { action: 'c', label: 'C', at: 2000 },
      { action: 'd', label: 'D', at: 92000 },
    ])
    const gaps = Array.from(
      host.querySelectorAll('.status-history__row'),
      row => row.querySelector('.status-history__gap')?.textContent ?? '',
    )
    // Last phase has no successor, so it renders no duration.
    expect(gaps).toEqual(['500ms', '1.5s', '1m 30s', ''])
  })
})
