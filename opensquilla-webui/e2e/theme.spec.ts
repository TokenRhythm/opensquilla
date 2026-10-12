import { test, expect, type Page } from '@playwright/test'
import { relativeLuminance } from './utils'

// End-to-end coverage for the pluggable theme engine (P0): the value-theme axis
// (light / dark / system) driven through the real Settings picker, persistence,
// registry-driven validation of the persisted choice, and the layered token
// contract (L1 per-theme values, the new ink/fill pairs, and L0 foundation
// tokens that stay invariant across themes). Runs against whatever build serves
// the SPA at the base URL — the gateway in CI, or a static server locally.

const CONTROL = '/control/'
const THEME_KEY = 'opensquilla-theme'

// Theme preferences are managed by the complete Settings > Interface picker.
const THEME_MODES = ['light', 'dark', 'system'] as const

async function bootShell(page: Page) {
  // The shell renders on every route, including without a configured provider.
  await page.goto(CONTROL + 'chat')
  await page.waitForSelector('.topbar[data-connection-state]', { timeout: 20000 })
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

async function pickTheme(page: Page, mode: typeof THEME_MODES[number]) {
  await page.goto(CONTROL + 'settings/interface')
  await page.locator(`.appearance-theme__opt:has(input[name="appearance-theme"][value="${mode}"])`).click()
  await expect(page.locator(`input[name="appearance-theme"][value="${mode}"]`)).toBeChecked()
}

// Persisted theme ids exercise the same theme application and lazy world-load
// path as the Settings radio group, without needing a live provider catalog.
async function bootWithTheme(page: Page, themeId: string) {
  await page.addInitScript(
    (v) => localStorage.setItem('opensquilla-theme', v),
    themeId,
  )
  await page.goto(CONTROL + 'chat')
  await page.waitForSelector('.topbar[data-connection-state]', { timeout: 20000 })
  await expect.poll(() => dataTheme(page)).toBe(themeId)
}

test.describe('Theme engine (P0)', () => {
  test('Settings picker applies each value theme and persists across reload', async ({ page }) => {
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

  test('Settings exposes all registered themes while the topbar stays quiet', async ({ page }) => {
    await bootShell(page)
    await expect(page.locator('.topbar .theme-menu-wrap')).toHaveCount(0)
    await page.goto(CONTROL + 'settings/interface')
    const values = await page.locator('input[name="appearance-theme"]').evaluateAll(elements =>
      elements.map(element => (element as HTMLInputElement).value))
    expect(values.sort()).toEqual(['light', 'dark', 'system', 'arctic', 'crt-green', 'ember', 'miami', 'synthwave', 'terminal', 'vapor'].sort())
    await page.locator('.appearance-theme__opt:has(input[value="arctic"])').click()
    await expect.poll(() => dataTheme(page)).toBe('arctic')
    await expect(page.locator('input[name="appearance-theme"][value="arctic"]')).toBeChecked()
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
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 20000 })
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
