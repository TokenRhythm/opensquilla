import { describe, expect, it } from 'vitest'

import type {
  ChatRenderedMessage,
  ChatStreamTimelineItem,
  ChatToolCallRenderItem,
} from '@/types/chat'
import de from '@/locales/de.json'
import en from '@/locales/en.json'
import es from '@/locales/es.json'
import fr from '@/locales/fr.json'
import ja from '@/locales/ja.json'
import zhHans from '@/locales/zh-Hans.json'
import {
  isSemanticActivityStatusStep,
  projectAssistantActivity,
  projectAssistantActivityTimeline,
  splitLiveAssistantTimeline,
} from './assistantActivity'
import type { AssistantActivityStatusStep } from './assistantActivity'

function message(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: '',
    timeStr: '',
    showHeader: false,
    ...overrides,
  }
}

function call(
  toolId: string,
  overrides: Partial<ChatToolCallRenderItem> = {},
): ChatToolCallRenderItem {
  return {
    toolId,
    renderKey: toolId,
    name: 'web_search',
    displayName: 'Search',
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

function toolGroup(
  calls: ChatToolCallRenderItem[],
  key = `group-${calls.map(item => item.toolId).join('-')}`,
): ChatStreamTimelineItem {
  return {
    type: 'tool-group',
    key,
    group: {
      groupId: key,
      operationKey: 'web.search',
      label: 'Search',
      iconName: 'search',
      calls,
      secondary: '',
      isRunning: false,
      isError: calls.some(item => item.isError),
      status: calls.some(item => item.isError) ? 'error' : 'success',
    },
  }
}

describe('projectAssistantActivity', () => {
  it('uses canonical message text and never guesses from the last timeline fragment', () => {
    const failed = call('failed', {
      status: 'error',
      isError: true,
      result: 'network error',
      resultPreview: 'network error',
    })
    const projection = projectAssistantActivity(
      message({
        text: 'Canonical prefix and suffix',
        timelineItems: [
          { type: 'text', key: 'prefix', html: 'Canonical prefix', rawText: 'Canonical prefix' },
          toolGroup([call('ok'), failed]),
          { type: 'text', key: 'suffix', html: ' and suffix', rawText: ' and suffix' },
        ],
      }),
      text => `<p>${text}</p>`,
    )

    expect(projection.canSeparateActivity).toBe(true)
    expect(projection.activityItems).toHaveLength(1)
    expect(projection.activityItems[0]?.type).toBe('tool-group')
    expect(projection.answerPart).toMatchObject({
      rawText: 'Canonical prefix and suffix',
      html: '<p>Canonical prefix and suffix</p>',
    })
    expect(projection.toolCount).toBe(2)
    expect(projection.failureCount).toBe(1)
    const tools = projection.activityItems.flatMap(item =>
      item.type === 'tool-group' ? item.group.calls : [],
    )
    expect(tools.map(item => item.toolId)).toEqual(['ok', 'failed'])
  })

  it('keeps tool-bounded candidate text as activity without repeating the final answer', () => {
    const projection = projectAssistantActivity(
      message({
        text: 'Final verified answer.',
        timelineItems: [
          toolGroup([call('inspect', { name: 'read_source' })], 'inspect'),
          {
            type: 'text',
            key: 'draft-candidate',
            html: '<p>Draft candidate.</p>',
            rawText: 'Draft candidate.',
          },
          toolGroup([call('verify', { name: 'execute_code' })], 'verify'),
          {
            type: 'text',
            key: 'final-snapshot',
            html: '<p>Final verified answer.</p>',
            rawText: 'Final verified answer.',
          },
        ],
      }),
      text => `<p>${text}</p>`,
    )

    expect(projection.activityItems.map(item => item.type)).toEqual([
      'tool-group',
      'text',
      'tool-group',
    ])
    expect(projection.activityItems[1]).toMatchObject({
      key: 'draft-candidate',
      rawText: 'Draft candidate.',
    })
    expect(JSON.stringify(projection.activityItems)).not.toContain('Final verified answer.')
    expect(projection.answerPart?.rawText).toBe('Final verified answer.')
    expect(projection.toolCount).toBe(2)
  })

  it('keeps a candidate that is followed by the first tool as process narration', () => {
    const projection = projectAssistantActivity(
      message({
        text: 'Final answer after verification.',
        timelineItems: [
          {
            type: 'text',
            key: 'early-candidate',
            html: '<p>Early candidate.</p>',
            rawText: 'Early candidate.',
          },
          toolGroup([call('verify', { name: 'execute_code' })], 'verify'),
          {
            type: 'text',
            key: 'final-snapshot',
            html: '<p>Final answer after verification.</p>',
            rawText: 'Final answer after verification.',
          },
        ],
      }),
      text => `<p>${text}</p>`,
    )

    expect(projection.activityItems.map(item => item.type)).toEqual([
      'text',
      'tool-group',
    ])
    expect(projection.activityItems[0]).toMatchObject({
      key: 'early-candidate',
      rawText: 'Early candidate.',
    })
    expect(JSON.stringify(projection.activityItems))
      .not.toContain('Final answer after verification.')
  })

  it('preserves the original timeline when old history has text but no canonical answer', () => {
    const timelineItems: ChatStreamTimelineItem[] = [
      { type: 'text', key: 'legacy-text', html: 'Legacy answer', rawText: 'Legacy answer' },
      toolGroup([call('legacy-tool')]),
    ]
    const projection = projectAssistantActivity(
      message({ text: '', timelineItems }),
      text => text,
    )

    expect(projection.canSeparateActivity).toBe(false)
    expect(projection.activityItems).toEqual([])
    expect(projection.answerPart).toBeNull()
    expect(projection.toolCount).toBe(0)
  })

  it('treats whitespace-only canonical text as missing for compatibility', () => {
    const projection = projectAssistantActivity(
      message({
        text: '   ',
        timelineItems: [{
          type: 'text',
          key: 'legacy-text',
          html: 'Legacy answer',
          rawText: 'Legacy answer',
        }],
      }),
      text => text,
    )

    expect(projection.canSeparateActivity).toBe(false)
    expect(projection.answerPart).toBeNull()
  })

  it('folds legacy tool-only calls without inventing an answer', () => {
    const fallback = [toolGroup([call('legacy-tool')])]
    const projection = projectAssistantActivity(
      message({ text: '', timelineItems: [] }),
      text => text,
      fallback,
    )

    expect(projection.canSeparateActivity).toBe(true)
    expect(projection.activityItems).toEqual(fallback)
    expect(projection.answerPart).toBeNull()
    expect(projection.toolCount).toBe(1)
  })
})

describe('projectAssistantActivityTimeline', () => {
  it('projects explicit lifecycle codes and marks only live calls as current', () => {
    const running = toolGroup([
      call('running', {
        name: 'bash_exec',
        isRunning: true,
        status: '',
      }),
    ])

    const working = projectAssistantActivityTimeline([running], { lifecycle: 'working' })
    expect(working.lifecycle).toBe('working')
    expect(working.lifecycleLabel).toEqual({
      code: 'chat.activity.lifecycle.working',
      params: {},
    })
    expect(working.activityClusters[0]).toMatchObject({
      state: 'running',
      isCurrent: true,
      isFailure: false,
    })
    expect(working.currentClusterKey).toBe(working.activityClusters[0]?.key)

    const settled = projectAssistantActivityTimeline([running])
    expect(settled.lifecycleLabel.code).toBe('chat.activity.lifecycle.settled')
    expect(settled.activityClusters[0]?.isCurrent).toBe(false)
    expect(settled.currentClusterKey).toBeNull()
  })

  it('labels in-flight clusters in the present tense and settled clusters in the past tense', () => {
    const running = toolGroup([
      call('running', {
        name: 'bash_exec',
        isRunning: true,
        status: '',
      }),
    ])

    const live = projectAssistantActivityTimeline([running], { lifecycle: 'working' })
    expect(live.activityClusters[0]?.purpose).toEqual({
      code: 'chat.activity.purposeRunning.run',
      params: { count: 1 },
    })

    const settled = projectAssistantActivityTimeline([running])
    expect(settled.activityClusters[0]?.purpose).toEqual({
      code: 'chat.activity.purpose.run',
      params: { count: 1 },
    })
  })

  it('keeps completed clusters past tense while a later cluster is still current', () => {
    const projection = projectAssistantActivityTimeline([
      toolGroup([
        call('done', { name: 'web_search' }),
        call('pending', { name: 'web_search', status: '' }),
      ]),
    ], { lifecycle: 'working' })

    expect(projection.activityClusters.map(cluster => cluster.purpose.code)).toEqual([
      'chat.activity.purpose.search',
      'chat.activity.purposeRunning.search',
    ])
  })

  it('groups contiguous completed calls with the same semantics and respects text boundaries', () => {
    const projection = projectAssistantActivityTimeline([
      toolGroup([
        call('write', {
          name: 'write_file',
          inputRaw: '{"path":"/repo/a.ts"}',
        }),
        call('edit', {
          name: 'edit_file',
          inputRaw: '{"path":"/repo/b.ts"}',
        }),
      ]),
      {
        type: 'text',
        key: 'reasoning-boundary',
        html: 'Checking the change',
        rawText: 'Checking the change',
      },
      toolGroup([call('patch', {
        name: 'apply_patch',
        inputRaw: '{"path":"/repo/c.ts"}',
      })]),
    ])

    expect(projection.activityClusters).toHaveLength(2)
    expect(projection.activityClusters.map(cluster => ({
      purpose: cluster.purpose.code,
      footprint: cluster.footprint.code,
      callCount: cluster.callCount,
      callIds: cluster.calls.map(item => item.toolId),
    }))).toEqual([
      {
        purpose: 'chat.activity.purpose.change',
        footprint: 'chat.activity.footprint.files',
        callCount: 2,
        callIds: ['write', 'edit'],
      },
      {
        purpose: 'chat.activity.purpose.change',
        footprint: 'chat.activity.footprint.files',
        callCount: 1,
        callIds: ['patch'],
      },
    ])
  })

  it('isolates running, pending, and failed calls from completed neighbors', () => {
    const projection = projectAssistantActivityTimeline([
      toolGroup([
        call('complete-before', { name: 'write_file' }),
        call('running', {
          name: 'edit_file',
          isRunning: true,
          status: '',
        }),
        call('complete-middle', { name: 'apply_patch' }),
        call('pending', {
          name: 'write_file',
          status: '',
        }),
        call('failed', {
          name: 'edit_file',
          status: 'error',
          isError: true,
        }),
        call('complete-after', { name: 'write_file' }),
      ]),
    ], { lifecycle: 'answering' })

    expect(projection.activityClusters.map(cluster => cluster.state)).toEqual([
      'complete',
      'running',
      'complete',
      'pending',
      'failed',
      'complete',
    ])
    expect(projection.activityClusters.every(cluster => cluster.callCount === 1)).toBe(true)
    expect(projection.activityClusters.filter(cluster => cluster.isCurrent).map(cluster =>
      cluster.calls[0]?.toolId,
    )).toEqual(['running', 'pending'])
    expect(projection.currentClusterKey).toBe(projection.activityClusters[3]?.key)
    expect(projection.activityClusters[4]).toMatchObject({
      isCurrent: false,
      isFailure: true,
    })
  })

  it('keeps cluster keys stable as calls accumulate without leaking tool details', () => {
    const first = call('tool-opaque-1', {
      name: 'bash_exec',
      displayName: 'Run /private/customer-a/secret.sh',
      inputRaw: '{"command":"cat /private/customer-a/secret.txt"}',
      inputPreview: 'cat /private/customer-a/secret.txt',
      result: '/private/customer-a/secret.txt',
      resultPreview: '/private/customer-a/secret.txt',
    })
    const initial = projectAssistantActivityTimeline([toolGroup([first])])
    const accumulated = projectAssistantActivityTimeline([
      toolGroup([
        {
          ...first,
          displayName: 'Run a command',
          inputRaw: '{"command":"printf safe"}',
          inputPreview: 'printf safe',
          result: 'safe',
          resultPreview: 'safe',
        },
        call('tool-opaque-2', { name: 'python_exec' }),
      ]),
    ])

    expect(accumulated.activityClusters).toHaveLength(1)
    expect(accumulated.activityClusters[0]?.callCount).toBe(2)
    expect(accumulated.activityClusters[0]?.key).toBe(initial.activityClusters[0]?.key)

    const publicProjection = {
      key: initial.activityClusters[0]?.key,
      lifecycleLabel: initial.lifecycleLabel,
      purpose: initial.activityClusters[0]?.purpose,
      footprint: initial.activityClusters[0]?.footprint,
      purposeSummary: initial.purposeSummary,
      footprintSummary: initial.footprintSummary,
    }
    expect(JSON.stringify(publicProjection)).not.toContain('/private/customer-a')
    expect(JSON.stringify(publicProjection)).not.toContain('secret')
    expect(initial.activityClusters[0]?.key).toMatch(/^activity-cluster:[a-z0-9]+$/)
  })

  it('limits semantic summaries to two codes and counts omitted calls in the overflow', () => {
    // The run kind gets two calls so the number of omitted kinds (2) and the
    // number of omitted calls (3) genuinely differ: the overflow segment must
    // count calls, matching the unit of the visible segments, while
    // `remainingCount` keeps reporting kinds.
    const projection = projectAssistantActivityTimeline([
      toolGroup([call('search', { name: 'web_search' })]),
      toolGroup([call('inspect', {
        name: 'read_file',
        inputRaw: '{"path":"/repo/file.ts"}',
      })]),
      toolGroup([
        call('run', { name: 'bash_exec' }),
        call('run-again', { name: 'python_exec' }),
      ]),
      toolGroup([call('artifact', { name: 'publish_artifact' })]),
    ])

    expect(projection.purposeSummary).toEqual({
      codes: [
        { code: 'chat.activity.purpose.search', params: { count: 1 } },
        { code: 'chat.activity.purpose.inspect', params: { count: 1 } },
      ],
      remainingCount: 2,
      remaining: { code: 'chat.activity.more', params: { count: 3 } },
    })
    expect(projection.footprintSummary).toEqual({
      codes: [
        { code: 'chat.activity.footprint.web', params: { count: 1 } },
        { code: 'chat.activity.footprint.files', params: { count: 1 } },
      ],
      remainingCount: 2,
      remaining: { code: 'chat.activity.more', params: { count: 3 } },
    })
  })

  it('uses a strict tool allowlist and degrades opaque read-like tools safely', () => {
    const projection = projectAssistantActivityTimeline([
      toolGroup([
        call('thread', { name: 'read_thread' }),
        call('resource', { name: 'read_mcp_resource' }),
      ]),
    ])

    expect(projection.activityClusters).toHaveLength(1)
    expect(projection.activityClusters[0]?.purpose.code).toBe('chat.activity.purpose.use')
    expect(projection.activityClusters[0]?.footprint).toEqual({
      code: 'chat.activity.footprint.tools',
      params: { count: 2 },
    })
  })

  it('classifies common built-in tools by their activity semantics', () => {
    const projection = projectAssistantActivityTimeline([
      toolGroup([
        call('read-source', {
          name: 'read_source',
          inputRaw: '{"path":"/repo/source.ts"}',
        }),
        call('read-sheet', {
          name: 'read_spreadsheet',
          inputRaw: '{"path":"/repo/data.xlsx"}',
        }),
      ]),
      toolGroup([
        call('write-scratch', {
          name: 'write_scratch',
          inputRaw: '{"path":"/repo/.scratch/repro.py"}',
        }),
        call('create-source', {
          name: 'create_source',
          inputRaw: '{"path":"/repo/new.ts"}',
        }),
        call('edit-source', {
          name: 'edit_source',
          inputRaw: '{"path":"/repo/source.ts"}',
        }),
      ]),
      toolGroup([call('execute', { name: 'execute_code' })]),
      toolGroup([call('request', { name: 'http_request' })]),
    ])

    expect(projection.activityClusters.map(cluster => ({
      calls: cluster.calls.map(item => item.toolId),
      purpose: cluster.purpose.code,
      footprint: cluster.footprint,
    }))).toEqual([
      {
        calls: ['read-source', 'read-sheet'],
        purpose: 'chat.activity.purpose.inspect',
        footprint: { code: 'chat.activity.footprint.files', params: { count: 2 } },
      },
      {
        calls: ['write-scratch', 'create-source', 'edit-source'],
        purpose: 'chat.activity.purpose.change',
        footprint: { code: 'chat.activity.footprint.files', params: { count: 3 } },
      },
      {
        calls: ['execute'],
        purpose: 'chat.activity.purpose.run',
        footprint: { code: 'chat.activity.footprint.commands', params: { count: 1 } },
      },
      {
        calls: ['request'],
        purpose: 'chat.activity.purpose.read',
        footprint: { code: 'chat.activity.footprint.web', params: { count: 1 } },
      },
    ])
  })

  it('counts unique structured file targets instead of file tool calls', () => {
    const projection = projectAssistantActivityTimeline([
      toolGroup([
        call('write-a', {
          name: 'write_file',
          inputRaw: '{"path":"/private/project/same.ts"}',
        }),
        call('edit-a', {
          name: 'edit_file',
          inputRaw: '{"path":"/private/project/same.ts"}',
        }),
      ]),
    ])

    expect(projection.activityClusters[0]?.callCount).toBe(2)
    expect(projection.activityClusters[0]?.footprint).toEqual({
      code: 'chat.activity.footprint.files',
      params: { count: 1 },
    })
    expect(projection.footprintSummary.codes).toEqual([
      { code: 'chat.activity.footprint.files', params: { count: 1 } },
    ])
    expect(JSON.stringify(projection.footprintSummary)).not.toContain('/private/project')
  })

  it('counts unstructured file work as operations rather than invented files', () => {
    const projection = projectAssistantActivityTimeline([
      toolGroup([
        call('write-a', { name: 'write_file', inputRaw: 'opaque input' }),
        call('edit-b', { name: 'edit_file', inputRaw: '{}' }),
      ]),
    ])

    expect(projection.activityClusters[0]?.footprint).toEqual({
      code: 'chat.activity.footprint.fileOperations',
      params: { count: 2 },
    })
  })

  it('projects status actions without exposing raw phase labels', () => {
    const projection = projectAssistantActivityTimeline([], {
      lifecycle: 'answering',
      statusHistory: [
        {
          action: 'Sending',
          label: 'Sending /private/customer/secret.txt',
          at: 1_000,
        },
        {
          action: 'write:1',
          label: 'Writing /private/customer/secret.txt',
          at: 2_000,
        },
      ],
    })

    expect(projection.statusSteps.map(step => step.label.code)).toEqual([
      'chat.activity.lifecycle.working',
      'chat.activity.lifecycle.answering',
    ])
    expect(projection.statusSteps[1]?.isCurrent).toBe(true)
    expect(JSON.stringify(projection.statusSteps)).not.toContain('/private/customer')
    expect(JSON.stringify(projection.statusSteps)).not.toContain('secret')
  })

  it('returns an empty semantic projection on the legacy compatibility path', () => {
    const projection = projectAssistantActivity(
      message({
        text: '',
        timelineItems: [
          { type: 'text', key: 'legacy', html: 'Legacy answer', rawText: 'Legacy answer' },
          toolGroup([call('hidden-from-projection')]),
        ],
      }),
      text => text,
    )

    expect(projection.canSeparateActivity).toBe(false)
    expect(projection.activityClusters).toEqual([])
    expect(projection.purposeSummary.codes).toEqual([])
    expect(projection.footprintSummary.codes).toEqual([])
  })
})

describe('isSemanticActivityStatusStep', () => {
  function statusStep(
    overrides: Partial<AssistantActivityStatusStep> = {},
  ): AssistantActivityStatusStep {
    return {
      key: 'activity-status:test',
      label: { code: 'chat.activity.purpose.search', params: {} },
      at: 1_000,
      isCurrent: false,
      ...overrides,
    }
  }

  it('accepts settled purpose steps', () => {
    expect(isSemanticActivityStatusStep(statusStep())).toBe(true)
    expect(isSemanticActivityStatusStep(statusStep({
      label: { code: 'chat.activity.purposeRunning.run', params: {} },
    }))).toBe(true)
  })

  it('rejects lifecycle phases so generic churn never counts as work', () => {
    expect(isSemanticActivityStatusStep(statusStep({
      label: { code: 'chat.activity.lifecycle.working', params: {} },
    }))).toBe(false)
    expect(isSemanticActivityStatusStep(statusStep({
      label: { code: 'chat.activity.lifecycle.answering', params: {} },
    }))).toBe(false)
  })

  it('rejects the live current step regardless of its code', () => {
    expect(isSemanticActivityStatusStep(statusStep({ isCurrent: true }))).toBe(false)
  })
})

describe('splitLiveAssistantTimeline', () => {
  it('keeps only the trailing text outside as the current answer candidate', () => {
    const timeline: ChatStreamTimelineItem[] = [
      toolGroup([call('inspect', { name: 'read_file' })]),
      {
        type: 'text',
        key: 'candidate',
        html: '<p>Drafting the answer</p>',
        rawText: 'Drafting the answer',
      },
    ]
    const snapshot = structuredClone(timeline)

    const split = splitLiveAssistantTimeline(timeline)

    expect(split.activityItems).toEqual([timeline[0]])
    expect(split.answerItem).toEqual(timeline[1])
    expect(split.answerItem).not.toBe(timeline[1])
    expect(timeline).toEqual(snapshot)
  })

  it('returns earlier text to activity when a later tool starts', () => {
    const narration: ChatStreamTimelineItem = {
      type: 'text',
      key: 'narration',
      html: '<p>I will verify that first.</p>',
      rawText: 'I will verify that first.',
    }
    const timeline = [
      toolGroup([call('inspect', { name: 'read_file' })]),
      narration,
      toolGroup([call('verify', { name: 'bash_exec', isRunning: true, status: '' })]),
    ]

    const split = splitLiveAssistantTimeline(timeline)

    expect(split.answerItem).toBeNull()
    expect(split.activityItems).toEqual(timeline)
  })

  it('recognizes a rendered trailing candidate when an older live item lacks raw text', () => {
    const timeline: ChatStreamTimelineItem[] = [
      toolGroup([call('inspect', { name: 'read_file' })]),
      {
        type: 'text',
        key: 'rendered-candidate',
        html: '<p>Rendered candidate</p>',
      },
    ]

    const split = splitLiveAssistantTimeline(timeline)

    expect(split.activityItems).toEqual([timeline[0]])
    expect(split.answerItem).toMatchObject({
      key: 'rendered-candidate',
      html: '<p>Rendered candidate</p>',
    })
  })
})

describe('assistant activity locale contract', () => {
  it('keeps lifecycle, purpose, and footprint code parity across all bundled locales', () => {
    const activities = [en, de, es, fr, ja, zhHans].map(locale => locale.chat.activity)
    const expected = activities[0]

    // Every past-tense purpose leaf needs a present-tense counterpart so a
    // current cluster can swap purpose -> purposeRunning without a miss.
    expect(Object.keys(expected.purposeRunning).sort()).toEqual(
      Object.keys(expected.purpose).sort(),
    )

    for (const activity of activities.slice(1)) {
      expect(Object.keys(activity.lifecycle).sort()).toEqual(
        Object.keys(expected.lifecycle).sort(),
      )
      expect(Object.keys(activity.purpose).sort()).toEqual(
        Object.keys(expected.purpose).sort(),
      )
      expect(Object.keys(activity.purposeRunning).sort()).toEqual(
        Object.keys(expected.purposeRunning).sort(),
      )
      expect(Object.keys(activity.footprint).sort()).toEqual(
        Object.keys(expected.footprint).sort(),
      )
      expect(typeof activity.more).toBe('string')
    }
  })
})
