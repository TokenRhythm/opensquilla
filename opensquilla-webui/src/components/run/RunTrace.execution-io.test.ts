// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp, defineComponent, h, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatStreamTimelineItem, ChatToolCallRenderItem } from '@/types/chat'
import RunTrace from './RunTrace.vue'

const mountedApps: App[] = []

function call(id: string, result: Record<string, unknown>): ChatToolCallRenderItem {
  return {
    toolId: id,
    renderKey: id,
    name: 'exec_command',
    displayName: 'Run command',
    inputRaw: JSON.stringify({ command: 'printf test' }),
    inputPreview: '{ command: "printf test" }',
    isRunning: false,
    status: 'success',
    isError: false,
    result: JSON.stringify(result),
    resultPreview: JSON.stringify(result),
    isOpen: false,
  }
}

function group(groupId: string, calls: ChatToolCallRenderItem[]): ChatStreamTimelineItem {
  return {
    type: 'tool-group',
    key: groupId,
    group: {
      groupId,
      operationKey: 'exec',
      label: 'Run command',
      iconName: 'gear',
      calls,
      secondary: '',
      isRunning: false,
      isError: false,
      status: 'success',
    },
  }
}

async function mountRunTrace(items: ChatStreamTimelineItem[]) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const Host = defineComponent({
    setup() {
      return () => h(RunTrace, {
        items,
        presentation: 'activity',
        isToolGroupOpen: () => false,
        isToolItemOpen: () => false,
      })
    },
  })
  const app = createApp(Host)
  mountedApps.push(app)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('RunTrace execution I/O presentation', () => {
  it('shows a successful PTY mode on the collapsed row', async () => {
    const { el } = await mountRunTrace([group('pty', [call('pty-call', {
      execution_id: 'pty-1', io_mode_requested: 'pty', io_mode_used: 'pty',
    })])])

    const row = el.querySelector('.tool-row')
    expect(row?.getAttribute('aria-expanded')).toBe('false')
    expect(row?.textContent).toContain('Real terminal (TTY)')
    expect(row?.textContent).not.toContain('TTY unavailable')
  })

  it('shows ordinary pipe mode without a fallback warning', async () => {
    const { el } = await mountRunTrace([group('pipe', [call('pipe-call', {
      execution_id: 'pipe-1', io_mode_requested: 'pipe', io_mode_used: 'pipe',
    })])])

    const row = el.querySelector('.tool-row')
    expect(row?.getAttribute('aria-expanded')).toBe('false')
    expect(row?.textContent).toContain('Regular pipe')
    expect(row?.textContent).not.toContain('TTY unavailable')
  })

  it('shows PTY fallback on a single collapsed row', async () => {
    const { el } = await mountRunTrace([group('fallback', [call('fallback-call', {
      execution_id: 'fallback-1',
      io_mode_requested: 'pty',
      io_mode_used: 'pipe',
      fallback_reason: 'PTY backend unavailable',
    })])])

    const row = el.querySelector('.tool-row')
    expect(row?.getAttribute('aria-expanded')).toBe('false')
    expect(row?.textContent).toContain('TTY unavailable; using a regular pipe')
    expect(row?.classList.contains('tool-row--member')).toBe(false)
  })

  it('shows PTY fallback on a collapsed multi-call group header', async () => {
    const { el } = await mountRunTrace([group('fallback-group', [
      call('fallback-1', {
        execution_id: 'fallback-1', io_mode_requested: 'pty', io_mode_used: 'pipe',
        fallback_reason: 'PTY backend unavailable',
      }),
      call('fallback-2', {
        execution_id: 'fallback-2', io_mode_requested: 'pty', io_mode_used: 'pipe',
        fallback_reason: 'PTY backend unavailable',
      }),
    ])])

    const header = el.querySelector('.tool-row--group')
    expect(header?.getAttribute('aria-expanded')).toBe('false')
    expect(header?.textContent).toContain('TTY unavailable; using a regular pipe')
  })

  it('never labels a mixed group as a TTY', async () => {
    const { el } = await mountRunTrace([group('mixed-group', [
      call('pty-call', { execution_id: 'pty-1', io_mode_used: 'pty' }),
      call('pipe-call', { execution_id: 'pipe-1', io_mode_used: 'pipe' }),
    ])])

    const header = el.querySelector('.tool-row--group')
    expect(header?.textContent).toContain('Mixed terminal modes')
    expect(header?.textContent).not.toContain('Real terminal (TTY)')
  })
})
