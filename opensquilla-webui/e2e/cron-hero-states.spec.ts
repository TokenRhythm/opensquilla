import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/cron'

interface CronFixtureJob {
  id: string
  name: string
  enabled: boolean
  expression: string
  next_run: string
  payloadKind: 'agent_turn' | 'reminder'
  message: string
}

async function installCronRpc(page: Page, jobs: CronFixtureJob[]) {
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    const respond = (id: unknown, payload: unknown) => ws.send(JSON.stringify({
      type: 'res',
      id,
      ok: true,
      payload,
    }))

    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      let frame: { type?: string; id?: unknown; method?: string }
      try {
        frame = JSON.parse(String(raw))
      } catch {
        return
      }
      if (frame.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }))
        return
      }
      if (frame.type !== 'req' || frame.id === undefined) return

      if (frame.method === 'connect') {
        ws.send(JSON.stringify({
          protocol: 3,
          policy: { tick_interval_ms: 30_000 },
          auth: { principal: { isOwner: true } },
          features: { methods: ['cron.list', 'workspaces.list'] },
        }))
        return
      }

      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {},
        'cron.list': { jobs },
        'sessions.list': { sessions: [], has_more: false },
        'usage.status': { sessions: [] },
        'workspaces.list': { workspaces: [] },
      }
      respond(frame.id, payloads[String(frame.method)] ?? {})
    })
  })
}

test('existing jobs render the compact automation status and task list together', async ({ page }) => {
  const nextRun = new Date(Date.now() + 18 * 60 * 1000).toISOString()
  await installCronRpc(page, [
    {
      id: 'daily-summary',
      name: 'Daily summary',
      enabled: true,
      expression: '30 9 * * 1-5',
      next_run: nextRun,
      payloadKind: 'agent_turn',
      message: 'Summarize yesterday and list today’s priorities.',
    },
    {
      id: 'project-review',
      name: 'Project review reminder',
      enabled: false,
      expression: '0 18 28-31 * *',
      next_run: nextRun,
      payloadKind: 'reminder',
      message: 'Review the project plan.',
    },
  ])

  await page.goto(CONTROL_URL)

  const launch = page.locator('.automation-launch')
  await expect(launch).toHaveClass(/automation-launch--compact/)
  await expect(launch.getByRole('heading', { name: 'Automations are running' })).toBeVisible()
  await expect(launch).toContainText('1 of 2 jobs enabled')
  await expect(launch.getByText('1 / 2')).toBeVisible()
  await expect(launch.getByRole('heading', { name: 'Start your first automation' })).toHaveCount(0)
  await expect(page.locator('[data-cron-row="daily-summary"]')).toBeVisible()
  await expect(page.locator('[data-cron-row="project-review"]')).toBeVisible()
})

test('a loaded empty list keeps the full animation and opens the create panel', async ({ page }) => {
  await installCronRpc(page, [])

  await page.goto(CONTROL_URL)

  const launch = page.locator('.automation-launch')
  await expect(launch).not.toHaveClass(/automation-launch--compact/)
  await expect(launch.getByRole('heading', { name: 'Start your first automation' })).toBeVisible()
  await expect(launch.getByRole('heading', { name: 'Automations are running' })).toHaveCount(0)
  await expect(page.locator('[data-cron-row]')).toHaveCount(0)

  await launch.getByRole('button', { name: 'Add automation' }).click()
  await expect(page.locator('.cron-panel')).toBeVisible()
})
