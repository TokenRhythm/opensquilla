import { strict as assert } from 'node:assert'
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')
const screenshotPath = String(process.env.OPENSQUILLA_DESKTOP_ONBOARDING_SCREENSHOT || '').trim()

async function waitFor(check, label, timeoutMs = 60_000) {
  const startedAt = Date.now()
  let lastError
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const value = await check()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await delay(250)
  }
  const suffix = lastError ? ` Last error: ${lastError.message || lastError}` : ''
  throw new Error(`Timed out waiting for ${label}.${suffix}`)
}

async function setupWindow(app) {
  return await waitFor(async () => {
    for (const page of app.windows()) {
      if (page.isClosed()) continue
      await page.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
      if (await page.locator('#setup-form').count().catch(() => 0)) return page
    }
    return null
  }, 'desktop onboarding window')
}

const userDataRoot = await mkdtemp(join(tmpdir(), 'opensquilla-electron-onboarding-test-'))
const userDataDir = join(userDataRoot, 'chromium-user-data')
const isolatedHome = join(userDataRoot, 'home')
await mkdir(isolatedHome, { recursive: true })
const app = await electron.launch({
  args: [
    '--use-mock-keychain',
    `--user-data-dir=${userDataDir}`,
    packageRoot,
  ],
  env: {
    ...process.env,
    HOME: isolatedHome,
    USERPROFILE: isolatedHome,
    OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
    OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
    OPENSQUILLA_DESKTOP_GATEWAY_PORT: '18897',
    OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_DESKTOP_MOCK_UPDATE_VERSION: '',
    LANG: 'en_US.UTF-8',
    LC_ALL: 'en_US.UTF-8',
  },
})

try {
  const page = await setupWindow(app)
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message || String(error)))
  const providerScreen = page.locator('[data-screen="1"]')
  async function chooseProvider(id) {
    await page.locator('#providerSelectToggle').click()
    await page.locator(`[data-provider-option="${id}"]`).click()
  }

  await page.locator('#onboardingLocale').selectOption('zh-Hans')
  assert.deepEqual(pageErrors, [], 'onboarding should not raise page-script errors during locale rendering')
  assert.equal(await page.evaluate(() => document.documentElement.lang), 'zh-Hans')
  assert.equal(await page.title(), '设置 OpenSquilla')
  assert.equal(await page.locator('[data-screen="0"]').count(), 0, 'setup-depth selection must be removed')
  assert.equal(await page.locator('[data-screen="2"], [data-screen="3"], [data-screen="4"]').count(), 0, 'onboarding must use a single setup screen')
  assert.equal(await page.locator('[data-setup-mode], [data-model-routing-mode]').count(), 0, 'advanced setup controls must be removed')
  assert.equal(await page.locator('.rail, .progress, .step').count(), 0, 'onboarding must not render a side rail or step tracker')
  assert.equal(await page.locator('.topbar .brand').innerText(), 'OpenSquilla')
  assert.equal(await page.locator('.eyebrow, .card-badge').count(), 0, 'decorative step labels and badges must be removed')
  assert.equal(await page.locator('#providerHint').count(), 0, 'provider hint banner must be removed')
  assert.equal(await providerScreen.isVisible(), true, 'onboarding should open directly on provider setup')
  assert.equal(await page.locator('.step-switcher, [data-route-step]').count(), 0, 'onboarding should not render a numbered step switcher')
  assert.equal(await providerScreen.locator('.context-label').count(), 0)
  assert.equal(await providerScreen.locator('h2').innerText(), '模型服务配置')
  assert.equal(await providerScreen.locator('.card-head > p').innerText(), '输入 API 密钥即可开始使用')
  assert.equal(await page.locator('#apiKeyRequiredMarker').innerText(), '*')
  assert.equal(await page.locator('#apiKeyRequiredMarker').isVisible(), true)
  assert.equal(
    await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()),
    '#BA4D0F',
    'onboarding should use the in-app light-theme accent',
  )
  await page.mouse.move(0, 0)
  assert.equal(
    await page.locator('#finish').evaluate((button) => getComputedStyle(button).backgroundColor),
    'rgb(52, 58, 64)',
    'the single primary action should use the softer graphite treatment',
  )
  assert.equal(await page.locator('#finish').innerText(), '启动 OpenSquilla')
  assert.equal(await page.locator('.next-button, .back-button').count(), 0, 'single-page onboarding must not render next or back actions')
  assert.equal(await providerScreen.locator('.provider-feature, .provider-disclosure').count(), 0, 'provider setup should use one unified select')
  assert.equal(await providerScreen.locator('.provider-promo').count(), 0, 'the promotion should not occupy a separate row')
  assert.equal(await providerScreen.locator('.provider-promo-token').count(), 0)
  assert.equal(await providerScreen.locator('.provider-promo-copy').isVisible(), true)
  assert.equal(await providerScreen.locator('.provider-promo-copy strong').innerText(), 'TokenRhythm 限时福利')
  assert.equal(await providerScreen.locator('.provider-promo-copy span').innerText(), '注册即领价值 68 元 Token')
  const promoTitleBox = await page.locator('.provider-promo-copy strong').boundingBox()
  const promoCopyBox = await page.locator('.provider-promo-copy span').boundingBox()
  assert.ok(
    promoTitleBox && promoCopyBox
      && Math.abs(
        (promoTitleBox.y + promoTitleBox.height / 2)
        - (promoCopyBox.y + promoCopyBox.height / 2),
    ) <= 2,
    'the limited-time promotion copy should render on one line',
  )
  assert.equal(
    await providerScreen.locator('.provider-promo-copy strong').evaluate((copy) => getComputedStyle(copy).color),
    'rgb(186, 77, 15)',
  )
  assert.equal(await page.locator('#endpointPanel, #endpointToggle').count(), 0, 'simple onboarding should not expose endpoint controls')
  assert.equal(await page.locator('#provider').inputValue(), 'tokenrhythm', 'TokenRhythm should be selected by default')
  assert.equal(await page.locator('.provider-field-head').count(), 0)
  assert.equal(await page.locator('#providerSelectLabel').innerText(), '提供商')
  assert.equal(await page.locator('#providerSelectValue').innerText(), 'TokenRhythm')
  assert.equal(
    await page.locator('#providerSelectToggle').evaluate((toggle) => getComputedStyle(toggle).backgroundColor),
    'rgb(247, 248, 247)',
    'the provider row should share the recommended-model surface',
  )
  assert.equal(
    await page.locator('#providerSelectToggle').evaluate((toggle) => getComputedStyle(toggle).borderTopWidth),
    '0px',
    'the provider row should use the same borderless treatment as the recommended-model row',
  )
  assert.equal(await page.locator('#modelSummary').isVisible(), true)
  assert.equal(await page.locator('#modelEditor').isVisible(), false)
  assert.equal(await page.locator('#modelSummaryLabel').innerText(), '推荐模型')
  assert.equal(await page.locator('#modelSummaryValue').innerText(), 'deepseek-v4-pro')
  assert.deepEqual(
    await page.evaluate(() => [
      getComputedStyle(document.getElementById('providerSelectLabel')).fontSize,
      getComputedStyle(document.getElementById('providerSelectValue')).fontSize,
      getComputedStyle(document.getElementById('modelSummaryLabel')).fontSize,
      getComputedStyle(document.getElementById('modelSummaryValue')).fontSize,
    ]),
    ['11.5px', '11.5px', '11.5px', '11.5px'],
    'provider and recommended-model rows should use one consistent font size',
  )
  assert.equal(await page.locator('#modelEditToggle').innerText(), '')
  assert.equal(await page.locator('#modelEditToggle').getAttribute('aria-label'), '修改')
  assert.equal(await page.locator('#modelEditToggle svg').count(), 1)
  assert.equal(
    await page.locator('#modelEditToggle').evaluate((button) => getComputedStyle(button).color),
    'rgb(122, 129, 138)',
    'the edit icon should use a neutral gray treatment',
  )
  await page.locator('#modelEditToggle').click()
  assert.equal(await page.locator('#modelSummary').isVisible(), false)
  assert.equal(await page.locator('#modelEditor').isVisible(), true)
  assert.equal(await page.locator('label[for="model"] > .field-label-text').innerText(), '模型名称')
  assert.equal(await page.locator('#modelEditDone').innerText(), '完成')
  await page.locator('#modelEditDone').click()
  assert.equal(await page.locator('#modelSummary').isVisible(), true)
  assert.equal(await page.locator('#apiKey').getAttribute('placeholder'), 'sk-...')
  assert.equal(await page.evaluate(() => typeof window.opensquillaDesktop.probeOnboarding), 'function')
  assert.equal(
    await page.locator('#verifyProvider, #providerVerifyStatus, #providerVerifyError, .provider-verify-inline').count(),
    0,
    'provider verification controls should not be exposed in onboarding',
  )
  const apiKeyLabelBox = await page.locator('.api-key-label').boundingBox()
  const providerLabelBox = await page.locator('#providerSelectLabel').boundingBox()
  const claimButtonBox = await page.locator('#tokenrhythmRegister').boundingBox()
  const initialApiKeyBox = await page.locator('#apiKey').boundingBox()
  assert.ok(
    apiKeyLabelBox && providerLabelBox
      && Math.abs(apiKeyLabelBox.x - providerLabelBox.x) <= 1,
    'the API-key heading should align with the inset provider label',
  )
  assert.ok(
    apiKeyLabelBox && promoTitleBox && promoCopyBox
      && Math.abs(
        (apiKeyLabelBox.y + apiKeyLabelBox.height / 2)
        - (promoTitleBox.y + promoTitleBox.height / 2),
      ) <= 3
      && Math.abs(
        (apiKeyLabelBox.y + apiKeyLabelBox.height / 2)
        - (promoCopyBox.y + promoCopyBox.height / 2),
      ) <= 3,
    'the limited-time promotion should share the API-key heading row',
  )
  assert.ok(
    apiKeyLabelBox && claimButtonBox
      && Math.abs(
        (apiKeyLabelBox.y + apiKeyLabelBox.height / 2)
        - (claimButtonBox.y + claimButtonBox.height / 2),
      ) <= 3,
    'the claim button should share the API-key heading row',
  )
  assert.ok(
    claimButtonBox && initialApiKeyBox
      && Math.abs(
        (claimButtonBox.x + claimButtonBox.width)
        - (initialApiKeyBox.x + initialApiKeyBox.width),
      ) <= 2,
    'the claim button should align to the right edge of the API-key input',
  )
  assert.equal(
    await page.locator('#providerSelectedBadges .provider-badge').count(),
    0,
    'the closed provider row should not repeat the limited-time promotion badge',
  )
  await page.locator('#providerSelectToggle').click()
  assert.equal(await page.locator('#providerSelectToggle').getAttribute('aria-expanded'), 'true')
  assert.equal(await page.locator('#providerSelectPanel').isVisible(), true)
  assert.equal(await page.locator('#providerSearch, .provider-search-wrap').count(), 0, 'the provider list should open directly without a search field')
  assert.equal(
    await page.locator('[data-provider-option="tokenrhythm"]').evaluate((option) => document.activeElement === option),
    true,
  )
  assert.deepEqual(
    await page.locator('[data-provider-option="tokenrhythm"] .provider-badge').allInnerTexts(),
    ['限时免费'],
    'TokenRhythm should expose only the limited-time badge in the provider list',
  )
  assert.equal(await page.locator('[data-provider-group="recommended"] .provider-option-group-label').innerText(), '推荐')
  assert.equal(await page.locator('[data-provider-group="cloud"] .provider-option-group-label').innerText(), '云端服务')
  assert.equal(await page.locator('[data-provider-group="local"] .provider-option-group-label').innerText(), '本地服务')
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('#providerSelectPanel').isVisible(), false)
  await page.locator('#finish').click()
  const apiKeyInput = page.locator('#apiKey')
  const apiKeyError = page.locator('#apiKeyError')
  assert.match(await apiKeyError.innerText(), /需要 TokenRhythm API 密钥/)
  assert.equal(await apiKeyInput.getAttribute('aria-invalid'), 'true')
  assert.equal(await page.locator('#error').innerText(), '', 'field validation must not use the global error region')
  const apiKeyBox = await apiKeyInput.boundingBox()
  const apiKeyErrorBox = await apiKeyError.boundingBox()
  const providerSelectBox = await page.locator('#providerSelectToggle').boundingBox()
  assert.ok(providerSelectBox && apiKeyBox && providerSelectBox.y + providerSelectBox.height <= apiKeyBox.y, 'provider selector must render above the API-key field')
  assert.ok(apiKeyBox && apiKeyErrorBox && apiKeyErrorBox.y >= apiKeyBox.y + apiKeyBox.height, 'API-key error must render below its input')
  await apiKeyInput.fill('temporary-key')
  assert.equal(await apiKeyError.innerText(), '', 'editing the API key should clear its field error')
  assert.equal(await apiKeyInput.getAttribute('aria-invalid'), null)
  await apiKeyInput.fill('')

  assert.equal(await page.locator('#provider').inputValue(), 'tokenrhythm')
  assert.equal(await page.locator('#baseUrl').inputValue(), 'https://tokenrhythm.studio/v1')
  assert.equal(await page.locator('#model').inputValue(), 'deepseek-v4-pro')
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.equal(await page.locator('#routerMode').inputValue(), 'recommended')

  const tokenRhythmCta = page.locator('#tokenrhythmRegister')
  assert.equal(await tokenRhythmCta.innerText(), '免费领取')
  assert.equal(
    await tokenRhythmCta.evaluate((link) => getComputedStyle(link, '::after').content),
    '"↗"',
    'external registration action should expose a direction cue',
  )
  assert.equal(
    await tokenRhythmCta.evaluate((link) => getComputedStyle(link).backgroundColor),
    'rgb(186, 77, 15)',
    'the registration call to action should use the canonical light-theme accent',
  )
  assert.equal(await tokenRhythmCta.evaluate((link) => getComputedStyle(link).color), 'rgb(255, 255, 255)')
  assert.equal(await tokenRhythmCta.evaluate((link) => getComputedStyle(link).borderRadius), '7px')
  assert.equal(await tokenRhythmCta.getAttribute('href'), 'https://tokenrhythm.studio/register')
  assert.equal(await tokenRhythmCta.getAttribute('target'), '_blank')
  assert.equal(await tokenRhythmCta.getAttribute('rel'), 'noopener noreferrer')
  assert.equal(await tokenRhythmCta.isVisible(), true)
  assert.equal(await page.locator('#providerMoreToggle, #providerMorePanel, #providerGrid, .provider').count(), 0)

  await page.locator('#onboardingLocale').selectOption('en')
  assert.equal(await providerScreen.locator('h2').innerText(), 'Model service setup')
  assert.equal(await page.locator('#provider').inputValue(), 'tokenrhythm', 'locale changes should preserve the selected provider')

  await chooseProvider('minimax_cn', 'MiniMax Mainland')
  assert.equal(await page.locator('#provider').inputValue(), 'minimax_cn')
  assert.equal(await tokenRhythmCta.isVisible(), true, 'the promotion should remain available when another provider is selected')
  assert.equal(await page.locator('#providerSelectedBadges .provider-badge').count(), 0)
  assert.equal(await page.locator('#model').inputValue(), 'MiniMax-M2.7')
  assert.equal(await page.locator('#modelSummaryValue').innerText(), 'MiniMax-M2.7')
  assert.equal(await page.locator('#modelSummary').isVisible(), true)
  assert.equal(await page.locator('#apiKeyRequiredMarker').isVisible(), true)

  await chooseProvider('ollama', 'Ollama')
  assert.equal(await page.locator('#provider').inputValue(), 'ollama')
  assert.equal(await page.locator('#apiKeyRequiredMarker').isVisible(), false)
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'direct')
  assert.equal(await page.locator('#routerMode').inputValue(), 'disabled')
  assert.equal(await page.locator('#model').inputValue(), '')
  assert.equal(await page.locator('#modelSummary').isVisible(), false)
  assert.equal(await page.locator('#modelEditor').isVisible(), true)
  assert.equal(await page.locator('#modelRequiredMarker').isVisible(), true)
  await page.locator('#finish').click()
  assert.equal(await providerScreen.isVisible(), true, 'invalid direct-model setup must remain on the provider screen')
  assert.match(await page.locator('#modelError').innerText(), /Direct model is required/)
  assert.equal(await page.locator('#model').getAttribute('aria-invalid'), 'true')
  assert.equal(await page.locator('#error').innerText(), '')

  await chooseProvider('tokenrhythm', 'TokenRhythm')
  assert.equal(await tokenRhythmCta.isVisible(), true)
  assert.equal(await page.locator('#providerSelectedBadges .provider-badge').count(), 0)
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.equal(await page.locator('#routerMode').inputValue(), 'recommended')
  assert.equal(await page.locator('#modelSummary').isVisible(), true)
  assert.equal(await page.locator('#modelSummaryValue').innerText(), 'deepseek-v4-pro')
  await page.locator('#apiKey').fill('synthetic-tokenrhythm-key')
  assert.equal(await page.locator('.inline-search-section').isVisible(), true)
  assert.equal(await page.locator('#inlineSearchHeading').innerText(), 'Choose web search')
  assert.equal(await page.locator('.inline-search-optional').innerText(), 'Optional')
  assert.equal(await page.locator('#inlineSearchToggle').getAttribute('aria-expanded'), 'false')
  assert.equal(await page.locator('#inlineSearchPanel').isVisible(), false)
  await page.locator('#inlineSearchToggle').click()
  assert.equal(await page.locator('#inlineSearchToggle').getAttribute('aria-expanded'), 'true')
  assert.equal(await page.locator('#inlineSearchPanel').isVisible(), true)
  assert.equal(
    await page.locator('[data-search-provider="duckduckgo"]').evaluate((choice) => getComputedStyle(choice).backgroundColor),
    'rgb(247, 248, 247)',
    'the selected default search should use the same neutral surface as the recommended model row',
  )
  assert.equal(
    await page.locator('[data-search-provider="duckduckgo"]').evaluate((choice) => getComputedStyle(choice).boxShadow),
    'none',
    'the selected default search should not add a separate accent rail',
  )
  assert.equal(
    await page.locator('[data-search-provider="duckduckgo"] .search-provider-billing').evaluate((billing) => getComputedStyle(billing).color),
    'rgb(142, 58, 10)',
    'the free status should use the canonical deep light-theme accent',
  )
  if (screenshotPath) {
    await mkdir(dirname(screenshotPath), { recursive: true })
    await page.screenshot({ path: screenshotPath })
  }
  assert.equal(await page.locator('#searchHint, .note').count(), 0, 'search provider descriptions should not be repeated in a separate banner')
  assert.equal(await page.locator('[data-search-provider="duckduckgo"] .search-provider-billing').innerText(), 'Free')
  assert.equal(await page.locator('#searchPaidToggle').getAttribute('aria-expanded'), 'false')
  assert.equal(await page.locator('[data-search-provider="bocha"]').isVisible(), false)
  assert.equal(await page.locator('#searchKeyLabel').isVisible(), false)
  await page.locator('#searchPaidToggle').click()
  assert.equal(await page.locator('#searchPaidToggle').getAttribute('aria-expanded'), 'true')
  assert.equal(await page.locator('[data-search-provider="bocha"]').isVisible(), true)
  assert.equal(await page.locator('[data-search-provider="bocha"] .search-provider-billing').innerText(), 'Paid')
  await page.locator('[data-search-provider="bocha"]').click()
  assert.equal(await page.locator('[data-search-provider-option="bocha"] #searchKeyLabel').isVisible(), true)
  assert.equal(await page.locator('#searchKeyLabel .required-marker').innerText(), '*')
  assert.equal(await page.locator('#searchApiKey').getAttribute('placeholder'), 'BOCHA_SEARCH_API_KEY')
  await page.locator('#inlineSearchToggle').click()
  assert.equal(await page.locator('#inlineSearchPanel').isVisible(), false)
  await page.locator('#finish').click()
  assert.equal(await page.locator('#inlineSearchPanel').isVisible(), true, 'search validation should reopen the collapsed section')
  assert.match(await page.locator('#searchApiKeyError').innerText(), /Bocha search API key is required/)
  assert.equal(await page.locator('#searchApiKey').getAttribute('aria-invalid'), 'true')
  assert.equal(await page.locator('#error').innerText(), '')
  await page.locator('[data-search-provider="duckduckgo"]').click()
  assert.equal(await page.locator('#searchKeyLabel').isVisible(), false)
  assert.equal(await page.locator('#searchApiKeyError').innerText(), '')
  assert.equal(await page.locator('#apiKey').inputValue(), 'synthetic-tokenrhythm-key')
  await page.locator('#finish').click()

  const saved = await waitFor(async () => {
    const credential = JSON.parse(await readFile(join(userDataDir, 'desktop-credential.json'), 'utf8'))
    if (credential.provider !== 'tokenrhythm') return null
    const config = await readFile(join(userDataDir, 'opensquilla', 'config.toml'), 'utf8')
    return { credential, config }
  }, 'saved simple onboarding credential and config')
  const { credential, config } = saved
  assert.equal(credential.provider, 'tokenrhythm')
  assert.equal(credential.modelRoutingMode, 'squilla_router')
  assert.equal(credential.routerMode, 'recommended')
  assert.equal(credential.routerDefaultTier, 'c1')
  assert.equal(credential.routerTiers.c0.model, 'deepseek-v4-flash')
  assert.equal(credential.routerTiers.c1.model, 'deepseek-v4-pro')
  assert.equal(credential.routerTiers.c2.model, 'kimi-k2.7-code')
  assert.equal(credential.routerTiers.c3.model, 'glm-5.2')
  assert.match(config, /\[squilla_router\]\nenabled = true/)
  assert.match(config, /\[squilla_router\.tiers\.c1\]\nprovider = "tokenrhythm"\nmodel = "deepseek-v4-pro"/)
  assert.match(config, /\[llm_ensemble\]\nenabled = false/)

  console.log(JSON.stringify({
    ok: true,
    steps: 1,
    provider: credential.provider,
    modelRoutingMode: credential.modelRoutingMode,
    routerMode: credential.routerMode,
    model: credential.model,
    screenshotPath: screenshotPath || null,
  }, null, 2))
} finally {
  await app.close().catch(() => {})
  await rm(userDataRoot, { recursive: true, force: true }).catch(() => {})
}
