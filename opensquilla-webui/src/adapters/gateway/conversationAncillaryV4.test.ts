import { describe, expect, it, vi } from 'vitest'

import { createV4ClarificationSubmission } from './clarificationSubmissionV4'
import { createV4CommandCatalog } from './commandCatalogV4'
import { createV4PromptCacheLease } from './promptCacheLeaseV4'
import { createV4RouteFeedback } from './routeFeedbackV4'
import { createV4UsageReporting } from './usageReportingV4'

function transport() {
  const request = vi.fn(async (method: string) => {
    if (method === 'usage.status') return { sessions: [] }
    if (method === 'usage.query') return { rows: [] }
    if (method === 'usage.cost') return { totalCostUsd: 0, breakdown: [] }
    if (method === 'commands.list_for_surface') {
      return { surface: 'web_chat', commands: [] }
    }
    if (method === 'router.feedback.submit') return { accepted: true, recorded: 'up' }
    if (method.startsWith('sessions.promptCacheKeepalive.')) {
      return {
        enabled: true,
        ttlSeconds: 300,
        intervalSeconds: 240,
        idleTimeoutSeconds: 3600,
        idleExpiresAt: null,
        state: 'scheduled',
        reason: null,
        hasSnapshot: true,
        lastCacheHitTokens: 8,
      }
    }
    if (method === 'chat.clarify_submit') return { resolved: true, requestId: 'request-1' }
    throw new Error(`unexpected method: ${method}`)
  })
  return { request, supports: vi.fn().mockReturnValue(true) }
}

describe('conversation ancillary v4 adapters', () => {
  it('maps all eight generated contract methods to narrow domain interfaces', async () => {
    const rpc = transport()
    const usage = createV4UsageReporting(rpc)
    const commands = createV4CommandCatalog(rpc)
    const feedback = createV4RouteFeedback(rpc)
    const promptCache = createV4PromptCacheLease(rpc)
    const clarification = createV4ClarificationSubmission(rpc)

    await usage.status('agent:main:webchat:test')
    await usage.query({ timezone: 'UTC' })
    await usage.costBreakdown()
    await commands.list('web_chat')
    await feedback.submit('decision-1', 'up')
    await promptCache.status('agent:main:webchat:test')
    await promptCache.setPolicy({
      key: 'agent:main:webchat:test',
      enabled: true,
      ttlSeconds: 300,
      idleTimeoutSeconds: 3600,
    })
    await clarification.submit({
      sessionKey: 'agent:main:webchat:test',
      fields: { scope: 'complete' },
      requestId: 'request-1',
      runId: 'run-1',
    })

    expect(rpc.request.mock.calls.map(call => call[0])).toEqual([
      'usage.status',
      'usage.query',
      'usage.cost',
      'commands.list_for_surface',
      'router.feedback.submit',
      'sessions.promptCacheKeepalive.status',
      'sessions.promptCacheKeepalive.set',
      'chat.clarify_submit',
    ])
    expect(rpc.request).toHaveBeenLastCalledWith('chat.clarify_submit', {
      sessionKey: 'agent:main:webchat:test',
      fields: { scope: 'complete' },
      requestId: 'request-1',
      run_id: 'run-1',
    })
  })

  it('rejects an invalid generated result before it reaches a consumer', async () => {
    const rpc = { request: vi.fn().mockResolvedValue([]) }
    const usage = createV4UsageReporting(rpc)

    await expect(usage.status()).rejects.toThrow('usage.status returned an invalid response')
  })
})
