import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'

async function openControl(page: Page) {
  await page.goto(CONTROL_URL)
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.waitForSelector('.conn-pill.connected', { timeout: 10000 }).catch(() => {})
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible()
}

type RpcParams = Record<string, unknown>

interface ProjectLifecycleState {
  sessionKey: string
  requestMethods: string[]
  pathListRequests: RpcParams[]
  workspaceListRequests: number
  sends: RpcParams[]
  historyDeleteRequests: RpcParams[]
  postDeleteWorkspaceLists: number
  postDeleteSessionLists: number
  projectPresent: boolean
  removed: boolean
  sent: boolean
  historyDeleted: boolean
}

async function installProjectLifecycleRpc(
  page: Page,
  options: { connectDelayMs?: number; owner?: boolean } = {},
): Promise<ProjectLifecycleState> {
  const state: ProjectLifecycleState = {
    sessionKey: 'agent:main:webchat:project-demo-task',
    requestMethods: [],
    pathListRequests: [],
    workspaceListRequests: 0,
    sends: [],
    historyDeleteRequests: [],
    postDeleteWorkspaceLists: 0,
    postDeleteSessionLists: 0,
    projectPresent: false,
    removed: false,
    sent: false,
    historyDeleted: false,
  }
  const workspace = () => ({
    id: 'project-demo',
    name: 'demo',
    path: '/repos/demo',
    taskCount: state.sent ? 1 : 0,
    pinned: false,
    available: true,
    removed: false,
  })
  const session = () => ({
    key: state.sessionKey,
    title: 'pwd',
    sessionKind: 'chat',
    surface: 'webchat',
    conversationKind: 'direct',
    effectiveAgentId: 'main',
    updatedAt: 1_753_500_000,
    messageCount: 1,
    status: 'ok',
    runStatus: 'idle',
    workspaceId: 'project-demo',
    workspace: '/repos/demo',
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
    ws.onMessage(raw => {
      let frame: {
        type?: string
        id?: unknown
        method?: string
        params?: RpcParams
      }
      try {
        frame = JSON.parse(String(raw))
      } catch {
        return
      }
      if (frame.type !== 'req' || frame.id === undefined) return
      state.requestMethods.push(String(frame.method || ''))
      const params = frame.params || {}
      switch (frame.method) {
        case 'connect':
          setTimeout(() => {
            ws.send(JSON.stringify({
              protocol: 3,
              policy: { tick_interval_ms: 30_000 },
              auth: { principal: { isOwner: options.owner !== false } },
              features: {
                methods: [
                  'workspaces.list',
                  'workspaces.open',
                  'workspaces.update',
                  'workspaces.pin',
                  'workspaces.remove',
                  'workspaces.history.delete',
                  'sandbox.path.list',
                  'sandbox.path.pick',
                  'sandbox.path.create-directory',
                ],
              },
            }))
          }, options.connectDelayMs || 0)
          return
        case 'sandbox.path.list':
          state.pathListRequests.push(params)
          respond(frame.id, {
            currentPath: '/repos',
            path: '/repos',
            parentPath: '/',
            entries: [
              {
                name: 'demo',
                path: '/repos/demo',
                kind: 'directory',
                selectable: true,
              },
              ...Array.from({ length: 40 }, (_, index) => ({
                name: `project-${String(index + 1).padStart(2, '0')}`,
                path: `/repos/project-${String(index + 1).padStart(2, '0')}`,
                kind: 'directory',
                selectable: true,
              })),
            ],
          })
          return
        case 'workspaces.open':
          expect(params).toMatchObject({ path: '/repos/demo', trusted: true })
          state.projectPresent = true
          state.removed = false
          respond(frame.id, { workspace: workspace() })
          return
        case 'workspaces.list':
          state.workspaceListRequests += 1
          if (state.historyDeleted) state.postDeleteWorkspaceLists += 1
          respond(frame.id, {
            workspaces: state.projectPresent ? [workspace()] : [],
          })
          return
        case 'chat.send':
          state.sends.push(params)
          state.sent = true
          respond(frame.id, {
            sessionKey: state.sessionKey,
            status: 'accepted',
            task_id: 'project-demo-task',
            message_id: 'project-demo-user-message',
          })
          return
        case 'chat.history':
          respond(frame.id, {
            messages: state.sent
              ? [{
                  role: 'user',
                  text: 'pwd',
                  message_id: 'project-demo-user-message',
                  timestamp: '2026-07-26T00:00:00.000Z',
                }]
              : [],
            has_more: false,
          })
          return
        case 'sessions.list':
          if (state.historyDeleted) state.postDeleteSessionLists += 1
          respond(frame.id, {
            sessions: state.sent ? [session()] : [],
            has_more: false,
          })
          return
        case 'sessions.messages.subscribe':
          respond(frame.id, {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
            workspaceId: state.sent ? 'project-demo' : undefined,
            projectWorkspace: state.sent
              ? state.removed
                ? {
                    ...workspace(),
                    available: false,
                    removed: true,
                    availabilityReason: 'removed',
                  }
                : workspace()
              : null,
          })
          return
        case 'workspaces.remove':
          expect(params).toEqual({ workspaceId: 'project-demo' })
          state.projectPresent = false
          state.removed = true
          respond(frame.id, { workspaceId: 'project-demo' })
          return
        case 'workspaces.history.delete':
          expect(params).toEqual({ workspaceId: 'project-demo' })
          state.historyDeleteRequests.push(params)
          state.historyDeleted = true
          state.sent = false
          respond(frame.id, {
            workspaceId: 'project-demo',
            deletedTaskCount: 1,
            deletedSessionKeys: [state.sessionKey],
          })
          return
        default: {
          const payloads: Record<string, unknown> = {
            'agents.list': { agents: [] },
            'commands.list_for_surface': { commands: [] },
            'config.get': {
              squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
              permissions: {},
              skills: {},
            },
            'onboarding.status': { audioConfigured: false },
            'usage.status': { sessions: [] },
          }
          respond(frame.id, payloads[String(frame.method)] ?? {})
        }
      }
    })
    ws.send(JSON.stringify({
      type: 'event',
      event: 'connect.challenge',
      payload: {},
    }))
  })
  return state
}

test.describe('Project workspaces', () => {
  test('non-owner can continue an existing project task without management RPCs', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page, { owner: false })
    state.projectPresent = true
    state.sent = true

    await page.goto(`/control/chat?session=${encodeURIComponent(state.sessionKey)}`)
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect(page.locator('.chat-project-chip')).toContainText('/repos/demo')
    await expect(page.getByRole('button', { name: 'Choose project' })).toHaveCount(0)

    await page.getByRole('textbox', { name: 'Message to send' }).fill('continue')
    await page.getByRole('button', { name: 'Send' }).click()
    await expect.poll(
      () => state.sends.length,
      { message: `RPC requests: ${state.requestMethods.join(', ')}` },
    ).toBe(1)

    expect(state.sends[0]).not.toHaveProperty('workspaceId')
    expect(state.workspaceListRequests).toBe(0)
    expect(state.pathListRequests).toEqual([])
  })

  test('waits for the connection before restoring a project draft', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page, { connectDelayMs: 800 })
    state.projectPresent = true

    await page.goto('/control/chat/new?agent=main&project=project-demo')
    await expect(page.locator('.conn-pill.connecting')).toBeVisible()
    await page.waitForTimeout(100)
    expect(await page.getByTestId('toast').count()).toBe(0)
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect.poll(() => state.workspaceListRequests).toBeGreaterThan(0)
    expect(await page.getByTestId('toast').count()).toBe(0)
  })

  test('keeps a long project directory list scrollable', async ({ page }) => {
    await installProjectLifecycleRpc(page)
    await openControl(page)

    await page
      .getByRole('navigation', { name: 'Control navigation' })
      .getByRole('button', { name: 'Choose project' })
      .click()

    const picker = page.getByRole('dialog', { name: 'Choose project' })
    const list = picker.locator('.project-picker__entries')
    await expect(picker.getByRole('option')).toHaveCount(41)

    const metrics = await list.evaluate(element => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
    }))
    expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight)

    await list.evaluate(element => {
      element.scrollTop = element.scrollHeight
    })
    await expect.poll(() => list.evaluate(element => element.scrollTop)).toBeGreaterThan(0)
  })

  test('offers project selection from both the sidebar and an ordinary draft', async ({ page }) => {
    await installProjectLifecycleRpc(page)
    await openControl(page)

    await expect(
      page
        .getByRole('navigation', { name: 'Control navigation' })
        .getByRole('button', { name: 'Choose project' }),
    ).toBeVisible()
    await page.locator('.sidebar-new-session').click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main$/)
    await expect(page.getByRole('button', { name: 'Choose project' }).last()).toBeVisible()
  })

  test('project names only disclose tasks while the pencil opens a project draft', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    state.projectPresent = true
    state.sent = true
    await openControl(page)
    const project = page.locator('.sidebar-history-row--workspace').first()

    const disclosure = project.getByTestId('project-workspace-disclosure')
    const info = project.getByTestId('project-workspace-info')
    const pencil = project.getByTestId('project-workspace-new-task')

    await expect(info).toBeVisible()
    await expect(disclosure).toHaveAttribute('aria-expanded', /true|false/)
    const startedExpanded = await disclosure.getAttribute('aria-expanded') === 'true'
    await disclosure.click()
    await expect(disclosure).toHaveAttribute('aria-expanded', String(!startedExpanded))
    await expect(page).not.toHaveURL(/\/chat\?session=/)

    await pencil.click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main&project=[^&]+$/)
    await expect(page.locator('.chat-project-chip')).toBeVisible()
  })

  test('project picker, trust, first send, reload, remove, reopen, and history delete', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    await openControl(page)

    await page
      .getByRole('navigation', { name: 'Control navigation' })
      .getByRole('button', { name: 'Choose project' })
      .click()
    await expect.poll(() => state.pathListRequests.length).toBe(1)
    expect(state.pathListRequests[0]).not.toHaveProperty('path')
    expect(state.pathListRequests[0]).toMatchObject({
      kind: 'workspace',
    })
    expect(state.pathListRequests[0].sessionKey).toEqual(expect.any(String))
    const picker = page.getByRole('dialog', { name: 'Choose project' })
    await picker.getByRole('option', { name: 'demo' }).click()
    await picker.getByRole('button', { name: 'Choose selected directory' }).click()
    await page.getByRole('button', { name: 'Trust and open' }).click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main&project=project-demo$/)
    const projectChip = page.locator('.chat-project-chip')
    await expect(projectChip).toContainText('/repos/demo')
    await expect(projectChip).toHaveAttribute('data-status', 'ready')
    await expect.poll(
      () => state.requestMethods.filter(method => method === 'sessions.messages.subscribe').length,
    ).toBeGreaterThanOrEqual(3)
    await expect(projectChip).toHaveAttribute('data-status', 'ready')

    await page.getByRole('textbox', { name: 'Message to send' }).fill('pwd')
    await page.getByRole('button', { name: 'Send' }).click()
    await expect.poll(
      () => state.sends.length,
      { message: `RPC requests: ${state.requestMethods.join(', ')}` },
    ).toBe(1)
    expect(state.sends[0]).toMatchObject({
      message: 'pwd',
      workspaceId: 'project-demo',
    })
    expect(state.sends[0]._source).toMatchObject({ runMode: 'full' })
    await expect(page).toHaveURL(/\/chat\?session=/)
    await expect(page.locator('.chat-project-chip')).toContainText('/repos/demo')

    await page.reload()
    await expect(page.locator('.chat-project-chip')).toContainText('/repos/demo')
    const projectRow = page.locator('.sidebar-history-row--workspace').first()
    await projectRow.getByTestId('project-workspace-more').click()
    await page.getByRole('menuitem', { name: 'Remove project' }).click()
    await page.getByRole('button', { name: 'Remove project' }).click()
    await expect.poll(() => state.removed).toBe(true)
    await expect(page.locator('.chat-project-chip')).toContainText('/repos/demo')
    const blockedSend = page.getByRole('button', { name: 'Send' })
    await page.getByRole('textbox', { name: 'Message to send' }).fill('must stay')
    await expect(blockedSend).toBeDisabled()
    expect(state.sends).toHaveLength(1)

    await page
      .getByRole('navigation', { name: 'Control navigation' })
      .getByRole('button', { name: 'Choose project' })
      .click()
    const reopenedPicker = page.getByRole('dialog', { name: 'Choose project' })
    await reopenedPicker.getByRole('option', { name: 'demo' }).click()
    await reopenedPicker
      .getByRole('button', { name: 'Choose selected directory' })
      .click()
    await page.getByRole('button', { name: 'Trust and open' }).click()
    await expect.poll(() => state.projectPresent).toBe(true)

    const reopenedRow = page.locator('.sidebar-history-row--workspace').first()
    await reopenedRow.getByTestId('project-workspace-more').click()
    await page
      .getByRole('menuitem', { name: 'Delete project task history' })
      .click()
    await page.getByRole('button', { name: 'Delete history' }).click()
    await expect.poll(() => state.historyDeleted).toBe(true)
    await expect.poll(() => state.postDeleteWorkspaceLists).toBeGreaterThan(0)
    await expect.poll(() => state.postDeleteSessionLists).toBeGreaterThan(0)
    expect(state.historyDeleteRequests).toEqual([{ workspaceId: 'project-demo' }])
    await expect(page.locator(`[data-session-key="${state.sessionKey}"]`)).toHaveCount(0)
    await expect(page.locator('.sidebar-workspace-empty')).toHaveText('No tasks')
  })
})
