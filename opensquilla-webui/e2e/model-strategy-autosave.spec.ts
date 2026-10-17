import { expect, test, type Page, type TestInfo } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { helloOkResponse } from './support/gateway-fixture'

type Mode = 'direct' | 'router' | 'ensemble'
const OUTPUT = process.env.STRATEGY_SCREENSHOTS
const MODEL = 'synthetic-model-one'
const SECOND_MODEL = 'synthetic-model-two'
const methods = [
  'config.get', 'config.effective', 'onboarding.catalog', 'onboarding.status',
  'onboarding.models.discover', 'onboarding.llmProfile.models.discover',
  'models.list', 'models.routing.get', 'models.routing.set', 'models.routing.resetRecommended', 'providers.status',
  'sessions.list', 'agents.list', 'commands.list_for_surface',
]

// Real Settings route and RPC decoder; only the Gateway boundary is synthetic.
async function fixture(page: Page, initialMode: Mode = 'direct') {
  let mode = initialMode
  const config = {
    llm: { provider: 'custom', model: MODEL, base_url: 'https://strategy.example.invalid/v1', api_key_env: 'SYNTHETIC_KEY' },
    squilla_router: {
      enabled: mode === 'router', rollout_phase: 'enforce', preset_binding: 'custom', default_tier: 'c1', visual_mode: 'real_candidates',
      tiers: Object.fromEntries(['c0', 'c1', 'c2', 'c3'].map((name, index) => [name, {
        provider: 'custom', model: index % 2 ? SECOND_MODEL : MODEL, thinking_level: 'off', ensemble_enabled: false,
      }])),
    },
    llm_ensemble: {
      enabled: mode === 'ensemble', selection_mode: 'custom_b5', min_successful_proposers: 2, all_failed_policy: 'fixed',
      candidates: [MODEL, SECOND_MODEL].map(model => ({ provider: 'custom', model, role: 'proposer', enabled: true, source: 'custom' })),
    },
    memory: { auto_capture_enabled: false }, models: {}, permissions: {}, skills: {},
  }
  const requests: Mode[] = []
  let pending: { mode: Mode; respond: (payload: unknown, ok?: boolean) => void } | undefined
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-theme', 'light')
  })
  await page.route('**/api/**', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }))
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
      if (method === 'models.routing.set') {
        requests.push(frame.params.mode)
        pending = { mode: frame.params.mode, respond }
        return
      }
      if (method.includes('discover')) {
        respond({ ok: true, source: 'live', models: [MODEL, SECOND_MODEL].map(id => ({ id, name: id, capabilities: ['chat'] })) })
        return
      }
      const payloads: Record<string, unknown> = {
        'config.get': config,
        'config.effective': { fields: { 'llm.provider': { value: 'custom', source: 'config' } } },
        'onboarding.catalog': { providers: [{
          providerId: 'custom', label: 'Custom (OpenAI)', runtimeSupported: true, acceptsApiKey: true,
          requiresApiKey: true, requiresBaseUrl: true, routerSupported: true, selectableModelCatalog: 'verified_live',
          fields: [{ name: 'model', label: 'Model ID', required: true }, { name: 'baseUrl', label: 'Base URL', required: true }],
        }] },
        'onboarding.status': {
          hasConfig: true, llmConfigured: true, audioConfigured: false,
          llmCredentialStatus: { provider: 'custom', available: true, source: 'env', envKey: 'SYNTHETIC_KEY' },
          sectionDetails: {
            router: { routerBinding: 'custom', enabled: config.squilla_router.enabled },
            ensemble: { configuredAllFailedPolicy: 'fixed', effectiveAllFailedPolicy: 'fixed' },
          },
        },
        'models.routing.get': { mode }, 'models.list': { models: [], errors: [] },
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'sessions.list': { sessions: [], count: 0, ts: 1800000000, has_more: false }, 'usage.status': { sessions: [] },
      }
      respond(payloads[method] ?? {})
    })
  })
  return {
    config, requests,
    release(ok = true) {
      expect(pending, 'the routing write must still be pending').toBeDefined()
      const request = pending!
      pending = undefined
      if (!ok) {
        request.respond({ code: 'INTERNAL_ERROR', message: 'Synthetic routing save failure' }, false)
        return
      }
      mode = request.mode
      config.squilla_router.enabled = mode === 'router'
      config.llm_ensemble.enabled = mode === 'ensemble'
      request.respond({ mode, patched: ['squilla_router.enabled', 'llm_ensemble.enabled'], restart_required: false, selection_mode: 'custom_b5' })
    },
  }
}

const card = (page: Page, mode: Mode) => page.locator(`[data-strategy-id="${mode === 'direct' ? 'single' : mode}"]`)
const dirtybar = (page: Page) => page.locator('.settings-dirtybar')
const tier = (page: Page) => page.locator('select[name="setup_model_strategy_router_default_tier"]')

async function ready(page: Page) {
  await page.goto('/control/settings/modelStrategy')
  await expect(page.locator('.setup-model-strategy')).toBeVisible({ timeout: 20_000 })
  await expect(dirtybar(page)).toHaveCount(0)
  // Wait out only the modal's entrance transform before measuring its geometry.
  await page.locator('.settings-modal').evaluate(async element => {
    await Promise.all(element.getAnimations().map(animation => animation.finished))
  })
}

async function capture(page: Page, info: TestInfo, name: string) {
  if (!OUTPUT) return
  mkdirSync(OUTPUT, { recursive: true })
  const path = join(OUTPUT, `${name}.png`)
  await page.screenshot({ path, animations: 'disabled' })
  await info.attach(name, { path, contentType: 'image/png' })
}

async function samplePending(page: Page, info: TestInfo, name: string, expectedHeight: number, draftText = '') {
  const samples = await page.evaluate(async () => {
    const values = []
    for (let frame = 0; frame < 40; frame++) {
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
      values.push({
        height: document.querySelector('.settings-panel')!.getBoundingClientRect().height,
        footer: document.querySelectorAll('.settings-dirtybar').length,
        text: document.querySelector('.settings-dirtybar__text')?.textContent?.trim() || '',
        dots: document.querySelectorAll('.settings-rail__dirty').length,
        summary: document.querySelectorAll('[data-testid="routing-unsaved"]').length,
      })
    }
    return values
  })
  await info.attach('pending-animation-frames', { body: JSON.stringify(samples, null, 2), contentType: 'application/json' })
  if (OUTPUT) {
    mkdirSync(OUTPUT, { recursive: true })
    writeFileSync(join(OUTPUT, `${name}-frames.json`), JSON.stringify(samples, null, 2))
  }
  expect(samples).toHaveLength(40)
  for (const sample of samples) {
    expect(sample.footer).toBe(draftText ? 1 : 0)
    expect(sample.text).toBe(draftText)
    expect(Math.abs(sample.height - expectedHeight)).toBeLessThanOrEqual(1)
    if (!draftText) {
      expect(sample.dots).toBe(0)
      expect(sample.summary).toBe(0)
    }
  }
}

test.use({ viewport: { width: 1440, height: 900 }, video: OUTPUT ? 'on' : 'off' })
test.setTimeout(90_000)

test('clean slow strategy switches never show manual-save indicators or resize the panel', async ({ page }, info) => {
  const gateway = await fixture(page)
  await ready(page)
  const height = (await page.locator('.settings-panel').boundingBox())!.height
  await capture(page, info, 'clean-before')
  for (const mode of ['router', 'ensemble', 'direct'] as const) {
    await card(page, mode).click()
    await expect.poll(() => gateway.requests.at(-1)).toBe(mode)
    await expect(card(page, mode).locator('input')).toBeChecked()
    await samplePending(page, info, `clean-${mode}`, height)
    await capture(page, info, `clean-${mode}-pending`)
    gateway.release()
    await expect(card(page, mode).locator('input')).toBeEnabled()
    await expect(dirtybar(page)).toHaveCount(0)
    await expect(page.locator('.settings-rail__dirty')).toHaveCount(0)
    await expect(page.getByTestId('routing-unsaved')).toHaveCount(0)
  }
  expect(gateway.requests).toEqual(['router', 'ensemble', 'direct'])
  await capture(page, info, 'clean-after')
})

test('a rejected clean autosave rolls the strategy back without a draft footer', async ({ page }, info) => {
  const gateway = await fixture(page)
  await ready(page)
  const height = (await page.locator('.settings-panel').boundingBox())!.height
  await card(page, 'router').click()
  await expect.poll(() => gateway.requests.length).toBe(1)
  await samplePending(page, info, 'clean-failure', height)
  gateway.release(false)
  await expect(card(page, 'direct').locator('input')).toBeChecked()
  await expect(card(page, 'direct').locator('input')).toBeEnabled()
  await expect(dirtybar(page)).toHaveCount(0)
  await capture(page, info, 'clean-failure-rollback')
})

for (const outcome of ['success', 'failure'] as const) {
  test(`existing manual drafts and footer survive a slow strategy ${outcome}`, async ({ page }, info) => {
    const gateway = await fixture(page, 'router')
    await ready(page)
    await page.locator('#settings-rail-advanced').click()
    await page.locator('input[name="memory_auto_capture"]').check()
    await page.locator('#settings-rail-modelStrategy').click()
    await page.getByTestId('router-advanced-options').locator('summary').click()
    const label = (await tier(page).locator('option[value="c2"]').textContent())!.trim()
    await tier(page).locator('..').getByRole('combobox').click()
    await page.getByRole('option', { name: label, exact: true }).click()
    await expect(tier(page)).toHaveValue('c2')
    await expect(dirtybar(page)).toContainText('Unsaved changes in 2 sections')
    await expect(page.locator('#settings-rail-modelStrategy .settings-rail__dirty')).toHaveCount(1)
    await expect(page.locator('#settings-rail-advanced .settings-rail__dirty')).toHaveCount(1)
    const draftText = (await page.locator('.settings-dirtybar__text').textContent())!.trim()
    const height = (await page.locator('.settings-panel').boundingBox())!.height
    await capture(page, info, `draft-${outcome}-before`)
    await card(page, 'direct').click()
    await expect.poll(() => gateway.requests.length).toBe(1)
    await samplePending(page, info, `draft-${outcome}`, height, draftText)
    await expect(dirtybar(page).getByRole('button', { name: /^Save/ })).toBeDisabled()
    await capture(page, info, `draft-${outcome}-pending`)
    gateway.release(outcome === 'success')
    await expect(card(page, outcome === 'success' ? 'direct' : 'router').locator('input')).toBeChecked()
    await expect(card(page, 'router').locator('input')).toBeEnabled()
    await expect(page.locator('.settings-dirtybar__text')).toHaveText(draftText)
    if (outcome === 'success') {
      await card(page, 'router').click()
      await expect.poll(() => gateway.requests.length).toBe(2)
      gateway.release()
      await expect(card(page, 'router').locator('input')).toBeEnabled()
    }
    await page.getByTestId('router-advanced-options').locator('summary').click()
    await expect(tier(page)).toHaveValue('c2')
    await expect(page.getByTestId('routing-unsaved')).toBeVisible()
    expect(gateway.config.squilla_router.default_tier).toBe('c1')
    await capture(page, info, `draft-${outcome}-after`)
    await page.locator('#settings-rail-advanced').click()
    await expect(page.locator('input[name="memory_auto_capture"]')).toBeChecked()
    expect(gateway.config.memory.auto_capture_enabled).toBe(false)
    await expect(dirtybar(page).getByRole('button', { name: /^Save/ })).toBeEnabled()
  })
}
