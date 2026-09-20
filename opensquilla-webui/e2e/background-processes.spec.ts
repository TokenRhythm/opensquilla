import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_KEY = 'agent:main:webchat:e2e-background-processes'
const OWNER = { session_key: SESSION_KEY, session_id: 'owner-processes', session_epoch: 0 }

async function installGateway(page: Page) {
  let socket: WebSocketRoute | undefined
  let process = {
    execution_id: 'service-e2e', task_id: 'finished-turn', command: 'python -m uvicorn app.main:app --port 8001',
    status: 'running', returncode: null as number | null, started_at: 1_800_000_000, ended_at: null as number | null,
  }
  const calls: { method: string; params: Record<string, unknown> }[] = []
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      const reply = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      const method = String(frame.method)
      if (method.startsWith('sessions.processes.')) calls.push({ method, params: frame.params })
      switch (method) {
        case 'connect': ws.send(helloOkResponse({
          features: { methods: ['sessions.processes.list', 'sessions.processes.log', 'sessions.processes.stop'],
            events: ['session.event.process_completed', 'session.event.done'] },
          auth: { principal: { isOwner: true, scopes: ['operator.read', 'operator.write'] },
            runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' } },
        })); return
        case 'chat.history': reply(chatHistoryPayload([{
          role: 'assistant', id: 'finished-answer', text: 'Service health check passed.', timestamp: 1000,
        }])); return
        case 'sessions.messages.subscribe': reply(sessionMessagesSubscribePayload(SESSION_KEY)); return
        case 'sessions.messages.hydrate': reply(sessionMessagesHydratePayload(SESSION_KEY)); return
        case 'sessions.messages.snapshot': reply(sessionMessagesSnapshotPayload(SESSION_KEY)); return
        case 'sessions.processes.list': reply({ ...OWNER, processes: [process] }); return
        case 'sessions.processes.log': reply({ ...OWNER, execution_id: process.execution_id,
          status: process.status, output: 'Application startup complete.\nListening on port 8001.', truncated: false }); return
        case 'sessions.processes.stop':
          process = { ...process, status: 'killed', returncode: -1, ended_at: 1_800_000_010 }
          reply({ ...OWNER, process }); return
        case 'sessions.list': reply({ sessions: [{ key: SESSION_KEY, title: 'Background service',
          sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main',
          updatedAt: 1000, messageCount: 1, status: 'ok', runStatus: 'idle' }],
          count: 1, ts: 1_800_000_000, has_more: false }); return
        case 'agents.list': reply({ agents: [] }); return
        case 'commands.list_for_surface': reply({ commands: [] }); return
        case 'models.routing.get': reply({ mode: 'direct' }); return
        default: reply({})
      }
    })
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
  })
  return {
    calls,
    complete(returncode: number) {
      process = { ...process, status: 'done', returncode, ended_at: 1_800_000_020 }
      if (!socket) throw new Error('No connected gateway')
      socket.send(JSON.stringify({ type: 'event', event: 'session.event.process_completed', payload: {
        ...OWNER, ...process, key: SESSION_KEY, stream_generation: 'e2e-stream-generation', stream_seq: 1,
      } }))
    },
  }
}

async function openProcesses(page: Page) {
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION_KEY)}`)
  const panel = page.getByTestId('background-processes')
  await expect(panel).toBeVisible()
  await panel.getByRole('button', { name: /Background processes/ }).click()
  return panel
}

test('a completed conversation retains independent process state, logs and stop', async ({ page }) => {
  const gateway = await installGateway(page)
  const panel = await openProcesses(page)
  const row = page.getByTestId('background-process-service-e2e')
  await expect(page.getByText('Service health check passed.', { exact: true })).toBeVisible()
  await expect(row).toHaveAttribute('data-status', 'running')
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveAttribute('aria-busy', 'false')
  await row.getByRole('button', { name: 'Logs', exact: true }).click()
  await expect(panel.locator('pre')).toContainText('Application startup complete.')
  await page.screenshot({ path: 'output/playwright/background-process-running.png', fullPage: true })
  await row.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(row).toHaveAttribute('data-status', 'killed')
  await expect(row.getByRole('button', { name: 'Stop', exact: true })).toHaveCount(0)
  expect(gateway.calls.filter(call => call.method === 'sessions.processes.stop')).toEqual([{
    method: 'sessions.processes.stop', params: { sessionKey: SESSION_KEY, executionId: 'service-e2e' },
  }])
  await expect(page.getByText('Service health check passed.', { exact: true })).toBeVisible()
})

test('completion after the turn and reload recover authoritative exit status', async ({ page }) => {
  const gateway = await installGateway(page)
  await openProcesses(page)
  const row = page.getByTestId('background-process-service-e2e')
  await expect(row).toHaveAttribute('data-status', 'running')
  gateway.complete(7)
  await expect(row).toHaveAttribute('data-status', 'done')
  await expect(row).toContainText('7')
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveAttribute('aria-busy', 'false')
  const before = gateway.calls.filter(call => call.method === 'sessions.processes.list').length
  await page.reload()
  const panel = page.getByTestId('background-processes')
  await expect(panel).toBeVisible()
  await panel.getByRole('button', { name: /Background processes/ }).click()
  await expect(row).toHaveAttribute('data-status', 'done')
  await expect(row).toContainText('7')
  expect(gateway.calls.filter(call => call.method === 'sessions.processes.list').length).toBeGreaterThan(before)
  await expect(page.getByText('Service health check passed.', { exact: true })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(row).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: 'output/playwright/background-process-completed-mobile.png', fullPage: true })
})
