import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/skills'
const INSTALL_DELAY_MS = 120
const FAILED_INDEX = 4
const UNCHANGED_INDEX = 7

type RpcFrame = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type SkillGatewayCapture = {
  inFlight: number
  installAttempts: Record<string, number>
  installIdentifiers: string[]
  installSources: string[]
  listCalls: number
  listRefreshDelayMs: number
  maxInFlight: number
  searchParams: Array<Record<string, unknown>>
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function rejectedResponse(id: string | number | undefined, message: string) {
  return JSON.stringify({
    type: 'res',
    id,
    ok: false,
    error: { code: 'INSTALL_RESPONSE_LOST', message, retryable: true },
  })
}

function lifecycle(
  readinessState: 'ready' | 'needs_setup' = 'ready',
) {
  return {
    install_state: 'tracked',
    load_state: 'loaded',
    selection_state: 'active',
    compatibility_state: 'instruction_only',
    readiness_state: readinessState,
  }
}

function catalogPayload() {
  return {
    skills: [
      {
        name: 'meta-synthetic',
        description: 'Synthetic meta Skill used only by the browser contract test.',
        kind: 'meta',
        layer: 'bundled',
        status: 'ready',
        lifecycle: {
          ...lifecycle(),
          install_state: 'untracked',
        },
      },
      {
        name: 'bundled-synthetic',
        description: 'Synthetic bundled Skill used only by the browser contract test.',
        kind: 'skill',
        layer: 'bundled',
        status: 'ready',
        lifecycle: {
          ...lifecycle(),
          install_state: 'untracked',
        },
      },
      {
        name: 'managed-synthetic',
        description: 'Synthetic managed Skill used only by the browser contract test.',
        kind: 'skill',
        layer: 'managed',
        status: 'needs_setup',
        lifecycle: lifecycle('needs_setup'),
      },
    ],
  }
}

function installPayload(
  identifier: string,
  index: number,
  source: string,
  attempt: number,
) {
  const suffix = String(index + 1).padStart(2, '0')
  const immutableRevision = `${suffix}`.repeat(20)
  const retryableSearchFailure = identifier.includes('synthetic-failure') && attempt === 1

  if (index === FAILED_INDEX || retryableSearchFailure) {
    return {
      success: false,
      unchanged: false,
      name: `synthetic-skill-${suffix}`,
      message: 'Synthetic compatibility failure',
      installed: false,
      active: false,
      instruction_usable: false,
      lifecycle: {
        install_state: 'missing',
        load_state: 'not_discovered',
        selection_state: 'active',
        compatibility_state: 'instruction_only',
        readiness_state: 'unknown',
      },
      // Deliberately model an older Gateway. The current UI must suppress these
      // success-only fields for failed operations.
      effectiveFrom: 'next_turn',
      catalogGeneration: 41,
      diagnostics: [{
        code: 'DIALECT_FIELD_UNSUPPORTED',
        severity: 'error',
        phase: 'compatibility',
        blocking: true,
        message: 'Synthetic scoped capability is unsupported.',
        hint: 'Remove the unsupported field before retrying.',
        details: {
          upstreamText: '<em data-e2e="must-stay-text">literal upstream text</em>',
        },
      }],
      resolution: {
        source,
        canonicalIdentifier: identifier,
        publisher: 'synthetic-publisher',
        version: '1.0.0',
        immutableRevision,
        immutable: true,
      },
    }
  }

  return {
    success: true,
    unchanged: index === UNCHANGED_INDEX,
    name: `synthetic-skill-${suffix}`,
    message: index === UNCHANGED_INDEX ? 'Already current' : 'Installed',
    installed: true,
    active: true,
    instruction_usable: true,
    installId: `synthetic-install-${suffix}`,
    lifecycle: lifecycle(),
    effectiveFrom: 'next_turn',
    catalogGeneration: 42,
    diagnostics: index === UNCHANGED_INDEX
      ? [{
          code: 'ALREADY_CURRENT',
          severity: 'info',
          phase: 'store',
          blocking: false,
          message: 'The immutable artifact is already installed.',
        }]
      : [],
    resolution: {
      source,
      canonicalIdentifier: identifier,
      publisher: 'synthetic-publisher',
      version: '1.0.0',
      immutableRevision,
      immutable: true,
    },
  }
}

async function installSkillGateway(page: Page): Promise<SkillGatewayCapture> {
  const capture: SkillGatewayCapture = {
    inFlight: 0,
    installAttempts: {},
    installIdentifiers: [],
    installSources: [],
    listCalls: 0,
    listRefreshDelayMs: 0,
    maxInFlight: 0,
    searchParams: [],
  }

  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))

  await page.routeWebSocket(/\/ws$/, ws => {
    ws.onMessage(raw => {
      let frame: RpcFrame
      try {
        frame = JSON.parse(String(raw)) as RpcFrame
      } catch {
        return
      }
      if (frame.type !== 'req') return

      if (frame.method === 'connect') {
        ws.send(JSON.stringify({
          type: 'hello-ok',
          protocol: 3,
          server: { version: 'e2e', conn_id: 'skills-add-drawer-e2e' },
          features: {
            methods: [
              'skills.list',
              'skills.search',
              'skills.install',
              'exec.proposals.list',
              'exec.proposals.auto_enabled.list',
              'exec.proposals.settings.get',
            ],
            events: [],
          },
          snapshot: {},
          policy: { tick_interval_ms: 30_000 },
          auth: { principal: { isOwner: true } },
        }))
        return
      }

      if (frame.method === 'skills.list') {
        capture.listCalls += 1
        const sendCatalog = () => ws.send(response(frame.id, catalogPayload()))
        if (capture.listCalls > 1 && capture.listRefreshDelayMs > 0) {
          setTimeout(sendCatalog, capture.listRefreshDelayMs)
        } else {
          sendCatalog()
        }
        return
      }

      if (frame.method === 'skills.search') {
        capture.searchParams.push(frame.params || {})
        const query = String(frame.params?.query || '')
        const isFailureFixture = query.includes('failure')
        const isUnknownFixture = query.includes('unknown')
        ws.send(response(frame.id, {
          results: [{
            name: isFailureFixture
              ? 'synthetic-search-failure'
              : isUnknownFixture
                ? 'synthetic-search-unknown'
                : 'synthetic-search-result',
            description: 'A synthetic registry result.',
            author: 'Synthetic Publisher',
            version: '1.0.0',
            source: 'clawhub',
            trust_level: 'community',
            installReference: isFailureFixture
              ? 'synthetic-publisher/synthetic-failure@1.0.0'
              : isUnknownFixture
                ? 'synthetic-publisher/synthetic-unknown@1.0.0'
                : 'synthetic-publisher/synthetic-search-result@1.0.0',
          }],
        }))
        return
      }

      if (frame.method === 'skills.install') {
        const identifier = String(frame.params?.identifier || '')
        const source = String(frame.params?.source || '')
        const index = capture.installIdentifiers.length
        capture.installIdentifiers.push(identifier)
        capture.installSources.push(source)
        const attempt = (capture.installAttempts[identifier] || 0) + 1
        capture.installAttempts[identifier] = attempt
        capture.inFlight += 1
        capture.maxInFlight = Math.max(capture.maxInFlight, capture.inFlight)
        const delay = source === 'clawhub' ? 750 : INSTALL_DELAY_MS
        setTimeout(() => {
          capture.inFlight -= 1
          if (identifier.includes('synthetic-unknown')) {
            ws.send(rejectedResponse(frame.id, 'Synthetic install response was interrupted.'))
          } else {
            ws.send(response(frame.id, installPayload(identifier, index, source, attempt)))
          }
        }, delay)
        return
      }

      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'exec.proposals.list': { proposals: [] },
        'exec.proposals.auto_enabled.list': { skills: [] },
        'exec.proposals.settings.get': {
          settings: {
            available: false,
            enabled: false,
            on_dream_complete: false,
            auto_enable: false,
            auto_enable_max_risk: 'low',
          },
        },
        'sessions.list': { sessions: [], has_more: false },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[String(frame.method)] ?? {}))
    })

    ws.send(JSON.stringify({
      type: 'event',
      event: 'connect.challenge',
      payload: { nonce: 'skills-add-drawer-e2e' },
    }))
  })

  return capture
}

async function openSkills(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-theme', 'light')
  })
  await page.goto(CONTROL_URL)
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('skills-catalog')).toBeVisible({ timeout: 15_000 })
}

test.describe('Add Skill drawer', () => {
  test('keeps the catalog full-width until the accessible overlay is opened', async ({ page }) => {
    await installSkillGateway(page)
    await openSkills(page)

    const trigger = page.getByTestId('skills-add-trigger')
    const catalog = page.getByTestId('skills-catalog')
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')
    await expect(page.getByRole('dialog', { name: 'Add Skill' })).toHaveCount(0)
    await expect(page.getByRole('tab', { name: 'Installed', exact: true })).toHaveCount(0)
    await expect(page.getByRole('tab', { name: 'Community', exact: true })).toHaveCount(0)

    const before = await catalog.boundingBox()
    expect(before).not.toBeNull()

    await trigger.click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    await expect(dialog).toBeVisible()
    await expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await expect(dialog.getByRole('button', { name: 'Close' })).toBeFocused()

    const after = await catalog.boundingBox()
    const drawer = await dialog.boundingBox()
    expect(after).not.toBeNull()
    expect(drawer).not.toBeNull()
    expect(Math.abs(after!.x - before!.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(after!.width - before!.width)).toBeLessThanOrEqual(1)
    expect(Math.abs(drawer!.width - 460)).toBeLessThanOrEqual(1)

    await page.keyboard.press('Shift+Tab')
    await expect(dialog.locator('#skills-add-github-input')).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(dialog.getByRole('button', { name: 'Close' })).toBeFocused()

    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(trigger).toBeFocused()
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')

    await trigger.click()
    await expect(dialog).toBeVisible()
    await page.getByTestId('skills-add-scrim').click({ position: { x: 2, y: 2 } })
    await expect(dialog).toHaveCount(0)
    await expect(trigger).toBeFocused()
  })

  test('installs a ClawHub search result by its exact server reference', async ({ page }) => {
    const capture = await installSkillGateway(page)
    capture.listRefreshDelayMs = 900
    await openSkills(page)

    await page.getByTestId('skills-add-trigger').click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    await dialog.getByRole('button', { name: 'ClawHub', exact: true }).click()
    await dialog.locator('#skills-add-clawhub-query').fill('synthetic search')
    await dialog.getByRole('button', { name: 'Search', exact: true }).click()

    await expect(dialog.locator('.sk-add-result')).toHaveCount(1)
    await expect(dialog.locator('.sk-add-result')).toContainText('Synthetic Publisher')
    expect(capture.searchParams).toEqual([{
      query: 'synthetic search',
      limit: 20,
      source: 'clawhub',
    }])

    const searchResult = dialog.locator('.sk-add-result')
    const searchResultAction = searchResult.getByRole('button', { name: 'Install', exact: true })
    await searchResultAction.click()
    await expect(searchResult).toHaveAttribute('data-status', 'installing')
    await expect(searchResult.getByRole('button')).toHaveAttribute('aria-busy', 'true')
    await expect(searchResult.getByRole('button')).toContainText('Installing')
    await expect(searchResult.getByRole('button').locator('.sk-spinner')).toHaveCount(0)
    await expect(dialog.locator('.sk-add-queue-item[data-status="installing"]')).toHaveCount(1)
    await expect(dialog.locator('.sk-add-queue-item[data-status="installing"] .sk-spinner')).toBeVisible()
    await expect(dialog.locator('#skills-add-tab-clawhub .sk-add-source-status')).toHaveCount(0)
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(1)
    await expect.poll(() => capture.installIdentifiers.length).toBe(1)
    expect(capture.installIdentifiers).toEqual([
      'synthetic-publisher/synthetic-search-result@1.0.0',
    ])
    expect(capture.installSources).toEqual(['clawhub'])
    const activity = dialog.locator('.sk-add-queue[data-source="clawhub"]')
    await expect(activity).toBeVisible()

    // The activity owns motion while its source is active. When the source is
    // in the background, the source tab becomes the sole progress indicator.
    await dialog.locator('#skills-add-tab-github').click()
    await expect(dialog.locator('.sk-add-queue')).toHaveCount(0)
    await expect(dialog.locator('#skills-add-tab-clawhub .sk-add-source-status .sk-spinner'))
      .toBeVisible()
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(1)
    await dialog.locator('#skills-add-tab-clawhub').click()
    await expect(activity.locator('.sk-add-queue-item[data-status="installing"] .sk-spinner'))
      .toBeVisible()
    await expect(dialog.locator('#skills-add-tab-clawhub .sk-add-source-status')).toHaveCount(0)
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(1)

    // The install RPC has finished, but the deliberately delayed catalog read
    // is still running. It must be labelled as a refresh, not as installation.
    await expect(activity.locator('.sk-add-queue-item[data-status="installed"]'))
      .toHaveCount(1, { timeout: 5_000 })
    await expect(activity.locator('.sk-add-section-title .sk-spinner')).toBeVisible()
    await expect(activity).toContainText('Reloading')
    await expect(searchResult.getByRole('button')).not.toHaveAttribute('aria-busy', 'true')
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(1)

    await expect(searchResult.getByRole('button')).toContainText('Installed')
    await expect(activity.locator('.sk-add-activity-toggle'))
      .toHaveAttribute('aria-expanded', 'false', { timeout: 5_000 })
    await expect(activity.locator('.sk-add-queue-item')).toBeHidden()
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(0)
    await expect(activity).toContainText('1 / 1 processed')
    await expect(activity).toContainText('1 installed')
    await expect(dialog.locator('#skills-add-tab-clawhub .sk-add-source-failures')).toHaveCount(0)
  })

  test('does not claim an install failed when the RPC response is interrupted', async ({ page }) => {
    await installSkillGateway(page)
    await openSkills(page)

    await page.getByTestId('skills-add-trigger').click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    await dialog.getByRole('button', { name: 'ClawHub', exact: true }).click()
    await dialog.locator('#skills-add-clawhub-query').fill('synthetic unknown')
    await dialog.getByRole('button', { name: 'Search', exact: true }).click()
    await dialog.locator('.sk-add-result').getByRole('button', { name: 'Install', exact: true }).click()

    const unknownItem = dialog.locator('.sk-add-queue-item[data-status="unknown"]')
    await expect(unknownItem).toBeVisible({ timeout: 5_000 })
    await expect(unknownItem).toContainText('Installation result unknown')
    await expect(unknownItem).not.toContainText('Not installed')
    await expect(unknownItem.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(0)
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(0)
    const unknownResult = dialog.locator('.sk-add-result')
    await expect(unknownResult).toContainText('Installation result unknown')
    await expect(unknownResult).not.toContainText('Synthetic install response was interrupted.')
    await expect(unknownResult.getByRole('button')).toHaveText('View details')
    await dialog.locator('.sk-add-activity-toggle').click()
    await expect(unknownItem).toBeHidden()
    await unknownResult.getByRole('button', { name: 'View details', exact: true }).click()
    await expect(unknownItem).toBeVisible()
    await expect(dialog.locator('.sk-add-activity-toggle')).toHaveAttribute('aria-expanded', 'true')
  })

  test('keeps failed ClawHub activity on its source and clears it after a successful retry', async ({ page }) => {
    const capture = await installSkillGateway(page)
    await openSkills(page)

    await page.getByTestId('skills-add-trigger').click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    const clawhubTab = dialog.locator('#skills-add-tab-clawhub')
    const githubTab = dialog.locator('#skills-add-tab-github')
    await clawhubTab.click()
    await dialog.locator('#skills-add-clawhub-query').fill('synthetic failure')
    await dialog.getByRole('button', { name: 'Search', exact: true }).click()
    await dialog.locator('.sk-add-result').getByRole('button', { name: 'Install', exact: true }).click()

    const clawhubActivity = dialog.locator('.sk-add-queue[data-source="clawhub"]')
    const failedItem = clawhubActivity.locator('.sk-add-queue-item[data-status="failed"]')
    await expect(failedItem).toBeVisible({ timeout: 5_000 })
    await expect(clawhubActivity.locator('.sk-add-activity-toggle')).toHaveAttribute('aria-expanded', 'true')
    await expect(failedItem).toContainText('Not installed')
    await expect(failedItem).not.toContainText('Installed files missing')
    await expect(failedItem).not.toContainText('Available next turn')
    await expect(failedItem).not.toContainText('Catalog generation')
    await expect(clawhubTab.locator('.sk-add-source-failures')).toHaveText('1')
    const failedResult = dialog.locator('.sk-add-result')
    await expect(failedResult).toContainText('Failed')
    await expect(failedResult).not.toContainText('Synthetic compatibility failure')
    await expect(failedResult).not.toContainText('Not installed')
    await expect(failedResult.getByRole('button')).toHaveText('View details')
    await expect(dialog.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(1)

    await githubTab.click()
    await expect(dialog.locator('.sk-add-queue')).toHaveCount(0)
    await expect(dialog.locator('.sk-add-queue-item')).toHaveCount(0)
    await expect(dialog).not.toContainText('Synthetic compatibility failure')
    await expect(clawhubTab.locator('.sk-add-source-failures')).toHaveText('1')

    await clawhubTab.click()
    await expect(failedItem).toBeVisible()
    await failedItem.getByRole('button', { name: 'Retry', exact: true }).click()
    await expect(clawhubActivity.locator('.sk-add-queue-item[data-status="installing"]')).toBeVisible()
    await expect.poll(() => capture.installAttempts['synthetic-publisher/synthetic-failure@1.0.0'])
      .toBe(2)
    await expect(clawhubActivity.locator('.sk-add-activity-toggle'))
      .toHaveAttribute('aria-expanded', 'false', { timeout: 5_000 })
    await expect(clawhubActivity.locator('.sk-add-queue-item[data-status="installed"]')).toBeHidden()
    await expect(clawhubTab.locator('.sk-add-source-failures')).toHaveCount(0)

    await clawhubActivity.getByRole('button', { name: 'Clear activity', exact: true }).click()
    await expect(clawhubActivity).toHaveCount(0)
  })

  test('enforces the ten-item cap, then installs serially and preserves progress across close', async ({ page }) => {
    const capture = await installSkillGateway(page)
    await openSkills(page)
    await expect.poll(() => capture.listCalls).toBe(1)
    const initialListCalls = capture.listCalls

    const references = Array.from({ length: 10 }, (_, index) => {
      const number = String(index + 1).padStart(2, '0')
      return `https://github.com/synthetic/skill-${number}/tree/${number.repeat(20)}/skill`
    })

    const trigger = page.getByTestId('skills-add-trigger')
    await trigger.click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    const input = dialog.locator('#skills-add-github-input')
    await input.fill([...references, 'https://github.com/synthetic/skill-11'].join('\n'))
    await expect(dialog.locator('#skills-add-github-batch-hint')).toContainText(
      '11 / 10 unique references',
    )
    await expect(dialog.locator('#skills-add-github-limit-hint')).toContainText(
      'batches of 10 or fewer',
    )
    await expect(dialog.getByTestId('skills-install-github')).toBeDisabled()
    expect(capture.installIdentifiers).toEqual([])

    await input.fill([...references, references[2]].join('\n'))
    await dialog.getByTestId('skills-install-github').click()

    await expect.poll(() => capture.installIdentifiers.length).toBeGreaterThan(0)
    await expect(dialog.locator('.sk-add-queue-item')).toHaveCount(10)
    const githubTab = dialog.locator('#skills-add-tab-github')
    const clawhubTab = dialog.locator('#skills-add-tab-clawhub')
    await expect(githubTab.locator('.sk-add-source-status')).toHaveCount(0)
    await expect(dialog.locator('.sk-add-queue-item[data-status="installing"] .sk-spinner'))
      .toBeVisible()
    await expect(dialog.getByTestId('skills-install-github')).toHaveAttribute('aria-busy', 'true')
    await expect(dialog.getByTestId('skills-install-github')).toContainText('Installing')
    await expect(dialog.getByTestId('skills-install-github').locator('.sk-spinner')).toHaveCount(0)
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(1)
    await expect(dialog.locator('.sk-add-queue').getByRole('button', { name: 'Clear activity' }))
      .toBeDisabled()
    await clawhubTab.click()
    await expect(dialog.locator('.sk-add-queue')).toHaveCount(0)
    await expect(githubTab.locator('.sk-add-source-status')).toBeVisible()
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(1)
    await expect(dialog.locator('#skills-add-clawhub-query')).toBeEnabled()
    await dialog.locator('#skills-add-clawhub-query').fill('background search')
    await dialog.getByRole('button', { name: 'Search', exact: true }).click()
    await expect(dialog.locator('.sk-add-result')).toHaveCount(1)
    await expect(dialog.locator('.sk-add-result').getByRole('button', { name: 'Install', exact: true }))
      .toBeDisabled()
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(1)
    await dialog.getByRole('button', { name: 'Close' }).click()
    await expect(dialog).toHaveCount(0)

    await expect.poll(() => capture.installIdentifiers.length).toBeGreaterThanOrEqual(3)
    await trigger.click()
    await expect(dialog).toBeVisible()
    await expect(clawhubTab).toHaveAttribute('aria-pressed', 'true')
    await expect(dialog.locator('.sk-add-queue-item')).toHaveCount(0)
    await expect(githubTab.locator('.sk-add-source-status')).toBeVisible()
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(1)

    await expect.poll(() => capture.installIdentifiers.length, { timeout: 15_000 }).toBe(10)
    await expect.poll(() => capture.inFlight, { timeout: 15_000 }).toBe(0)
    await expect(githubTab.locator('.sk-add-source-status')).toHaveCount(0)
    await expect(dialog.locator('.sk-spinner:visible')).toHaveCount(0)
    await expect(githubTab.locator('.sk-add-source-failures')).toHaveText('1')
    await githubTab.click()
    await expect(dialog.locator('.sk-add-queue-item')).toHaveCount(10)
    await expect(dialog.locator('.sk-add-queue-item[data-status="queued"]')).toHaveCount(0)
    await expect(dialog.locator('.sk-add-queue-item[data-status="installing"]')).toHaveCount(0)
    await expect(dialog.locator('.sk-add-queue-item[data-status="installed"]')).toHaveCount(8)
    await expect(dialog.locator('.sk-add-queue-item[data-status="unchanged"]')).toHaveCount(1)
    await expect(dialog.locator('.sk-add-queue-item[data-status="failed"]')).toHaveCount(1)

    expect(capture.maxInFlight).toBe(1)
    expect(capture.installIdentifiers).toEqual(references)
    await expect.poll(() => capture.listCalls).toBe(initialListCalls + 1)

    const failed = dialog.locator('.sk-add-queue-item[data-status="failed"]')
    await expect(failed).toContainText('Synthetic compatibility failure')
    await expect(failed).toContainText('Not installed')
    await expect(failed).not.toContainText('Installed files missing')
    await expect(failed).not.toContainText('Available next turn')
    await expect(failed).not.toContainText('Catalog generation')
    await failed.locator('summary').click()
    await expect(failed).toContainText('DIALECT_FIELD_UNSUPPORTED')
    await expect(failed).toContainText('literal upstream text')
    await expect(failed.locator('[data-e2e="must-stay-text"]')).toHaveCount(0)

    const unchanged = dialog.locator('.sk-add-queue-item[data-status="unchanged"]')
    await expect(unchanged).toContainText('synthetic-skill-08')
    await expect(unchanged).toContainText('Available next turn')
    await unchanged.locator('summary').click()
    await expect(unchanged).toContainText('ALREADY_CURRENT')

    await expect(dialog.locator('#skills-add-github-input')).toHaveValue(references[FAILED_INDEX])
    await expect(dialog.locator('.sk-add-queue-item').last()).toHaveAttribute('data-status', 'installed')
    await expect(dialog.locator('.sk-add-queue')).toContainText('10 / 10 processed')
    await expect(dialog.locator('.sk-add-queue')).toContainText('8 installed')
    await expect(dialog.locator('.sk-add-queue')).toContainText('1 already current')
    await expect(dialog.locator('.sk-add-queue')).toContainText('1 failed')
  })

  test('uses a full-width drawer at the 390px breakpoint', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await installSkillGateway(page)
    await openSkills(page)

    await page.getByTestId('skills-add-trigger').click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    await expect(dialog).toBeVisible()
    await expect.poll(async () => Math.abs((await dialog.boundingBox())?.x ?? 390))
      .toBeLessThanOrEqual(1)
    const box = await dialog.boundingBox()
    expect(box).not.toBeNull()
    expect(Math.abs(box!.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(box!.width - 390)).toBeLessThanOrEqual(1)
    expect(Math.abs(box!.height - 844)).toBeLessThanOrEqual(1)
  })
})
