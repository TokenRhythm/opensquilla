// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import ActivityNarration from './ActivityNarration.vue'
import type { ChatStreamTimelineItem } from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'

vi.mock('@/utils/browser', async importOriginal => ({
  ...await importOriginal<typeof import('@/utils/browser')>(),
  copyTextWithFallback: vi.fn(async () => {}),
}))

const mountedApps: App[] = []

function narration(rawText: string): Extract<ChatStreamTimelineItem, { type: 'text' }> {
  return {
    type: 'text',
    key: `narration:${rawText.length}`,
    rawText,
    html: `<p>${rawText}</p>`,
  }
}

function mount(rawText: string, html?: string) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(ActivityNarration, { item: { ...narration(rawText), ...(html ? { html } : {}) } }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  vi.mocked(copyTextWithFallback).mockClear()
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ActivityNarration visible commentary', () => {
  it('uses the shared source-copy control for process code and updates its locale', async () => {
    const host = mount('Code', '<pre><span class="code-lang">ts</span><code>const count = 1;</code></pre>')
    await nextTick()
    const button = host.querySelector<HTMLButtonElement>('.code-copy-btn')!
    expect(button).not.toBeNull()
    button.click()
    await Promise.resolve()
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith('const count = 1;')
    i18n.global.locale.value = 'zh-Hans'
    await nextTick()
    expect(host.querySelectorAll('.code-copy-btn')).toHaveLength(1)
    expect(host.querySelector<HTMLButtonElement>('.code-copy-btn')!.title).toBe(i18n.global.t('chat.copy'))
  })

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
    'shell command failed with exit_code=1 and stderr=permission denied',
    '当前环境没有 `nano-banana` skill，因此无法调用 nano-banana CLI。我会用已有的 SVG + cairosvg 生成信息图，全部保存到 `outputs/T1/`。',
  ])('keeps assistant explanations visible when they mention technical terms: %s', (text) => {
    const host = mount(text)

    expect(host.querySelector('details')).toBeNull()
    expect(host.querySelector('.activity-narration')?.textContent).toBe(text)
  })
})
