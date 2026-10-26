import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'

const primaryModel = 'synthetic-primary-model'
const foreignModel = 'synthetic/selected-model'
type Tier = { provider: string; model: string; ensemble_enabled?: boolean }

// Exercise the real Settings UI and wire decoder against an offline Gateway.
// Backend persistence and HTTP endpoint/key selection are covered separately
// by test_router_explicit_provider_save.py.
async function installGateway(page: Page, flag: boolean | undefined, legacy: boolean) {
  const router = {
    enabled: true, rollout_phase: 'full', preset_binding: 'custom',
    default_tier: 'c1', visual_mode: 'real_candidates',
    ...(flag === undefined ? {} : { cross_provider_tiers: flag }),
    tiers: Object.fromEntries(['c0', 'c1', 'c2', 'c3'].map(name => [name, {
      provider: legacy && name === 'c3' ? 'openrouter' : 'tokenrhythm',
      model: legacy && name === 'c3' ? 'synthetic/old-model' : primaryModel,
      ensemble_enabled: false,
    }])) as Record<string, Tier>,
  }
  const config = {
    llm: { provider: 'tokenrhythm', model: primaryModel },
    llm_profiles: { openrouter: { model: foreignModel } },
    squilla_router: router,
    llm_ensemble: { enabled: false, selection_mode: 'custom_b5', candidates: [] },
    memory: {}, models: {}, permissions: {}, skills: {},
  }
  const writes: Record<string, unknown>[] = []
  let rejectNextSave = false
  const methods = [
    'config.get', 'config.effective', 'onboarding.catalog', 'onboarding.status',
    'onboarding.models.discover', 'onboarding.llmProfile.models.discover',
    'onboarding.router.configure', 'models.list', 'models.routing.get',
    'providers.status', 'sessions.list', 'agents.list', 'commands.list_for_surface',
  ]
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-theme', 'light')
  })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      const method = frame.method as string
      const respond = (payload: unknown, ok = true) => ws.send(JSON.stringify({
        type: 'res', id: frame.id, ok, ...(ok ? { payload } : { error: payload }),
      }))
      if (method === 'connect') {
        ws.send(helloOkResponse({ features: { methods, events: [] } }))
        return
      }
      if (method === 'onboarding.router.configure') {
        writes.push(frame.params)
        if (rejectNextSave) {
          rejectNextSave = false
          respond({ code: 'INTERNAL_ERROR', message: 'Synthetic save failure' }, false)
          return
        }
        if (frame.params.crossProviderTiers !== true) {
          respond({ code: 'ROUTER_PROVIDER_CONFLICT', message: 'Cross-provider execution was not selected' }, false)
          return
        }
        router.cross_provider_tiers = true
        router.tiers = structuredClone(frame.params.tiers)
        respond({ changed: true, restartRequired: false, configPath: '/synthetic/config.toml', entry: router, warnings: [] })
        return
      }
      if (method.includes('discover')) {
        const foreign = frame.params?.providerId === 'openrouter'
        respond({ ok: true, source: 'live', models: (foreign ? [foreignModel, 'synthetic/old-model'] : [primaryModel])
          .map(id => ({ id, name: id, capabilities: ['chat'] })) })
        return
      }
      const payloads: Record<string, unknown> = {
        'config.get': config,
        'config.effective': { fields: { 'llm.provider': { value: 'tokenrhythm', source: 'config' } } },
        'onboarding.catalog': { providers: ['tokenrhythm', 'openrouter'].map(providerId => ({
          providerId, label: providerId === 'tokenrhythm' ? 'TokenRhythm' : 'OpenRouter',
          runtimeSupported: true, requiresApiKey: true, routerSupported: true,
          selectableModelCatalog: 'verified_live', fields: [{ name: 'model', label: 'Model', required: true }],
        })) },
        'onboarding.status': {
          hasConfig: true, llmConfigured: true,
          llmCredentialStatus: { provider: 'tokenrhythm', available: true, source: 'explicit' },
          llmProfileStatus: [{ provider: 'openrouter', ready: true, credentialSource: 'profile', primaryEligible: true }],
          sectionDetails: { router: { enabled: true, routerBinding: 'custom' } },
        },
        'models.routing.get': { mode: 'router' }, 'models.list': { models: [], errors: [] },
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'sessions.list': { sessions: [], count: 0, ts: 1800000000, has_more: false },
      }
      respond(payloads[method] ?? {})
    })
  })
  return { router, writes, rejectNext: () => { rejectNextSave = true } }
}

async function selectForeignModel(page: Page, legacy: boolean) {
  if (!legacy) {
    await page.getByRole('combobox', { name: 'c3 request entry', exact: true }).click()
    await page.getByRole('option', { name: 'OpenRouter', exact: true }).click()
  }
  const model = page.getByRole('combobox', { name: 'C3 processing mode or model', exact: true })
  await model.click()
  await page.getByRole('option', { name: foreignModel, exact: true }).click()
}

test.use({ viewport: { width: 1440, height: 1000 } })

for (const scenario of [
  { name: 'new installation with missing flag', flag: undefined, legacy: false },
  { name: 'upgraded disabled flag', flag: false, legacy: true },
  { name: 'upgraded enabled flag', flag: true, legacy: true },
]) {
  test(`${scenario.name} saves selected providers and retains them after reload`, async ({ page }, info) => {
    const gateway = await installGateway(page, scenario.flag, scenario.legacy)
    await page.goto('/control/settings/modelStrategy')
    await expect(page.locator('.setup-model-strategy')).toBeVisible()
    await expect(page.locator('.settings-dirtybar')).toHaveCount(0)
    expect(gateway.writes).toHaveLength(0)
    await selectForeignModel(page, scenario.legacy)
    await page.locator('.settings-dirtybar').getByRole('button', { name: /^Save/ }).click()
    await expect(page.locator('.settings-dirtybar')).toHaveCount(0)
    expect(gateway.writes).toHaveLength(1)
    expect(gateway.writes[0]).toMatchObject({
      crossProviderTiers: true,
      tiers: { c0: { provider: 'tokenrhythm', model: primaryModel }, c3: { provider: 'openrouter', model: foreignModel } },
    })
    await page.reload()
    await expect(page.getByTestId('routing-cross-provider-enabled')).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'c3 request entry', exact: true })).toContainText('OpenRouter')
    await expect(page.getByRole('combobox', { name: 'C3 processing mode or model', exact: true })).toHaveValue(foreignModel)
    await expect(page.locator('.settings-dirtybar')).toHaveCount(0)
    expect(gateway.writes).toHaveLength(1)
    await page.screenshot({ path: info.outputPath('saved-providers.png'), animations: 'disabled' })
  })
}

test('a rejected save keeps the selected providers available for retry', async ({ page }) => {
  const gateway = await installGateway(page, false, false)
  await page.goto('/control/settings/modelStrategy')
  await expect(page.locator('.setup-model-strategy')).toBeVisible()
  await selectForeignModel(page, false)
  gateway.rejectNext()
  const save = page.locator('.settings-dirtybar').getByRole('button', { name: /^Save/ })
  await save.click()
  await expect.poll(() => gateway.writes.length).toBe(1)
  await expect(save).toBeEnabled()
  await expect(page.getByRole('combobox', { name: 'C3 processing mode or model', exact: true })).toHaveValue(foreignModel)
  expect(gateway.router.cross_provider_tiers).toBe(false)
  await save.click()
  await expect(page.locator('.settings-dirtybar')).toHaveCount(0)
  expect(gateway.writes).toHaveLength(2)
  expect(gateway.router.cross_provider_tiers).toBe(true)
})
