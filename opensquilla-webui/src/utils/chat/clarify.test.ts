import { describe, expect, it } from 'vitest'

import {
  clarifyRequestFromValue,
  userInputOutcomeFromValue,
} from './clarify'

describe('deferred user-input protocol', () => {
  it('normalizes legacy choice while preserving option presentation', () => {
    expect(clarifyRequestFromValue({
      kind: 'user_input',
      paused: true,
      request_id: 'request-1',
      run_id: 'task-1',
      step: 'plan',
      clarify_schema: {
        intro: 'Choose a scope.',
        fields: [{
          name: 'scope',
          header: 'Scope',
          prompt: 'Which scope?',
          type: 'choice',
          required: true,
          choices: ['Core', 'Full'],
          options: [
            { label: 'Core', description: 'Runtime only.' },
            { label: 'Full' },
          ],
          allow_other: true,
        }],
      },
    })).toEqual({
      intro: 'Choose a scope.',
      fields: [{
        name: 'scope',
        header: 'Scope',
        prompt: 'Which scope?',
        type: 'enum',
        required: true,
        defaultValue: '',
        choices: ['Core', 'Full'],
        options: [
          { label: 'Core', description: 'Runtime only.' },
          { label: 'Full', description: '' },
        ],
        allowOther: true,
      }],
      requestId: 'request-1',
      runId: 'task-1',
      step: 'plan',
    })
  })

  it('recognizes only terminal outcomes with a request identity', () => {
    expect(userInputOutcomeFromValue(JSON.stringify({
      kind: 'user_input',
      status: 'answered',
      paused: false,
      request_id: 'request-1',
    }))).toEqual({ requestId: 'request-1', status: 'answered' })
    expect(userInputOutcomeFromValue({
      kind: 'user_input',
      status: 'answered',
      paused: true,
      request_id: 'request-1',
    })).toBeNull()
  })
})
