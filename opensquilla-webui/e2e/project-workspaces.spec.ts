import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

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
  subscriptions: RpcParams[]
  pendingInitialSubscription: (() => void) | null
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
  deferWorkspaceOpen: boolean
  pendingWorkspaceOpens: Array<() => void>
  additionalWorkspaces: RpcParams[]
  workspaceUpdateRequests: RpcParams[]
  removed: boolean
  sent: boolean
  historyDeleted: boolean
}

async function installProjectLifecycleRpc(
  page: Page,
  options: { connectDelayMs?: number; owner?: boolean; deferInitialSubscription?: boolean; durableDraftIdentity?: boolean } = {},
): Promise<ProjectLifecycleState> {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  if (process.env.OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI === 'preview') {
    // Match the Gateway's built entry asset projection on nested SPA routes.
    await page.route('**/control/chat/new*', async route => {
      if (!route.request().isNavigationRequest()) return route.fallback()
      const response = await route.fetch()
      const body = (await response.text()).replace(/(src|href)="\.\//g, '$1="/control/')
      await route.fulfill({ response, body })
    })
  }
  const state: ProjectLifecycleState = {
    sessionKey: 'agent:main:webchat:project-demo-task',
    requestMethods: [],
    subscriptions: [],
    pendingInitialSubscription: null,
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
    deferWorkspaceOpen: false,
    pendingWorkspaceOpens: [],
    additionalWorkspaces: [],
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

  // Every backend request is synthetic, including HTTP reads outside the RPC fixture.
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
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
      const key = String(params.key || params.sessionKey || '')
      const projectMetadata = {
        workspaceId: state.sent && key === state.sessionKey ? 'project-demo' : null,
        projectWorkspace: state.sent && key === state.sessionKey
          ? state.removed
            ? { ...workspace(), available: false, removed: true, availabilityReason: 'removed' }
            : workspace()
          : null,
      }
      switch (frame.method) {
        case 'connect':
          setTimeout(() => {
            ws.send(helloOkResponse({
              auth: { principal: { isOwner: options.owner !== false,
                ...(options.durableDraftIdentity ? { authenticated: true, authState: 'authenticated',
                  role: 'operator', scopes: ['operator.read', 'operator.write'],
                  capabilities: ['chat.read', 'chat.write'] } : {}),
              } },
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
                  'plans.setMode',
                  'plans.capabilities',
                  'goals.set',
                  'goals.capabilities',
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
        case 'workspaces.open': {
          expect(params).toMatchObject({ trusted: true })
          state.workspaceOpenRequests.push(params)
          if (state.workspaceOpenError) {
            reject(frame.id, state.workspaceOpenError)
            return
          }
          const opened = params.path === '/repos/demo' ? workspace() : {
            ...workspace(),
            id: `project-${String(params.path).split('/').at(-1)}`,
            name: String(params.path).split('/').at(-1),
            path: params.path,
          }
          const finish = () => {
            state.projectPresent = true
            state.removed = false
            if (opened.id !== 'project-demo') state.additionalWorkspaces.push(opened)
            respond(frame.id, { workspace: opened })
          }
          if (state.deferWorkspaceOpen) state.pendingWorkspaceOpens.push(finish)
          else finish()
          return
        }
        case 'workspaces.update':
          state.workspaceUpdateRequests.push(params)
          state.workspaceName = String(params.name || state.workspaceName)
          respond(frame.id, { workspace: workspace() })
          return
        case 'workspaces.list':
          state.workspaceListRequests += 1
          if (state.historyDeleted) state.postDeleteWorkspaceLists += 1
          respond(frame.id, {
            workspaces: state.projectPresent ? [workspace(), ...state.additionalWorkspaces] : [],
          })
          return
        case 'chat.send':
          state.sends.push(params)
          state.sessionKey = String(params.sessionKey)
          state.sent = true
          respond(frame.id, {
            sessionKey: state.sessionKey,
            status: 'accepted',
            task_id: 'project-demo-task',
            message_id: 'project-demo-user-message',
          })
          return
        case 'chat.history':
          respond(frame.id, chatHistoryPayload(state.sent && key === state.sessionKey
              ? [{
                  role: 'user',
                  text: 'pwd',
                  message_id: 'project-demo-user-message',
                  timestamp: '2026-07-26T00:00:00.000Z',
                }]
              : []))
          return
        case 'sessions.list':
          if (state.historyDeleted) state.postDeleteSessionLists += 1
          respond(frame.id, {
            sessions: state.sent ? [session()] : [],
            count: state.sent ? 1 : 0,
            ts: 1_800_000_000,
            has_more: false,
          })
          return
        case 'sessions.messages.subscribe':
          state.subscriptions.push(params)
          if (options.deferInitialSubscription && state.subscriptions.length === 1) {
            state.pendingInitialSubscription = () => respond(
              frame.id, sessionMessagesSubscribePayload(key, projectMetadata),
            )
            return
          }
          respond(frame.id, sessionMessagesSubscribePayload(key, projectMetadata))
          return
        case 'sessions.messages.hydrate':
          respond(frame.id, sessionMessagesHydratePayload(key, projectMetadata))
          return
        case 'sessions.messages.snapshot':
          respond(frame.id, sessionMessagesSnapshotPayload(key))
          return
        case 'workspaces.remove':
          expect(params).toEqual({ workspaceId: 'project-demo' })
          state.projectPresent = false
          state.removed = true
          respond(frame.id, {
            removed: true, workspaceId: 'project-demo', pausedCronJobIds: [], pausedCronJobCount: 0,
          })
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
            'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
            'goals.capabilities': {
              supported: true, executionEnabled: true, maxTurns: 50,
              runtimeBudgetSeconds: 3600, methods: ['goals.set'],
            },
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

async function chooseDraftDirectory(page: Page, name = 'demo') {
  await page.getByRole('button', { name: 'Choose project', exact: true }).click()
  const picker = page.getByRole('dialog', { name: 'Choose project' })
  await picker.getByRole('option', { name, exact: true }).click()
  await picker.getByRole('button', { name: 'Choose selected directory', exact: true }).click()
}

async function attachDraftFiles(page: Page) {
  await page.route('**/api/v1/files/upload', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      file_uuid: 'draft-staged-pdf', filename: 'draft.pdf',
      mime: 'application/pdf', size: 2_000_001,
    }),
  }))
  await page.locator('.chat input[type="file"]').setInputFiles([
    { name: 'draft.txt', mimeType: 'text/plain', buffer: Buffer.from('keep this attachment') },
    { name: 'draft.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(2_000_001) },
  ])
  await expect(page.locator('.attachment-chip')).toHaveCount(2)
  await expect(page.locator('.attachment-chip--busy')).toHaveCount(0)
}

async function draftKey(page: Page): Promise<string> {
  return page.evaluate(() => String(window.history.state?.draftSessionKey || ''))
}

test.describe('Project workspaces', () => {
  test('preserves drafted text and inline/staged attachments when choosing a project', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    await openControl(page)
    await page.locator('.sidebar-new-session').click()
    const message = page.getByRole('textbox', { name: 'Message to send' })
    await message.fill('Review the attached files')
    await attachDraftFiles(page)
    const key = await draftKey(page)
    expect(key).not.toBe('')
    const subscriptions = state.subscriptions.length

    await chooseDraftDirectory(page)
    await page.getByRole('button', { name: 'Trust and open', exact: true }).click()
    await expect(page.locator('.chat-project-chip')).toHaveAttribute('data-status', 'ready')
    await expect(message).toHaveValue('Review the attached files')
    await expect(page.locator('.attachment-chip')).toHaveCount(2)
    expect(await draftKey(page)).toBe(key)
    expect(state.subscriptions).toHaveLength(subscriptions)

    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => state.sends.length).toBe(1)
    expect(state.sends[0]).toMatchObject({
      message: 'Review the attached files', sessionKey: key, workspaceId: 'project-demo',
      attachments: [
        { name: 'draft.txt', mime: 'text/plain', data: expect.any(String) },
        { name: 'draft.pdf', mime: 'application/pdf', file_uuid: 'draft-staged-pdf' },
      ],
    })
  })

  test('preserves drafted text and attachments when clearing a project', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    state.projectPresent = true
    await page.goto('/control/chat/new?agent=main&project=project-demo')
    await expect(page.locator('.chat-project-chip')).toHaveAttribute('data-status', 'ready')
    const message = page.getByRole('textbox', { name: 'Message to send' })
    await message.fill('Keep my default-workspace draft')
    await attachDraftFiles(page)
    const key = await draftKey(page)
    await page.getByRole('button', { name: 'Use the default workspace', exact: true }).click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main$/)
    await expect(page.locator('.chat-project-chip')).toHaveCount(0)
    await expect(message).toHaveValue('Keep my default-workspace draft')
    await expect(page.locator('.attachment-chip')).toHaveCount(2)
    expect(await draftKey(page)).toBe(key)
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => state.sends.length).toBe(1)
    expect(state.sends[0]).not.toHaveProperty('workspaceId')
    expect(state.sends[0]).toMatchObject({ sessionKey: key, attachments: expect.any(Array) })
  })

  for (const outcome of ['picker cancel', 'trust cancel', 'open error'] as const) {
    test(`restores the route project after ${outcome} during initial subscription`, async ({ page }) => {
      const state = await installProjectLifecycleRpc(page, { deferInitialSubscription: true })
      state.projectPresent = true
      await page.goto('/control/chat/new?agent=main&project=project-demo')
      await expect.poll(() => state.pendingInitialSubscription).not.toBeNull()
      const message = page.getByRole('textbox', { name: 'Message to send' })
      await message.fill('Keep the original project and draft')
      const key = await draftKey(page)
      expect(key).not.toBe('')

      if (outcome === 'picker cancel') {
        await page.getByRole('button', { name: 'Choose project', exact: true }).click()
        await page.getByRole('dialog', { name: 'Choose project' })
          .getByRole('button', { name: 'Cancel', exact: true }).click()
      } else {
        await chooseDraftDirectory(page, 'project-01')
        if (outcome === 'trust cancel') {
          await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click()
        } else {
          state.workspaceOpenError = 'synthetic open failure'
          await page.getByRole('button', { name: 'Trust and open', exact: true }).click()
          await expect(page.getByTestId('toast')).toContainText('synthetic open failure')
        }
      }

      state.pendingInitialSubscription!()
      await expect(page.locator('.chat-project-chip')).toHaveAttribute('data-status', 'ready')
      await expect(page).toHaveURL(/\/chat\/new\?agent=main&project=project-demo$/)
      await expect(message).toHaveValue('Keep the original project and draft')
      expect(await draftKey(page)).toBe(key)
      await page.getByRole('button', { name: 'Send', exact: true }).click()
      await expect.poll(() => state.sends.length).toBe(1)
      expect(state.sends[0]).toMatchObject({
        message: 'Keep the original project and draft', sessionKey: key, workspaceId: 'project-demo',
      })
    })
  }

  test('restores the same draft text, attachment bytes and project after a page reload', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page, { durableDraftIdentity: true })
    await openControl(page)
    await page.locator('.sidebar-new-session').click()
    const message = page.getByRole('textbox', { name: 'Message to send' })
    await message.fill('Continue this draft after refresh')
    await attachDraftFiles(page)
    const key = await draftKey(page)
    await chooseDraftDirectory(page)
    await page.getByRole('button', { name: 'Trust and open', exact: true }).click()
    await expect(page.locator('.chat-project-chip')).toHaveAttribute('data-status', 'ready')
    await expect(message).toHaveValue('Continue this draft after refresh')

    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect(page.locator('.chat-project-chip')).toHaveAttribute('data-status', 'ready')
    await expect(message).toHaveValue('Continue this draft after refresh')
    await expect(page.locator('.attachment-chip')).toHaveCount(2)
    await expect(page.locator('.attachment-chip--busy')).toHaveCount(0)
    expect(await draftKey(page)).toBe(key)
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => state.sends.length).toBe(1)
    expect(state.sends[0]).toMatchObject({
      sessionKey: key, workspaceId: 'project-demo',
      attachments: expect.arrayContaining([
        expect.objectContaining({ name: 'draft.txt', data: Buffer.from('keep this attachment').toString('base64') }),
        expect.objectContaining({ name: 'draft.pdf', file_uuid: 'draft-staged-pdf' }),
      ]),
    })
  })

  for (const mode of ['plan', 'goal'] as const) {
    test(`keeps ${mode} mode when choosing a project and resets it for an explicit new task`, async ({ page }) => {
      await installProjectLifecycleRpc(page)
      await openControl(page)
      await page.locator('.sidebar-new-session').click()
      const message = page.getByRole('textbox', { name: 'Message to send' })
      await message.fill(`Keep this ${mode} draft`)
      const key = await draftKey(page)
      await page.getByRole('button', { name: 'Add', exact: true }).click()
      await page.getByRole('menuitem', { name: mode === 'plan' ? /Plan mode/ : /Goal mode/ }).click()
      const indicator = page.locator(`.composer-${mode}-mode`)
      await expect(indicator).toBeVisible()
      await chooseDraftDirectory(page)
      await page.getByRole('button', { name: 'Trust and open', exact: true }).click()
      await expect(page.locator('.chat-project-chip')).toHaveAttribute('data-status', 'ready')
      await expect(indicator).toBeVisible()
      await expect(message).toHaveValue(`Keep this ${mode} draft`)
      expect(await draftKey(page)).toBe(key)

      await page.locator('.sidebar-new-session').click()
      await expect(message).toHaveValue('')
      await expect(indicator).toHaveCount(0)
      await expect(page.locator('.chat-project-chip')).toHaveCount(0)
    })
  }

  for (const outcome of ['picker cancel', 'trust cancel', 'open error'] as const) {
    test(`preserves a draft and attachments after ${outcome}`, async ({ page }) => {
      const state = await installProjectLifecycleRpc(page)
      await openControl(page)
      await page.locator('.sidebar-new-session').click()
      const message = page.getByRole('textbox', { name: 'Message to send' })
      await message.fill('Keep this unfinished task')
      await attachDraftFiles(page)
      const key = await draftKey(page)
      if (outcome === 'picker cancel') {
        await page.getByRole('button', { name: 'Choose project', exact: true }).click()
        await page.getByRole('dialog', { name: 'Choose project' })
          .getByRole('button', { name: 'Cancel', exact: true }).click()
      } else {
        await chooseDraftDirectory(page)
        if (outcome === 'trust cancel') {
          await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click()
        } else {
          state.workspaceOpenError = 'synthetic directory unavailable'
          await page.getByRole('button', { name: 'Trust and open', exact: true }).click()
          await expect(page.getByTestId('toast')).toContainText('synthetic directory unavailable')
        }
      }
      await expect(page.getByRole('button', { name: 'Choose project', exact: true })).toBeEnabled()
      await expect(message).toHaveValue('Keep this unfinished task')
      await expect(page.locator('.attachment-chip')).toHaveCount(2)
      await expect(page.locator('.chat-project-chip')).toHaveCount(0)
      expect(await draftKey(page)).toBe(key)
      expect(state.workspaceOpenRequests).toHaveLength(outcome === 'open error' ? 1 : 0)
      expect(state.sends).toEqual([])
    })
  }

  test('does not apply a delayed project open to a different existing session', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    state.projectPresent = true
    state.sent = true
    state.deferWorkspaceOpen = true
    const existingSessionKey = state.sessionKey
    await openControl(page)
    await page.locator('.sidebar-new-session').click()
    await page.getByRole('textbox', { name: 'Message to send' }).fill('Draft A')
    await chooseDraftDirectory(page)
    await page.getByRole('button', { name: 'Trust and open', exact: true }).click()
    await expect.poll(() => state.pendingWorkspaceOpens.length).toBe(1)
    await expect(page.getByRole('button', { name: 'Choose project', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled()

    const disclosure = page.getByTestId('project-workspace-disclosure').first()
    if (await disclosure.getAttribute('aria-expanded') === 'false') await disclosure.click()
    await page.locator(`[data-session-key="${state.sessionKey}"] .sidebar-history-item`).click()
    await expect(page).toHaveURL(/\/chat\?session=/)
    const selectedUrl = page.url()
    await page.getByRole('textbox', { name: 'Message to send' }).fill('Existing session B')
    const listsBeforeLateResponse = state.workspaceListRequests
    state.pendingWorkspaceOpens[0]()
    await expect.poll(() => state.workspaceListRequests).toBeGreaterThan(listsBeforeLateResponse)
    await expect(page).toHaveURL(selectedUrl)
    await expect(page.getByRole('textbox', { name: 'Message to send' })).toHaveValue('Existing session B')
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => state.sends.length).toBe(1)
    expect(state.sends[0]).toMatchObject({ message: 'Existing session B', sessionKey: existingSessionKey })
    expect(state.sends[0]).not.toHaveProperty('workspaceId')
  })

  test('a new draft can choose another project while an old project open is pending', async ({ page }) => {
    const state = await installProjectLifecycleRpc(page)
    state.deferWorkspaceOpen = true
    await openControl(page)
    await page.locator('.sidebar-new-session').click()
    await page.getByRole('textbox', { name: 'Message to send' }).fill('Draft A owns the older choice')
    const firstKey = await draftKey(page)
    await chooseDraftDirectory(page)
    await page.getByRole('button', { name: 'Trust and open', exact: true }).click()
    await expect.poll(() => state.pendingWorkspaceOpens.length).toBe(1)
    await expect(page.getByRole('button', { name: 'Choose project', exact: true })).toBeDisabled()

    await page.locator('.sidebar-new-session').click()
    await page.getByRole('textbox', { name: 'Message to send' }).fill('Draft B owns the newer choice')
    await expect.poll(() => draftKey(page)).not.toBe(firstKey)
    const secondKey = await draftKey(page)
    await chooseDraftDirectory(page, 'project-01')
    await page.getByRole('button', { name: 'Trust and open', exact: true }).click()
    await expect.poll(() => state.pendingWorkspaceOpens.length).toBe(2)
    state.pendingWorkspaceOpens[1]()
    await expect(page.locator('.chat-project-chip')).toContainText('project-01')
    const listsBeforeLateResponse = state.workspaceListRequests
    state.pendingWorkspaceOpens[0]()
    await expect.poll(() => state.workspaceListRequests).toBeGreaterThan(listsBeforeLateResponse)
    await expect(page.locator('.chat-project-chip')).toContainText('project-01')
    await expect(page.getByRole('textbox', { name: 'Message to send' })).toHaveValue('Draft B owns the newer choice')
    expect(await draftKey(page)).toBe(secondKey)
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => state.sends.length).toBe(1)
    expect(state.sends[0]).toMatchObject({ sessionKey: secondKey, workspaceId: 'project-project-01' })
  })

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
    const subscriptionsBeforeProject = state.subscriptions.length
    const keyBeforeProject = state.pathListRequests[0].sessionKey
    await page.getByRole('button', { name: 'Trust and open' }).click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main&project=project-demo$/)
    const projectChip = page.locator('.chat-project-chip')
    await expect(projectChip).toContainText('demo')
    await expect(projectChip).not.toContainText('/repos/demo')
    await expect(projectChip).toHaveAttribute('data-status', 'ready')
    expect(state.subscriptions).toHaveLength(subscriptionsBeforeProject)
    expect(await draftKey(page)).toBe(keyBeforeProject)
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
