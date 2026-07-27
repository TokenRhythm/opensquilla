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
      const hasSetupForm = await page.locator('#setup-form').count().catch(() => 0)
      if (hasSetupForm > 0) return page
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

  await page.locator('#onboardingLocale').selectOption('zh-Hans')
  assert.equal(await page.evaluate(() => document.documentElement.lang), 'zh-Hans')
  assert.equal(await page.locator('[data-screen="0"] h2').innerText(), '选择设置深度')
  assert.equal(await page.title(), '设置 OpenSquilla')
  assert.doesNotMatch(await page.locator('[data-setup-mode="advanced"]').innerText(), /Smart Router mode/)
  await page.locator('[data-setup-mode="advanced"]').click()

  await page.locator('[data-screen="0"].active .next-button').click()
  await page.locator('[data-screen="1"].active').waitFor({ state: 'visible', timeout: 10_000 })
  assert.equal(await page.locator('[data-step-label="2"]').count(), 1, 'advanced setup should expose the routing-mode progress step')

  assert.equal(await page.locator('#provider').inputValue(), 'tokenrhythm')
  assert.equal(await page.locator('#baseUrl').inputValue(), 'https://tokenrhythm.studio/v1')
  assert.equal(await page.locator('#model').inputValue(), 'deepseek-v4-pro')
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.equal(await page.locator('#routerMode').inputValue(), 'recommended')

  const tokenRhythmFeature = page.locator('[data-provider-feature="tokenrhythm"]')
  assert.equal(await tokenRhythmFeature.count(), 1)
  assert.equal(await tokenRhythmFeature.locator('[data-tokenrhythm-title]').innerText(), '推荐使用 TokenRhythm')
  assert.equal(
    await tokenRhythmFeature.locator('[data-tokenrhythm-value]').innerText(),
    'TokenRhythm API 调用限时免费。',
  )
  assert.equal(
    await tokenRhythmFeature.locator('[data-tokenrhythm-registration]').innerText(),
    '活动期间，注册并获取 API Key，即可免费调用 DeepSeek、GLM、MiniMax、Kimi 等主流模型。',
  )
  const tokenRhythmCta = tokenRhythmFeature.locator('#tokenrhythmRegister')
  assert.equal(await tokenRhythmCta.innerText(), '注册并获取 API Key')
  assert.equal(await tokenRhythmCta.getAttribute('href'), 'https://tokenrhythm.studio/register')
  assert.equal(await tokenRhythmCta.getAttribute('target'), '_blank')
  assert.equal(await tokenRhythmCta.getAttribute('rel'), 'noopener noreferrer')
  assert.equal(
    await tokenRhythmCta.getAttribute('aria-label'),
    '注册并获取 API Key — TokenRhythm（在外部浏览器中打开）',
  )
  assert.equal(await tokenRhythmFeature.locator('img, svg, canvas').count(), 0)
  assert.equal(await tokenRhythmFeature.locator('[data-provider="tokenrhythm"]').getAttribute('aria-pressed'), 'true')

  const providerMoreToggle = page.locator('#providerMoreToggle')
  const providerMorePanel = page.locator('#providerMorePanel')
  assert.equal(await providerMoreToggle.getAttribute('aria-expanded'), 'false')
  assert.equal(await providerMoreToggle.getAttribute('aria-controls'), 'providerMorePanel')
  assert.equal(await providerMorePanel.isHidden(), true)

  await page.locator('#onboardingLocale').selectOption('en')
  assert.equal(await page.evaluate(() => document.documentElement.lang), 'en')
  assert.equal(await page.locator('[data-screen="1"] h2').innerText(), 'Connect a provider')
  assert.equal(await page.locator('#provider').inputValue(), 'tokenrhythm', 'locale changes should preserve the selected provider')
  assert.equal(await tokenRhythmFeature.locator('[data-tokenrhythm-title]').innerText(), 'Recommended: TokenRhythm')
  assert.equal(
    await tokenRhythmFeature.locator('[data-tokenrhythm-value]').innerText(),
    'TokenRhythm API calls are free for a limited time.',
  )
  assert.equal(
    await tokenRhythmFeature.locator('[data-tokenrhythm-registration]').innerText(),
    'During the promotion, register and get an API key to call DeepSeek, GLM, MiniMax, Kimi, and other leading models for free.',
  )
  assert.equal(await tokenRhythmCta.innerText(), 'Register and get an API key')
  assert.equal(
    await tokenRhythmCta.getAttribute('aria-label'),
    'Register and get an API key — TokenRhythm (opens in external browser)',
  )

  await providerMoreToggle.click()
  assert.equal(await providerMoreToggle.getAttribute('aria-expanded'), 'true')
  assert.equal(await providerMorePanel.isVisible(), true)
  const openRouterProvider = page.locator('#providerGrid [data-provider="openrouter"]')
  await openRouterProvider.click()
  assert.equal(await page.locator('#provider').inputValue(), 'openrouter')
  assert.equal(await openRouterProvider.getAttribute('aria-pressed'), 'true')
  assert.equal(await tokenRhythmFeature.locator('[data-provider="tokenrhythm"]').getAttribute('aria-pressed'), 'false')
  await page.locator('#onboardingLocale').selectOption('zh-Hans')
  assert.equal(await page.locator('#provider').inputValue(), 'openrouter', 'locale changes should preserve another provider selection')
  await page.locator('#onboardingLocale').selectOption('en')
  assert.equal(await page.locator('#provider').inputValue(), 'openrouter')
  await tokenRhythmFeature.locator('[data-provider="tokenrhythm"]').click()
  assert.equal(await page.locator('#provider').inputValue(), 'tokenrhythm', 'TokenRhythm should remain re-selectable')
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.equal(await tokenRhythmFeature.locator('[data-provider="tokenrhythm"]').getAttribute('aria-pressed'), 'true')
  await page.locator('#onboardingLocale').selectOption('zh-Hans')
  await page.locator('#apiKey').fill('synthetic-tokenrhythm-key')
  await page.locator('[data-screen="1"].active .next-button').click()
  await page.locator('[data-screen="2"].active').waitFor({ state: 'visible', timeout: 10_000 })
  await page.waitForTimeout(300)
  assert.equal(await page.locator('[data-screen="2"] h2').innerText(), '选择路由模式')
  assert.equal(await page.locator('[data-model-routing-mode="squilla_router"]').isEnabled(), true)
  assert.equal(await page.locator('[data-model-routing-mode="direct"]').isEnabled(), true)
  assert.equal(await page.locator('[data-model-routing-mode="llm_ensemble"]').isEnabled(), true)
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.match(
    await page.locator('[data-model-routing-mode="squilla_router"] small').innerText(),
    /此提供商现有的 Squilla Router 层级默认值/,
  )
  assert.match(
    await page.locator('[data-model-routing-mode="llm_ensemble"] small').innerText(),
    /当前提供商的 static B5 Ensemble/,
  )
  if (screenshotPath) {
    await mkdir(dirname(screenshotPath), { recursive: true })
    await page.screenshot({ path: screenshotPath })
  }
  await page.locator('[data-screen="2"].active .next-button').click()
  await page.locator('[data-screen="3"].active').waitFor({ state: 'visible', timeout: 10_000 })
  assert.equal(await page.locator('[data-screen="3"] h2').innerText(), '候选模型池')
  const tokenRhythmTierText = await page.locator('#tierBody').innerText()
  for (const modelId of ['deepseek-v4-flash', 'deepseek-v4-pro', 'kimi-k2.7-code', 'glm-5.2', 'kimi-k2.6']) {
    assert.match(tokenRhythmTierText, new RegExp(modelId.replaceAll('.', '\\.')))
  }
  await page.locator('[data-screen="3"].active .back-button').click()
  await page.locator('[data-screen="2"].active').waitFor({ state: 'visible', timeout: 5_000 })
  await page.locator('[data-model-routing-mode="direct"]').click()
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'direct')
  assert.equal(await page.locator('#directModelRoute').inputValue(), 'deepseek-v4-pro')
  await page.locator('#directModelRoute').fill('glm-5.2')
  assert.equal(await page.locator('#model').inputValue(), 'glm-5.2')
  await page.locator('[data-model-routing-mode="llm_ensemble"]').click()
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'llm_ensemble')
  await page.locator('[data-screen="2"].active .next-button').click()
  await page.locator('[data-screen="4"].active').waitFor({ state: 'visible', timeout: 10_000 })
  await page.locator('[data-screen="4"].active .back-button').click()
  await page.locator('[data-screen="2"].active').waitFor({ state: 'visible', timeout: 5_000 })
  await page.locator('[data-screen="2"].active .back-button').click()
  await page.locator('[data-screen="1"].active').waitFor({ state: 'visible', timeout: 5_000 })
  await page.locator('#onboardingLocale').selectOption('en')

  await page.locator('#providerGrid [data-provider="ollama"]').click()
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'direct')
  assert.equal(await page.locator('#routerMode').inputValue(), 'disabled')
  assert.equal(await page.locator('#model').inputValue(), '', 'direct-only providers without a default model should not inherit the previous provider model')
  assert.equal(await page.locator('#endpointToggle').getAttribute('aria-expanded'), 'true', 'direct-only providers that need a model should open the endpoint panel')
  await page.locator('[data-screen="1"].active .next-button').click()
  await page.locator('[data-screen="2"].active').waitFor({ state: 'visible', timeout: 5_000 })
  assert.equal(await page.locator('[data-model-routing-mode="squilla_router"]').isDisabled(), true)
  assert.equal(await page.locator('[data-model-routing-mode="direct"]').isEnabled(), true)
  assert.equal(await page.locator('[data-model-routing-mode="llm_ensemble"]').isDisabled(), true)
  assert.equal(await page.locator('[data-step-label="3"]').isVisible(), false, 'route-excluded tier step should be hidden from the progress rail')
  await page.locator('[data-screen="2"].active .next-button').click()
  await page.locator('[data-screen="2"].active').waitFor({ state: 'visible', timeout: 5_000 })
  assert.match(await page.locator('#error').innerText(), /Direct model is required/)
  await page.locator('[data-screen="2"].active .back-button').click()
  await page.locator('[data-screen="1"].active').waitFor({ state: 'visible', timeout: 5_000 })

  await page.locator('#providerGrid [data-provider="openai"]').click()

  assert.equal(await page.locator('#provider').inputValue(), 'openai')
  assert.equal(await page.locator('#baseUrl').inputValue(), 'https://api.openai.com/v1')
  assert.equal(await page.locator('#model').inputValue(), 'gpt-5.4-mini')
  await page.locator('#providerGrid [data-provider="openai"].active').waitFor({ state: 'visible', timeout: 5_000 })
  const openAiHint = await page.locator('#providerHint').innerText()
  assert.match(openAiHint, /OpenAI-only tier profile/)
  assert.doesNotMatch(openAiHint, /OPENAI_API_KEY/)

  await page.locator('#providerGrid [data-provider="bailian_coding_cn"]').click()
  assert.equal(await page.locator('#provider').inputValue(), 'bailian_coding_cn')
  assert.equal(
    await page.locator('#baseUrl').inputValue(),
    'https://coding.dashscope.aliyuncs.com/v1',
  )
  assert.equal(await page.locator('#model').inputValue(), 'qwen3.7-plus')
  assert.match(await page.locator('#providerHint').innerText(), /dedicated sk-sp- API key/)

  await page.locator('#providerGrid [data-provider="openai"]').click()
  await page.locator('#apiKey').fill('test-openai-key')
  await page.locator('[data-screen="1"].active .next-button').click()
  await page.locator('[data-screen="2"].active').waitFor({ state: 'visible', timeout: 10_000 })
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.equal(await page.locator('[data-model-routing-mode="squilla_router"]').isEnabled(), true)
  assert.equal(await page.locator('[data-model-routing-mode="direct"]').isEnabled(), true)
  assert.equal(await page.locator('[data-model-routing-mode="llm_ensemble"]').isDisabled(), true)
  await page.locator('[data-screen="2"].active .next-button').click()
  await page.locator('[data-screen="3"].active').waitFor({ state: 'visible', timeout: 10_000 })
  assert.match(await page.locator('[data-screen="3"] .eyebrow').innerText(), /step 04/i)
  assert.equal(await page.locator('[data-screen="3"] h2').innerText(), 'Review tier models')

  await page.locator('[data-screen="3"].active .back-button').click()
  await page.locator('[data-screen="2"].active').waitFor({ state: 'visible', timeout: 5_000 })
  await page.locator('[data-screen="2"].active .back-button').click()
  await page.locator('[data-screen="1"].active').waitFor({ state: 'visible', timeout: 5_000 })
  await tokenRhythmFeature.locator('[data-provider="tokenrhythm"]').click()
  await page.locator('#apiKey').fill('synthetic-tokenrhythm-key')
  await page.locator('[data-screen="1"].active .next-button').click()
  await page.locator('[data-screen="2"].active').waitFor({ state: 'visible', timeout: 5_000 })
  await page.locator('[data-model-routing-mode="llm_ensemble"]').click()
  await page.locator('[data-screen="2"].active .next-button').click()
  await page.locator('[data-screen="4"].active').waitFor({ state: 'visible', timeout: 5_000 })
  await page.locator('#finish').click()
  const saved = await waitFor(async () => {
    const credential = JSON.parse(await readFile(join(userDataDir, 'desktop-credential.json'), 'utf8'))
    if (credential.modelRoutingMode !== 'llm_ensemble') return null
    const config = await readFile(join(userDataDir, 'opensquilla', 'config.toml'), 'utf8')
    return { credential, config }
  }, 'saved ensemble credential and config')
  const { credential, config } = saved
  assert.equal(credential.provider, 'tokenrhythm')
  assert.equal(credential.modelRoutingMode, 'llm_ensemble')
  assert.equal(credential.routerMode, 'recommended')
  assert.equal(credential.routerTiers.c0.model, 'deepseek-v4-flash')
  assert.equal(credential.routerTiers.c1.model, 'deepseek-v4-pro')
  assert.equal(credential.routerTiers.c2.model, 'kimi-k2.7-code')
  assert.equal(credential.routerTiers.c3.model, 'glm-5.2')
  assert.equal(credential.routerTiers.image_model.model, 'kimi-k2.6')
  assert.match(config, /\[squilla_router\]\nenabled = true/)
  assert.doesNotMatch(config, /tier_profile = "tokenrhythm"/)
  assert.match(config, /\[squilla_router\.tiers\.c0\]\nprovider = "tokenrhythm"\nmodel = "deepseek-v4-flash"/)
  assert.match(config, /\[squilla_router\.tiers\.c3\]\nprovider = "tokenrhythm"\nmodel = "glm-5\.2"/)
  assert.match(config, /\[llm_ensemble\]\nenabled = true\nselection_mode = "static_tokenrhythm_b5"/)

  console.log(JSON.stringify({
    ok: true,
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
