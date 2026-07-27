import { expect, test, type Locator, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2eworkbench'
const PNG_1x1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

const ARTIFACTS = [
  {
    id: 'workbench-notes',
    name: 'notes.txt',
    mime: 'text/plain',
    size: 18,
    download_url: '/api/v1/artifacts/workbench-notes',
  },
  {
    id: 'workbench-guide',
    name: 'guide.md',
    mime: 'text/markdown',
    size: 28,
    download_url: '/api/v1/artifacts/workbench-guide',
  },
  {
    id: 'workbench-demo',
    name: 'demo.html',
    mime: 'text/html',
    size: 80,
    download_url: '/api/v1/artifacts/workbench-demo',
  },
]

async function installWorkbenchGateway(
  page: Page,
  requests: Map<string, number> = new Map(),
  artifacts = ARTIFACTS,
) {
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.route('**/api/v1/artifacts/**', route => {
    const pathname = new URL(route.request().url()).pathname
    requests.set(pathname, (requests.get(pathname) || 0) + 1)
    if (pathname.endsWith('/workbench-notes')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/plain',
        body: 'Workbench notes stay mounted.',
      })
    }
    if (pathname.endsWith('/workbench-guide')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/markdown',
        body: '# Guide\n\nPersistent markdown preview.',
      })
    }
    if (pathname.endsWith('/workbench-demo')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/html',
        body: '<!doctype html><title>Demo</title><p id="preview">Offline demo</p>',
      })
    }
    if (pathname.endsWith('/workbench-report')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/pdf',
        body: Buffer.from('%PDF-1.4\\n%EOF\\n'),
      })
    }
    if (pathname.endsWith('/workbench-image-a') || pathname.endsWith('/workbench-image-b')) {
      return route.fulfill({
        status: 200,
        contentType: 'image/png',
        body: PNG_1x1,
      })
    }
    return route.fulfill({ status: 404, body: 'missing artifact' })
  })
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(message)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      if (method === 'connect') {
        ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
        return
      }
      if (method === 'chat.history') {
        ws.send(JSON.stringify({
          type: 'res',
          id: frame.id,
          ok: true,
          payload: {
            messages: [
              {
                role: 'user',
                text: 'Create previewable files.',
                id: 'workbench-user',
                timestamp: Math.floor(Date.now() / 1000) - 120,
              },
              {
                role: 'assistant',
                text: 'The files are ready.',
                id: 'workbench-assistant',
                timestamp: Math.floor(Date.now() / 1000) - 60,
                artifacts,
              },
            ],
            has_more: false,
          },
        }))
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
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.subscribe': {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'idle',
        },
        'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({
        type: 'res',
        id: frame.id,
        ok: true,
        payload: payloads[method] ?? {},
      }))
    })
  })
}

async function openWorkbenchSession(
  page: Page,
  requests: Map<string, number> = new Map(),
  artifacts = ARTIFACTS,
) {
  await page.addInitScript(() => {
    window.OPENSQUILLA_FEATURES = {
      ...(window.OPENSQUILLA_FEATURES || {}),
      artifactWorkbench: true,
    }
  })
  await installWorkbenchGateway(page, requests, artifacts)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill')).toBeVisible({ timeout: 10000 })
  await expect(page.locator([
    '.msg-artifact-chip',
    '.msg-media-card',
    '.msg-audio-card',
    '.msg-video-card',
  ].join(','))).toHaveCount(
    artifacts.length,
    { timeout: 10000 },
  )
}

async function visibleHeaderAction(page: Page, testId: string): Promise<Locator> {
  const action = page.locator(`[data-testid="${testId}"]:visible`).first()
  await expect(action).toBeVisible()
  return action
}

async function deliverablesHeaderAction(page: Page): Promise<Locator> {
  const direct = page.locator('[data-testid="chat-session-action-deliverables"]:visible').first()
  if (await direct.isVisible()) return direct

  const primary = page.locator('[data-testid="chat-header-primary-action"]:visible').first()
  if (await primary.isVisible() && await primary.getAttribute('data-action') === 'deliverables') {
    return primary
  }

  await page.getByTestId('chat-session-actions-trigger').click()
  return visibleHeaderAction(page, 'chat-session-action-deliverables')
}

test.describe('Application Workbench', () => {
  test('header opens the latest preview and uses a compact artifact switcher', async ({ page }) => {
    const requests = new Map<string, number>()
    await openWorkbenchSession(page, requests)

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench).toHaveAttribute('role', 'complementary')
    await expect(workbench.locator('.workbench-host__single-title'))
      .toContainText('demo.html')
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)
    await expect(workbench.locator('.artifact-preview__frame--html')).toBeVisible()
    expect(requests.get('/api/v1/artifacts/workbench-demo')).toBe(1)

    const switcher = workbench.getByTestId('workbench-artifact-switcher')
    await expect(switcher).toHaveCount(1)
    await expect(switcher.locator('option')).toHaveCount(3)
    await switcher.selectOption({ label: 'notes.txt' })
    await expect(workbench.getByRole('tab')).toHaveCount(2)
    await expect(workbench.locator('.artifact-preview__text'))
      .toContainText('Workbench notes stay mounted.')
    expect(requests.get('/api/v1/artifacts/workbench-notes')).toBe(1)

    await switcher.selectOption({ label: 'notes.txt' })
    await expect(workbench.getByRole('tab')).toHaveCount(2)
    expect(requests.get('/api/v1/artifacts/workbench-notes')).toBe(1)

    await switcher.selectOption({ label: 'guide.md' })
    await expect(workbench.getByRole('tab')).toHaveCount(3)
    await expect(workbench.locator('.artifact-preview__markdown')).toContainText('Guide')
    expect(requests.get('/api/v1/artifacts/workbench-guide')).toBe(1)

    await workbench.getByRole('button', { name: 'Collapse workbench' }).click()
    await expect(workbench).toBeHidden()

    await (await deliverablesHeaderAction(page)).click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__markdown')).toContainText('Guide')
    await expect(workbench.locator('.artifact-collection__item')).toHaveCount(0)
    expect(requests.get('/api/v1/artifacts/workbench-guide')).toBe(2)
  })

  test('download-only deliverables stay in the conversation instead of opening a panel', async ({ page }) => {
    const downloadOnlyArtifacts = [{
      id: 'workbench-data',
      name: 'data.json',
      mime: 'application/json',
      size: 24,
      download_url: '/api/v1/artifacts/workbench-data',
    }]
    await openWorkbenchSession(page, new Map(), downloadOnlyArtifacts)

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toHaveCount(0)
    await expect(page.getByRole('dialog', { name: 'Deliverables (1)' })).toHaveCount(0)
    await expect(page.locator('[data-testid="chat-session-action-deliverables"]:visible'))
      .toHaveCount(0)
    await expect(page.locator('.msg-artifact-chip', { hasText: 'data.json' })
      .getByRole('button', { name: 'Download data.json' })).toBeVisible()
  })

  test('one previewable document plus PPTX does not show a misleading switcher', async ({ page }) => {
    const requests = new Map<string, number>()
    const artifacts = [
      {
        id: 'workbench-slides',
        name: 'deck.pptx',
        mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        size: 48,
        download_url: '/api/v1/artifacts/workbench-slides',
      },
      ARTIFACTS[2],
    ]
    await openWorkbenchSession(page, requests, artifacts)

    const deliverables = await deliverablesHeaderAction(page)
    await expect(deliverables).toHaveAccessibleName(/Deliverables \(1\)/)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__frame--html')).toBeVisible()
    await expect(workbench.getByTestId('workbench-artifact-switcher')).toHaveCount(0)
    await expect(page.locator('.msg-artifact-chip', { hasText: 'deck.pptx' })
      .getByRole('button', { name: 'Download deck.pptx' })).toBeVisible()
    expect(requests.get('/api/v1/artifacts/workbench-slides')).toBeUndefined()
  })

  test('the compact navigator lists only Workbench-previewable documents', async ({ page }) => {
    const requests = new Map<string, number>()
    const mixedArtifacts = [
      {
        id: 'workbench-data',
        name: 'data.json',
        mime: 'application/json',
        size: 24,
        download_url: '/api/v1/artifacts/workbench-data',
      },
      {
        id: 'workbench-audio',
        name: 'sample.wav',
        mime: 'audio/wav',
        size: 44,
        download_url: '/api/v1/artifacts/workbench-audio',
      },
      {
        id: 'workbench-video',
        name: 'sample.webm',
        mime: 'video/webm',
        size: 44,
        download_url: '/api/v1/artifacts/workbench-video',
      },
      {
        id: 'workbench-image-a',
        name: 'poster.png',
        mime: 'image/png',
        size: PNG_1x1.length,
        download_url: '/api/v1/artifacts/workbench-image-a',
        thumbnail_url: '/api/v1/artifacts/workbench-image-a?variant=thumb',
      },
      {
        id: 'workbench-slides',
        name: 'deck.pptx',
        mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        size: 48,
        download_url: '/api/v1/artifacts/workbench-slides',
      },
      ARTIFACTS[0],
      ARTIFACTS[2],
    ]
    await openWorkbenchSession(page, requests, mixedArtifacts)

    const deliverables = await deliverablesHeaderAction(page)
    await expect(deliverables).toHaveAccessibleName(/Deliverables \(2\)/)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__frame--html')).toBeVisible()
    const switcher = workbench.getByTestId('workbench-artifact-switcher')
    await expect(switcher.locator('option')).toHaveCount(2)
    await expect(switcher.locator('option')).toHaveText([
      'notes.txt',
      'demo.html',
    ])

    await switcher.selectOption({ label: 'notes.txt' })
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__text'))
      .toContainText('Workbench notes stay mounted.')
    await expect(workbench.locator('[data-preview-kind="unsupported"]')).toHaveCount(0)
    await expect(page.locator('.msg-artifact-chip', { hasText: 'deck.pptx' })
      .getByRole('button', { name: 'Download deck.pptx' })).toBeVisible()
    expect(requests.get('/api/v1/artifacts/workbench-slides')).toBeUndefined()
  })

  test('opening the same artifact card reuses one tab after the Workbench collapses', async ({ page }) => {
    await openWorkbenchSession(page)
    const notes = page.locator('.msg-artifact-chip', { hasText: 'notes.txt' })
    const open = notes.getByRole('button', { name: 'Open notes.txt' })

    await open.click()
    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)

    await open.click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)
    await expect(workbench.locator('.workbench-host__tabs')).toHaveCount(0)

    await workbench.getByRole('button', { name: 'Collapse workbench' }).click()
    await expect(workbench).toBeHidden()

    await open.click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)
  })

  test('mobile HTML preview enters the dialog Tab order and bridges Escape', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })
    await openWorkbenchSession(page)

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench).toHaveAttribute('role', 'dialog')
    await expect(workbench).toHaveAttribute('aria-modal', 'true')
    await expect(workbench).toHaveCSS('width', '375px')
    const mobileFrame = workbench.locator('.artifact-preview__frame--html')
    await expect(mobileFrame).not.toHaveAttribute('aria-hidden', 'true')
    await expect(mobileFrame).toHaveAttribute('tabindex', '0')
    await expect(mobileFrame).toHaveCSS('pointer-events', 'auto')

    await expect(
      workbench.locator('.workbench-host__actions')
        .getByRole('button', { name: 'Collapse workbench' }),
    ).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(mobileFrame).toBeFocused()

    // The opaque sandbox cannot bubble key events to the parent document.
    // Its injected bridge posts a narrow Escape message instead.
    await page.keyboard.press('Escape')
    await expect(workbench).toBeHidden()
    await expect(page.getByTestId('chat-header-primary-action')).toBeFocused()
  })

  test('mobile PDF preview offers a focus-revealed exit after the browser viewer', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })
    await openWorkbenchSession(page, new Map(), [{
      id: 'workbench-report',
      name: 'report.pdf',
      mime: 'application/pdf',
      size: 16,
      download_url: '/api/v1/artifacts/workbench-report',
    }])

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    const pdfFrame = workbench.locator('.artifact-preview__frame--pdf')
    const frameExit = workbench.locator('.artifact-preview__frame-exit')
    await expect(workbench).toHaveAttribute('role', 'dialog')
    await expect(pdfFrame).toHaveAttribute('tabindex', '0')

    await expect(
      workbench.locator('.workbench-host__actions')
        .getByRole('button', { name: 'Collapse workbench' }),
    ).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(pdfFrame).toBeFocused()

    // Chromium's built-in PDF viewer owns its document and cannot receive our
    // injected HTML Escape bridge. A trailing skip-style control provides a
    // deterministic keyboard exit without replacing the viewer with PDF.js.
    await frameExit.focus()
    await expect(frameExit).toBeFocused()
    await expect(frameExit).toBeVisible()
    await expect(frameExit).toHaveCSS('position', 'static')
    const pdfBox = await pdfFrame.boundingBox()
    const exitBox = await frameExit.boundingBox()
    expect(pdfBox).not.toBeNull()
    expect(exitBox).not.toBeNull()
    expect(exitBox!.y).toBeGreaterThanOrEqual(pdfBox!.y + pdfBox!.height)
    await page.keyboard.press('Enter')
    await expect(workbench).toBeHidden()
    await expect(deliverables).toBeFocused()
  })

  test('mobile image cards keep using the transcript Lightbox outside Workbench navigation', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })
    const artifacts = [
      {
        id: 'workbench-image-a',
        name: 'first.png',
        mime: 'image/png',
        size: PNG_1x1.length,
        download_url: '/api/v1/artifacts/workbench-image-a',
        thumbnail_url: '/api/v1/artifacts/workbench-image-a?variant=thumb',
      },
      ARTIFACTS[2],
    ]
    await openWorkbenchSession(page, new Map(), artifacts)

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()
    const workbench = page.getByTestId('workbench-host')
    await expect(workbench.getByTestId('workbench-artifact-switcher')).toHaveCount(0)
    await workbench.getByRole('button', { name: 'Collapse workbench' }).click()
    await expect(workbench).toBeHidden()

    const imageTrigger = page.locator('.msg-media-card__img')
    await expect(imageTrigger.locator('img')).toBeVisible()
    await imageTrigger.click()

    const lightbox = page.locator('.deliv-preview[role="dialog"]')
    await expect(lightbox).toBeVisible()
    await expect(workbench).toBeHidden()

    await page.keyboard.press('Escape')
    await expect(lightbox).toHaveCount(0)
    await expect(imageTrigger).toBeFocused()

    await deliverables.click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__frame--html')).toBeVisible()
  })
})
