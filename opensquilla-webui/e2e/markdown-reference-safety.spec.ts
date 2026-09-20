import { expect, test } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:markdown-reference-safety'
const BLOCKED_TARGETS = [
  'codex://threads/example',
  'opensquilla://open/session/example',
  'opensquilla://sessions/example',
  'file:///workspace/src/example.py',
  'custom-app://open/example',
  '/workspace/src/example.py',
  'src/example.py:2-4',
]

// Exercise the shipped renderer in a real DOM: DOMPurify relies on native
// Node prototype getters, which happy-dom does not implement equivalently.
// All Gateway messages are synthetic; no model, credentials or external API.
test('blocks local and custom URI links while preserving HTTPS in assistant Markdown', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message)) as {
        type?: string; id?: string; method?: string
      }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse())
        return
      }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload([{
          role: 'assistant', id: 'markdown-boundary-answer', timestamp: 1_800_000_000,
          text: [
            'Resource link safety fixture.',
            ...BLOCKED_TARGETS.map((target, index) => `[Blocked target ${index}](${target})`),
            '[Documentation](https://example.com/docs)',
            'Plain path: src/example.py:2-4',
          ].join('\n\n'),
        }]),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION),
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {}, skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sandbox.run_mode.preference.get': { runMode: 'safe', source: 'config' },
        'sandbox.capability.status': { available: false },
        'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({
        type: 'res', id: frame.id, ok: true, payload: payloads[frame.method || ''] ?? {},
      }))
    })
  })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const answer = page.locator('.msg-ai').filter({ hasText: 'Resource link safety fixture.' })
  await expect(answer).toBeVisible()
  for (const [index] of BLOCKED_TARGETS.entries()) {
    await expect(answer).toContainText(`Blocked target ${index}`)
  }
  await expect(answer.locator('a[href]')).toHaveCount(1)
  await expect(answer.getByRole('link', { name: 'Documentation' }))
    .toHaveAttribute('href', 'https://example.com/docs')
  await expect(answer.getByRole('link', { name: 'Documentation' }))
    .toHaveAttribute('target', '_blank')
  await expect(answer.getByRole('link', { name: 'Documentation' }))
    .toHaveAttribute('rel', 'noopener noreferrer')
  await expect(answer).toContainText('Plain path: src/example.py:2-4')
  await expect(answer.locator('[data-testid="workspace-reference-card"]')).toHaveCount(0)
})
