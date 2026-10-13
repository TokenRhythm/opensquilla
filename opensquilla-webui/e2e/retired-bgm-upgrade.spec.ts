import { expect, test } from '@playwright/test'
import {
  expectTopbarConsoleClean,
  openTopbarSession,
  TOPBAR_SESSION_KEY,
} from './support/topbar-fixture'

type RetiredMusicProbe = {
  storageCalls: string[]
  audioConstructions: number
  playCalls: number
  storedValue: () => string | null
}

type ProbedWindow = Window & { __retiredMusicProbe: RetiredMusicProbe }

const LEGACY_STATES = [
  {
    name: 'enabled and playing',
    value: JSON.stringify({ enabled: true, playing: true, trackId: 'synthetic-old-track', volume: 0.5 }),
  },
  { name: 'malformed JSON', value: '{invalid-json' },
  { name: 'null JSON', value: 'null' },
]

test.afterEach(({ page }) => {
  expectTopbarConsoleClean(page)
})

for (const legacy of LEGACY_STATES) {
  test(`retired music state is untouched when ${legacy.name}`, async ({ page }) => {
    const musicRequests: string[] = []
    page.on('request', request => {
      if (/\/music(?:\/|$)/.test(new URL(request.url()).pathname)) {
        musicRequests.push(request.url())
      }
    })
    await page.addInitScript((value: string) => {
      const key = 'opensquilla-bgm'
      const originalGet = Storage.prototype.getItem
      const originalSet = Storage.prototype.setItem
      const originalRemove = Storage.prototype.removeItem
      const originalClear = Storage.prototype.clear
      originalSet.call(localStorage, key, value)
      const probe: RetiredMusicProbe = {
        storageCalls: [],
        audioConstructions: 0,
        playCalls: 0,
        // Keep the assertion itself out of the application-access audit.
        storedValue: () => originalGet.call(localStorage, key),
      }
      ;(window as ProbedWindow).__retiredMusicProbe = probe
      Storage.prototype.getItem = function (name: string) {
        if (this === localStorage && name === key) probe.storageCalls.push('getItem')
        return originalGet.call(this, name)
      }
      Storage.prototype.setItem = function (name: string, nextValue: string) {
        if (this === localStorage && name === key) probe.storageCalls.push('setItem')
        return originalSet.call(this, name, nextValue)
      }
      Storage.prototype.removeItem = function (name: string) {
        if (this === localStorage && name === key) probe.storageCalls.push('removeItem')
        return originalRemove.call(this, name)
      }
      Storage.prototype.clear = function () {
        if (this === localStorage) probe.storageCalls.push('clear')
        return originalClear.call(this)
      }
      window.Audio = new Proxy(window.Audio, {
        construct(target, args) {
          probe.audioConstructions += 1
          return Reflect.construct(target, args)
        },
      })
      HTMLMediaElement.prototype.play = function () {
        probe.playCalls += 1
        return Promise.resolve()
      }
    }, legacy.value)

    await page.setViewportSize({ width: 1440, height: 1000 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-retired-music`,
      locale: 'en',
    })
    await expect(page.locator('[data-testid^="bgm-"]')).toHaveCount(0)
    await expect(page.getByRole('button', { name: /background music/i })).toHaveCount(0)

    await page.locator('.sidebar-foot button').click()
    const settings = page.getByRole('dialog', { name: 'Settings', exact: true })
    await expect(settings).toBeVisible()
    await settings.getByRole('tab', { name: 'Interface', exact: true }).click()
    await expect(settings.getByRole('radio', { name: 'Light', exact: true })).toBeChecked()
    await expect(settings.getByText(/background music/i)).toHaveCount(0)
    await settings.getByRole('radio', { name: 'Dark', exact: true }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    for (const query of ['music', 'bgm']) {
      await settings.getByRole('searchbox').fill(query)
      await expect(settings.locator('.settings-search__empty')).toBeVisible()
      await expect(settings.locator('.settings-search__result')).toHaveCount(0)
    }
    await settings.getByRole('button', { name: 'Close', exact: true }).click()
    await expect(settings).toHaveCount(0)

    await page.keyboard.press('ControlOrMeta+k')
    const palette = page.locator('.cmdp-dialog')
    await expect(palette).toBeVisible()
    for (const query of ['music', 'bgm']) {
      await palette.getByRole('combobox').fill(query)
      await expect(palette.locator('.cmdp-empty')).toBeVisible()
      await expect(palette.getByRole('option')).toHaveCount(0)
    }
    await page.keyboard.press('Escape')
    await expect(palette).toHaveCount(0)
    await expect(page.locator('.msg-ai-main').last()).toBeVisible()
    expect(await page.evaluate(() => {
      const probe = (window as ProbedWindow).__retiredMusicProbe
      return {
        storageCalls: probe.storageCalls,
        audioConstructions: probe.audioConstructions,
        playCalls: probe.playCalls,
        storedValue: probe.storedValue(),
      }
    })).toEqual({
      storageCalls: [],
      audioConstructions: 0,
      playCalls: 0,
      storedValue: legacy.value,
    })
    expect(musicRequests).toEqual([])
  })
}
