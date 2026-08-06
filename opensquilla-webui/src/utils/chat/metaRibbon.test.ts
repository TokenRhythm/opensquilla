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
      run_id: 'run-1',
      meta_skill_name: 'meta-kid-project-planner',
      language: 'en',
      total: 4,
      steps: [
        { id: 'a', label: 'A', kind: 'llm_chat', depends_on: [] },
        { id: 'b', label: 'B', kind: 'llm_chat', depends_on: [] },
        { id: 'optional_c', label: 'Optional C', kind: 'llm_chat', depends_on: [] },
        { id: 'optional_d', label: 'Optional D', kind: 'llm_chat', depends_on: [] },
      ],
    })

    completeRun(ribbon, {
      run_id: 'run-1',
      outcome: 'ok',
      completed_steps: ['a', 'b'],
      failed_steps: [],
      recovered_steps: [],
      skipped_steps: [],
    })

    expect(progressPercent(ribbon)).toBe(100)
    expect(counterText(ribbon, ribbonCopy('en'))).toBe('Step 4 of 4')
  })
})

describe('metaRibbon rescue actions', () => {
  it('suppresses the duplicate partial-context choice without removing backend compatibility', () => {
    const ribbon = createRibbon({
      run_id: 'run-1',
      meta_skill_name: 'meta-paper-write',
      language: 'en',
      total: 1,
      steps: [{ id: 'compile', label: 'Compile', kind: 'skill_exec', depends_on: [] }],
    })
    updateStep(ribbon, {
      run_id: 'run-1',
      step_id: 'compile',
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
      run_id: 'run-1',
      outcome: 'failed',
      completed_steps: [],
      failed_steps: ['compile'],
      recovered_steps: [],
      skipped_steps: [],
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
