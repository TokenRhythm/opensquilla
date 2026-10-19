// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, type App } from 'vue'

const mocks = vi.hoisted(() => ({
  copy: vi.fn().mockResolvedValue(undefined),
  download: vi.fn(),
}))

vi.mock('@/utils/browser', () => ({
  copyTextWithFallback: mocks.copy,
  downloadText: mocks.download,
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
  it('keeps a truncated saved generic result distinct from a complete record', async () => {
    const { el } = await mountDetails(call({ result: 'line\n'.repeat(2000), resultTruncated: true }))
    expect(el.querySelector('[data-testid="activity-tool-detail-download"]')?.getAttribute('aria-label')).toBe('Download')
    expect(el.querySelectorAll('.activity-tool-details__truncated')).toHaveLength(1)
    expect(el.querySelector('.activity-tool-details__truncated')?.textContent).toBe('Only part of the content was retained.')
  })
  it('shares the preview budget across raw input and result, and saves the retained full text', async () => {
    const inputRaw = 'i'.repeat(600) + '\n'
    const result = `${'r'.repeat(600)}\n`.repeat(600) + 'LAST LINE'
    const { el } = await mountDetails(call({ inputRaw: inputRaw.repeat(600), result }))
    const visible = Array.from(el.querySelectorAll('pre'), element => element.textContent || '').join('')
    expect(new TextEncoder().encode(visible).length).toBeLessThanOrEqual(256 * 1024)
    expect(visible).not.toContain('LAST LINE')
    expect(el.querySelector('[data-testid="activity-tool-detail-copy"]')).toBeNull()
    el.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-download"]')!.click()
    await vi.waitFor(() => expect(mocks.download).toHaveBeenCalled())
    expect(mocks.download.mock.calls[mocks.download.mock.calls.length - 1]?.[2]).toContain('LAST LINE')
  })
  it.each([
    ['360 characters', 'x'.repeat(360), true],
    ['361 characters', 'x'.repeat(361), true],
    ['6 lines', Array.from({ length: 6 }, (_, index) => `line ${index}`).join('\n'), true],
    ['7 lines', Array.from({ length: 7 }, (_, index) => `line ${index}`).join('\n'), true],
  ])('keeps %s of generic detail inline in one scroll window', async (_name, input, bounded) => {
    const { el } = await mountDetails(call({
      inputRaw: input,
      inputPreview: input.slice(0, 200),
    }))

    expect(el.querySelector('.activity-tool-details--bounded') !== null).toBe(bounded)
    expect(el.querySelector('.activity-tool-details__window') !== null).toBe(bounded)
    expect(el.querySelector('[data-testid="activity-tool-detail-copy"]') !== null).toBe(bounded)
  })

  it('shows and copies complete redacted command content without a second viewer', async () => {
    const secret = 'sk-secretvalue123456789'
    const command = [
      'echo begin',
      `OPENAI_API_KEY=${secret}`,
      ...Array.from({ length: 7 }, (_, index) => `command line ${index + 3}`),
    ].join('\n')
    const inputRaw = JSON.stringify({ command })
    const result = Array.from(
      { length: 400 },
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

    const window = el.querySelector('.activity-code-details__viewport')
    const copy = el.querySelector<HTMLButtonElement>(
      '[data-testid="activity-tool-detail-copy"]',
    )
    const viewFull = el.querySelector<HTMLButtonElement>('.activity-tool-details__view')

    expect(window?.textContent).toContain('echo begin')
    expect(window?.textContent).toContain('command line 9')
    expect(window?.textContent).toContain('output line 400')
    expect(window?.textContent).toContain('[redacted]')
    expect(window?.textContent).not.toContain(secret)
    expect(window?.textContent).not.toContain('Run command')
    expect(el.querySelector('.activity-tool-details__summary')).toBeNull()
    expect(el.querySelector('.activity-tool-details__fade')).toBeNull()
    expect(copy?.getAttribute('aria-label')).toBe('Copy')
    expect(viewFull).toBeNull()

    copy?.click()

    expect(mocks.copy).toHaveBeenCalledWith(redactActivityDetail(
      `$ ${command}\n\n${result}`,
    ))
    await vi.waitFor(() => {
      expect(copy?.getAttribute('aria-label')).toBe('Copy')
    })

    expect(onShowResult).not.toHaveBeenCalled()
  })

  it('shows file edits as a changes section without exposing raw invocation parameters', async () => {
    const inputRaw = JSON.stringify({
      path: 'src/App.vue',
      old_text: 'const value = 1',
      new_text: 'const value = 2',
      justification: 'private invocation metadata',
    })
    const { el, onShowResult } = await mountDetails(call({
      name: 'edit_file',
      inputRaw,
      inputPreview: inputRaw,
      result: 'Edited src/App.vue: replaced 15 chars with 15 chars',
    }), 'file.edit')

    const window = el.querySelector('.activity-code-details__viewport')
    expect(window?.textContent).toContain('-const value = 1')
    expect(window?.textContent).toContain('+const value = 2')
    expect(window?.textContent).not.toContain('justification')
    expect(window?.textContent).not.toContain('Edited src/App.vue')

    el.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    expect(mocks.copy).toHaveBeenCalledWith(
      [
        '--- before',
        '+++ after',
        '@@',
        '-const value = 1',
        '+const value = 2',
      ].join('\n'),
    )
    expect(onShowResult).not.toHaveBeenCalled()
  })

  it('keeps an omitted-only file summary from exposing an empty card or raw marker', async () => {
    const { el } = await mountDetails(call({ name: 'write_file',
      inputRaw: JSON.stringify({ path: 'index.html', content: '[historical_tool_argument_omitted]\nhead: snippet' }),
      result: 'Written 5000 bytes',
    }), 'file.write')
    expect(el.querySelector('section,pre,button')).toBeNull()
    expect(el.textContent).not.toContain('historical_tool_argument_omitted')
  })

  it('keeps complete generic tool content after the previous preview limit', async () => {
    const result = Array.from({ length: 300 }, (_, i) => `result ${i}`).join('\n')
    const { el } = await mountDetails(call({ result }))
    expect(el.querySelector('pre')?.textContent).toBe(result)
    el.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    expect(mocks.copy).toHaveBeenCalledWith(`RESULT\n${result}`)
  })
})
