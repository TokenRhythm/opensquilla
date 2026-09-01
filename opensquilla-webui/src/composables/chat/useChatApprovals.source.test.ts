import { describe, expect, it } from 'vitest'

import source from './useChatApprovals.ts?raw'

describe('useChatApprovals clarify submit source contract', () => {
  it('can submit a recovered inline clarify request without pendingClarify', () => {
    expect(source).toContain('requestOverride?: ChatClarifyRequest')
    expect(source).toContain('const request = requestOverride || pendingClarify.value')
    expect(source).toContain('if (!requestOverride && clarifySubmitted.value) return')
    expect(source).toContain('if (request.runId) params.run_id = request.runId')
  })

  it('keeps the request busy until the backend acknowledges it', () => {
    const awaitAck = source.indexOf('await conversation.submitClarify(params)')
    const pendingState = source.indexOf(
      "setInterruptState(key, { resolution: null, busy: true, error: '' })",
    )
    const repliedState = source.indexOf(
      "setInterruptState(key, { resolution: 'replied', busy: false })",
    )

    expect(pendingState).toBeGreaterThan(-1)
    expect(pendingState).toBeLessThan(awaitAck)
    expect(repliedState).toBeGreaterThan(awaitAck)
    expect(source).toContain('clarifySubmitted.value = false')
    expect(source).toContain('setInterruptState(key, { resolution: null, busy: false, error: message })')
  })
})
