import { describe, expect, it } from 'vitest'

import {
  completeRun,
  counterText,
  createRibbon,
  failSummary,
  progressPercent,
  RESCUE_ACTION_IDS,
  ribbonCopy,
  updateStep,
} from './metaRibbon'

describe('metaRibbon completed progress', () => {
  it('shows a completed run as total of total even when optional steps never emitted terminal states', () => {
    const ribbon = createRibbon({
      runId: 'run-1',
      metaSkillName: 'meta-kid-project-planner',
      language: 'en',
      total: 4,
      steps: [
        { id: 'a', label: 'A', kind: 'llm_chat', dependsOn: [] },
        { id: 'b', label: 'B', kind: 'llm_chat', dependsOn: [] },
        { id: 'optional_c', label: 'Optional C', kind: 'llm_chat', dependsOn: [] },
        { id: 'optional_d', label: 'Optional D', kind: 'llm_chat', dependsOn: [] },
      ],
    })

    completeRun(ribbon, {
      runId: 'run-1',
      outcome: 'ok',
      completedSteps: ['a', 'b'],
      failedSteps: [],
      recoveredSteps: [],
      skippedSteps: [],
    })

    expect(progressPercent(ribbon)).toBe(100)
    expect(counterText(ribbon, ribbonCopy('en'))).toBe('Step 4 of 4')
  })
})

describe('metaRibbon rescue actions', () => {
  it('suppresses the duplicate partial-context choice without removing backend compatibility', () => {
    const ribbon = createRibbon({
      runId: 'run-1',
      metaSkillName: 'meta-paper-write',
      language: 'en',
      total: 1,
      steps: [{ id: 'compile', label: 'Compile', kind: 'skill_exec', dependsOn: [] }],
    })
    updateStep(ribbon, {
      runId: 'run-1',
      stepId: 'compile',
      state: 'failed',
      error: 'Compilation failed',
      rescue: {
        actions: [
          { id: 'retry-step', label: 'Retry failed step' },
          { id: 'review-paid-submit', label: 'Review paid submission' },
          { id: 'retry-with-partial-context', label: 'Retry with partial context' },
          { id: 'install-dependency', label: 'Install dependency' },
        ],
      },
    })
    completeRun(ribbon, {
      runId: 'run-1',
      outcome: 'failed',
      completedSteps: [],
      failedSteps: ['compile'],
      recoveredSteps: [],
      skippedSteps: [],
    })

    expect(RESCUE_ACTION_IDS.has('retry-with-partial-context')).toBe(true)
    expect(failSummary(ribbon, ribbonCopy('en')).buttons.map((button) => button.action)).toEqual([
      'retry-step',
      'review-paid-submit',
      'install-dependency',
      'show-detail',
    ])
  })
})
