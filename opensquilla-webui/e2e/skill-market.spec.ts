import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'

type RpcFrame = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type MarketCapture = {
  searchParams: Array<Record<string, unknown>>
  installIdentifiers: string[]
  installSources: string[]
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function marketRow(index: number) {
  const names = ['pdf-toolkit', 'deep-research', 'cad-design']
  const downloads = [1_000, 90_000, 5_000]
  return {
    name: names[index],
    description: `${names[index]} description used by the marketplace browser test.`,
    version: '1.0.0',
    author: 'synthetic-publisher',
    source: 'skillhub',
    trust_level: 'trusted',
    identifier: names[index],
    installReference: names[index],
    installed: false,
    license: 'MIT',
    category: 'documents',
    tags: ['pdf', 'office'],
    downloads: downloads[index],
    installs: downloads[index] + 1,
    icon_url: '',
  }
}

async function marketGateway(page: Page): Promise<MarketCapture> {
  const capture: MarketCapture = { searchParams: [], installIdentifiers: [], installSources: [] }
  const installed = new Set<string>()

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
        ws.send(helloOkResponse({
          server: { version: 'e2e', conn_id: 'skill-market-e2e' },
          features: {
            methods: ['skills.list', 'skills.search', 'skills.install'],
            events: [],
          },
          snapshot: {},
          policy: { tick_interval_ms: 30_000 },
          auth: { principal: { isOwner: true } },
        }))
        return
      }

      if (frame.method === 'skills.list') {
        ws.send(response(frame.id, { skills: [] }))
        return
      }

      if (frame.method === 'skills.search') {
        capture.searchParams.push(frame.params || {})
        const query = String(frame.params?.query ?? '')
        // Browse mode must ask for the shelf; keyword mode narrows to matches.
        const rows = (query ? [marketRow(0)] : [marketRow(0), marketRow(1), marketRow(2)])
          .map(row => ({ ...row, installed: installed.has(row.identifier) }))
        ws.send(response(frame.id, { results: rows }))
        return
      }

      if (frame.method === 'skills.install') {
        const identifier = String(frame.params?.identifier || '')
        capture.installIdentifiers.push(identifier)
        capture.installSources.push(String(frame.params?.source || ''))
        installed.add(identifier)
        ws.send(response(frame.id, {
          success: true,
          installed: true,
          name: identifier,
          installId: `install:${identifier}`,
          message: 'Installed',
          effectiveFrom: 'next_turn',
          lifecycle: {
            install_state: 'tracked',
            load_state: 'loaded',
            selection_state: 'active',
            compatibility_state: 'instruction_only',
            readiness_state: 'ready',
          },
          diagnostics: [],
        }))
        return
      }

      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false }, permissions: {}, skills: {} },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[String(frame.method)] ?? {}))
    })

    ws.send(JSON.stringify({
      type: 'event',
      event: 'connect.challenge',
      payload: { nonce: 'skill-market-e2e' },
    }))
  })

  return capture
}

async function openMarket(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-theme', 'light')
  })
  await page.goto('/control/market')
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('market-grid')).toBeVisible({ timeout: 15_000 })
}

test.describe('Skill marketplace', () => {
  test('browses the shelf on open and trends the most-downloaded skill first', async ({ page }) => {
    const capture = await marketGateway(page)
    await openMarket(page)

    // Empty query is a browse request across every configured source.
    expect(capture.searchParams[0]).toEqual({ query: '', limit: 40, source: null })

    await expect(page.getByTestId('market-card-pdf-toolkit')).toBeVisible()
    await expect(page.getByTestId('market-card-deep-research')).toBeVisible()
    await expect(page.getByTestId('market-card-cad-design')).toBeVisible()

    // 90k downloads is the trending head of the shelf.
    await expect(page.getByTestId('market-featured-deep-research')).toBeVisible()
    await expect(page.getByTestId('market-featured-deep-research')).toContainText('90k')

    // A keyword search narrows the grid and drops the trending shelf.
    await page.getByTestId('market-search-input').fill('pdf')
    await page.getByTestId('market-search-button').click()
    await expect(page.getByTestId('market-card-pdf-toolkit')).toBeVisible()
    await expect(page.getByTestId('market-card-deep-research')).toHaveCount(0)
    await expect(page.getByTestId('market-featured-deep-research')).toHaveCount(0)
    expect(capture.searchParams[1]).toMatchObject({ query: 'pdf', source: null })
  })

  test('previews a card in the detail dialog and installs from it', async ({ page }) => {
    const capture = await marketGateway(page)
    await openMarket(page)

    await page.getByTestId('market-detail-pdf-toolkit').click()
    const dialog = page.getByRole('dialog', { name: 'pdf-toolkit' })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('1.0k')
    await expect(dialog.locator('.sk-mdlg__desc')).toContainText(
      'pdf-toolkit description used by the marketplace browser test.',
    )
    await expect(dialog).toContainText('documents')

    await dialog.getByTestId('market-detail-install').click()
    await expect.poll(() => capture.installIdentifiers).toEqual(['pdf-toolkit'])
    await expect.poll(() => capture.installSources).toEqual(['skillhub'])
    // The footer status owns the install result announcement.
    await expect(dialog.getByRole('status')).toContainText('Installed')

    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    // The silent catalog refresh re-answers the search with installed:true;
    // the card keeps its badge and cannot install twice.
    const card = page.getByTestId('market-card-pdf-toolkit')
    await expect(card.getByTestId('market-installed-badge')).toBeVisible({ timeout: 10_000 })
    await expect(card.getByTestId('market-install-pdf-toolkit')).toBeDisabled()
    expect(capture.installIdentifiers).toEqual(['pdf-toolkit'])
  })

  test('stars a skill, filters to favorites, and keeps them across a reload', async ({ page }) => {
    await marketGateway(page)
    await openMarket(page)

    await expect(page.getByTestId('market-favorites-filter')).toHaveCount(0)
    await page.getByTestId('market-favorite-pdf-toolkit').click()
    await expect(page.getByTestId('market-favorite-pdf-toolkit'))
      .toHaveAttribute('aria-pressed', 'true')

    const filter = page.getByTestId('market-favorites-filter')
    await expect(filter).toBeVisible()
    await filter.click()
    await expect(page.getByTestId('market-card-pdf-toolkit')).toBeVisible()
    await expect(page.getByTestId('market-card-deep-research')).toHaveCount(0)
    await expect(page.getByTestId('market-card-cad-design')).toHaveCount(0)
    // The trending shelf steps aside while browsing favorites.
    await expect(page.getByTestId('market-featured')).toHaveCount(0)

    // Favorites are an owner-local preference: a full reload must restore them
    // from localStorage without any extra Gateway traffic.
    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('market-grid')).toBeVisible({ timeout: 15_000 })
    await page.getByTestId('market-favorites-filter').click()
    await expect(page.getByTestId('market-card-pdf-toolkit')).toBeVisible()
    await expect(page.getByTestId('market-card-deep-research')).toHaveCount(0)
    await expect(page.getByTestId('market-favorite-pdf-toolkit'))
      .toHaveAttribute('aria-pressed', 'true')
  })
})
