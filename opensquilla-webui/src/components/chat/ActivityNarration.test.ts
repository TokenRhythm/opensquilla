// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp, h, type App } from 'vue'
import i18n from '@/i18n'
import ActivityNarration from './ActivityNarration.vue'
import type { ChatStreamTimelineItem } from '@/types/chat'

const mountedApps: App[] = []

function narration(rawText: string): Extract<ChatStreamTimelineItem, { type: 'text' }> {
  return {
    type: 'text',
    key: `narration:${rawText.length}`,
    rawText,
    html: `<p>${rawText}</p>`,
  }
}

function mount(rawText: string) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(ActivityNarration, { item: narration(rawText) }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ActivityNarration progressive disclosure', () => {
  it('keeps a short readable update directly visible', () => {
    const host = mount('Checked the project and found the routing delay.')

    expect(host.querySelector('details')).toBeNull()
    expect(host.querySelector('.activity-narration--plain')?.textContent)
      .toContain('Checked the project')
  })

  it('summarizes a long update and keeps its full body collapsed', () => {
    const host = mount(
      'I checked the project flow and verified the current session ownership. '.repeat(8),
    )
    const fold = host.querySelector<HTMLDetailsElement>('details.activity-narration')

    expect(fold).not.toBeNull()
    expect(fold?.open).toBe(false)
    expect(fold?.querySelector('.activity-narration__summary-text')?.textContent)
      .toMatch(/I checked the project flow/)
    expect(fold?.querySelector('.activity-narration__hint')?.textContent)
      .toContain('view details')
  })

  it('replaces command and error prose with a plain technical-details label', () => {
    const technical = 'code-task failed with exit_code=1 and stderr=permission denied'
    const host = mount(technical)
    const fold = host.querySelector<HTMLDetailsElement>('details.activity-narration--technical')

    expect(fold).not.toBeNull()
    expect(fold?.open).toBe(false)
    expect(fold?.querySelector('summary')?.textContent).toContain('Technical details')
    expect(fold?.querySelector('summary')?.textContent).not.toContain('exit_code')
  })
})
