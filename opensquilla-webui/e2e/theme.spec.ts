import { test, expect, type Page } from '@playwright/test'
import { relativeLuminance } from './utils'

// End-to-end coverage for the pluggable theme engine (P0): the value-theme axis
// (light / dark / system) driven through the real appearance menu, persistence,
// registry-driven validation of the persisted choice, and the layered token
// contract (L1 per-theme values, the new ink/fill pairs, and L0 foundation
// tokens that stay invariant across themes). Runs against whatever build serves
// the SPA at the base URL — the gateway in CI, or a static server locally.

const CONTROL = '/control/'
const THEME_KEY = 'opensquilla-theme'

// Topbar menu labels as rendered by themePickerOptions({ scope: 'basic' }): the
// built-in modes use the translated chrome.themeMode.* keys (English in CI). The
// topbar now lists only these three plus a "More themes…" action; custom themes
// (shown by their manifest name) live in Settings → Appearance. Selecting by
// label stays correct as the registry grows.
const MENU_LABEL = { light: 'Light', dark: 'Dark', system: 'System' } as const

async function bootShell(page: Page) {
  // The topbar (and its appearance control) render for every route and do not
  // depend on the RPC backend, so the shell is reachable without live data.
  await page.goto(CONTROL + 'chat')
  await page.waitForSelector('.conn-pill', { timeout: 20000 })
  // initTheme() applies data-theme on mount; wait until it has.
  await expect
    .poll(() => page.evaluate(() => document.documentElement.getAttribute('data-theme')))
    .toMatch(/^(light|dark)$/)
}

function dataTheme(page: Page) {
  return page.evaluate(() => document.documentElement.getAttribute('data-theme'))
}

function bodyBackground(page: Page) {
  return page.evaluate(() => getComputedStyle(document.body).backgroundColor)
}

/** Resolve a custom property through a real element so var() chains substitute. */
function resolveColor(page: Page, expr: string) {
  return page.evaluate((value) => {
    const el = document.createElement('span')
    el.style.color = value
    document.body.appendChild(el)
    const c = getComputedStyle(el).color
    el.remove()
    return c
  }, expr)
}

function rootToken(page: Page, name: string) {
  return page.evaluate(
    (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(),
    name,
  )
}

async function pickTheme(page: Page, mode: keyof typeof MENU_LABEL) {
  // Target the appearance button by its accessible name — `.theme-menu-wrap` is
  // not unique (the language switcher reuses the wrapper class).
  await page.getByRole('button', { name: 'Theme' }).click()
  await page.waitForSelector('.theme-menu', { timeout: 5000 })
  await page
    .locator('.theme-menu .theme-menu__item')
    .filter({ hasText: MENU_LABEL[mode] })
    .first()
    .click()
  // menu closes after a pick
  await expect(page.locator('.theme-menu')).toHaveCount(0)
}

// The topbar menu now lists only the basic modes + a "More themes…" action; the
// full custom-theme list lives in Settings → Appearance. A custom theme is
// therefore selected in Settings and persisted like any other choice — so, as
// with the P2 skin coverage below, custom/world themes are exercised here by
// seeding the persisted id and booting, which drives the very same apply +
// lazy-world path the Settings radio does, without depending on the settings
// dialog rendering in the backend-free shell.
async function bootWithTheme(page: Page, themeId: string) {
  await page.addInitScript(
    (v) => localStorage.setItem('opensquilla-theme', v),
    themeId,
  )
  await page.goto(CONTROL + 'chat')
  await page.waitForSelector('.conn-pill', { timeout: 20000 })
  await expect.poll(() => dataTheme(page)).toBe(themeId)
}

test.describe('Theme engine (P0)', () => {
  test('appearance menu applies each value theme and persists across reload', async ({ page }) => {
    await bootShell(page)

    await pickTheme(page, 'light')
    expect(await dataTheme(page)).toBe('light')
    // body background-color transitions (~200ms), so poll until it settles.
    await expect.poll(async () => relativeLuminance(await bodyBackground(page))).toBeGreaterThan(0.7)
    const bgLight = await rootToken(page, '--bg')

    await pickTheme(page, 'dark')
    expect(await dataTheme(page)).toBe('dark')
    await expect.poll(async () => relativeLuminance(await bodyBackground(page))).toBeLessThan(0.3)
    const bgDark = await rootToken(page, '--bg')

    expect(bgLight).not.toBe(bgDark) // the L1 layer really re-skinned

    // persistence: the picked theme survives a fresh load (localStorage-backed)
    await bootShell(page)
    expect(await dataTheme(page)).toBe('dark')
    expect(await page.evaluate((k) => localStorage.getItem(k), THEME_KEY)).toBe('dark')
  })

  test('system mode follows the OS colour-scheme, live', async ({ page }) => {
    await page.addInitScript((k) => localStorage.removeItem(k), THEME_KEY)
    await page.emulateMedia({ colorScheme: 'dark' })
    await bootShell(page)
    expect(await dataTheme(page)).toBe('dark') // no saved choice → system → OS dark

    // flipping the OS preference updates the ground live (matchMedia listener)
    await page.emulateMedia({ colorScheme: 'light' })
    await expect.poll(() => dataTheme(page)).toBe('light')
  })

  test('a persisted value-theme id is honoured over the OS preference', async ({ page }) => {
    await page.addInitScript((k) => localStorage.setItem(k, 'light'), THEME_KEY)
    await page.emulateMedia({ colorScheme: 'dark' })
    await bootShell(page)
    // registry recognises 'light' as a value theme → it wins over OS dark
    expect(await dataTheme(page)).toBe('light')
  })

  test('an unknown persisted theme falls back to system (registry validation)', async ({ page }) => {
    await page.addInitScript((k) => localStorage.setItem(k, 'ferrari-red'), THEME_KEY)
    await page.emulateMedia({ colorScheme: 'dark' })
    await bootShell(page)
    // isValueThemeId('ferrari-red') === false → store stays 'system' → OS dark
    expect(await dataTheme(page)).toBe('dark')
  })

  test('the ink/fill token pairs resolve and alias their ink (P0 contract)', async ({ page }) => {
    await bootShell(page)
    await pickTheme(page, 'dark')

    for (const ch of ['ok', 'warn', 'danger', 'info', 'queued']) {
      const fill = await resolveColor(page, `var(--${ch}-fill)`)
      const ink = await resolveColor(page, `var(--${ch})`)
      expect(fill, `--${ch}-fill must resolve to a real colour`).toMatch(/^rgb/)
      // dark aliases ink as fill; warn-fill is hand-tuned but still a real colour.
      if (ch !== 'warn') {
        expect(fill, `--${ch}-fill should alias --${ch}`).toBe(ink)
      }
    }
  })

  test('L0 foundation tokens stay invariant across themes', async ({ page }) => {
    await bootShell(page)

    await pickTheme(page, 'dark')
    const darkRadius = await rootToken(page, '--radius-md')
    const darkFs = await rootToken(page, '--fs-md')

    await pickTheme(page, 'light')
    const lightRadius = await rootToken(page, '--radius-md')
    const lightFs = await rootToken(page, '--fs-md')

    expect(darkRadius).toBe(lightRadius)
    expect(darkFs).toBe(lightFs)
    expect(darkRadius).toBe('8px') // the ladder's control tier, unchanged by P0
  })

  test('the topbar menu lists only the basic modes plus "More themes…"', async ({ page }) => {
    await bootShell(page)
    await page.getByRole('button', { name: 'Theme' }).click()
    await page.waitForSelector('.theme-menu', { timeout: 5000 })
    const items = page.locator('.theme-menu .theme-menu__item')
    // Light / Dark / System + the "More themes…" action = 4 rows, no custom themes.
    await expect(items).toHaveCount(4)
    await expect(items.filter({ hasText: 'Arctic' })).toHaveCount(0)
    await expect(items.filter({ hasText: 'Synthwave' })).toHaveCount(0)
    await expect(items.filter({ hasText: 'More themes' })).toHaveCount(1)

    // "More themes…" deep-links to Settings → Appearance (the full theme list).
    await items.filter({ hasText: 'More themes' }).click()
    await expect(page).toHaveURL(/\/settings\/appearance$/)
  })

  test('a custom value theme (Arctic) applies when selected and is visually distinct', async ({ page }) => {
    // A custom theme is chosen in Settings → Appearance and persisted; persisting
    // the id + reloading drives the same apply path. Arctic is a real re-skin, not
    // dark relabelled (regression guard for the "custom tokens never bundled" bug).
    await bootShell(page)

    await page.evaluate((k) => localStorage.setItem(k, 'dark'), THEME_KEY)
    await page.reload()
    // data-theme is stamped by the pre-paint head script, so poll the TOKEN,
    // not just the attribute — the stylesheet may still be streaming and
    // getComputedStyle would read custom properties as "" in that window.
    await expect.poll(() => dataTheme(page)).toBe('dark')
    await expect.poll(() => rootToken(page, '--bg')).not.toBe('')
    const darkBg = await rootToken(page, '--bg')

    await page.evaluate((k) => localStorage.setItem(k, 'arctic'), THEME_KEY)
    await page.reload()
    await expect.poll(() => dataTheme(page)).toBe('arctic')
    await expect.poll(() => rootToken(page, '--bg')).not.toBe('')
    const arcticBg = await rootToken(page, '--bg')

    expect(arcticBg).not.toBe(darkBg)
  })

  test('a legacy persisted theme id ("nord") migrates to its renamed theme ("arctic")', async ({ page }) => {
    // Old clients persisted 'nord'; after the rename it must resolve to 'arctic'
    // and NOT fall back to system/default. (Can't use bootShell here — it asserts
    // a light/dark ground, whereas this loads straight into the renamed theme.)
    await page.addInitScript((k) => localStorage.setItem(k, 'nord'), THEME_KEY)
    await page.goto(CONTROL + 'chat')
    await page.waitForSelector('.conn-pill', { timeout: 20000 })
    await expect.poll(() => dataTheme(page)).toBe('arctic')
    // the canonical id is written back on first load
    await expect
      .poll(() => page.evaluate((k) => localStorage.getItem(k), THEME_KEY))
      .toBe('arctic')
  })
})

test.describe('Global world theme (Terminal)', () => {
  test('selecting Terminal applies its palette globally and lazy-loads the world layer', async ({ page }) => {
    await bootWithTheme(page, 'terminal')
    // The mono type remap is now EAGER (moved to tokens.css so it applies before
    // first paint — no sans→mono reflow on cold load), so it proves the palette,
    // not the world.
    await expect.poll(() => rootToken(page, '--font-sans')).toMatch(/mono/i)
    // The lazy WORLD layer (world.css) paints the global CRT scanline overlay on
    // body::after — a signal ONLY world.css provides — proving it loaded and
    // applied to the whole console.
    await expect
      .poll(() => page.evaluate(() => getComputedStyle(document.body, '::after').backgroundImage))
      .toMatch(/gradient/i)
  })

  test('a new world theme (Vapor) applies and lazy-loads its world', async ({ page }) => {
    await bootWithTheme(page, 'vapor')
    // the display remap is eager (applies with the palette, before first paint)…
    await expect.poll(() => rootToken(page, '--font-display')).toMatch(/mono/i)
    // …while the lazy WORLD layer paints the neon perspective grid on the theme
    // host's ::before — a world-only signal proving world.css loaded + applied.
    await expect
      .poll(() =>
        page.evaluate(() => getComputedStyle(document.documentElement, '::before').backgroundImage),
      )
      .toMatch(/gradient/i)
  })
})
