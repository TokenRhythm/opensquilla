import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'

async function openControl(page: Page) {
  await page.goto(CONTROL_URL)
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.waitForSelector('.conn-pill.connected', { timeout: 10000 }).catch(() => {})
  await expect(page.locator('#sidebar-nav')).toBeVisible()
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
  workspaceName: string
  workspacePath: string
  workspaceOpenError: string | null
  workspaceOpenRequests: RpcParams[]
  workspaceUpdateRequests: RpcParams[]
  removed: boolean
  sent: boolean
  historyDeleted: boolean
}

async function installProjectLifecycleRpc(
  page: Page,
  options: { connectDelayMs?: number; owner?: boolean } = {},
): Promise<ProjectLifecycleState> {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
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
    workspaceName: 'demo',
    workspacePath: '/repos/demo',
    workspaceOpenError: null,
    workspaceOpenRequests: [],
    workspaceUpdateRequests: [],
    removed: false,
    sent: false,
    historyDeleted: false,
  }
  const workspace = () => ({
    id: 'project-demo',
    name: state.workspaceName,
    path: state.workspacePath,
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
    const reject = (id: unknown, message: string) => ws.send(JSON.stringify({
      type: 'res',
      id,
      ok: false,
      error: { code: 'WORKSPACE_OPEN_FAILED', message },
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
      if (frame.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }))
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
            systemPickerAvailable: false,
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
          state.workspaceOpenRequests.push(params)
          if (state.workspaceOpenError) {
            reject(frame.id, state.workspaceOpenError)
            return
          }
          state.projectPresent = true
          state.removed = false
          respond(frame.id, { workspace: workspace() })
          return
        case 'workspaces.update':
          state.workspaceUpdateRequests.push(params)
          state.workspaceName = String(params.name || state.workspaceName)
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

async function submitProjectFromHeader(page: Page, name = 'demo') {
  let creator = page.getByRole('dialog', { name: 'Create project' })
  await expect(async () => {
    if (!await creator.isVisible()) {
      await page.getByTestId('sidebar-create-project').click()
    }
    await page.waitForTimeout(250)
    await expect(creator).toBeVisible()
  }).toPass({ timeout: 10_000 })
  await creator
    .getByRole('button', { name: 'Add a folder OpenSquilla can read and edit', exact: true })
    .click()

  const picker = page.getByRole('dialog', { name: 'Choose project' })
  await picker.getByRole('option', { name: 'demo', exact: true }).click()
  await picker.getByRole('button', { name: 'Choose selected directory', exact: true }).click()

  creator = page.getByRole('dialog', { name: 'Create project' })
  const nameInput = creator.getByRole('textbox', { name: 'Project name' })
  await expect(nameInput).toHaveValue('demo')
  if (name !== 'demo') await nameInput.fill(name)
  await creator.getByRole('button', { name: 'Create project', exact: true }).click()
  await page.getByRole('button', { name: 'Trust and open', exact: true }).click()
}

test.describe('Project workspaces', () => {
  test('non-owner can continue an existing project task without management RPCs', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page, { owner: false })
    state.projectPresent = true
    state.sent = true

    await page.goto(`/control/chat?session=${encodeURIComponent(state.sessionKey)}`)
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect(page.locator('.chat-project-chip')).toHaveCount(0)
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

    await page.locator('.sidebar-new-session').click()
    await page.getByRole('button', { name: 'Choose project', exact: true }).click()

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

  test('offers project selection in an ordinary draft but not the sidebar navigation', async ({ page }) => {
    await installProjectLifecycleRpc(page)
    await openControl(page)

    await expect(
      page
        .getByRole('navigation', { name: 'Control navigation' })
        .getByRole('button', { name: 'Choose project' }),
    ).toHaveCount(0)
    await page.locator('.sidebar-new-session').click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main$/)
    await expect(page.getByRole('button', { name: 'Choose project', exact: true })).toBeVisible()
  })

  test('creates a project from the projects header without starting a task', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    await openControl(page)

    await submitProjectFromHeader(page)

    await expect.poll(() => state.projectPresent).toBe(true)
    await expect.poll(() => state.pathListRequests.length).toBe(1)
    expect(state.requestMethods).not.toContain('sandbox.path.pick')
    await expect(page.locator('.sidebar-history-row--workspace')).toHaveCount(1)
    await expect(page.locator('[data-session-key^="draft:project:"]')).toHaveCount(0)
    await expect(page).not.toHaveURL(/project=project-demo/)
    const toast = page.getByTestId('toast')
    await expect(toast).toContainText('Created project “demo”')
    await expect(toast).toHaveClass(/toast--ok/)
  })

  test('reports an existing project by returned workspace identity', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    state.projectPresent = true
    // The picker submits an alias while the server returns its canonical path.
    // Duplicate detection must use the stable workspace id rather than either string.
    state.workspacePath = '/canonical/repos/demo'
    await openControl(page)
    await expect(page.locator('.sidebar-history-row--workspace')).toHaveCount(1)

    await submitProjectFromHeader(page)

    await expect(page.getByRole('dialog', { name: 'Create project' })).toHaveCount(0)
    const toast = page.getByTestId('toast')
    await expect(toast).toContainText(
      'Project “demo” already exists; using the existing project',
    )
    await expect(toast).toHaveClass(/toast--info/)
    await expect(page.locator('.sidebar-history-row--workspace')).toHaveCount(1)
    expect(state.workspaceOpenRequests).toHaveLength(1)
    expect(state.workspaceUpdateRequests).toEqual([])
    await expect(page).not.toHaveURL(/project=project-demo/)
  })

  test('renames an existing project and explains the update', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    state.projectPresent = true
    await openControl(page)
    await expect(page.locator('.sidebar-history-row--workspace')).toHaveCount(1)

    await submitProjectFromHeader(page, 'renamed')

    const toast = page.getByTestId('toast')
    await expect(toast).toContainText(
      'The project already existed and was renamed to “renamed”',
    )
    await expect(toast).toHaveClass(/toast--info/)
    expect(state.workspaceOpenRequests).toHaveLength(1)
    expect(state.workspaceUpdateRequests).toEqual([{
      workspaceId: 'project-demo',
      name: 'renamed',
    }])
    await expect(page.locator('.sidebar-history-row--workspace')).toContainText('renamed')
  })

  test('keeps open failures distinct from duplicate feedback', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    state.projectPresent = true
    state.workspaceOpenError = 'synthetic open failure'
    await openControl(page)
    await expect(page.locator('.sidebar-history-row--workspace')).toHaveCount(1)

    await submitProjectFromHeader(page)

    const toast = page.getByTestId('toast')
    await expect(toast).toContainText('synthetic open failure')
    await expect(toast).not.toContainText('already exists')
    await expect(toast).toHaveClass(/toast--danger/)
    await expect(page.getByRole('dialog', { name: 'Create project' })).toBeVisible()
    expect(state.workspaceUpdateRequests).toEqual([])
  })

  test('project names only disclose tasks while the plus opens a project draft', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    state.projectPresent = true
    state.sent = true
    await openControl(page)
    const project = page.locator('.sidebar-history-row--workspace').first()

    const disclosure = project.getByTestId('project-workspace-disclosure')
    const info = project.getByTestId('project-workspace-info')
    const plus = project.getByTestId('project-workspace-new-task')

    await expect(info).toBeVisible()
    await expect(plus).toHaveCSS('opacity', '0')
    await expect(disclosure).toHaveAttribute('aria-expanded', /true|false/)
    const startedExpanded = await disclosure.getAttribute('aria-expanded') === 'true'
    await disclosure.click()
    await expect(disclosure).toHaveAttribute('aria-expanded', String(!startedExpanded))
    await expect(page).not.toHaveURL(/\/chat\?session=/)

    await project.hover()
    await expect(plus).toHaveCSS('opacity', '1')
    await plus.click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main&project=[^&]+$/)
    await expect(page.locator('.chat-project-chip')).toBeVisible()
    await expect(disclosure).toHaveAttribute('aria-expanded', 'true')
    const draftRow = page.locator('[data-session-key^="draft:project:"]')
    await expect(draftRow).toHaveCount(1)
    await expect(draftRow.locator('.sidebar-history-item')).toHaveClass(/is-current/)
    await expect(draftRow.locator('.sidebar-history-title')).not.toBeEmpty()
  })

  test('project picker, trust, first send, reload, remove, reopen, and history delete', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    await openControl(page)

    await page.locator('.sidebar-new-session').click()
    await page.getByRole('button', { name: 'Choose project', exact: true }).click()
    await expect.poll(() => state.pathListRequests.length).toBe(1)
    expect(state.pathListRequests[0]).not.toHaveProperty('path')
    expect(state.pathListRequests[0]).toMatchObject({
      kind: 'workspace',
    })
    expect(state.pathListRequests[0].sessionKey).toEqual(expect.any(String))
    const picker = page.getByRole('dialog', { name: 'Choose project' })
    await picker.getByRole('option', { name: 'demo' }).click()
    await picker.getByRole('button', { name: 'Choose selected directory' }).click()
    const subscriptionsBeforeProject = state.requestMethods
      .filter(method => method === 'sessions.messages.subscribe')
      .length
    await page.getByRole('button', { name: 'Trust and open' }).click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main&project=project-demo$/)
    const projectChip = page.locator('.chat-project-chip')
    await expect(projectChip).toContainText('demo')
    await expect(projectChip).not.toContainText('/repos/demo')
    await expect(projectChip).toHaveAttribute('data-status', 'ready')
    await expect.poll(
      () => state.requestMethods.filter(method => method === 'sessions.messages.subscribe').length,
    ).toBeGreaterThan(subscriptionsBeforeProject)
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
    await expect(page.locator('.chat-project-chip')).toHaveCount(0)

    await page.reload()
    await expect(page.locator('.chat-project-chip')).toHaveCount(0)
    const projectRow = page.locator('.sidebar-history-row--workspace').first()
    await projectRow.getByTestId('project-workspace-more').click()
    await page.getByRole('menuitem', { name: 'Remove' }).click()
    await page.getByRole('button', { name: 'Remove project' }).click()
    await expect.poll(() => state.removed).toBe(true)
    await expect(page.locator('.chat-project-chip')).toContainText('demo')
    const blockedSend = page.getByRole('button', { name: 'Send' })
    await page.getByRole('textbox', { name: 'Message to send' }).fill('must stay')
    await expect(blockedSend).toBeDisabled()
    expect(state.sends).toHaveLength(1)

    await page.locator('.sidebar-new-session').click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main$/)
    await page.getByRole('button', { name: 'Choose project', exact: true }).click()
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
      .getByRole('menuitem', { name: 'Delete history' })
      .click()
    await page.getByRole('button', { name: 'Delete history' }).click()
    await expect.poll(() => state.historyDeleted).toBe(true)
    await expect.poll(() => state.postDeleteWorkspaceLists).toBeGreaterThan(0)
    await expect.poll(() => state.postDeleteSessionLists).toBeGreaterThan(0)
    expect(state.historyDeleteRequests).toEqual([{ workspaceId: 'project-demo' }])
    await expect(page.locator(`[data-session-key="${state.sessionKey}"]`)).toHaveCount(0)
    await expect(page.locator('.sidebar-workspace-empty')).toHaveCount(0)
    await expect(page.locator('.sidebar-zone-empty__body')).toHaveText('No tasks yet.')
  })
})
