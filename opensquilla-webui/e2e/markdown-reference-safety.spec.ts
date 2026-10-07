import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

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
async function stubGatewayWithAnswer(page: Page, session: string, text: string, marker: string): Promise<void> {
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
          role: 'assistant', id: `markdown-boundary-answer-${marker}`, timestamp: 1_800_000_000,
          text,
        }]),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(session),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(session),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(session),
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
  await page.goto(`/control/chat?session=${encodeURIComponent(session)}`)
}

test('blocks local and custom URI links while preserving HTTPS in assistant Markdown', async ({ page }) => {
  const SESSION = 'agent:main:webchat:markdown-reference-safety'
  await stubGatewayWithAnswer(page, SESSION, [
    'Resource link safety fixture.',
    ...BLOCKED_TARGETS.map((target, index) => `[Blocked target ${index}](${target})`),
    '[Documentation](https://example.com/docs)',
    'Plain path: src/example.py:2-4',
  ].join('\n\n'), 'links')
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

test('renders only vetted markdown image sources and live mermaid fences', async ({ page }) => {
  const SESSION = 'agent:main:webchat:markdown-image-mermaid-safety'
  await stubGatewayWithAnswer(page, SESSION, [
    'Rich render fixture.',
    // Allowed: https image.
    '![Ok image](https://example.com/ok.png)',
    // Blocked: active-scheme, relative, and svg data URIs (active documents).
    '![Js image](javascript:alert(1))',
    '![Relative image](./local.png)',
    '![Svg data image](data:image/svg+xml;base64,PHN2Zy8+)',
    // Allowed: raster data URI.
    '![Tiny png](data:image/png;base64,iVBORw0KGgo=)',
    // Live diagram + a broken fence that must keep its source.
    ['```mermaid', 'flowchart TD', '  a --> b', '```'].join('\n'),
    ['```mermaid', 'not a diagram at all <<<', '```'].join('\n'),
  ].join('\n\n'), 'rich')
  const answer = page.locator('.msg-ai').filter({ hasText: 'Rich render fixture.' })
  await expect(answer).toBeVisible()

  const images = answer.locator('.msg-ai-text img')
  await expect(images).toHaveCount(2)
  await expect(answer.locator('img[src="https://example.com/ok.png"]'))
    .toHaveAttribute('loading', 'lazy')
  await expect(answer.locator('img[src="https://example.com/ok.png"]'))
    .toHaveAttribute('decoding', 'async')
  await expect(answer.locator('img[src^="javascript:"]')).toHaveCount(0)
  await expect(answer.locator('img[src="./local.png"]')).toHaveCount(0)
  await expect(answer.locator('img[src^="data:image/svg+xml"]')).toHaveCount(0)
  await expect(answer.locator('img[src^="data:image/png;base64,iVBORw0KGgo="]')).toHaveCount(1)

  // The mermaid chunk loads lazily and renders the valid fence live; the
  // broken one becomes an error card that still exposes the source text.
  // (Scoped to the svg wrap: the toolbar's copy icon is an svg too.)
  const diagram = answer.locator('.mermaid-svg-wrap > svg')
  await expect(diagram).toBeVisible({ timeout: 20_000 })
  await expect(answer.locator('.mermaid-toolbar')).toHaveCount(1)
  await expect(answer.locator('.mermaid-error')).toHaveCount(1)
  await expect(answer.locator('.mermaid-error')).toContainText('No diagram type detected')
  await expect(answer.locator('.mermaid-err-src')).toContainText('not a diagram at all <<<')
  // Click-to-zoom chrome is bound to the decorated images.
  await expect(answer.locator('img.md-img')).toHaveCount(2)
})
