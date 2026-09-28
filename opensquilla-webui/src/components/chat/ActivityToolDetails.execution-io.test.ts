// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatToolCallRenderItem } from '@/types/chat'
import ActivityToolDetails from './ActivityToolDetails.vue'

const mountedApps: App[] = []

function call(result: Record<string, unknown>): ChatToolCallRenderItem {
  const encoded = JSON.stringify(result)
  return {
    toolId: 'execution-call',
    renderKey: 'execution-call',
    name: 'exec_command',
    displayName: 'Run command',
    inputRaw: JSON.stringify({ command: 'printf test' }),
    inputPreview: '{ command: "printf test" }',
    isRunning: false,
    status: 'success',
    isError: false,
    result: encoded,
    resultPreview: encoded,
    isOpen: true,
  }
}

async function mountDetails(toolCall: ChatToolCallRenderItem, onShowResult = () => undefined) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const Host = defineComponent({
    setup() {
      return () => h(ActivityToolDetails, {
        call: toolCall,
        label: 'Run command',
        operationKey: 'command.run',
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
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ActivityToolDetails execution I/O', () => {
  it('shows actual PTY mode in expanded details', async () => {
    const { el } = await mountDetails(call({
      execution_id: 'pty-1', io_mode_requested: 'pty', io_mode_used: 'pty', output: 'ok',
    }))

    expect(el.querySelector('.activity-tool-details__execution-io')?.textContent)
      .toContain('Real terminal (TTY)')
    expect(el.querySelector('.activity-tool-details__execution-io')?.textContent)
      .not.toContain('TTY unavailable')
  })

  it('shows fallback mode and keeps the diagnostic reason available', async () => {
    const reason = 'PTY backend unavailable'
    const { el } = await mountDetails(call({
      execution_id: 'fallback-1',
      io_mode_requested: 'pty',
      io_mode_used: 'pipe',
      fallback_reason: reason,
      output: 'ok',
    }))

    expect(el.querySelector('.activity-tool-details__execution-io')?.textContent)
      .toContain('TTY unavailable; using a regular pipe')
    expect(el.querySelector('.activity-tool-details__execution-io-reason')?.textContent)
      .toContain(reason)
  })

  it('passes the I/O projection to the full result viewer', async () => {
    const onShowResult = vi.fn()
    const { el } = await mountDetails(call({
      execution_id: 'fallback-1',
      io_mode_requested: 'pty',
      io_mode_used: 'pipe',
      fallback_reason: 'PTY backend unavailable',
      output: 'ok',
    }), onShowResult)

    el.querySelector<HTMLButtonElement>('.activity-tool-details__view')?.click()
    await nextTick()
    const context = onShowResult.mock.calls[0]?.[2] as { executionIo?: { kind?: string } } | undefined
    expect(context?.executionIo?.kind).toBe('fallback')
  })
})
