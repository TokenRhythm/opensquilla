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

describe('ActivityNarration visible commentary', () => {
  it('keeps a short readable update directly visible', () => {
    const host = mount('Checked the project and found the routing delay.')

    expect(host.querySelector('details')).toBeNull()
    expect(host.querySelector('.activity-narration--plain')?.textContent)
      .toContain('Checked the project')
  })

  it('keeps the full update visible regardless of length or line count', () => {
    const text = Array(8).fill('I checked the project flow and verified the current session ownership.').join('\n')
    const host = mount(text)

    expect(host.querySelector('details')).toBeNull()
    expect(host.querySelector('.activity-narration')?.textContent).toBe(text)
  })

  it.each([
    'code-task failed with exit_code=1 and stderr=permission denied',
    '当前环境没有 `nano-banana` skill，因此无法调用 nano-banana CLI。我会用已有的 SVG + cairosvg 生成信息图，全部保存到 `outputs/T1/`。',
  ])('keeps assistant explanations visible when they mention technical terms: %s', (text) => {
    const host = mount(text)

    expect(host.querySelector('details')).toBeNull()
    expect(host.querySelector('.activity-narration')?.textContent).toBe(text)
  })
})
