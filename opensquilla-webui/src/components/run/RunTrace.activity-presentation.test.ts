// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, type App } from 'vue'

import ToolCallTimeline from '@/components/chat/ToolCallTimeline.vue'
import i18n from '@/i18n'
import type {
  ChatStreamTimelineItem,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
} from '@/types/chat'
import runTraceSource from './RunTrace.vue?raw'

const mountedApps: App[] = []

function ruleBody(selector: string) {
  const selectorStart = runTraceSource.indexOf(selector)
  expect(selectorStart).toBeGreaterThanOrEqual(0)

  const blockStart = runTraceSource.indexOf('{', selectorStart)
  const blockEnd = runTraceSource.indexOf('}', blockStart)
  return runTraceSource.slice(blockStart + 1, blockEnd)
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
        isToolGroupOpen: () => false,
        isToolItemOpen: () => options.itemOpen === true,
        toolGroupStatusText: (toolGroup: ChatToolCallGroup) => {
          if (toolGroup.isRunning) return 'Running'
          if (toolGroup.isError) return 'Failed'
          return 'Done'
        },
        toolStatusText: options.toolStatusText ?? (() => ''),
        toolSecondaryText: () => '',
        onShowResult: options.onShowResult,
      })
    },
  })

  const app = createApp(Host)
  mountedApps.push(app)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return el
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('RunTrace activity presentation', () => {
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
    expect(ruleBody('.tool-timeline--activity .msg-ai-text')).toContain(
      'color: var(--text-muted);',
    )
    expect(ruleBody('.tool-overflow-note')).toContain(
      'color: var(--text-muted);',
    )
  })

  it('aligns subordinate activity content to the post-icon text origin', () => {
    // Row text starts at 0.125rem padding + 0.875rem icon + 0.625rem gap =
    // 1.625rem; detail bodies and narration share that origin, and nested
    // member rows indent by one 1.5rem (icon + gap) level.
    expect(ruleBody('.tool-timeline--activity .tool-row-body')).toContain(
      'padding: 0 0 0.125rem 1.625rem;',
    )
    expect(ruleBody('.tool-timeline--activity .msg-ai-text')).toContain(
      'margin: 0.125rem 0 0.25rem 1.625rem;',
    )
    expect(ruleBody('.tool-timeline--activity .step-group-members')).toContain(
      'padding-left: 1.5rem;',
    )
  })

  it('rests the activity chevron visible on hoverless devices', () => {
    // Touch has no hover to reveal the chevron; without this media block the
    // row reads as inert text. Slice to the block's first rule — the arrow
    // rule sits first inside it.
    const mediaStart = runTraceSource.indexOf('@media (hover: none)')
    expect(mediaStart).toBeGreaterThanOrEqual(0)
    const ruleEnd = runTraceSource.indexOf('}', mediaStart)
    const mediaRule = runTraceSource.slice(mediaStart, ruleEnd)
    expect(mediaRule).toContain('.tool-timeline--activity .tool-row__activity-arrow')
    expect(mediaRule).toContain('opacity: 0.55;')
    expect(mediaRule).not.toContain('translateX(-')
  })

  it('gives the running activity icon a local reduced-motion-safe heartbeat', () => {
    const runningIconRule = ruleBody('.tool-timeline--activity .tool-row__activity-icon--running')
    expect(runningIconRule).toContain('color: var(--accent);')
    expect(runningIconRule).toContain(
      'animation: activity-tool-breathe var(--dur-pulse) var(--ease-standard) infinite;',
    )
    expect(runTraceSource).toContain('@keyframes activity-tool-breathe')
    const reducedMotionStart = runTraceSource.indexOf('@media (prefers-reduced-motion: reduce)')
    expect(reducedMotionStart).toBeGreaterThanOrEqual(0)
    expect(runTraceSource.slice(reducedMotionStart)).toContain(
      '.tool-row__activity-icon--running',
    )
    expect(runTraceSource.slice(reducedMotionStart)).toContain('animation: none;')
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
      '<Transition name="activity-tool-detail" :css="presentation === \'activity\'">',
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

  it('keeps the existing card and success affordances by default', async () => {
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
  })

  it('neutralizes completed chrome and omits failed activity', async () => {
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
    // The footprint secondary already carries the call count, so the raw
    // "N calls" pill stays out of activity group rows.
    expect(el.querySelector('.step-count')).toBeNull()
    expect(
      Array.from(el.querySelectorAll('.tool-row--group .tool-row__status'))
        .map(node => node.textContent),
    ).toEqual([])
    expect(
      el.querySelector('.tool-row--group')?.getAttribute('aria-expanded'),
    ).toBe('false')
    expect(el.querySelector('.tool-row__bullet--err')).toBeNull()
    expect(el.querySelector('.tool-row__activity-icon--error')).toBeNull()
    expect(el.querySelector('.tool-row__state-icon--err')).toBeNull()
    expect(el.querySelector('.activity-tool-details__line--error')).toBeNull()
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
  })

  it('omits a single failed activity call', async () => {
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

    expect(el.querySelector('.tool-row--error')).toBeNull()
    expect(el.textContent).not.toContain('single-failure-group')
    expect(el.querySelector('[role="status"]')).toBeNull()
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

  it('keeps a restored document writer group-level failure visible', async () => {
    const restoredFailure = group('document.update', [
      call('restored-writer', {
        name: 'document_patch',
        status: 'success',
      }),
    ])
    restoredFailure.group.isError = true
    restoredFailure.group.status = 'error'
    const el = await mountTimeline([restoredFailure], { presentation: 'activity' })

    expect(el.querySelector('.tool-row--error')).not.toBeNull()
    expect(el.textContent).toContain("Didn't complete")
  })

  it('omits cancelled activity even when it has injected status copy', async () => {
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

    expect(el.querySelector('.tool-row--error')).toBeNull()
    expect(el.textContent).not.toContain('Cancelled')
    expect(el.querySelector('[role="status"]')).toBeNull()
  })

  it('keeps successful calls from a mixed activity group', async () => {
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

    expect(el.querySelectorAll('.tool-row')).toHaveLength(1)
    expect(el.querySelector('.tool-row--error')).toBeNull()
    expect(el.textContent).toContain('mixed-group')
    expect(el.textContent).not.toContain('failed-call')
  })

  it('keeps a successful activity call collapsed until explicitly opened', async () => {
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

      const row = el.querySelector('.tool-row--group')
      expect(row?.hasAttribute('aria-expanded')).toBe(false)
      expect(el.querySelector('.step-chevron')).toBeNull()
      expect(el.querySelector('.tool-row-body')).toBeNull()
      expect(el.textContent).not.toMatch(/private|sha256|cursor|grant|binding/i)
    },
  )

  it.each([undefined, 'activity' as const])(
    'shows only safe document failure guidance in %s presentation',
    async (presentation) => {
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
      ], presentation ? { presentation, itemOpen: true } : {})

      expect(el.querySelector('.step-chevron')).not.toBeNull()
      expect(el.textContent).toContain('Preview unavailable')
      expect(el.textContent).toContain('Retry policy: start a new task')
      expect(el.textContent).not.toMatch(/private|binding|<html>/i)
      expect(el.querySelector('.activity-tool-details__view')).toBeNull()
      expect(el.querySelector('.tool-row-section')).toBeNull()
    },
  )

  it('shows long activity details in one bounded preview and preserves raw forwarding', async () => {
    const result = 'file contents\n'.repeat(30)
    const onShowResult = vi.fn()
    // A read-shaped call: content-size summaries are reserved for read
    // operations, so this is the compact-summary path.
    const el = await mountTimeline([
      group('long-result-group', [
        call('long-result', {
          name: 'read_file',
          result,
          resultPreview: result.slice(0, 200),
        }),
      ]),
    ], {
      presentation: 'activity',
      itemOpen: true,
      onShowResult,
    })

    const details = el.querySelector('.activity-tool-details')
    const window = el.querySelector('.activity-tool-details__window')
    const viewFull = el.querySelector<HTMLButtonElement>(
      '.activity-tool-details__view',
    )
    expect(details).not.toBeNull()
    expect(details?.classList.contains('activity-tool-details--bounded')).toBe(true)
    expect(window?.textContent).toContain('file contents')
    expect(window?.textContent).toContain('view full')
    expect(el.querySelector('.activity-tool-details__summary')).toBeNull()
    expect(el.querySelectorAll('.activity-tool-details__window')).toHaveLength(1)
    expect(el.querySelector('.tool-row-section')).toBeNull()
    expect(el.querySelector('.activity-tool-details__fade')).not.toBeNull()
    expect(viewFull?.tagName).toBe('BUTTON')
    expect(viewFull?.hasAttribute('data-share-control')).toBe(true)

    viewFull?.click()

    expect(onShowResult).toHaveBeenCalledWith(
      `INPUT\n{}\n\nRESULT\n${result.trim()}`,
      'long-result-group · details',
      {
        toolName: 'read_file',
        inputRaw: '{}',
        section: undefined,
      },
    )
  })
})
