import { describe, expect, it } from 'vitest'

import source from './useChatApprovals.ts?raw'

describe('useChatApprovals clarify submit source contract', () => {
  it('can submit a recovered inline clarify request without pendingClarify', () => {
    expect(source).toContain('requestOverride?: ChatClarifyRequest')
    expect(source).toContain('const request = requestOverride || pendingClarify.value')
    expect(source).toContain('if (!requestOverride && clarifySubmitted.value) return')
    expect(source).toContain('await clarificationSubmission.submit({')
    expect(source).toContain('...(request.runId ? { runId: request.runId } : {})')
  })

  it('optimistically acknowledges the click before the backend finishes', () => {
    expect(source).toContain('clarifySubmitted.value = true')
    expect(source).toContain("setInterruptState(key, { resolution: 'replied', busy: true, error: '' })")
    expect(source).toContain('clarifySubmitted.value = false')
    expect(source).toContain('setInterruptState(key, { resolution: null, busy: false, error: message })')
  })
})
