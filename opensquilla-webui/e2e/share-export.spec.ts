import fs from 'node:fs'
import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

interface StageProbe {
  metrics: {
    width: number
    contentWidth: number
    exportScale: number
    bottomSafeArea: number
    top: number
    brandHeight: number
    brandGap: number
    footerHeight: number
    qrSize: number
    caption: string
    disclaimer: string
  }
  stageWidth: number
  stageHeight: number
  clones: number
  roles: string[]
  thinkingFolds: number
  costEls: number
  metaMore: number
  actionRows: number
  selectionChrome: number
  modelEls: number
  savedEls: number
  finalVisibleContentRole: string | null
  finalVisibleContentSelector: string | null
  finalVisibleContentBottomGap: number | null
  interMessageGap: number | null
  text: string
}

test.describe('Share export', () => {
  test('live share export saves a content-first PNG with a clean filename', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(300000)

    // Capture the offscreen export stage (the template DOM) before the
    // composable rasterizes and removes it.
    await page.addInitScript(() => {
      const w = window as unknown as { __shareStageProbe?: StageProbe }
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
              const final = finalContent(lastClone)
              const finalBox = final.element?.getBoundingClientRect()
              w.__shareStageProbe = {
                metrics: JSON.parse(node.dataset.shareTemplateMetrics || '{}'),
                stageWidth: stageBox.width,
                stageHeight: Math.max(node.scrollHeight, node.offsetHeight),
                clones: clones.length,
                roles: clones.map(cloneRole).filter(Boolean) as string[],
                thinkingFolds: node.querySelectorAll('.thinking-fold').length,
                costEls: node.querySelectorAll('.msg-meta__cost').length,
                metaMore: node.querySelectorAll('.msg-meta__more').length,
                actionRows: node.querySelectorAll('.msg-ai-actions, .msg-user-actions').length,
                selectionChrome: node.querySelectorAll([
                  '.chat-share-picker',
                  '.share-select-check',
                  '.share-select-checkbox',
                  '.chat-share-checkbox',
                  '[data-share-checkbox]',
                  '[data-share-control]',
                ].join(',')).length,
                modelEls: node.querySelectorAll('.msg-meta__model').length,
                savedEls: node.querySelectorAll('.savings-indicator').length,
                finalVisibleContentRole: cloneRole(lastClone),
                finalVisibleContentSelector: final.selector,
                finalVisibleContentBottomGap: finalBox ? stageBox.bottom - finalBox.bottom : null,
                interMessageGap: firstBox && secondBox ? secondBox.top - firstBox.bottom : null,
                text: node.innerText,
              }
            })
          })
        }
      // documentElement may not exist yet when init scripts run; the
      // Document node itself is always observable.
      }).observe(document, { childList: true, subtree: true })
    })

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    // One real turn so there are two bubbles to share. The prompt is CJK on
    // purpose: the title (and thus the filename slug) derives from the first
    // user message, and this asserts non-ASCII titles survive the view→export
    // wiring (a pre-slug mangler in the view once stripped them to "chat").
    const prompt = '用一个词回答我：好'
    await page.locator('.chat-textarea').fill(prompt)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect(page.locator('.msg-ai').first()).toBeVisible({ timeout: 120000 })
    await expect(page.locator('.work-card')).toHaveCount(0, { timeout: 120000 })

    const liveModelCount = await page.locator('.chat-thread .msg-meta__model').count()
    const liveSavedCount = await page.locator('.chat-thread .savings-indicator').count()

    // Enter share mode via the keyboard path: this spec owns the export
    // pipeline, not the header layout, so it must not gate on pointer
    // reachability of the floating topbar cluster.
    await page.getByRole('button', { name: /^Share/ }).first().focus()
    await page.keyboard.press('Enter')
    const pickers = page.getByRole('button', { name: 'Add to share image' })
    await expect(pickers.first()).toBeVisible()
    expect(await pickers.count()).toBeGreaterThanOrEqual(2)
    await pickers.first().focus()
    await page.keyboard.press('Enter')
    // The toggled picker's accessible name flips to "Remove from share
    // image", so first() resolves to the next unselected bubble.
    await pickers.first().focus()
    await page.keyboard.press('Enter')
    await expect(page.locator('.chat-share-banner__count')).toHaveText(/2 selected/)

    // Save no longer downloads blind: it renders the PNG and opens the preview
    // modal. The download fires only when the user commits via Download image.
    await page.getByRole('button', { name: /Save PNG/ }).focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: 'Share preview' })
    await expect(dialog).toBeVisible()
    const previewImg = dialog.getByRole('img', { name: 'Share preview' })
    await expect(previewImg).toBeVisible()
    await expect.poll(async () => previewImg.evaluate((img: HTMLImageElement) => img.naturalWidth))
      .toBeGreaterThan(0)

    // The export defaults to a light theme; the segmented toggle re-renders the
    // preview on demand. Exercise Dark then return to Light (the asset the rest
    // of this test reasons about) and confirm each switch produces an image.
    const themeGroup = dialog.getByRole('group', { name: 'Export theme' })
    const lightBtn = themeGroup.getByRole('button', { name: 'Light' })
    const darkBtn = themeGroup.getByRole('button', { name: 'Dark' })
    await expect(lightBtn).toHaveAttribute('aria-pressed', 'true')
    await expect(darkBtn).toHaveAttribute('aria-pressed', 'false')
    await darkBtn.click()
    await expect(darkBtn).toHaveAttribute('aria-pressed', 'true')
    await expect(dialog.locator('[aria-busy="false"] img')).toBeVisible()
    await lightBtn.click()
    await expect(lightBtn).toHaveAttribute('aria-pressed', 'true')
    await expect(dialog.locator('[aria-busy="false"] img')).toBeVisible()

    // Copy image is offered when the browser supports clipboard images
    // (Chromium does); the live runner asserts presence, not the OS clipboard.
    await expect(dialog.getByRole('button', { name: 'Copy image' })).toBeVisible()

    const downloadPromise = page.waitForEvent('download')
    await dialog.getByRole('button', { name: 'Download image' }).click()
    const download = await downloadPromise

    // Filename contract: opensquilla-{slug}-{YYYY-MM-DD}.png, no duplicated
    // adjacent segments (the old template emitted "opensquilla-chat-chat-…").
    const filename = download.suggestedFilename()
    expect(filename).toMatch(/^opensquilla-[^/\\]+-\d{4}-\d{2}-\d{2}\.png$/)
    expect(filename).not.toContain('chat-chat')
    const slugPart = filename.replace(/^opensquilla-/, '').replace(/-\d{4}-\d{2}-\d{2}\.png$/, '')
    expect(slugPart.length).toBeGreaterThan(0)
    expect(slugPart).not.toMatch(/(^|-)([^-]+)-\2(-|$)/)
    // The CJK title must survive into the slug, not collapse to the fallback.
    expect(slugPart).toMatch(/[一-鿿]/)
    expect(slugPart).not.toBe('chat')

    // Blob is a real PNG of meaningful size.
    const filePath = await download.path()
    const buffer = fs.readFileSync(filePath)
    expect(buffer.length).toBeGreaterThan(10 * 1024)
    expect(Array.from(buffer.subarray(0, 8))).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
    const pngWidth = buffer.readUInt32BE(16)
    const pngHeight = buffer.readUInt32BE(20)

    const probe = await page.evaluate(
      () => (window as unknown as { __shareStageProbe?: unknown }).__shareStageProbe,
    ) as StageProbe | undefined
    expect(probe, 'export stage was never observed').toBeTruthy()
    const { metrics } = probe!

    // Content-first template: small brand row, one-line footer, small QR,
    // no marketing taglines.
    expect(metrics.qrSize).toBeLessThanOrEqual(80)
    expect(metrics.brandHeight).toBeLessThanOrEqual(32)
    expect(metrics.caption).toContain('opensquilla.ai')
    expect(metrics.caption).not.toMatch(/Token-Efficient|Meta-Skills|AI Agent|Scan the QR/i)
    expect(metrics.disclaimer).toBe('Content generated by AI, for reference only.')
    const chrome = metrics.top + metrics.brandHeight + metrics.brandGap + metrics.footerHeight
    expect(chrome).toBeLessThanOrEqual(170)

    // PNG dimensions follow the template: fixed width, height = chrome +
    // content scaled to the content column.
    const scale = pngWidth / metrics.width
    expect(scale).toBeGreaterThanOrEqual(2)
    expect(scale).toBe(metrics.exportScale)
    expect(probe!.stageWidth).toBe(metrics.contentWidth)
    const contentDrawn = Math.ceil((probe!.stageHeight * metrics.contentWidth) / probe!.stageWidth)
    expect(Math.abs(pngHeight / scale - (chrome + contentDrawn))).toBeLessThanOrEqual(12)

    // The cloned conversation keeps content and drops interactive remnants.
    expect(probe!.clones).toBe(2)
    expect(probe!.text).toContain(prompt)
    expect(probe!.thinkingFolds).toBe(0)
    expect(probe!.metaMore).toBe(0)
    expect(probe!.actionRows).toBe(0)
    expect(probe!.selectionChrome).toBe(0)
    expect(probe!.costEls).toBe(0)
    expect(probe!.text).not.toMatch(/\$\d/)
    expect(probe!.text).not.toMatch(/Token-Efficient|Meta-Skills/i)
    expect(metrics.bottomSafeArea).toBeGreaterThan(0)
    expect(probe!.roles).toEqual(['user', 'assistant'])
    expect(probe!.finalVisibleContentRole).toBe('assistant')
    expect(probe!.finalVisibleContentSelector).toBe('.msg-ai-meta')
    expect(probe!.finalVisibleContentBottomGap).not.toBeNull()
    expect(probe!.finalVisibleContentBottomGap ?? 0).toBeGreaterThanOrEqual(metrics.bottomSafeArea)
    expect(probe!.interMessageGap).not.toBeNull()
    expect(probe!.interMessageGap ?? 0).toBeLessThanOrEqual(1)
    if (liveModelCount > 0) expect(probe!.modelEls).toBeGreaterThan(0)
    if (liveSavedCount > 0) expect(probe!.savedEls).toBeGreaterThan(0)

    // Download closes the preview and exits share mode.
    await expect(page.getByRole('dialog', { name: 'Share preview' })).toHaveCount(0)
    await expect(page.getByTestId('share-banner')).toHaveCount(0)
  })
})
