import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2eshare'
const SYSTEM_ONLY_SESSION_KEY = 'agent:main:webchat:e2esharesysonly'
const SEEDED_MODEL = 'deepseek-v4-flash-20260423'
const SEEDED_COST = '$0.000646'

type SeededMessage = {
  role: 'user' | 'assistant' | 'system'
  text: string
  id: string
  timestamp: number
  model?: string
  usage?: {
    model: string
    input_tokens: number
    output_tokens: number
    cost_usd: number
  }
}

interface ShareStageProbe {
  metrics: {
    contentWidth: number
    exportScale: number
    bottomSafeArea: number
    userBubbleRightOffset: number
  }
  stageWidth: number
  clones: number
  roles: string[]
  costEls: number
  modelEls: number
  finalVisibleContentRole: string | null
  finalVisibleContentSelector: string | null
  finalVisibleContentBottomGap: number | null
  finalVisibleContentRightGap: number | null
  finalCloneRightGap: number | null
  interMessageGap: number | null
  text: string
}

interface SeedHistoryOptions {
  includeAssistantMeta?: boolean
  trailingUser?: boolean
}

// Seed a settled turn through the real WS pipeline: the page talks to the
// real gateway, but chat.history responses are rewritten in flight so a
// user + assistant exchange renders without a live agent run. With
// withMessages=false the thread holds a single system message: the header
// renders but no bubble is shareable.
async function seedHistory(page: Page, withMessages: boolean, options: SeedHistoryOptions = {}) {
  await page.routeWebSocket(/\/ws$/, ws => {
    const server = ws.connectToServer()
    const historyIds = new Set<string>()
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'req' && frame.method === 'chat.history') {
          historyIds.add(String(frame.id))
        }
      } catch {}
      server.send(message)
    })
    server.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'res' && frame.id !== undefined && historyIds.has(String(frame.id))) {
          historyIds.delete(String(frame.id))
          frame.ok = true
          delete frame.error
          const now = Math.floor(Date.now() / 1000)
          const messages: SeededMessage[] = withMessages
            ? [
              {
                role: 'user',
                text: 'Summarize the launch checklist.',
                id: 'msg-share-user',
                timestamp: now - 180,
              },
              {
                role: 'assistant',
                text: 'The checklist has three open items.',
                id: 'msg-share-assistant',
                timestamp: now - 120,
                ...(options.includeAssistantMeta
                  ? {
                      model: SEEDED_MODEL,
                      usage: {
                        model: SEEDED_MODEL,
                        input_tokens: 321,
                        output_tokens: 45,
                        cost_usd: 0.000646,
                      },
                    }
                  : {}),
              },
            ]
            : [
              {
                role: 'system',
                text: 'Session restored.',
                id: 'msg-share-system',
                timestamp: now - 60,
              },
            ]
          if (withMessages && options.trailingUser) {
            messages.push({
              role: 'user',
              text: 'Ship it when the checklist is green.',
              id: 'msg-share-user-trailing',
              timestamp: now - 60,
            })
          }
          frame.payload = {
            messages,
            has_more: false,
          }
          ws.send(JSON.stringify(frame))
          return
        }
      } catch {}
      ws.send(message)
    })
  })
}

async function openSeededSession(
  page: Page,
  key: string,
  withMessages: boolean,
  options: SeedHistoryOptions = {},
) {
  await seedHistory(page, withMessages, options)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(key))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.waitForSelector('.chat-header', { timeout: 10000 })
}

async function installShareStageProbe(page: Page) {
  await page.addInitScript(() => {
    const w = window as unknown as { __shareStageProbe?: ShareStageProbe }
    w.__shareStageProbe = undefined
    const cloneRole = (clone: HTMLElement | null) => {
      if (!clone) return null
      if (clone.classList.contains('msg-user')) return 'user'
      if (clone.classList.contains('msg-ai')) return 'assistant'
      return 'unknown'
    }
    const finalContent = (clone: HTMLElement | null) => {
      if (!clone) return { element: null, selector: null }
      if (clone.classList.contains('msg-user')) {
        return {
          element: clone.querySelector<HTMLElement>('.msg-user-bubble') || clone,
          selector: clone.querySelector('.msg-user-bubble') ? '.msg-user-bubble' : '.msg-user',
        }
      }
      if (clone.classList.contains('msg-ai')) {
        const meta = clone.querySelector<HTMLElement>('.msg-ai-meta')
        if (meta) return { element: meta, selector: '.msg-ai-meta' }
        const ending = clone.querySelector<HTMLElement>('.msg-ai-ending')
        if (ending) return { element: ending, selector: '.msg-ai-ending' }
        return {
          element: clone.querySelector<HTMLElement>('.msg-ai-text') || clone,
          selector: clone.querySelector('.msg-ai-text') ? '.msg-ai-text' : '.msg-ai',
        }
      }
      return { element: clone, selector: null }
    }
    new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        mutation.addedNodes.forEach((node) => {
          if (!(node instanceof HTMLElement) || node.id !== 'opensquilla-share-export-stage') return
          requestAnimationFrame(() => {
            const clones = Array.from(node.querySelectorAll<HTMLElement>('[data-share-message-id]'))
            const lastClone = clones[clones.length - 1] || null
            const firstBox = clones[0]?.getBoundingClientRect()
            const secondBox = clones[1]?.getBoundingClientRect()
            const stageBox = node.getBoundingClientRect()
            const lastBox = lastClone?.getBoundingClientRect()
            const final = finalContent(lastClone)
            const finalBox = final.element?.getBoundingClientRect()
            w.__shareStageProbe = {
              metrics: JSON.parse(node.dataset.shareTemplateMetrics || '{}'),
              stageWidth: stageBox.width,
              clones: clones.length,
              roles: clones.map(cloneRole).filter(Boolean) as string[],
              costEls: node.querySelectorAll('.msg-meta__cost').length,
              modelEls: node.querySelectorAll('.msg-meta__model').length,
              finalVisibleContentRole: cloneRole(lastClone),
              finalVisibleContentSelector: final.selector,
              finalVisibleContentBottomGap: finalBox ? stageBox.bottom - finalBox.bottom : null,
              finalVisibleContentRightGap: finalBox ? stageBox.right - finalBox.right : null,
              finalCloneRightGap: lastBox ? stageBox.right - lastBox.right : null,
              interMessageGap: firstBox && secondBox ? secondBox.top - firstBox.bottom : null,
              text: node.innerText,
            }
          })
        })
      }
    }).observe(document, { childList: true, subtree: true })
  })
}

async function enterShareMode(page: Page) {
  await expect(page.locator('.msg-ai-main').last()).toBeVisible({ timeout: 10000 })
  await page.getByRole('button', { name: 'Share' }).click()
  await expect(page.getByTestId('share-banner')).toBeVisible()
}

function expectBottomSafeArea(probe: ShareStageProbe) {
  expect(probe.metrics.bottomSafeArea).toBeGreaterThan(0)
  expect(probe.finalVisibleContentBottomGap).not.toBeNull()
  expect(probe.finalVisibleContentBottomGap ?? 0)
    .toBeGreaterThanOrEqual(probe.metrics.bottomSafeArea)
}

function expectNoExtraInterMessageGap(probe: ShareStageProbe) {
  expect(probe.interMessageGap).not.toBeNull()
  expect(probe.interMessageGap ?? 0).toBeLessThanOrEqual(1)
}

function expectUserBubbleRightOffset(probe: ShareStageProbe) {
  expect(probe.metrics.userBubbleRightOffset).toBeGreaterThan(0)
  expect(probe.finalVisibleContentRightGap).not.toBeNull()
  expect(probe.finalCloneRightGap).not.toBeNull()
  const offset = (probe.finalVisibleContentRightGap ?? 0) - (probe.finalCloneRightGap ?? 0)
  expect(offset).toBeGreaterThanOrEqual(probe.metrics.userBubbleRightOffset - 0.5)
}

test.describe('Share mode interaction shell', () => {
  test('entering share mode opens the banner; the header keeps no share controls', async ({ page }) => {
    await openSeededSession(page, SESSION_KEY, true)
    await enterShareMode(page)

    const banner = page.getByTestId('share-banner')
    await expect(banner).toContainText('Select bubbles to share')
    await expect(banner).toContainText('0 selected')
    await expect(banner.locator('[role="status"]')).toHaveAttribute('aria-live', 'polite')

    // The banner owns the mode: save/cancel live there, not in the header.
    await expect(banner.getByRole('button', { name: 'Save PNG' })).toBeVisible()
    await expect(banner.getByRole('button', { name: 'Cancel' })).toBeVisible()
    const header = page.locator('.chat-header')
    await expect(header.getByRole('button', { name: 'Save PNG' })).toHaveCount(0)
    await expect(header.getByRole('button', { name: 'Cancel' })).toHaveCount(0)
    await expect(header.getByRole('button', { name: 'Share' })).toHaveCount(0)

    // The banner sits below the header band, clear of the floating topbar.
    const headerBox = await header.boundingBox()
    const bannerBox = await banner.boundingBox()
    expect(bannerBox!.y).toBeGreaterThanOrEqual(headerBox!.y + headerBox!.height - 1)

    // Entering share mode moves focus to the banner.
    const bannerFocused = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="share-banner"]')
      return !!el && !!document.activeElement && el.contains(document.activeElement)
    })
    expect(bannerFocused).toBe(true)
  })

  test('checkbox indicators are always visible and selection drives the count', async ({ page }) => {
    await openSeededSession(page, SESSION_KEY, true)
    await enterShareMode(page)

    const pickers = page.locator('.chat-share-picker')
    await expect(pickers).toHaveCount(2)
    await expect(pickers.first()).toBeVisible()
    await expect(pickers.last()).toBeVisible()
    await expect(pickers.first()).toHaveAttribute('aria-pressed', 'false')

    // Clicking anywhere on a bubble toggles it; selecting two updates the count.
    await page.locator('.msg-user-bubble').first().click()
    await expect(page.getByTestId('share-banner')).toContainText('1 selected')
    await pickers.last().click()
    await expect(page.getByTestId('share-banner')).toContainText('2 selected')

    await expect(page.locator('.msg-user--share-selected')).toHaveCount(1)
    await expect(page.locator('.msg-ai--share-selected')).toHaveCount(1)
    await expect(pickers.first()).toHaveAttribute('aria-pressed', 'true')
    await expect(pickers.last()).toHaveAttribute('aria-pressed', 'true')

    // Keyboard path: the indicator is a real button, Enter toggles it off.
    await pickers.first().focus()
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('share-banner')).toContainText('1 selected')
    await expect(pickers.first()).toHaveAttribute('aria-pressed', 'false')
  })

  test('share mode hides message action controls while keeping bubble selection active', async ({ page }) => {
    await openSeededSession(page, SESSION_KEY, true)

    await expect(page.locator('.msg-user-actions')).toHaveCount(1)
    await expect(page.locator('.msg-ai-actions')).toHaveCount(1)
    await enterShareMode(page)

    await expect(page.locator('.msg-user-actions')).toHaveCount(0)
    await expect(page.locator('.msg-ai-actions')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Edit' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Regenerate' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Fork conversation' })).toHaveCount(0)

    await page.locator('.msg-user-bubble').click()
    await expect(page.getByTestId('share-banner')).toContainText('1 selected')
    await expect(page.locator('.msg-user--share-selected')).toHaveCount(1)
  })

  test('user-only share export keeps bottom breathing room', async ({ page }) => {
    await installShareStageProbe(page)
    await openSeededSession(page, SESSION_KEY, true)
    await enterShareMode(page)

    await page.locator('.msg-user-bubble').click()
    await expect(page.getByTestId('share-banner')).toContainText('1 selected')
    await page.getByTestId('share-banner').getByRole('button', { name: 'Save PNG' }).click()
    await expect(page.getByRole('dialog', { name: 'Share preview' })).toBeVisible({ timeout: 15000 })

    const probe = await page.evaluate(
      () => (window as unknown as { __shareStageProbe?: ShareStageProbe }).__shareStageProbe,
    )
    expect(probe, 'export stage was never observed').toBeTruthy()
    expect(probe!.clones).toBe(1)
    expect(probe!.roles).toEqual(['user'])
    expect(probe!.finalVisibleContentRole).toBe('user')
    expect(probe!.finalVisibleContentSelector).toBe('.msg-user-bubble')
    expect(probe!.text).toContain('Summarize the launch checklist.')
    expectBottomSafeArea(probe!)
    expectUserBubbleRightOffset(probe!)
  })

  test('mixed share export keeps the shared bottom safe area when the final message is user', async ({ page }) => {
    await installShareStageProbe(page)
    await openSeededSession(page, `${SESSION_KEY}-trailing-user`, true, {
      includeAssistantMeta: true,
      trailingUser: true,
    })
    await enterShareMode(page)

    await page.locator('.chat-share-picker').nth(1).click()
    await page.locator('.msg-user-bubble').last().click()
    await expect(page.getByTestId('share-banner')).toContainText('2 selected')
    await page.getByTestId('share-banner').getByRole('button', { name: 'Save PNG' }).click()
    await expect(page.getByRole('dialog', { name: 'Share preview' })).toBeVisible({ timeout: 15000 })

    const probe = await page.evaluate(
      () => (window as unknown as { __shareStageProbe?: ShareStageProbe }).__shareStageProbe,
    )
    expect(probe, 'export stage was never observed').toBeTruthy()
    expect(probe!.clones).toBe(2)
    expect(probe!.roles).toEqual(['assistant', 'user'])
    expect(probe!.finalVisibleContentRole).toBe('user')
    expect(probe!.finalVisibleContentSelector).toBe('.msg-user-bubble')
    expect(probe!.text).toContain('The checklist has three open items.')
    expect(probe!.text).toContain('Ship it when the checklist is green.')
    expectBottomSafeArea(probe!)
    expectUserBubbleRightOffset(probe!)
    expectNoExtraInterMessageGap(probe!)
  })

  test('save is visibly disabled at zero selected and Escape exits the mode', async ({ page }) => {
    await openSeededSession(page, SESSION_KEY, true)
    await enterShareMode(page)

    const save = page.getByTestId('share-banner').getByRole('button', { name: 'Save PNG' })
    await expect(save).toBeDisabled()
    const saveOpacity = await save.evaluate(el => parseFloat(getComputedStyle(el).opacity))
    expect(saveOpacity).toBeGreaterThanOrEqual(0.6)

    await page.keyboard.press('Escape')
    await expect(page.getByTestId('share-banner')).toHaveCount(0)
    await expect(page.locator('.chat-share-picker')).toHaveCount(0)
    await expect(page.locator('.chat-header').getByRole('button', { name: 'Share' })).toBeVisible()
  })

  test('banner Cancel ends the mode', async ({ page }) => {
    await openSeededSession(page, SESSION_KEY, true)
    await enterShareMode(page)

    await page.getByTestId('share-banner').getByRole('button', { name: 'Cancel' }).click()
    await expect(page.getByTestId('share-banner')).toHaveCount(0)
    await expect(page.locator('.chat-header').getByRole('button', { name: 'Share' })).toBeVisible()
  })

  test('at 700px the entry button collapses to an icon that stays visible', async ({ page }) => {
    await page.setViewportSize({ width: 700, height: 900 })
    await openSeededSession(page, SESSION_KEY, true)
    await expect(page.locator('.msg-ai-main').last()).toBeVisible({ timeout: 10000 })

    const entry = page.getByTestId('chat-header-primary-action')
    await expect(entry).toBeVisible()
    await expect(entry).toHaveAttribute('data-action', 'share')
    await expect(entry).toHaveAccessibleName('Share')

    const iconBox = await entry.locator('svg').boundingBox()
    expect(iconBox).not.toBeNull()
    expect(iconBox!.width).toBeGreaterThan(0)
    expect(iconBox!.height).toBeGreaterThan(0)

    // Icon-only buttons keep a mobile-adequate tap target.
    const entryBox = await entry.boundingBox()
    expect(entryBox!.width).toBeGreaterThanOrEqual(43)
    expect(entryBox!.height).toBeGreaterThanOrEqual(43)
  })

  test('at 375px the responsive session action opens share mode without occlusion', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await openSeededSession(page, SESSION_KEY, true)
    await expect(page.locator('.msg-ai-main').last()).toBeVisible({ timeout: 10000 })

    const header = page.locator('.chat-header')
    const layout = await header.getAttribute('data-layout')
    expect(layout).toMatch(/^(compact|tight)$/)

    let entry = page.getByTestId('chat-header-primary-action')
    if (layout === 'tight') {
      await page.getByTestId('chat-session-actions-trigger').click()
      entry = page.getByTestId('chat-session-action-share')
    }
    await expect(entry).toBeVisible()
    await expect(entry).toHaveAccessibleName('Share')

    // A tap at the action center must land on that action, not another piece
    // of app chrome. This holds whether Share is primary or menu-contained.
    const hitIsEntry = await entry.evaluate((btn) => {
      const r = btn.getBoundingClientRect()
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
      return hit === btn || btn.contains(hit)
    })
    expect(hitIsEntry).toBe(true)

    await entry.click()
    await expect(page.getByTestId('share-banner')).toBeVisible()
  })

  test('a thread without shareable bubbles keeps the entry visible-disabled with an explanation', async ({ page }) => {
    await openSeededSession(page, SYSTEM_ONLY_SESSION_KEY, false)
    await expect(page.locator('.msg-system')).toBeVisible({ timeout: 10000 })

    const entry = page.locator('.chat-header').getByRole('button', { name: 'Send a message first to share' })
    await expect(entry).toBeVisible()
    await expect(entry).toBeDisabled()
    await expect(entry).toHaveAttribute('title', 'Send a message first to share')
    const opacity = await entry.evaluate(el => parseFloat(getComputedStyle(el).opacity))
    expect(opacity).toBeGreaterThanOrEqual(0.6)
  })

  test('Save opens the preview modal; Escape closes only the modal and keeps share mode', async ({ page }) => {
    await installShareStageProbe(page)
    await openSeededSession(page, SESSION_KEY, true, { includeAssistantMeta: true })
    await enterShareMode(page)
    await expect(page.locator('.chat-thread .msg-meta__model')).toContainText(SEEDED_MODEL)
    await expect(page.locator('.chat-thread .msg-meta__cost')).toContainText(SEEDED_COST)

    // Select both bubbles, then Save renders the PNG and opens the preview.
    await page.locator('.msg-user-bubble').first().click()
    await page.locator('.chat-share-picker').last().click()
    await expect(page.getByTestId('share-banner')).toContainText('2 selected')
    await page.getByTestId('share-banner').getByRole('button', { name: 'Save PNG' }).click()

    const dialog = page.getByRole('dialog', { name: 'Share preview' })
    await expect(dialog).toBeVisible({ timeout: 15000 })
    await expect(dialog).toHaveAttribute('aria-modal', 'true')
    const previewImg = dialog.getByRole('img', { name: 'Share preview' })
    await expect(previewImg).toBeVisible()
    await expect(dialog.getByRole('button', { name: 'Download image' })).toBeVisible()
    await expect.poll(async () => previewImg.evaluate((img: HTMLImageElement) => img.naturalWidth))
      .toBeGreaterThan(0)

    const imageMetrics = await previewImg.evaluate((img: HTMLImageElement) => {
      const box = img.getBoundingClientRect()
      return {
        naturalWidth: img.naturalWidth,
        naturalHeight: img.naturalHeight,
        displayedWidth: box.width,
        displayedHeight: box.height,
      }
    })
    expect(imageMetrics.naturalWidth).toBeGreaterThanOrEqual(1520)
    expect(imageMetrics.naturalHeight).toBeGreaterThan(imageMetrics.displayedHeight)
    expect(imageMetrics.displayedWidth).toBeLessThanOrEqual(imageMetrics.naturalWidth)

    const probe = await page.evaluate(
      () => (window as unknown as { __shareStageProbe?: ShareStageProbe }).__shareStageProbe,
    )
    expect(probe, 'export stage was never observed').toBeTruthy()
    expect(probe!.clones).toBe(2)
    expect(probe!.metrics.exportScale).toBeGreaterThanOrEqual(2)
    expect(probe!.stageWidth).toBe(probe!.metrics.contentWidth)
    expect(probe!.roles).toEqual(['user', 'assistant'])
    expect(probe!.finalVisibleContentRole).toBe('assistant')
    expect(probe!.finalVisibleContentSelector).toBe('.msg-ai-meta')
    expectBottomSafeArea(probe!)
    expectNoExtraInterMessageGap(probe!)
    expect(probe!.costEls).toBe(0)
    expect(probe!.modelEls).toBeGreaterThan(0)
    expect(probe!.text).toContain(SEEDED_MODEL)
    expect(probe!.text).not.toContain(SEEDED_COST)
    expect(probe!.text).not.toMatch(/\$\d/)

    // Escape closes the preview but leaves share mode active (the banner stays),
    // and focus returns to the share banner — the header Share button is
    // unmounted while share mode is on, so the banner is the mode's anchor.
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(page.getByTestId('share-banner')).toBeVisible()
    const bannerFocusedAfterEscape = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="share-banner"]')
      return !!el && document.activeElement === el
    })
    expect(bannerFocusedAfterEscape).toBe(true)
  })

  test('the modal close button dismisses the preview and returns focus to the Share entry', async ({ page }) => {
    await openSeededSession(page, SESSION_KEY, true)
    await enterShareMode(page)

    await page.locator('.msg-user-bubble').first().click()
    await page.locator('.chat-share-picker').last().click()
    await page.getByTestId('share-banner').getByRole('button', { name: 'Save PNG' }).click()

    const dialog = page.getByRole('dialog', { name: 'Share preview' })
    await expect(dialog).toBeVisible({ timeout: 15000 })

    await dialog.getByRole('button', { name: 'Close' }).click()
    await expect(dialog).toHaveCount(0)
    await expect(page.getByTestId('share-banner')).toBeVisible()
    const bannerFocusedAfterClose = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="share-banner"]')
      return !!el && document.activeElement === el
    })
    expect(bannerFocusedAfterClose).toBe(true)
  })
})
