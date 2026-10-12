import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
// Pages participating in the shared setup catalog expose live/save status in
// their accessible tab names. The remaining destinations do not.
const SECTIONS = [
  'Gateway',
  'Model Service',
  'Model Routing',
  'Capabilities',
  'General',
  'Security & Privacy',
  'Advanced',
]
const STATUSLESS_SECTIONS = ['Interface', 'Shortcuts', 'Memory & Export']

const settingsRow = (page: import('@playwright/test').Page) =>
  page.locator('.sidebar-foot button')

const dialog = (page: import('@playwright/test').Page) =>
  page.locator('.settings-modal[role="dialog"]')

const railTab = (page: import('@playwright/test').Page, name: string) =>
  dialog(page).getByRole('tab', { name: new RegExp(`^${name}(?::|$)`) })

async function openFromSidebar(page: import('@playwright/test').Page) {
  await page.goto(CONTROL_URL)
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await settingsRow(page).click()
  await expect(dialog(page)).toBeVisible()
  await expect(dialog(page)).toHaveAccessibleName('Settings')
}

async function expectModelServiceLoaded(page: import('@playwright/test').Page) {
  await expect(railTab(page, 'Model Service')).toHaveAttribute('aria-selected', 'true')
  await expect(dialog(page).locator('.setup-provider-page')).toBeVisible()
  await expect(dialog(page).locator('.settings-loading')).toHaveCount(0)
}

async function chooseSelectValue(page: import('@playwright/test').Page, name: string, value: string) {
  const select = dialog(page).locator(`select[name="${name}"]`)
  const label = await select.locator(`option[value="${value}"]`).textContent()
  await select.locator('..').getByRole('combobox').click()
  await dialog(page).getByRole('option', { name: label!.trim(), exact: true }).click()
}

async function editSearchProvider(page: import('@playwright/test').Page) {
  await railTab(page, 'Capabilities').click()
  const trigger = dialog(page).getByRole('button', { name: /^Web search/ })
  if (await trigger.getAttribute('aria-expanded') !== 'true') await trigger.click()
  const field = dialog(page).locator('select[name="setup_search_provider"]')
  const original = await field.inputValue()
  const next = await field.locator('option').evaluateAll((options, value) =>
    (options as HTMLOptionElement[]).find(option => !option.disabled && option.value !== value)?.value,
  original)
  expect(next).toBeTruthy()
  await chooseSelectValue(page, 'setup_search_provider', next!)
  return { field, original }
}

test.describe('Settings modal', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  })

  test('opens from the sidebar with AI configuration first and local readiness status', async ({ page }) => {
    await openFromSidebar(page)

    // Dialog a11y: focus moves into the modal on open, before any interaction.
    await expect(page.getByRole('button', { name: 'Close' })).toBeFocused()

    // Rail has setup-catalog sections with readiness/save status, plus
    // destinations that intentionally expose no shared catalog status.
    const tabs = dialog(page).getByRole('tab')
    await expect(tabs).toHaveCount(SECTIONS.length + STATUSLESS_SECTIONS.length)
    for (const name of SECTIONS) {
      await expect(railTab(page, name)).toBeVisible()
    }
    for (const name of STATUSLESS_SECTIONS) {
      await expect(dialog(page).getByRole('tab', { name, exact: true })).toBeVisible()
    }

    await expectModelServiceLoaded(page)
    await expect(railTab(page, 'Model Service')).toHaveAttribute('aria-label', /Model Service: (Ready|Needs action|Review)/)
    await expect(dialog(page).locator('.settings-banner')).toHaveCount(0)
    await expect(dialog(page).getByRole('button', { name: 'CLI handoff' })).toHaveCount(0)
  })

  test('no YAML editor, raw key search, or guided-setup wording anywhere', async ({ page }) => {
    await openFromSidebar(page)

    await expect(dialog(page).getByRole('button', { name: 'YAML', exact: true })).toHaveCount(0)
    await expect(dialog(page).getByRole('button', { name: 'Form', exact: true })).toHaveCount(0)
    await expect(dialog(page).locator('#cfg-search')).toHaveCount(0)
    await expect(dialog(page).locator('textarea#cfg-yaml-area')).toHaveCount(0)
    await expect(dialog(page).getByText('Guided setup')).toHaveCount(0)

    // Config details live in Advanced; routine settings keep the footer clear.
    await expect(dialog(page).locator('.settings-foot')).toHaveCount(0)
    await expect(dialog(page).getByRole('button', { name: 'Copy config path' })).toHaveCount(0)
    await railTab(page, 'Advanced').click()
    const file = dialog(page).getByTestId('advanced-config-file')
    await expect(file).toContainText('Configuration file')
    await expect(file.locator('code')).toContainText(/config.*\.toml/)
    await file.getByRole('button', { name: 'Copy config path' }).click()
    await expect(page.locator('.toast', { hasText: /Copied/ }).first()).toBeVisible()
  })

  test('renders a failure toast above Settings from the body portal', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: { writeText: () => Promise.reject(new Error('Clipboard denied')) },
      })
      Object.defineProperty(document, 'execCommand', {
        configurable: true,
        value: () => false,
      })
    })
    await openFromSidebar(page)

    await railTab(page, 'Advanced').click()
    await dialog(page).getByRole('button', { name: 'Copy config path' }).click()
    const toast = page.locator('.toast.toast--danger', { hasText: /Copy failed/ }).first()
    await expect(toast).toBeVisible()

    const placement = await toast.evaluate(element => {
      const host = element.closest('[data-testid="toast-host"]')
      const app = document.querySelector('#app')
      const rect = element.getBoundingClientRect()
      const topmost = document.elementFromPoint(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2,
      )
      return {
        bodyChild: host?.parentElement === document.body,
        outsideApp: Boolean(host && app && !app.contains(host)),
        fullyInViewport: rect.left >= 0
          && rect.top >= 0
          && rect.right <= window.innerWidth
          && rect.bottom <= window.innerHeight,
        aboveModal: topmost === element || element.contains(topmost),
      }
    })
    expect(placement).toEqual({
      bodyChild: true,
      outsideApp: true,
      fullyInViewport: true,
      aboveModal: true,
    })

    await toast.getByRole('button', { name: 'Dismiss notification' }).click()
    await expect(toast).toHaveCount(0)
    await expect(dialog(page)).toBeVisible()
  })

  test('rail switches sections, marks the active tab, and syncs the URL with replace', async ({ page }) => {
    await openFromSidebar(page)
    await expect(page).toHaveURL(/\/settings$/)

    await railTab(page, 'Capabilities').click()
    await expect(railTab(page, 'Capabilities')).toHaveAttribute('aria-selected', 'true')
    // Switching a section reflects in the URL so it stays deep-linkable.
    await expect(page).toHaveURL(/\/settings\/capabilities$/)
    // The Capabilities panel renders its tools as stacked sections (Web search,
    // Memory, Image, Audio) rather than a redundant "Capabilities" card heading.
    await expect(dialog(page).getByRole('heading', { name: 'Web search' })).toBeVisible()

    await railTab(page, 'Model Routing').click()
    await expect(railTab(page, 'Model Routing')).toHaveAttribute('aria-selected', 'true')
    await expect(page).toHaveURL(/\/settings\/modelStrategy$/)
    await expect(dialog(page).getByRole('heading', { name: 'Model routing', exact: true })).toBeVisible()

    // Section navigation uses replace, so a single Back exits Settings rather
    // than walking section history.
    await page.goBack()
    await expect(dialog(page)).toBeHidden()
    await expect(page).not.toHaveURL(/\/settings/)
  })

  test('opens Memory & Export directly and canonicalizes the old profile-import route', async ({ page }) => {
    await openFromSidebar(page)

    const memoryTab = railTab(page, 'Memory & Export')
    await memoryTab.click()

    await expect(memoryTab).toHaveAttribute('aria-selected', 'true')
    await expect(page).toHaveURL(/\/settings\/memory$/)
    await expect(
      dialog(page).getByRole('heading', { name: 'Import memory from another AI' }),
    ).toBeVisible()
    await expect(dialog(page).locator('[data-testid="settings-memory-panel"]')).toBeVisible()
    await expect(memoryTab).toHaveAttribute('aria-selected', 'true')
    await expect(dialog(page).locator('.settings-dirtybar')).toBeHidden()

    await page.goto(CONTROL_URL + 'settings/profileImport')
    await expect(page).toHaveURL(/\/settings\/memory$/)
    await expect(dialog(page).locator('[data-testid="settings-memory-panel"]')).toBeVisible()
    await expect(railTab(page, 'Memory & Export')).toHaveAttribute('aria-selected', 'true')
  })

  test('keeps the SettingsDialog mounted when routing from default settings to a section', async ({ page }) => {
    await openFromSidebar(page)
    await expect(page).toHaveURL(/\/settings$/)
    await expectModelServiceLoaded(page)
    await expect(dialog(page).locator('.settings-loading')).toHaveCount(0)

    await page.evaluate(() => {
      const modal = document.querySelector('.settings-modal')
      if (!modal) throw new Error('settings modal missing')
      const state = {
        modal,
        removed: false,
        loadingSeen: false,
        observer: null as MutationObserver | null,
      }
      state.observer = new MutationObserver(() => {
        state.removed = state.removed || !document.body.contains(state.modal)
        state.loadingSeen = state.loadingSeen || !!document.querySelector('.settings-loading')
      })
      state.observer.observe(document.body, { childList: true, subtree: true })
      ;(window as typeof window & { __settingsModalProbe?: typeof state }).__settingsModalProbe = state
    })

    await railTab(page, 'Capabilities').click()
    await expect(page).toHaveURL(/\/settings\/capabilities$/)
    await expect(railTab(page, 'Capabilities')).toHaveAttribute('aria-selected', 'true')
    await expect(dialog(page).getByRole('heading', { name: 'Web search' })).toBeVisible()
    // Wait past the route fade and Settings pop enter windows so delayed
    // remounts, re-entry animations, or loading fallbacks are caught.
    await page.waitForTimeout(350)

    const probe = await page.evaluate(() => {
      const state = (window as typeof window & {
        __settingsModalProbe?: {
          modal: Element
          removed: boolean
          loadingSeen: boolean
          observer: MutationObserver | null
        }
      }).__settingsModalProbe
      if (!state) throw new Error('settings modal probe missing')
      state.observer?.disconnect()
      return {
        sameNode: state.modal === document.querySelector('.settings-modal'),
        removed: state.removed,
        loadingSeen: state.loadingSeen,
      }
    })
    expect(probe.sameNode).toBe(true)
    expect(probe.removed).toBe(false)
    expect(probe.loadingSeen).toBe(false)
  })

  test('opens Settings without applying the app route fade to the overlay', async ({ page }) => {
    await page.goto(CONTROL_URL)
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await page.evaluate(() => {
      const state = {
        overlaySeen: false,
        routeFadeSeen: false,
        observer: null as MutationObserver | null,
      }
      const inspect = () => {
        const overlay = document.querySelector('.settings-overlay')
        if (!overlay) return
        state.overlaySeen = true
        state.routeFadeSeen = state.routeFadeSeen || String(overlay.className).includes('route-fade-')
      }
      state.observer = new MutationObserver(inspect)
      state.observer.observe(document.body, {
        attributeFilter: ['class'],
        attributes: true,
        childList: true,
        subtree: true,
      })
      inspect()
      ;(window as typeof window & { __settingsOpenProbe?: typeof state }).__settingsOpenProbe = state
    })

    await settingsRow(page).click()
    await expect(dialog(page)).toBeVisible()
    await expectModelServiceLoaded(page)
    await page.waitForTimeout(200)

    const probe = await page.evaluate(() => {
      const state = (window as typeof window & {
        __settingsOpenProbe?: {
          overlaySeen: boolean
          routeFadeSeen: boolean
          observer: MutationObserver | null
        }
      }).__settingsOpenProbe
      if (!state) throw new Error('settings open probe missing')
      state.observer?.disconnect()
      return {
        overlaySeen: state.overlaySeen,
        routeFadeSeen: state.routeFadeSeen,
      }
    })
    expect(probe.overlaySeen).toBe(true)
    expect(probe.routeFadeSeen).toBe(false)
  })

  test('Model Routing saves its fallback tier through the current dropdown', async ({ page }) => {
    test.setTimeout(90000)
    await openFromSidebar(page)

    const fallbackTier = () => dialog(page).locator('select[name="setup_model_strategy_router_default_tier"]')
    const tierTrigger = () => fallbackTier().locator('..').getByRole('combobox')
    const chooseTier = (value: string) => chooseSelectValue(page, 'setup_model_strategy_router_default_tier', value)
    const dirtybar = dialog(page).locator('.settings-dirtybar')

    await railTab(page, 'Model Routing').click()
    // The picker rides with the router details, so make the router strategy
    // active first if the gateway under test starts on another strategy (the
    // strategy choice is then saved along with the tier — acceptable for the
    // disposable gateways this suite runs against).
    const routerCard = dialog(page).locator('[data-strategy-id="router"]')
    await expect(routerCard).toBeVisible()
    if ((await routerCard.getAttribute('aria-checked')) !== 'true') await routerCard.click()
    await dialog(page).getByTestId('router-advanced-options').locator('summary').click()
    await expect(tierTrigger()).toBeVisible()
    const initial = await fallbackTier().inputValue()
    const next = initial === 'c0' ? 'c1' : 'c0'

    await chooseTier(next)
    await expect(dirtybar).toBeVisible()
    await expect(dirtybar).toContainText('Unsaved changes in Model Routing')
    await dirtybar.getByRole('button', { name: 'Save' }).click()
    await expect(page.locator('.toast', { hasText: /Router saved/ }).first()).toBeVisible()
    await expect(dirtybar).toBeHidden({ timeout: 10000 })

    await page.reload()
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(dialog(page)).toBeVisible()
    await railTab(page, 'Model Routing').click()
    await dialog(page).getByTestId('router-advanced-options').locator('summary').click()
    await expect(fallbackTier()).toHaveValue(next)

    await chooseTier(initial)
    await dirtybar.getByRole('button', { name: 'Save' }).click()
    await expect(dirtybar).toBeHidden({ timeout: 10000 })
  })

  test('Escape closes the overlay, leaves /settings, and returns focus to the invoker', async ({ page }) => {
    await openFromSidebar(page)
    await expect(page).toHaveURL(/\/settings$/)

    await page.keyboard.press('Escape')
    await expect(dialog(page)).toBeHidden()
    // The overlay is route-mounted: closing navigates back off /settings.
    await expect(page).not.toHaveURL(/\/settings/)
    await expect(settingsRow(page)).toBeFocused()

    // Escape inside the overlay must not collapse the docked sidebar.
    await expect(page.locator('.sidebar.docked')).toBeVisible()
  })

  test('closing Settings restores the previous route view without blanking the outlet', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(page.getByLabel('Message to send')).toBeVisible()

    await settingsRow(page).click()
    await expect(dialog(page)).toBeVisible()
    await page.getByRole('button', { name: 'Close' }).click()

    await expect(dialog(page)).toBeHidden()
    await expect(page).toHaveURL(/\/chat\/new$/)
    await expect(page.getByLabel('Message to send')).toBeVisible()
  })

  test('legacy /settings/connection deep link opens Gateway and preserves its subsection', async ({ page }) => {
    // Land directly on a section with no in-app invoker.
    await page.goto(CONTROL_URL + 'settings/connection')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(dialog(page)).toBeVisible()
    await expect(railTab(page, 'Gateway')).toHaveAttribute('aria-selected', 'true')
    await expect(page).toHaveURL(/\/settings\/gateway#connection$/)
    // Connection renders even before/without a loaded config gate.
    await expect(dialog(page).getByRole('heading', { name: 'Connection' })).toBeVisible()

    await page.keyboard.press('Escape')
    await expect(dialog(page)).toBeHidden()
    await expect(page).not.toHaveURL(/\/settings/)
    // Cold entry mounts Chat and then canonicalizes its empty draft. Neither
    // automatic focus step may replace Settings' restored keyboard location.
    await expect(page).toHaveURL(/\/chat\/new$/)
    await expect(page.locator('.chat-textarea')).toBeVisible()
    await expect(settingsRow(page)).toBeFocused()

    await page.locator('.sidebar-new-session').click()
    await expect(page.locator('.chat-textarea')).toBeFocused()
  })

  test('/config deep link redirects into the settings overlay', async ({ page }) => {
    await page.goto(CONTROL_URL + 'config')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(dialog(page)).toBeVisible()
    // /config now redirects to /settings (default section).
    await expect(page).toHaveURL(/\/settings$/)
    await expect(railTab(page, 'Model Service')).toHaveAttribute('aria-selected', 'true')
  })

  test('/setup deep link opens the readiness-selected AI configuration section', async ({ page }) => {
    await page.goto(CONTROL_URL + 'setup')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(dialog(page)).toBeVisible()
    // /setup redirects to /settings/auto (first not-ready section).
    await expect(page).toHaveURL(/\/settings\/auto$/)

    // Optional capability diagnostics stay in their own panel. The automatic
    // destination remains an AI section with an accessible, associated panel.
    const selected = dialog(page).getByRole('tab', { selected: true })
    await expect(selected).toHaveCount(1)
    await expect(selected).toHaveAttribute('id', /^settings-rail-(provider|modelStrategy|capabilities)$/)
    await expect(dialog(page).getByRole('tabpanel')).toBeVisible()
    await expect(dialog(page).getByRole('tabpanel')).toHaveAttribute('aria-labelledby', await selected.getAttribute('id') || '')
    await expect(dialog(page).locator('.settings-loading')).toHaveCount(0)
    await expect(dialog(page).locator('.settings-banner')).toHaveCount(0)
  })

  test('dirty edits raise the bar, guard close, and Discard restores values', async ({ page }) => {
    await openFromSidebar(page)

    const { field, original } = await editSearchProvider(page)

    const dirtybar = dialog(page).locator('.settings-dirtybar')
    await expect(dirtybar).toBeVisible()
    await expect(dirtybar).toContainText('Unsaved changes in Capabilities')

    // Closing with unsaved edits offers save or discard; Escape keeps the modal open.
    await page.keyboard.press('Escape')
    const confirm = page.getByRole('dialog', { name: 'Unsaved changes' })
    await expect(confirm).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(confirm).toBeHidden()
    await expect(dialog(page)).toBeVisible()

    await dirtybar.getByRole('button', { name: 'Discard' }).click()
    await expect(dirtybar).toBeHidden()
    await expect(field).toHaveValue(original)
  })

  test('history Back with dirty edits raises the discard confirm instead of silently closing', async ({ page }) => {
    await openFromSidebar(page)

    const { field, original } = await editSearchProvider(page)
    await expect(page).toHaveURL(/\/settings\/capabilities$/)
    await expect(dialog(page).locator('.settings-dirtybar')).toBeVisible()

    // History traversal (browser Back, a trackpad back-swipe) unmounts the
    // route-mounted overlay without passing through requestClose, so the
    // router-level guard must raise the same confirm. Declining cancels the
    // navigation and restores the /settings URL.
    await page.goBack()
    const confirm = page.getByRole('dialog', { name: 'Unsaved changes' })
    await expect(confirm).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(confirm).toBeHidden()
    await expect(dialog(page)).toBeVisible()
    await expect(page).toHaveURL(/\/settings\/capabilities$/)
    await expect(field).not.toHaveValue(original)

    // Accepting the discard lets the same Back proceed and close the overlay.
    await page.goBack()
    await expect(confirm).toBeVisible()
    await confirm.getByRole('button', { name: 'Discard changes' }).click()
    await expect(dialog(page)).toBeHidden()
    await expect(page).not.toHaveURL(/\/settings/)
    await expect(settingsRow(page)).toBeFocused()
  })

  test('viewport opts out of horizontal overscroll so a trackpad swipe cannot pop history', async ({ page }) => {
    await page.goto(CONTROL_URL)
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    // Chromium turns a two-finger horizontal overscroll into history navigation;
    // with Settings and chat sessions living on the history stack that gesture
    // would dismiss them wholesale. Both propagation roots must opt out.
    const behaviors = await page.evaluate(() => [
      getComputedStyle(document.documentElement).overscrollBehaviorX,
      getComputedStyle(document.body).overscrollBehaviorX,
    ])
    expect(behaviors).toEqual(['none', 'none'])
  })

  test('live save round-trip persists the Advanced conversation-capture toggle', async ({ page }) => {
    test.setTimeout(90000)
    await openFromSidebar(page)

    // memory.auto_capture_enabled is hot-applied via the config.patch path.
    const capture = () => dialog(page).locator('input[name="memory_auto_capture"]')
    const dirtybar = () => dialog(page).locator('.settings-dirtybar')

    await railTab(page, 'Advanced').click()
    await expect(capture()).toBeVisible()
    const initial = await capture().isChecked()

    await capture().setChecked(!initial)
    await expect(dirtybar()).toContainText('Unsaved changes in Advanced')
    await dirtybar().getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.locator('.toast', { hasText: 'Conversation capture saved.' }).first()).toBeVisible()
    await expect(dirtybar()).toBeHidden({ timeout: 10000 })

    // Reload: the persisted value must survive a fresh modal.
    await page.reload()
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(dialog(page)).toBeVisible()
    await railTab(page, 'Advanced').click()
    await expect(capture()).toBeVisible()
    expect(await capture().isChecked()).toBe(!initial)

    // Restore the original value.
    await capture().setChecked(initial)
    await dirtybar().getByRole('button', { name: 'Save', exact: true }).click()
    await expect(dirtybar()).toBeHidden({ timeout: 10000 })
    await page.reload()
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(dialog(page)).toBeVisible()
    await railTab(page, 'Advanced').click()
    await expect(capture()).toBeVisible()
    expect(await capture().isChecked()).toBe(initial)
  })

  test('mobile: full-screen categories open details and return without a horizontal strip', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto(CONTROL_URL + 'config')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(dialog(page)).toBeVisible()

    const modalBox = await dialog(page).boundingBox()
    expect(modalBox?.width).toBe(390)

    const rail = dialog(page).getByRole('navigation', { name: 'Settings sections' })
    await expect(rail).toBeVisible()
    await expect(dialog(page).getByRole('tablist')).toHaveCount(0)
    await rail.getByRole('button', { name: /^Capabilities:/ }).click()
    await expect(rail).toBeHidden()
    await expect(dialog(page)).toHaveAccessibleName('Capabilities')
    await expect(dialog(page).getByRole('heading', { name: 'Web search' })).toBeVisible()
    await dialog(page).getByRole('button', { name: 'Back to settings categories' }).click()
    await expect(dialog(page)).toHaveAccessibleName('Settings')
    await expect(rail).toBeVisible()
    await expect(rail.getByRole('button', { name: /^Capabilities:/ })).toBeFocused()

    // No horizontal scroll on the page at 390px.
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(0)
  })

  test('boolean settings render as the canonical control-switch and keep checkbox semantics', async ({ page }) => {
    await openFromSidebar(page)
    await railTab(page, 'Advanced').click()

    const capture = dialog(page).locator('input[name="memory_auto_capture"]')
    await expect(capture).toBeVisible()

    // It carries the shared switch primitive, not a raw browser checkbox, and
    // is exposed to assistive tech as a switch.
    await expect(capture).toHaveClass(/control-switch/)
    await expect(capture).toHaveAttribute('role', 'switch')

    // The primitive CSS actually loaded and applied end-to-end (built bundle →
    // gateway → page): the checkbox is restyled (appearance:none) and sized as
    // a 36px switch track.
    const appearance = await capture.evaluate((el) => getComputedStyle(el).appearance)
    expect(appearance).toBe('none')
    const box = await capture.boundingBox()
    expect(box?.width).toBeGreaterThanOrEqual(34)
    expect(box?.width).toBeLessThanOrEqual(40)

    // Native checkbox semantics are preserved: it stays a real, keyboard-
    // operable input (Space toggles, :checked round-trips).
    const before = await capture.isChecked()
    await capture.focus()
    await page.keyboard.press('Space')
    expect(await capture.isChecked()).toBe(!before)
    await page.keyboard.press('Space')
    expect(await capture.isChecked()).toBe(before)
  })

  test('every boolean control in Capabilities uses the switch primitive (no raw checkboxes)', async ({ page }) => {
    await openFromSidebar(page)
    await railTab(page, 'Capabilities').click()
    await expect(dialog(page).getByRole('heading', { name: 'Web search' })).toBeVisible()
    await dialog(page).getByRole('button', { name: /^Image generation/ }).click()

    const checkboxes = dialog(page).locator('input[type="checkbox"]')
    const total = await checkboxes.count()
    expect(total).toBeGreaterThan(0)
    const switches = dialog(page).locator('input[type="checkbox"].control-switch')
    expect(await switches.count()).toBe(total)
  })

  test('Interface section applies theme instantly without a dirty bar', async ({ page }) => {
    await openFromSidebar(page)

    const appearanceTab = dialog(page).getByRole('tab', { name: 'Interface', exact: true })
    await expect(appearanceTab).toBeVisible()
    await appearanceTab.click()
    await expect(appearanceTab).toHaveAttribute('aria-selected', 'true')
    await expect(dialog(page).getByRole('heading', { name: 'Interface', exact: true })).toBeVisible()

    // Theme radios flip the live document theme with no save step, and persist
    // to the same store the sidebar shortcut reads.
    const dark = dialog(page).getByRole('radio', { name: 'Dark' })
    const light = dialog(page).getByRole('radio', { name: 'Light' })
    await dark.click()
    await expect(dark).toBeChecked()
    await expect.poll(() => page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark')
    await light.click()
    await expect.poll(() => page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('light')
    expect(await page.evaluate(() => localStorage.getItem('opensquilla-theme'))).toBe('light')

    // Visual effects are a low-frequency browser preference and live here
    // instead of adding another popover to the chat composer.
    const visualEffects = dialog(page).getByRole('switch', { name: 'Model routing display' })
    await expect(visualEffects).toBeVisible()
    await visualEffects.click()
    await expect(visualEffects).not.toBeChecked()
    expect(await page.evaluate(() => (
      JSON.parse(localStorage.getItem('opensquilla.routerFx') || '{}').enabled
    ))).toBe(false)

    // Client-only: theme changes never raise the settings dirty bar.
    await expect(dialog(page).locator('.settings-dirtybar')).toBeHidden()
  })

  test('Interface persists the tool-detail default without a save step', async ({ page }) => {
    await openFromSidebar(page)
    await dialog(page).getByRole('tab', { name: 'Interface', exact: true }).click()

    const collapsed = dialog(page).getByRole('radio', { name: 'Collapsed by default', exact: true })
    const expanded = dialog(page).getByRole('radio', { name: 'Expanded by default', exact: true })
    await expect(collapsed).toBeChecked()

    await expanded.click()
    await expect(expanded).toBeChecked()
    await expect(dialog(page).locator('.settings-dirtybar')).toBeHidden()
    expect(await page.evaluate(() => (
      localStorage.getItem('opensquilla.appearance.toolDetails.v1')
    ))).toBe('expanded')

    await page.reload()
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(dialog(page)).toBeVisible()
    await expect(dialog(page).getByRole('radio', { name: 'Expanded by default', exact: true })).toBeChecked()

    await dialog(page).getByRole('radio', { name: 'Collapsed by default', exact: true }).click()
  })

  test('Advanced client experiments still apply instantly without a dirty bar', async ({ page }) => {
    await openFromSidebar(page)
    const advTab = railTab(page, 'Advanced')
    await expect(advTab).toBeVisible()
    await advTab.click()
    await expect(advTab).toHaveAttribute('aria-selected', 'true')
    await expect(dialog(page).getByRole('heading', { name: 'Advanced' })).toBeVisible()

    // A boolean Labs flag is the shared switch and writes its raw '1'/'0' key.
    const poll = dialog(page).locator('input[name="labs_approval_poll"]')
    await expect(poll).toHaveAttribute('role', 'switch')
    const before = await poll.isChecked()
    await poll.click()
    expect(await page.evaluate(() => localStorage.getItem('opensquilla.chat.approvalPoll'))).toBe(before ? '0' : '1')

    // The answer-reveal window persists as a validated "min,max" string.
    const min = dialog(page).locator('input[name="labs_reveal_min"]')
    const max = dialog(page).locator('input[name="labs_reveal_max"]')
    await min.fill('1000')
    await max.fill('3000')
    await max.blur()
    expect(await page.evaluate(() => localStorage.getItem('opensquilla.chat.answerReveal'))).toBe('1000,3000')

    // These client-only experiment edits do not raise the settings dirty bar;
    // the Gateway-backed conversation-capture row above still does.
    await expect(dialog(page).locator('.settings-dirtybar')).toBeHidden()

    await expect(dialog(page).getByRole('button', { name: 'Open: Agent configuration (advanced)' })).toHaveCount(0)
  })
})
