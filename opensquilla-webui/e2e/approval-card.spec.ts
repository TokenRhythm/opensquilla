import { test, expect, type Page } from '@playwright/test'
import type { ApprovalResolveResult } from '../src/contracts/generated/v4/approvalResolve'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload } from './support/session-read-fixtures'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'
const SESSION_KEY = 'agent:main:webchat:e2eapproval'

interface MockApproval {
  id: string
  namespace: string
  toolName: string
  command?: string
  argv?: string[]
  args?: Record<string, unknown>
  warning?: string
  agent?: string
  sessionKey: string
  created_at?: number
  displayKind?: string
  displayTarget?: string
  destructive?: boolean
  irreversible?: boolean
  backupState?: string
}

const execApproval: MockApproval = {
  id: 'ap-e2e-1',
  namespace: 'exec',
  toolName: 'shell',
  command: 'rm -rf build/cache',
  args: { command: 'rm -rf build/cache' },
  agent: 'main',
  sessionKey: SESSION_KEY,
  created_at: Date.now() / 1000,
  displayKind: 'run_command',
  displayTarget: 'rm -rf build/cache',
}

const genericApproval: MockApproval = {
  id: 'ap-e2e-2',
  namespace: 'exec',
  toolName: 'browser_navigate',
  args: { url: 'https://example.com/admin', reason: 'inspect dashboard' },
  sessionKey: SESSION_KEY,
}

const destructiveApproval: MockApproval = {
  id: 'ap-e2e-destructive',
  namespace: 'exec',
  toolName: 'sandbox_elevation',
  args: { action_kind: 'fs.recursive_delete', internal_policy: true },
  sessionKey: SESSION_KEY,
  displayKind: 'delete',
  displayTarget: '/srv/demo/old-project',
  destructive: true,
  irreversible: false,
  backupState: 'enabled',
}

function snapshot(pending: MockApproval[]) {
  return { pending, mode: 'prompt', allowPatterns: [], denyPatterns: [] }
}

function resolvedApproval(id: string, approved: boolean): ApprovalResolveResult {
  return { id, mode: 'prompt', resolved: true, approved,
    resolution: approved ? 'approved' : 'denied', deadline: null, consumed: false, pending: false }
}

async function mockApprovalsRoute(page: Page, getPending: () => MockApproval[], activeExecution = false, holdSnapshot = false) {
  // A mocked approval belongs to a mocked, existing conversation. Leaving the
  // socket live makes this depend on an unrelated Gateway having this test key.
  const rpcCalls: string[] = []
  let emit: (event: string, payload: Record<string, unknown>) => void = () => { throw new Error('Gateway socket is not connected') }
  let releaseSnapshot: () => void = () => { throw new Error('Live snapshot has not been requested') }
  const activeTask = activeExecution
    ? { task_id: 'approval-e2e-task', status: 'approval_pending', started_at: Date.now() / 1000 }
    : null
  const runtime = activeTask
    ? { run_status: 'approval_pending', active_task: activeTask, tasks: [activeTask] }
    : {}
  const history = activeTask
    ? [{ id: 'approval-e2e-user', message_id: 'approval-e2e-user', role: 'user', text: 'Run the command that requires approval.' }]
    : []
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route =>
    route.fulfill({ json: snapshot(getPending()) }))
  await page.routeWebSocket(/\/ws$/, ws => {
    emit = (event, payload) => ws.send(JSON.stringify({ type: 'event', event, payload }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message)) as { type?: string; id?: string; method?: string }
      if (frame.type !== 'req') return
      const method = frame.method || ''
      rpcCalls.push(method)
      if (method === 'connect') { ws.send(helloOkResponse()); return }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload(history),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION_KEY, runtime),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION_KEY, { task_id: activeTask?.task_id ?? null }),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION_KEY, runtime),
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} }, permissions: {}, skills: {} },
        'onboarding.status': { audioConfigured: false },
        'sandbox.run_mode.preference.get': { runMode: 'standard', source: 'config' },
        'sandbox.capability.status': { available: false }, 'usage.status': { sessions: [] },
      }
      const reply = () => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[method] ?? {} }))
      if (holdSnapshot && method === 'sessions.messages.snapshot') releaseSnapshot = reply
      else reply()
    })
  })
  return { rpcCalls, emit: (event: string, payload: Record<string, unknown>) => emit(event, payload),
    releaseSnapshot: () => releaseSnapshot() }
}

async function openMockedChat(page: Page) {
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
}

test.describe('In-thread approval card (mocked snapshot)', () => {
  test('exec approval renders the command in a mono block with the allow-once and deny actions', async ({ page }) => {
    await mockApprovalsRoute(page, () => [execApproval])
    await openMockedChat(page)

    const card = page.getByTestId('approval-card')
    await expect(card).toBeVisible({ timeout: 10000 })
    await expect(card.locator('.approval-card__pre--cmd')).toContainText('rm -rf build/cache')
    await expect(card.getByText('Approval required')).toBeVisible()
    await expect(card.getByRole('button', { name: 'Allow once' })).toBeVisible()
    // The persistent "always allow" shortcut is gone; a plain exec approval offers
    // only Allow once / Deny.
    await expect(card.getByRole('button', { name: 'Always allow this' })).toHaveCount(0)
    await expect(card.getByRole('button', { name: 'Deny' })).toBeVisible()
    await expect(card.locator('.approval-card__note')).toHaveCount(0)
  })

  test('generic approval uses a safe label and never renders tool names or raw args', async ({ page }) => {
    await mockApprovalsRoute(page, () => [genericApproval])
    await openMockedChat(page)

    const card = page.getByTestId('approval-card')
    await expect(card).toBeVisible({ timeout: 10000 })
    await expect(card.getByRole('heading', { name: 'Sensitive operation' })).toBeVisible()
    await expect(card).not.toContainText('browser_navigate')
    await expect(card).not.toContainText('https://example.com/admin')
    // No command and not a sandbox request — the same-type shortcut never renders.
    await expect(card.getByRole('button', { name: 'Always allow this' })).toHaveCount(0)
  })

  test('keeps a pending approval when its hydration precedes the idle session snapshot', async ({ page }) => {
    const gateway = await mockApprovalsRoute(page, () => [genericApproval], false, true)
    await openMockedChat(page)
    await expect.poll(() => gateway.rpcCalls.includes('sessions.messages.snapshot')).toBe(true)
    const card = page.getByTestId('approval-card')
    await expect(card).toBeVisible()
    gateway.releaseSnapshot()
    // The composer becoming ready proves the held snapshot was installed.
    await expect(page.locator('.chat-run-mode-btn')).toBeEnabled()
    await expect(card).toBeVisible()
    await expect(card.getByRole('heading', { name: 'Sensitive operation' })).toBeVisible()
    await expect(page.locator('.conversation-live-edge')).toHaveCount(0)
  })

  test('destructive approval shows the exact target and backup guarantee without internals', async ({ page }) => {
    await mockApprovalsRoute(page, () => [destructiveApproval])
    await openMockedChat(page)

    const card = page.getByTestId('approval-card')
    await expect(card.getByRole('heading', { name: 'Delete files or folders' })).toBeVisible()
    await expect(card).toContainText('/srv/demo/old-project')
    await expect(card).toContainText('A recoverable backup will be created before this change.')
    await expect(card).toContainText('Older backups may be removed automatically to make room.')
    await expect(card).not.toContainText('sandbox_elevation')
    await expect(card).not.toContainText('fs.recursive_delete')
    await expect(card).not.toContainText('internal_policy')
  })

  test('unavailable backup requires an explicit irreversible confirmation', async ({ page }) => {
    await mockApprovalsRoute(page, () => [{
      ...destructiveApproval,
      id: 'ap-e2e-no-backup',
      irreversible: true,
      backupState: 'unavailable_requires_confirmation',
    }])
    await openMockedChat(page)

    const card = page.getByTestId('approval-card')
    await expect(card).toContainText('No backup is available')
    await expect(card).toContainText('This action cannot be recovered.')
    await expect(card.getByRole('button', { name: 'Continue without backup' })).toBeVisible()
    await expect(card.getByRole('button', { name: 'Always allow this' })).toHaveCount(0)
  })

  test('disabled backup warns about recovery and points to the file-safety setting', async ({ page }) => {
    await mockApprovalsRoute(page, () => [{
      ...destructiveApproval,
      id: 'ap-e2e-backup-disabled',
      irreversible: true,
      backupState: 'disabled',
    }])
    await openMockedChat(page)

    const card = page.getByTestId('approval-card')
    await expect(card).toContainText('No backup will be created')
    await expect(card).toContainText('This action may be impossible to recover.')
    await expect(card).toContainText('Turn on backups in Settings > Sandbox > File safety.')
    await expect(card.getByRole('button', { name: 'Always allow this' })).toHaveCount(0)
  })

  test('Allow once resolves and collapses into an approved outcome row', async ({ page }) => {
    let resolved = false
    await mockApprovalsRoute(page, () => (resolved ? [] : [execApproval]), true)
    await page.route('**/api/approvals/resolve', async route => {
      const body = route.request().postDataJSON() as Record<string, unknown>
      expect(body.id).toBe('ap-e2e-1')
      expect(body.approved).toBe(true)
      // The removed persistent-approval params must never ride the resolve call.
      expect(body.allowAlways).toBeUndefined()
      expect(body.rememberIntent).toBeUndefined()
      resolved = true
      await route.fulfill({ json: resolvedApproval('ap-e2e-1', true) })
    })
    await openMockedChat(page)

    const card = page.getByTestId('approval-card')
    await expect(card).toBeVisible({ timeout: 10000 })
    await card.getByRole('button', { name: 'Allow once' }).click()

    const outcome = page.getByTestId('approval-outcome')
    await expect(outcome).toBeVisible()
    await expect(outcome).toContainText('Approved. Run resumed')
    await expect(page.getByTestId('approval-card')).toHaveCount(0)
  })

  test('Deny sends approved=false exactly once and does not create another agent turn', async ({ page }) => {
    let resolved = false
    let resolveCalls = 0
    const gateway = await mockApprovalsRoute(page, () => (resolved ? [] : [execApproval]), true)
    await page.route('**/api/approvals/resolve', async route => {
      resolveCalls += 1
      const body = route.request().postDataJSON() as Record<string, unknown>
      expect(body.id).toBe('ap-e2e-1')
      expect(body.approved).toBe(false)
      resolved = true
      await route.fulfill({ json: resolvedApproval('ap-e2e-1', false) })
    })
    await openMockedChat(page)

    const card = page.getByTestId('approval-card')
    await expect(card).toBeVisible({ timeout: 10000 })
    await card.getByRole('button', { name: 'Deny' }).click()

    await expect(page.getByTestId('approval-outcome')).toContainText('Denied')
    expect(resolveCalls).toBe(1)
    expect(gateway.rpcCalls.filter(method => method === 'chat.send')).toEqual([])
  })

  test('topbar pill deep-links to the blocked session chat, not the retired approvals destination', async ({ page }) => {
    await mockApprovalsRoute(page, () => [execApproval])
    await openMockedChat(page)

    const pill = page.getByRole('button', { name: 'Approval required: 1 · Open the blocked session', exact: true })
    await expect(pill).toBeVisible({ timeout: 10000 })
    await pill.click()

    await expect(page).toHaveURL(new RegExp('/chat\\?session='))
    expect(new URL(page.url()).searchParams.get('session')).toBe(SESSION_KEY)
    expect(page.url()).not.toContain('/approvals')
    await expect(page.getByTestId('approval-card')).toBeVisible()
  })

  for (const approved of [true, false]) {
    test(`standalone idle approval ${approved ? 'Allow once' : 'Deny'} does not manufacture an execution`, async ({ page }) => {
      let resolveCalls = 0
      const gateway = await mockApprovalsRoute(page, () => resolveCalls ? [] : [execApproval])
      await page.route('**/api/approvals/resolve', async route => {
        expect(route.request().postDataJSON()).toMatchObject({ id: execApproval.id, approved })
        resolveCalls += 1
        await route.fulfill({ json: resolvedApproval(execApproval.id, approved) })
      })
      await openMockedChat(page)
      const card = page.getByTestId('approval-card')
      await expect(card).toBeVisible()
      await expect(page.locator('.conversation-live-edge')).toHaveCount(0)
      await expect(page.locator('.chat-run-mode-btn')).toBeEnabled()
      await card.getByRole('button', { name: approved ? 'Allow once' : 'Deny', exact: true }).click()
      await expect(card).toHaveCount(0)
      await expect(page.getByTestId('approval-outcome')).toHaveCount(0)
      await expect(page.locator('.conversation-live-edge')).toHaveCount(0)
      await expect(page.locator('.chat-run-mode-btn')).toBeEnabled()
      await expect(page.locator('.chat-textarea')).toBeEditable()
      expect(resolveCalls).toBe(1)
      expect(gateway.rpcCalls.filter(method => method === 'chat.send')).toEqual([])
    })

    test(`standalone pushed approval ${approved ? 'Allow once' : 'Deny'} keeps execution mode and send usable`, async ({ page }) => {
      let pending: MockApproval[] = []
      const gateway = await mockApprovalsRoute(page, () => pending)
      await page.route('**/api/approvals/resolve', async route => {
        expect(route.request().postDataJSON()).toMatchObject({ id: execApproval.id, approved })
        pending = []
        await route.fulfill({ json: resolvedApproval(execApproval.id, approved) })
      })
      await openMockedChat(page)
      const mode = page.locator('.chat-run-mode-btn')
      await expect(mode).toBeEnabled()
      pending = [execApproval]
      gateway.emit('exec.approval.requested', {
        ...execApproval, approval_id: execApproval.id, session_key: SESSION_KEY,
        tool_name: execApproval.toolName, warning: '',
      })
      const card = page.getByTestId('approval-card')
      await expect(card).toBeVisible()
      await expect(mode).toBeEnabled()
      await expect(page.locator('.chat-stop-btn')).toHaveCount(0)
      await card.getByRole('button', { name: approved ? 'Allow once' : 'Deny', exact: true }).click()
      await expect(card).toHaveCount(0)
      await expect(mode).toBeEnabled()
      await expect(page.locator('.conversation-live-edge')).toHaveCount(0)
      await page.locator('.chat-textarea').fill('Ready for the next message')
      await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
      expect(gateway.rpcCalls.filter(method => method === 'chat.send')).toEqual([])
    })
  }
})

test.describe('Approval flow (live gateway, Standard execution mode)', () => {
  test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')

  test.beforeEach(async ({ page }) => {
    // Run mode is the execution authority. The retired approval-policy setting
    // is intentionally not consulted by sandbox execution.
    await page.addInitScript(() => {
      localStorage.setItem('opensquilla.chat.runMode', 'standard')
    })
  })

  test('blocked shell command surfaces a card; Allow once resumes the run', async ({ page }) => {
    test.slow()
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state="connected"]', { timeout: 15000 })

    const textarea = page.locator('.chat-textarea')
    // `rm` is on the shell warnlist, so this command blocks on approval; the
    // file does not exist and -f makes the command a harmless no-op once allowed.
    await textarea.fill('Run this exact shell command with your shell tool, then repeat the exact command you ran and its exit code: rm -f approval-e2e-ok.txt')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const card = page.getByTestId('approval-card')
    await expect(card).toBeVisible({ timeout: 90000 })
    await expect(card.locator('.approval-card__pre--cmd')).toContainText('approval-e2e-ok')

    await card.getByRole('button', { name: 'Allow once' }).click()
    await expect(page.getByTestId('approval-outcome')).toContainText('Approved. Run resumed')
    await expect(page.locator('.msg-ai').last()).toContainText('approval-e2e-ok', { timeout: 120000 })
  })

  test('Deny terminates the command without scheduling another agent turn', async ({ page }) => {
    test.slow()
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state="connected"]', { timeout: 15000 })

    const textarea = page.locator('.chat-textarea')
    // Warnlisted command (see the Allow-once test): guaranteed to raise an approval.
    await textarea.fill('Run this exact shell command with your shell tool, then repeat the exact command you ran and its exit code: rm -f approval-e2e-deny.txt')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const card = page.getByTestId('approval-card')
    await expect(card).toBeVisible({ timeout: 90000 })

    await card.getByRole('button', { name: 'Deny' }).click()
    await expect(page.getByTestId('approval-outcome')).toContainText('Denied')
  })
})
