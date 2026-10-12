// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, reactive, type App } from 'vue'

import ToolCallTimeline from '@/components/chat/ToolCallTimeline.vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import type {
  ChatStreamTimelineItem,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
} from '@/types/chat'
import { BROWSER_WORKBENCH_OPEN_EVENT } from '@/workbench/browserItems'
import runTraceSource from './RunTrace.vue?raw'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'
import { createPinia } from 'pinia'
import { copyTextWithFallback } from '@/utils/browser'

vi.mock('@/utils/browser', () => ({ copyTextWithFallback: vi.fn().mockResolvedValue(undefined) }))

const mountedApps: App[] = []

function ruleBody(selector: string) {
  const selectorStart = runTraceSource.indexOf(selector)
  expect(selectorStart).toBeGreaterThanOrEqual(0)

  const blockStart = runTraceSource.indexOf('{', selectorStart)
  const blockEnd = runTraceSource.indexOf('}', blockStart)
  return runTraceSource.slice(blockStart + 1, blockEnd)
}

function expectStatusDot(dot: Element | null, status: string) {
  expect(dot).not.toBeNull()
  expect(dot?.getAttribute('role')).toBe('img')
  expect(dot?.getAttribute('aria-label')).toBe(`${status} · view details`)
  expect(dot?.getAttribute('title')).toBe(`${status} · view details`)
  expect(dot?.textContent).toBe('')
}

function call(
  renderKey: string,
  overrides: Partial<ChatToolCallRenderItem> = {},
): ChatToolCallRenderItem {
  return {
    toolId: renderKey,
    renderKey,
    name: 'shell',
    displayName: renderKey,
    inputRaw: '{}',
    inputPreview: '{}',
    isRunning: false,
    status: 'success',
    isError: false,
    result: 'ok',
    resultPreview: 'ok',
    isOpen: false,
    ...overrides,
  }
}

function group(
  groupId: string,
  calls: ChatToolCallRenderItem[],
): Extract<ChatStreamTimelineItem, { type: 'tool-group' }> {
  const isError = calls.some(entry => entry.isError || entry.status === 'error')
  const isRunning = calls.some(entry => entry.isRunning)
  return {
    type: 'tool-group',
    key: groupId,
    group: {
      groupId,
      operationKey: groupId,
      label: groupId,
      iconName: 'gear',
      calls,
      secondary: '',
      isRunning,
      isError,
      status: isError
        ? 'error'
        : (calls.every(entry => entry.status === 'success') ? 'success' : ''),
    },
  }
}

async function mountTimeline(
  items: ChatStreamTimelineItem[],
  options: {
    presentation?: 'activity'
    executing?: boolean
    groupOpen?: boolean
    itemOpen?: boolean
    onShowResult?: (content: string, title: string, context?: unknown) => void
    toolStatusText?: (call: ChatToolCallRenderItem) => string
  } = {},
) {
  const el = document.createElement('div')
  document.body.appendChild(el)

  const Host = defineComponent({
    setup() {
      return () => h(ToolCallTimeline, {
        items,
        ...(options.presentation ? { presentation: options.presentation } : {}),
        executing: options.executing,
        isToolGroupOpen: () => options.groupOpen,
        isToolItemOpen: () => options.itemOpen,
        toolGroupStatusText: (toolGroup: ChatToolCallGroup) => {
          if (toolGroup.isRunning) return 'Running'
          if (toolGroup.isError) return 'Failed'
          return 'Done'
        },
        toolStatusText: options.toolStatusText ?? (() => ''),
        toolSecondaryText: () => '',
        onShowResult: options.onShowResult,
        onToggleGroup: (_id: string, open: boolean) => { options.groupOpen = open },
        onToggleItem: (_id: string, open: boolean) => { options.itemOpen = open },
      })
    },
  })

  const app = createApp(Host)
  mountedApps.push(app)
  app.use(i18n)
  app.use(createPinia())
  app.mount(el)
  await nextTick()
  return el
}

beforeEach(() => {
  useToolDetailPreference().setMode('collapsed')
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('RunTrace activity presentation', () => {
  it('localizes an unknown tool action without losing its identifier or manual disclosure', async () => {
    const name = 'mcp__acme-v2__diagram_probe'
    const el = await mountTimeline([group('tool.acme.probe', [call('probe', {
      name, inputRaw: '{"action":"probe"}', result: 'retained result', resultPreview: 'retained result',
    })])], reactive({ presentation: 'activity' as const, itemOpen: false }))
    const row = el.querySelector<HTMLButtonElement>('.tool-row')!
    expect(row.getAttribute('aria-expanded')).toBe('false')
    expect(row.querySelector('.tool-row__label')?.textContent).toBe('Tool call')
    expect(row.querySelector('.tool-row__arg')?.textContent).toBe(name)
    row.click()
    await nextTick()
    expect(row.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('.activity-tool-details')?.textContent).toContain('retained result')
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    await nextTick()
    expect(row.querySelector('.tool-row__label')?.textContent).toBe('调用工具')
    expect(row.querySelector('.tool-row__arg')?.textContent).toBe(name)
    expect(row.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('.activity-tool-details')?.textContent).toContain('retained result')
  })

  it('renders a Chinese process action and real command target while preserving its error', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const processCall = call('poll', {
      name: 'process', status: 'error', isError: true,
      inputRaw: '{"action":"poll","execution_id":"exec-1"}',
      result: JSON.stringify({ status: 'ok', session: {
        command: 'python -m http.server 8080', status: 'timed_out', returncode: 1,
      } }),
    })
    const el = await mountTimeline([group('process', [processCall])], { presentation: 'activity' })
    expect(el.querySelector('.tool-row__label')?.textContent).toBe('检查后台命令')
    expect(el.querySelector('.tool-row__arg')?.textContent).toBe('python -m http.server 8080')
    expect(el.querySelector('.tool-row--error')).not.toBeNull()
    expect(el.querySelector('.tool-row')?.textContent).not.toContain('poll')
  })

  it('renders a retained safe label when an old call has no tool name', async () => {
    useToolDetailPreference().setMode('expanded')
    const incomplete = call('old-call')
    delete (incomplete as Partial<ChatToolCallRenderItem>).name
    const el = await mountTimeline([group('Retained activity', [incomplete])], { presentation: 'activity' })
    expect(el.querySelector('.tool-row__label')?.textContent).toBe('Retained activity')
    expect(el.textContent).toContain('ok')
  })
  it('uses AA text roles instead of transparent blends for activity copy', () => {
    expect(ruleBody('.tool-timeline--activity .tool-row__label')).toContain(
      'color: var(--text-muted);',
    )

    const secondaryTextRule = ruleBody('.tool-timeline--activity .tool-row__status')
    expect(secondaryTextRule).toContain('color: var(--text-muted);')
    expect(secondaryTextRule).not.toContain('transparent')
    expect(
      ruleBody('.tool-timeline--activity .tool-row--error .tool-row__status'),
    ).toContain('color: var(--warn);')
    expect(
      ruleBody('.tool-timeline--activity .tool-row__activity-icon--error'),
    ).toContain('color: var(--warn);')

    expect(ruleBody('.tool-timeline--activity .tool-row--running .tool-row__arg')).toContain(
      'color: var(--text-muted);',
    )
    expect(ruleBody('.tool-overflow-note')).toContain(
      'color: var(--text-muted);',
    )
  })

  it('aligns the detail outer edge with its tool row without an extra indent', () => {
    expect(ruleBody('.tool-timeline--activity .tool-row-body')).toContain(
      'padding: 0 0 0.125rem;',
    )
    expect(ruleBody('.tool-timeline--activity .step-group-members')).toContain(
      'padding-left: 1.5rem;',
    )
  })

  it('keeps disclosure arrows hidden at rest and enlarges touch targets', () => {
    expect(ruleBody('.tool-timeline--activity .tool-row__activity-arrow')).toContain('opacity: 0;')
    const mediaStart = runTraceSource.indexOf('@media (pointer: coarse)')
    expect(mediaStart).toBeGreaterThanOrEqual(0)
    const ruleEnd = runTraceSource.indexOf('}', mediaStart)
    const mediaRule = runTraceSource.slice(mediaStart, ruleEnd)
    expect(mediaRule).toContain('.tool-timeline--activity button.tool-row')
    expect(mediaRule).toContain('min-height: 2.75rem;')
  })

  it('applies one whole-row sweep only during real execution and stops it immediately', async () => {
    const running = reactive(call('build', {
      isRunning: true, status: '', inputRaw: '{"command":"npm run build"}',
    }))
    const options = reactive({ presentation: 'activity' as const, executing: true })
    const el = await mountTimeline([group('build-group', [running])], options)
    const motion = () => el.querySelector('.activity-motion--active')

    expect(motion()).not.toBeNull()
    expect(motion()?.querySelector('.tool-row__activity-icon')).not.toBeNull()
    expect(motion()?.querySelector('.tool-row__label')?.textContent).toBe('Executing command')
    expect(motion()?.querySelector('.tool-row__arg')?.textContent?.trim()).toBe('npm run build')
    expect(motion()?.querySelector('.tool-row__activity-arrow')).not.toBeNull()
    expect(el.querySelectorAll('.activity-motion--active')).toHaveLength(1)

    options.executing = false
    await nextTick()
    expect(motion()).toBeNull()
    options.executing = true
    await nextTick()
    expect(motion()).not.toBeNull()
    running.isRunning = false
    running.status = 'success'
    await nextTick()
    expect(motion()).toBeNull()
    expect(el.querySelector('.tool-row__arg')?.textContent?.trim()).toBe('npm run build')
  })

  it.each([undefined, false])('keeps a restored running flag static when executing is %s', async executing => {
    const el = await mountTimeline([group('restored', [call('restored-call', {
      isRunning: true, status: '', inputRaw: '{"command":"npm run build"}',
    })])], { presentation: 'activity', executing })

    expect(el.querySelector('.tool-row__label')?.textContent).toBe('Executing command')
    expect(el.querySelector('.activity-motion--active')).toBeNull()
  })

  it.each(['success', 'error'] as const)('keeps terminal %s static even with a stale running flag', async status => {
    const el = await mountTimeline([group('terminal', [call('terminal-call', {
      isRunning: true, status, isError: status === 'error',
    })])], { presentation: 'activity', executing: true })

    expect(el.querySelector('.activity-motion--active')).toBeNull()
    if (status === 'error') expect(el.querySelector('.tool-row--error')).not.toBeNull()
  })

  it('keeps completed batch members static while only the executing call sweeps', async () => {
    const el = await mountTimeline([group('mixed-batch', [
      call('written', { name: 'write_file', inputRaw: '{"path":"index.html","content":"html"}' }),
      call('build', { isRunning: true, status: '', inputRaw: '{"command":"npm run build"}' }),
    ])], { presentation: 'activity', executing: true, groupOpen: true })
    const rows = el.querySelectorAll('.tool-row')

    expect(rows).toHaveLength(2)
    expect(el.querySelector('.tool-row--group')).toBeNull()
    expect(rows[0]?.querySelector('.activity-motion--active')).toBeNull()
    expect(rows[1]?.querySelector('.activity-motion--active')).not.toBeNull()
    expect(el.querySelectorAll('.activity-motion--active')).toHaveLength(1)
  })

  it('preserves the reader\'s manually opened detail after execution stops', async () => {
    const running = reactive(call('build', { isRunning: true, status: '', result: 'building output' }))
    const options = reactive({
      presentation: 'activity' as const,
      executing: true,
      itemOpen: undefined as boolean | undefined,
    })
    const el = await mountTimeline([group('build-group', [running])], options)
    const row = () => el.querySelector<HTMLButtonElement>('.tool-row')

    expect(row()?.getAttribute('aria-expanded')).toBe('false')
    row()?.click()
    await nextTick()
    expect(row()?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('.tool-row-body')).not.toBeNull()

    running.isRunning = false
    running.status = 'success'
    options.executing = false
    await nextTick()
    expect(row()?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('.tool-row-body')).not.toBeNull()
    expect(el.querySelector('.activity-motion--active')).toBeNull()
    row()?.click()
    await nextTick()
    row()?.click()
    await nextTick()
    expect(row()?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('.activity-motion--active')).toBeNull()
  })

  it('animates calls appended inside an existing open tool group', () => {
    expect(runTraceSource).toContain('name="tool-member"')
    expect(runTraceSource).toContain('class="step-group-members"')
    expect(ruleBody('.tool-member-enter-from')).toContain('opacity: 0;')
    expect(ruleBody('.tool-member-enter-active,')).toContain(
      'opacity var(--dur-base) var(--ease-out)',
    )
  })

  it('fades only activity detail bodies and disables the motion when requested', () => {
    expect(runTraceSource).toContain(
      '<Transition name="activity-tool-detail" :css="activityVisible && presentation === \'activity\'">',
    )
    expect(ruleBody('.tool-timeline--activity .activity-tool-detail-enter-active')).toContain(
      'opacity var(--dur-base) var(--ease-out)',
    )
    expect(ruleBody('.tool-timeline--activity .activity-tool-detail-enter-from,')).toContain(
      'opacity: 0;',
    )

    const reducedMotionStart = runTraceSource.indexOf('@media (prefers-reduced-motion: reduce)')
    const reducedMotion = runTraceSource.slice(reducedMotionStart)
    expect(reducedMotion).toContain(
      '.tool-timeline--activity .activity-tool-detail-enter-active',
    )
    expect(reducedMotion).toContain('transition: none;')
  })

  const completedGroup = group('completed-group', [
    call('completed-one'),
    call('completed-two'),
  ])
  const failedGroup = group('failed-group', [
    call('failed-one', {
      status: 'error',
      isError: true,
      result: 'failed',
      resultPreview: 'failed',
    }),
    call('failed-two', {
      status: 'error',
      isError: true,
      result: 'failed',
      resultPreview: 'failed',
    }),
  ])

  it('keeps the existing card and success affordances when details are expanded', async () => {
    useToolDetailPreference().setMode('expanded')
    const el = await mountTimeline([completedGroup, failedGroup])

    expect(el.querySelector('.tool-timeline--activity')).toBeNull()
    expect(el.querySelector('.tool-row__bullet--ok')).not.toBeNull()
    expect(el.querySelector('.tool-row__state-icon--ok')).not.toBeNull()
    expect(el.querySelector('.step-chevron')).not.toBeNull()
    expect(el.querySelector('.tool-timeline__bulk-icon')).not.toBeNull()
    expect(el.querySelector('.step-count')?.textContent).toBe('2 calls')
    expect(
      Array.from(el.querySelectorAll('.tool-row--group .tool-row__status'))
        .map(node => node.textContent),
    ).toEqual(['Done', 'Failed'])
    expect(el.querySelector('.tool-row__status-dot')).toBeNull()
  })

  it('neutralizes completed chrome while keeping failed activity visible', async () => {
    const el = await mountTimeline(
      [completedGroup, failedGroup],
      { presentation: 'activity' },
    )

    expect(el.querySelector('.tool-timeline--activity')).not.toBeNull()
    expect(el.querySelector('.tool-row__bullet--ok')).toBeNull()
    expect(el.querySelector('.tool-row__bullet')).toBeNull()
    expect(el.querySelector('.tool-row__activity-icon')).not.toBeNull()
    expect(el.querySelector('.tool-row__state-icon--ok')).toBeNull()
    expect(el.querySelector('.tool-row__activity-arrow')).not.toBeNull()
    expect(
      el.querySelector('.tool-row__activity-arrow')?.hasAttribute('data-share-control'),
    ).toBe(true)
    expect(el.querySelector('.tool-timeline__toolbar')).toBeNull()
    expect(el.querySelector('.tool-timeline__bulk-icon')).toBeNull()
    expect(el.querySelector('.step-count')).toBeNull()
    expect(el.querySelector('.tool-row--group')).toBeNull()
    expect(el.querySelectorAll('.tool-row')).toHaveLength(4)
    const failureDots = [...el.querySelectorAll('.tool-row__status-dot')]
    expect(failureDots).toHaveLength(2)
    for (const dot of failureDots) expectStatusDot(dot, "Didn't complete")
    expect(el.textContent).toContain("Didn't complete")
    expect([...el.querySelectorAll('.tool-row--error')]
      .every(row => row.getAttribute('aria-expanded') === 'false')).toBe(true)
    expect(el.querySelector('.tool-row__bullet--err')).toBeNull()
    expect(el.querySelector('.tool-row__activity-icon--error')).not.toBeNull()
    expect(el.querySelector('.tool-row__state-icon--err')).toBeNull()
    expect(el.querySelectorAll('.activity-code-details__output')).toHaveLength(0)
    expect(el.querySelectorAll('.tool-row-body')).toHaveLength(0)
    expect([...el.querySelectorAll('.tool-row__error-summary')].map(node => node.textContent))
      .toEqual(['failed', 'failed'])
    expect(el.querySelector('.tool-row-section--error')).toBeNull()
    expect(el.textContent).not.toContain('failed-group')
  })

  it('uses the running treatment without repeating a running status label', async () => {
    const runningGroup = group('running-group', [
      call('running-one', { isRunning: true, status: '' }),
      call('running-two', { isRunning: true, status: '' }),
    ])
    const el = await mountTimeline([runningGroup], { presentation: 'activity' })

    expect(el.querySelector('.tool-row__bullet--running')).toBeNull()
    expect(el.querySelector('.tool-row__activity-icon--running')).not.toBeNull()
    expect(el.querySelector('.tool-row--group .tool-row__status')).toBeNull()
    expect(el.querySelector('.tool-row__status-dot')).toBeNull()
  })

  it('shows a collapsed failed activity call with visible status and an accessible dot', async () => {
    const el = await mountTimeline([
      group('single-failure-group', [
        call('single-failure', {
          status: 'error',
          isError: true,
          result: 'failed',
          resultPreview: 'failed',
        }),
      ]),
    ], { presentation: 'activity' })

    expect(el.querySelector('.tool-row--error')).not.toBeNull()
    expect(el.querySelector('.tool-row__label')?.textContent).toBe('Run command')
    expect(el.querySelector('.tool-row__status')?.textContent).toBe("Didn't complete")
    expectStatusDot(el.querySelector('.tool-row__status-dot'), "Didn't complete")
    expect(el.textContent).toContain("Didn't complete")
    expect(el.querySelector('.tool-row')?.getAttribute('aria-expanded')).toBe('false')
    expect(el.querySelector('.activity-tool-details')).toBeNull()
    expect(el.querySelector('.tool-row__error-summary')?.textContent).toBe('failed')
  })

  it('keeps the collapsed error summary to one redacted line without opening the full output', async () => {
    const result = 'Permission denied; token=private-token-value\nFull diagnostic continuation'
    const el = await mountTimeline([
      group('custom.failure', [call('failed-custom', {
        name: 'custom_tool', status: 'error', isError: true,
        result, resultPreview: result,
      })]),
    ], { presentation: 'activity' })

    expect(el.querySelector('.tool-row__error-summary')?.textContent)
      .toBe('Permission denied; token=[redacted]')
    expect(el.textContent).not.toContain('private-token-value')
    expect(el.textContent).not.toContain('Full diagnostic continuation')
    expect(el.querySelector('.activity-tool-details')).toBeNull()
  })

  it('does not expose legacy document payloads through the collapsed error summary', async () => {
    const result = JSON.stringify({
      category: 'DOCUMENT_PREVIEW_UNAVAILABLE',
      message_key: 'document.previewUnavailable',
      user_message: '<html>private source</html>',
      bindingToken: 'private-binding-token',
    })
    const el = await mountTimeline([
      group('document.failure', [call('failed-document', {
        name: 'document_apply', status: 'error', isError: true,
        result, resultPreview: result,
      })]),
    ], { presentation: 'activity' })

    expect(el.querySelector('.tool-row__status')?.textContent).toBe("Didn't complete")
    expect(el.querySelector('.tool-row-body')).toBeNull()
    expect(el.textContent).not.toMatch(/private|binding|<html>/i)
  })

  it.each(['document_apply', 'document_patch'])(
    'keeps a failed %s activity call visible',
    async (writerName) => {
      const el = await mountTimeline([
        group(writerName, [
          call(`failed-${writerName}`, {
            name: writerName,
            status: 'error',
            isError: true,
            result: 'failed',
            resultPreview: 'failed',
          }),
        ]),
      ], { presentation: 'activity' })

      expect(el.querySelector('.tool-row--error')).not.toBeNull()
      expect(el.textContent).toContain(writerName)
    },
  )

  it.each(['shell', 'document_patch'])('keeps a restored %s group-level failure visible', async (name) => {
    const restoredFailure = group('restored-failure', [
      call('restored-writer', {
        name,
        status: 'success',
      }),
    ])
    restoredFailure.group.isError = true
    restoredFailure.group.status = 'error'
    const el = await mountTimeline([restoredFailure], { presentation: 'activity' })

    expect(el.querySelector('.tool-row--error')).not.toBeNull()
    expect(el.querySelector('.tool-row__status')?.textContent).toBe("Didn't complete")
    expectStatusDot(el.querySelector('.tool-row__status-dot'), "Didn't complete")
    expect(el.textContent).toContain("Didn't complete")
    expect(el.querySelector('.tool-row__label')?.textContent)
      .toBe(name === 'shell' ? 'Run command' : 'restored-failure')
    expect(restoredFailure.group.calls[0]?.status).toBe('success')
    expect(restoredFailure.group.calls[0]?.isError).toBe(false)
  })

  it.each(['collapsed', 'expanded'] as const)('keeps cancelled status visible with %s details', async (mode) => {
    useToolDetailPreference().setMode(mode)
    const el = await mountTimeline([
      group('single-cancelled-group', [
        call('single-cancelled', {
          status: 'error',
          isError: true,
          result: 'cancelled',
          resultPreview: 'cancelled',
        }),
      ]),
    ], {
      presentation: 'activity',
      toolStatusText: () => 'Cancelled',
    })

    expect(el.querySelector('.tool-row--error')).not.toBeNull()
    expect(el.querySelector('.tool-row__status')?.textContent).toBe('Cancelled')
    expectStatusDot(el.querySelector('.tool-row__status-dot'), 'Cancelled')
    expect(el.textContent).toContain('Cancelled')
    expect(el.querySelector('.tool-row__label')?.textContent).toBe('Run command')
    expect(el.querySelector('.tool-row')?.getAttribute('aria-expanded')).toBe(String(mode === 'expanded'))
    if (mode === 'expanded') {
      expect(el.querySelector('.activity-tool-details')?.textContent).toContain('cancelled')
      expect(el.querySelector('.tool-row__error-summary')).toBeNull()
    } else {
      expect(el.querySelector('.activity-tool-details')).toBeNull()
      expect(el.querySelector('.tool-row__error-summary')?.textContent).toBe('cancelled')
    }
  })

  it('keeps successful and failed calls from a mixed activity group', async () => {
    const el = await mountTimeline([
      group('mixed-group', [
        call('successful-call'),
        call('failed-call', {
          status: 'error',
          isError: true,
          result: 'failed',
          resultPreview: 'failed',
        }),
      ]),
    ], { presentation: 'activity' })

    expect(el.querySelectorAll('.tool-row')).toHaveLength(2)
    expect(el.querySelector('.tool-row--error')).not.toBeNull()
    expect(el.querySelector('.tool-row--group')).toBeNull()
    expect([...el.querySelectorAll('.tool-row__label')].map(row => row.textContent))
      .toEqual(['Ran', 'Run command'])
    expect(el.querySelectorAll('.tool-row__status-dot')).toHaveLength(1)
  })

  it('retains an unattributed aggregate failure without relabelling successful calls as failed', async () => {
    const item = group('aggregate-failure', [call('one'), call('two')])
    item.group.isError = true
    item.group.status = 'error'
    const el = await mountTimeline([item], { presentation: 'activity', executing: true, groupOpen: true })
    expectStatusDot(el.querySelector('.tool-row--group .tool-row__status-dot'), "Didn't complete")
    expect(el.querySelectorAll('.tool-row--member .tool-row__status-dot')).toHaveLength(0)
    expect(el.querySelectorAll('.tool-row--member.tool-row--error')).toHaveLength(0)
    expect(el.querySelector('.tool-row--group .activity-motion--active')).toBeNull()
    expect(el.querySelector('.tool-row--member .activity-motion--active')).toBeNull()
    expect(item.group.calls.every(call => call.status === 'success' && !call.isError)).toBe(true)
  })

  it('keeps a successful activity call collapsed by default', async () => {
    const el = await mountTimeline([
      group('single-success-group', [
        call('single-success'),
      ]),
    ], { presentation: 'activity' })

    const row = el.querySelector('.tool-row')
    expect(row?.getAttribute('aria-expanded')).toBe('false')
    expect(el.querySelector('.tool-row-body')).toBeNull()
    expect(el.querySelector('.activity-tool-details')).toBeNull()
  })

  it('keeps search URLs collapsed without exposing invocation details', async () => {
    const inputRaw = JSON.stringify({ query: 'AI news today 2026-08-26' })
    const el = await mountTimeline([
      group('web.search', [
        call('search-call', {
          name: 'web_search',
          inputRaw,
          inputPreview: inputRaw,
          sources: [{ url: 'https://example.test/result', title: 'Result' }],
          result: JSON.stringify({
            ok: true,
            provider_attempts: [{ provider: 'brave', status: 'success' }],
          }),
        }),
      ]),
    ], { presentation: 'activity' })

    const row = el.querySelector('.tool-row')
    expect(row?.getAttribute('aria-expanded')).toBe('false')
    expect(el.querySelector('.step-chevron')).not.toBeNull()
    expect(el.querySelector('.tool-row-targets')).toBeNull()
    expect(el.querySelector('.tool-row-body')).toBeNull()
    expect(el.querySelector('.activity-tool-details')).toBeNull()
    expect(el.textContent).not.toMatch(/INPUT|RESULT|provider_attempts/)
  })

  it('lists web-search result URLs as side-browser targets without exposing the query', async () => {
    const opened: string[] = []
    const onOpen = (event: Event) => {
      opened.push((event as CustomEvent<{ url: string }>).detail.url)
    }
    window.addEventListener(BROWSER_WORKBENCH_OPEN_EVENT, onOpen)

    const searchCall = (id: string, urls: string[]) => call(id, {
      name: 'web_search',
      inputRaw: '{}',
      inputPreview: '',
      sources: urls.map(url => ({ url, title: id })),
      presentation: {
        category: 'search',
        primaryArguments: [],
        argumentDisplay: 'primary',
        lifecycleDisplay: 'boundary',
      },
    })
    const el = await mountTimeline([
      group('web.search', [
        searchCall('first result', [
          'https://example.test/one',
          'https://example.test/two',
        ]),
        searchCall('second result', ['https://docs.example.test/three']),
      ]),
    ], { presentation: 'activity', groupOpen: true, itemOpen: true })

    try {
      const memberRows = el.querySelectorAll('.tool-row')
      const urlTargets = el.querySelectorAll<HTMLButtonElement>('.tool-row-target--url')

      expect(el.querySelector('.tool-row--group')).toBeNull()
      expect(memberRows).toHaveLength(2)
      expect(Array.from(memberRows).every(row => row.getAttribute('aria-expanded') === 'true')).toBe(true)
      expect(urlTargets).toHaveLength(3)
      expect(urlTargets[0]?.textContent).toContain('example.test/one')
      expect(memberRows[0]?.textContent).toContain('2 results')
      expect(el.textContent).not.toMatch(/INPUT|RESULT|query|provider_attempts/)
      expect(el.querySelector('.activity-tool-details')).toBeNull()

      urlTargets[0]?.click()
      expect(opened).toEqual(['https://example.test/one'])
    } finally {
      window.removeEventListener(BROWSER_WORKBENCH_OPEN_EVENT, onOpen)
    }
  })

  it('keeps a row static until actual detail arrives', async () => {
    const pending = reactive(call('empty-command', {
      inputRaw: '', inputPreview: '', result: '', resultPreview: '',
    }))
    const el = await mountTimeline([group('shell', [pending])], { presentation: 'activity' })
    expect(el.querySelector('.tool-row')?.tagName).toBe('DIV')
    expect(el.querySelector('button.tool-row')).toBeNull()
    expect(el.querySelector('.tool-row')?.hasAttribute('tabindex')).toBe(false)

    pending.result = 'command output'
    pending.resultPreview = 'command output'
    await nextTick()
    expect(el.querySelector('.tool-row')?.tagName).toBe('BUTTON')
    expect(el.querySelector('.tool-row')?.getAttribute('aria-expanded')).toBe('false')
    expect(el.querySelector('.tool-row__activity-arrow')).not.toBeNull()
    expect(el.querySelector('.tool-row-body')).toBeNull()
  })

  it('keeps non-actionable members static within an actionable group', async () => {
    const el = await mountTimeline([group('mixed', [
      call('empty', { inputRaw: '', inputPreview: '', result: '', resultPreview: '' }),
      call('with-output'),
    ])], { groupOpen: true })
    expect(el.querySelector('.tool-row--group')?.tagName).toBe('BUTTON')
    const members = el.querySelectorAll('.tool-row--member')
    expect(members[0]?.tagName).toBe('DIV')
    expect(members[0]?.querySelector('.tool-row__activity-arrow')).toBeNull()
    expect(members[1]?.tagName).toBe('BUTTON')
  })

  it('retains real file-read contents behind the one call disclosure', async () => {
    const inputRaw = JSON.stringify({ path: 'src/App.vue' })
    const el = await mountTimeline([
      group('file.inspect', [call('read-file', {
        name: 'read_file',
        inputRaw,
        inputPreview: inputRaw,
        result: 'private file contents',
        resultPreview: 'private file contents',
        presentation: {
          category: 'file_read',
          primaryArguments: ['path'],
          argumentDisplay: 'primary',
          lifecycleDisplay: 'boundary',
        },
      })]),
    ], { presentation: 'activity', itemOpen: true })

    const row = el.querySelector('.tool-row')
    expect(row?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('.tool-row__arg')?.textContent).toBe('src/App.vue')
    expect(el.querySelector('.tool-row-target--url')).toBeNull()
    expect(el.textContent).toContain('private file contents')
    expect(el.textContent).not.toMatch(/INPUT|RESULT/)
  })

  it.each([undefined, 'activity' as const])(
    'removes successful document disclosure in %s presentation',
    async (presentation) => {
      const sensitive = JSON.stringify({
        source: '<html>private source</html>',
        expectedSha256: 'a'.repeat(64),
        cursor: 'private-cursor',
        grant: 'private-grant',
        bindingToken: 'private-binding-token',
      })
      const el = await mountTimeline([
        group('document.update', [
          call('document-success-one', {
            name: 'document_apply',
            inputRaw: sensitive,
            inputPreview: sensitive,
            result: sensitive,
            resultPreview: sensitive,
          }),
          call('document-success-two', {
            name: 'document_patch',
            inputRaw: sensitive,
            inputPreview: sensitive,
            result: sensitive,
            resultPreview: sensitive,
          }),
        ]),
      ], presentation ? { presentation } : {})

      const row = el.querySelector('.tool-row')
      expect(row?.hasAttribute('aria-expanded')).toBe(false)
      expect(row?.tagName).toBe('DIV')
      expect(row?.hasAttribute('tabindex')).toBe(false)
      expect(row?.classList.contains('tool-row--static')).toBe(true)
      expect(el.querySelector('.step-chevron')).toBeNull()
      expect(el.querySelector('.tool-row-body')).toBeNull()
      expect(el.textContent).not.toMatch(/private|sha256|cursor|grant|binding/i)
    },
  )

  it.each([undefined, 'activity' as const])(
    'shows only safe document failure guidance in %s presentation',
    async (presentation) => {
      useToolDetailPreference().setMode('expanded')
      const sensitive = JSON.stringify({
        category: 'DOCUMENT_PREVIEW_UNAVAILABLE',
        message_key: 'document.previewUnavailable',
        user_message: '<html>private source</html>',
        retry_policy: 'new_turn',
        next_action: 'finalize_without_tools',
        expectedSha256: 'a'.repeat(64),
        cursor: 'private-cursor',
        grant: 'private-grant',
        bindingToken: 'private-binding-token',
      })
      const onShowResult = vi.fn()
      const el = await mountTimeline([
        group('document.update', [call('document-failure', {
          name: 'document_apply',
          status: 'error',
          isError: true,
          inputRaw: sensitive,
          inputPreview: sensitive,
          result: sensitive,
          resultPreview: sensitive,
        })]),
      ], {
        ...(presentation ? { presentation } : {}),
        onShowResult,
      })

      expect(el.querySelector('.step-chevron')).not.toBeNull()
      expect(el.textContent).toContain('Preview unavailable')
      expect(el.textContent).toContain('Retry policy: start a new task')
      expect(el.textContent).not.toMatch(/private|sha256|cursor|grant|binding|<html>/i)
      expect(el.querySelector('.activity-tool-details__view')).toBeNull()
      expect(el.querySelector('.tool-row-section')).toBeNull()
      expect(onShowResult).not.toHaveBeenCalled()
    },
  )

  it.each([undefined, 'activity' as const])(
    'restores a safe document failure disclosure from structured history in %s presentation',
    async (presentation) => {
      useToolDetailPreference().setMode('expanded')
      const persistedFailure = JSON.stringify({
        ok: false,
        status: 'error',
        category: 'DOCUMENT_PREVIEW_UNAVAILABLE',
        message_key: 'document.previewUnavailable',
        retry_policy: 'new_turn',
        next_action: 'finalize_without_tools',
        source: '<html>private source</html>',
        bindingToken: 'private-binding-token',
      })
      const el = await mountTimeline([
        group('document.read', [call('restored-document-failure', {
          name: 'document_browser_inspect',
          status: 'success',
          isError: false,
          result: persistedFailure,
          resultPreview: persistedFailure,
        })]),
      ], presentation ? { presentation } : {})

      expect(el.querySelector('.step-chevron')).not.toBeNull()
      expect(el.textContent).toContain('Preview unavailable')
      expect(el.textContent).toContain('Retry policy: start a new task')
      expect(el.textContent).not.toMatch(/private|binding|<html>/i)
      expect(el.querySelector('.activity-tool-details__view')).toBeNull()
      expect(el.querySelector('.tool-row-section')).toBeNull()
    },
  )

  it('keeps all generic-tool detail inline and copies the full record without a second expansion', async () => {
    useToolDetailPreference().setMode('expanded')
    const result = 'custom tool result\n'.repeat(30)
    const onShowResult = vi.fn()
    const el = await mountTimeline([
      group('long-result-group', [
        call('long-result', {
          name: 'custom_tool',
          result,
          resultPreview: result.slice(0, 200),
        }),
      ]),
    ], {
      presentation: 'activity',
      onShowResult,
    })

    const details = el.querySelector('.activity-tool-details')
    const window = el.querySelector('.activity-tool-details__window')
    expect(details).not.toBeNull()
    expect(details?.classList.contains('activity-tool-details--bounded')).toBe(true)
    expect(window?.textContent).toContain('custom tool result')
    expect(window?.textContent).not.toContain('expand content')
    expect(window?.textContent?.match(/custom tool result/g)).toHaveLength(30)
    expect(el.querySelector('.activity-tool-details__summary')).toBeNull()
    expect(el.querySelectorAll('.activity-tool-details__window')).toHaveLength(1)
    expect(el.querySelector('.tool-row-section')).toBeNull()
    expect(el.querySelector('.activity-tool-details__fade')).toBeNull()
    expect(el.querySelector('.activity-tool-details__view')).toBeNull()
    el.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    await nextTick()
    expect(copyTextWithFallback).toHaveBeenCalledWith(`INPUT\n{}\n\nRESULT\n${result.trim()}`)
    expect(onShowResult).not.toHaveBeenCalled()
  })
})
