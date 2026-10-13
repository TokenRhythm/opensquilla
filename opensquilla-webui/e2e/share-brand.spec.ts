import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { test, expect, type Page } from '@playwright/test'
import { installSidebarFixture, SIDEBAR_ACTIVE_SESSION_KEY } from './support/sidebar-fixture'

const brandAssets = ['opensquilla-mark.png', 'QRcode.png'] as const
type BrandDraw = { name: string; x: number; y: number; width: number; height: number; pixels: number[] }

declare global {
  interface Window {
    __shareBrandDraws: BrandDraw[]
    __shareBrandClipboardWrites: number
  }
}

async function openBrandFixture(page: Page) {
  await installSidebarFixture(page)
  // Exact production URL + actual Gateway assets. Do not fulfill arbitrary
  // image URLs: a broken base path must remain broken and fail pixel checks.
  for (const name of brandAssets) {
    await page.route(`**/control/static/img/${name}`, route => route.fulfill({
      contentType: 'image/png',
      body: readFileSync(new URL(`../../src/opensquilla/gateway/static/img/${name}`, import.meta.url)),
    }))
  }
  await page.addInitScript(() => {
    window.__shareBrandDraws = []
    window.__shareBrandClipboardWrites = 0
    const drawImage = CanvasRenderingContext2D.prototype.drawImage
    CanvasRenderingContext2D.prototype.drawImage = function (...args: unknown[]) {
      Reflect.apply(drawImage, this, args)
      const image = args[0]
      if (!(image instanceof HTMLImageElement) || args.length !== 5) return
      const name = new URL(image.src).pathname.split('/').pop()!
      if (!['opensquilla-mark.png', 'QRcode.png'].includes(name)) return
      const transform = this.getTransform()
      const x = Math.round(Number(args[1]) * transform.a + transform.e)
      const y = Math.round(Number(args[2]) * transform.d + transform.f)
      const width = Math.round(Number(args[3]) * transform.a)
      const height = Math.round(Number(args[4]) * transform.d)
      window.__shareBrandDraws.push({ name, x, y, width, height,
        pixels: Array.from(this.getImageData(x, y, width, height).data) })
    } as typeof drawImage
    // Observe a successful native write before reading the clipboard. This
    // never inspects pre-existing clipboard content and does not fake success.
    const write = navigator.clipboard.write.bind(navigator.clipboard)
    navigator.clipboard.write = async items => {
      await write(items)
      window.__shareBrandClipboardWrites += 1
    }
  })
  await page.goto(`/control/chat?session=${encodeURIComponent(SIDEBAR_ACTIVE_SESSION_KEY)}`)
  await expect(page.locator('.msg-ai-main')).toBeVisible()
  await page.getByTestId('chat-session-actions-trigger').click()
  await page.getByRole('menuitem', { name: 'Share', exact: true }).click()
  await page.locator('.chat-share-picker').first().click()
  await page.locator('.chat-share-picker').last().click()
  await page.getByTestId('share-banner').getByRole('button', { name: 'Save PNG' }).click()
  await expect(page.getByRole('dialog', { name: 'Share preview' })).toBeVisible()
}

test.describe('Share brand pixels with real Gateway images', () => {
  // Native clipboard is shared; keep both theme cases on a single worker.
  test.describe.configure({ mode: 'serial' })

  for (const theme of ['light', 'dark']) {
    test(`${theme} export retains Logo and QR in preview, download and native clipboard`, async ({ page, context }, testInfo) => {
      await context.grantPermissions(['clipboard-read', 'clipboard-write'])
      await openBrandFixture(page)
      const dialog = page.getByRole('dialog', { name: 'Share preview' })
      if (theme === 'dark') await dialog.getByRole('button', { name: 'Dark', exact: true }).click()
      const image = dialog.locator('[aria-busy="false"] img')
      await expect(image).toBeVisible()
      await expect.poll(() => image.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)

      const preview = await image.evaluate(async (image: HTMLImageElement) => {
        const digest = async (data: Uint8Array | Uint8ClampedArray) => Array.from(
          new Uint8Array(await crypto.subtle.digest('SHA-256', new Uint8Array(data))),
        ).map(byte => byte.toString(16).padStart(2, '0')).join('')
        const blob = await (await fetch(image.src)).blob()
        const canvas = document.createElement('canvas')
        canvas.width = image.naturalWidth
        canvas.height = image.naturalHeight
        const ctx = canvas.getContext('2d')!
        ctx.drawImage(image, 0, 0)
        const draws = ['opensquilla-mark.png', 'QRcode.png'].map(name =>
          window.__shareBrandDraws.filter(draw => draw.name === name).at(-1),
        )
        return {
          width: canvas.width, height: canvas.height,
          sha256: await digest(new Uint8Array(await blob.arrayBuffer())),
          pixelHash: await digest(ctx.getImageData(0, 0, canvas.width, canvas.height).data),
          draws: await Promise.all(draws.map(async draw => {
            if (!draw) return null
            const pixels = ctx.getImageData(draw.x, draw.y, draw.width, draw.height).data
            let orange = 0, black = 0, white = 0
            for (let i = 0; i < pixels.length; i += 4) {
              if (pixels[i]! > 150 && pixels[i + 1]! > 35 && pixels[i + 1]! < 190 && pixels[i + 2]! < 110) orange++
              if (pixels[i]! < 70 && pixels[i + 1]! < 70 && pixels[i + 2]! < 70) black++
              if (pixels[i]! > 220 && pixels[i + 1]! > 220 && pixels[i + 2]! > 220) white++
            }
            return { name: draw.name, y: draw.y, height: draw.height, orange, black, white,
              sourceDrawHash: await digest(new Uint8Array(draw.pixels)), finalHash: await digest(pixels) }
          })),
        }
      })
      expect(preview.draws.every(Boolean), JSON.stringify(preview.draws)).toBe(true)
      const [mark, qr] = preview.draws
      expect(mark!.sourceDrawHash).toBe(mark!.finalHash)
      expect(qr!.sourceDrawHash).toBe(qr!.finalHash)
      expect(mark!.orange).toBeGreaterThan(30)
      expect(mark!.y + mark!.height).toBeLessThan(preview.height / 2)
      expect(qr!.black).toBeGreaterThan(100)
      expect(qr!.white).toBeGreaterThan(100)
      expect(qr!.y).toBeGreaterThan(preview.height / 2)
      await page.screenshot({ path: testInfo.outputPath(`share-brand-${theme}-preview.png`) })

      await dialog.getByRole('button', { name: 'Copy image', exact: true }).click()
      await expect.poll(() => page.evaluate(() => window.__shareBrandClipboardWrites)).toBe(1)
      const pasted = await page.evaluate(async () => {
        const item = (await navigator.clipboard.read()).find(item => item.types.includes('image/png'))!
        const image = await createImageBitmap(await item.getType('image/png'))
        const canvas = document.createElement('canvas')
        canvas.width = image.width
        canvas.height = image.height
        const ctx = canvas.getContext('2d')!
        ctx.drawImage(image, 0, 0)
        const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data
        const pixelHash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', pixels)))
          .map(byte => byte.toString(16).padStart(2, '0')).join('')
        image.close()
        return { width: canvas.width, height: canvas.height, pixelHash }
      })
      expect(pasted).toEqual({ width: preview.width, height: preview.height, pixelHash: preview.pixelHash })

      const downloaded = page.waitForEvent('download')
      await dialog.getByRole('button', { name: 'Download image', exact: true }).click()
      const download = await downloaded
      await download.saveAs(testInfo.outputPath(`share-brand-${theme}-export.png`))
      const png = readFileSync((await download.path())!)
      expect(createHash('sha256').update(png).digest('hex')).toBe(preview.sha256)
      const proofPath = testInfo.outputPath(`share-brand-${theme}-pixels.json`)
      writeFileSync(proofPath, JSON.stringify({ preview, pasted }, null, 2))
      await testInfo.attach(`share-brand-${theme}-pixels`, { path: proofPath, contentType: 'application/json' })
    })
  }
})
