// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, type App } from 'vue'

const mocks = vi.hoisted(() => ({
  copy: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@/utils/browser', () => ({
  copyTextWithFallback: mocks.copy,
}))

import ActivityToolDetails from '@/components/chat/ActivityToolDetails.vue'
import i18n from '@/i18n'
import type { ChatToolCallRenderItem } from '@/types/chat'
import { redactActivityDetail } from '@/utils/chat/activityToolDetails'

const mountedApps: App[] = []

function call(overrides: Partial<ChatToolCallRenderItem> = {}): ChatToolCallRenderItem {
  return {
    toolId: 'detail-call',
    renderKey: 'detail-call',
    name: 'custom_tool',
    displayName: 'Custom tool',
    inputRaw: '',
    inputPreview: '',
    isRunning: false,
    status: 'success',
    isError: false,
    result: '',
    resultPreview: '',
    isOpen: true,
    ...overrides,
  }
}

async function mountDetails(
  toolCall: ChatToolCallRenderItem,
  operationKey = 'tool.custom',
  onShowResult = vi.fn(),
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const Host = defineComponent({
    setup() {
      return () => h(ActivityToolDetails, {
        call: toolCall,
        label: 'Run command',
        operationKey,
        onShowResult,
      })
    },
  })
  const app = createApp(Host)
  mountedApps.push(app)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { el, onShowResult }
}

beforeEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
  mocks.copy.mockReset()
  mocks.copy.mockResolvedValue(undefined)
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('ActivityToolDetails adaptive detail window', () => {
  it.each([
    ['360 characters', 'x'.repeat(360), false],
    ['361 characters', 'x'.repeat(361), true],
    ['6 lines', Array.from({ length: 6 }, (_, index) => `line ${index}`).join('\n'), false],
    ['7 lines', Array.from({ length: 7 }, (_, index) => `line ${index}`).join('\n'), true],
  ])('uses the bounded window beyond the %s threshold', async (_name, input, bounded) => {
    const { el } = await mountDetails(call({
      inputRaw: input,
      inputPreview: input.slice(0, 200),
    }))

    expect(el.querySelector('.activity-tool-details--bounded') !== null).toBe(bounded)
    expect(el.querySelector('.activity-tool-details__window') !== null).toBe(bounded)
    expect(el.querySelector('[data-testid="activity-tool-detail-copy"]') !== null).toBe(bounded)
  })

  it('shows real redacted lines, copies safe full detail, and preserves View full', async () => {
    const secret = 'sk-secretvalue123456789'
    const command = [
      'echo begin',
      `OPENAI_API_KEY=${secret}`,
      ...Array.from({ length: 7 }, (_, index) => `command line ${index + 3}`),
    ].join('\n')
    const inputRaw = JSON.stringify({ command })
    const result = Array.from(
      { length: 40 },
      (_, index) => `output line ${index + 1}`,
    ).join('\n')
    const toolCall = call({
      name: 'shell',
      inputRaw,
      inputPreview: inputRaw.slice(0, 200),
      result,
      resultPreview: result.slice(0, 200),
    })
    const { el, onShowResult } = await mountDetails(toolCall, 'command.run')

    const window = el.querySelector('.activity-tool-details__window')
    const copy = el.querySelector<HTMLButtonElement>(
      '[data-testid="activity-tool-detail-copy"]',
    )
    const viewFull = el.querySelector<HTMLButtonElement>('.activity-tool-details__view')

    expect(window?.textContent).toContain('echo begin')
    expect(window?.textContent).toContain('command line 9')
    expect(window?.textContent).toContain('output line 40')
    expect(window?.textContent).toContain('[redacted]')
    expect(window?.textContent).not.toContain(secret)
    expect(window?.textContent).not.toContain('Run command')
    expect(el.querySelector('.activity-tool-details__summary')).toBeNull()
    expect(el.querySelector('.activity-tool-details__fade')).not.toBeNull()
    expect(copy?.getAttribute('aria-label')).toBe('Copy')
    expect(viewFull?.textContent?.trim()).toBe('view full')

    copy?.click()

    expect(mocks.copy).toHaveBeenCalledWith(redactActivityDetail(
      `INPUT\n${inputRaw}\n\nRESULT\n${result}`,
    ))
    await vi.waitFor(() => {
      expect(copy?.getAttribute('aria-label')).toBe('Copied')
    })

    viewFull?.click()
    expect(onShowResult).toHaveBeenCalledWith(
      `INPUT\n${inputRaw}\n\nRESULT\n${result}`,
      'Run command · details',
      {
        toolName: 'shell',
        inputRaw,
        section: undefined,
      },
    )
  })
})
