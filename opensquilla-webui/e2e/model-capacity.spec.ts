import { expect, test, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { helloOkResponse } from './support/gateway-fixture';
const MODEL = 'example.vendor/unknown-model.v1:latest';
const KNOWN = 'example.vendor/catalog-model.v2:latest';
const OUTPUT = process.env.CAPACITY_SCREENSHOTS;
const BASELINE = process.env.CAPACITY_BASELINE === '1';
const methods = ['config.get', 'config.effective', 'config.patch', 'config.patch.safe', 'onboarding.catalog', 'onboarding.status', 'onboarding.models.discover', 'onboarding.llmProfile.models.discover', 'onboarding.llmProfile.draft.models.discover', 'onboarding.llmProfile.upsert', 'onboarding.llmProfile.upsertAndActivate', 'onboarding.llmProfile.activate', 'models.list', 'models.capacity.resolve', 'providers.status', 'models.routing.get', 'models.routing.set', 'onboarding.router.configure', 'onboarding.ensemble.configure', 'sessions.list', 'agents.list', 'commands.list_for_surface'];
async function fixture(page: Page, mode: string, locale: string, theme: string) {
    const config: Record<string, any> = {
        llm: { provider: 'custom', model: MODEL, base_url: 'https://capacity.example.invalid/v1', api_key_env: 'SYNTHETIC_MODEL_KEY' },
        llm_profiles: { custom_anthropic: { model: MODEL, base_url: 'https://anthropic.example.invalid/v1', api_key_env: 'SYNTHETIC_ANTHROPIC_KEY' } },
        squilla_router: { enabled: mode === 'router', rollout_phase: 'enforce', preset_binding: 'custom', default_tier: 'c1', visual_mode: 'real_candidates', tiers: Object.fromEntries(['c0', 'c1', 'c2', 'c3'].map((name, i) => [name, { provider: i === 2 ? 'custom_anthropic' : 'custom', model: i === 1 ? KNOWN : MODEL, thinking_level: 'off', ensemble_enabled: i === 3, ensemble_selection_mode: i === 3 ? 'custom_b5' : '' }])) },
        llm_ensemble: { enabled: mode === 'ensemble', selection_mode: 'custom_b5', min_successful_proposers: 2, all_failed_policy: 'fixed', candidates: [['custom', MODEL], ['custom_anthropic', MODEL], ['custom', KNOWN]].map(([provider, model]) => ({ provider, model, role: 'proposer', enabled: true, source: 'custom' })) }, models: {}, permissions: {}, skills: {},
    };
    const writes: any[] = [], calls: string[] = [];
    const state = { reject: false, delayed: false, release: () => { } };
    await page.addInitScript(({ locale, theme }) => { localStorage.setItem('opensquilla-locale', locale); localStorage.setItem('opensquilla-theme', theme); }, { locale, theme });
    await page.route('**/api/**', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
    await page.routeWebSocket(/\/ws$/, ws => {
        ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }));
        ws.onMessage(async (message) => {
            const frame = JSON.parse(String(message));
            if (frame.type !== 'req')
                return;
            const method = frame.method as string;
            calls.push(method);
            const respond = (payload: unknown, ok = true) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok, ...(ok ? { payload } : { error: payload }) }));
            if (method === 'connect') {
                ws.send(helloOkResponse({ features: { methods, events: [] }, auth: { scopes: ['operator.read', 'operator.write', 'operator.admin'], runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' } } }));
                return;
            }
            if (method === 'models.capacity.resolve') {
                respond({ models: frame.params.models.map((item: any) => {
                        const override = config.models[item.provider]?.[item.model] || {}, known = item.model === KNOWN;
                        const limit = (field: string, automatic: number) => ({ automatic, automaticSource: known ? 'catalog' : 'default', override: override[field] ?? null, value: override[field] ?? automatic, source: override[field] ? 'override' : known ? 'catalog' : 'default', editable: true });
                        return { ...item, contextWindow: limit('context_window', known ? 131072 : 8192), maxOutputTokens: limit('max_output_tokens', 16384), localRuntime: false };
                    }) });
                return;
            }
            if (method === 'config.patch') {
                writes.push(frame.params);
                if (state.delayed)
                    await new Promise<void>(resolve => { state.release = resolve; });
                if (state.reject) {
                    respond({ code: 'INTERNAL_ERROR', message: 'Synthetic save failure' }, false);
                    return;
                }
                for (const [provider, models] of Object.entries(frame.params.patch?.models || {}) as any) {
                    config.models[provider] ||= {};
                    for (const [model, fields] of Object.entries(models) as any) {
                        config.models[provider][model] ||= {};
                        for (const [field, value] of Object.entries(fields))
                            if (value === null)
                                delete config.models[provider][model][field];
                            else
                                config.models[provider][model][field] = value;
                    }
                }
                respond({ changed: true, restartRequired: false, patched: ['models'] });
                return;
            }
            if (method.includes('discover')) {
                respond({ ok: true, failureKind: '', detail: '', source: 'live', models: [MODEL, KNOWN].map(id => ({ id, name: id, contextWindow: id === KNOWN ? 131072 : 8192, maxOutputTokens: 16384, capabilities: ['chat'], pricing: null, capabilitySource: id === KNOWN ? 'catalog' : 'synthesized' })) });
                return;
            }
            const payloads: Record<string, unknown> = {
                'config.get': config, 'config.effective': { fields: { 'llm.provider': { value: 'custom', source: 'config' } } },
                'onboarding.catalog': { providers: ['custom', 'custom_anthropic'].map(providerId => ({ providerId, label: providerId === 'custom' ? 'Custom (OpenAI)' : 'Custom (Anthropic)', runtimeSupported: true, acceptsApiKey: true, requiresApiKey: true, requiresBaseUrl: true, routerSupported: true, selectableModelCatalog: 'verified_live', fields: [{ name: 'model', label: 'Model ID', required: true }, { name: 'baseUrl', label: 'Base URL', required: true }] })) },
                'onboarding.status': { hasConfig: true, llmConfigured: true, audioConfigured: false, llmCredentialStatus: { provider: 'custom', available: true, source: 'env', envKey: 'SYNTHETIC_MODEL_KEY' }, llmProfileStatus: [{ provider: 'custom_anthropic', ready: true, primaryEligible: true, primaryBlockReason: '', credentialSource: 'profile' }], sectionDetails: { router: { routerBinding: 'custom', enabled: mode === 'router' }, ensemble: { configuredAllFailedPolicy: 'fixed', effectiveAllFailedPolicy: 'fixed' } } },
                'models.list': { models: [], errors: [] }, 'models.routing.get': { mode: mode === 'single' ? 'direct' : mode }, 'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] }, 'sessions.list': { sessions: [], count: 0, ts: 1800000000, has_more: false }, 'usage.status': { sessions: [] },
            };
            respond(payloads[method] ?? {});
        });
    });
    return { config, writes, calls, state };
}
async function shot(page: Page, name: string) { if (OUTPUT) {
    mkdirSync(OUTPUT, { recursive: true });
    await page.screenshot({ animations: 'disabled', path: join(OUTPUT, `${BASELINE ? 'before' : 'after'}-${name}.png`) });
} }
async function ready(page: Page) { await page.goto('/control/settings/modelStrategy'); await expect(page.locator('.setup-model-strategy')).toBeVisible({ timeout: 20000 }); }
for (const mode of ['single', 'router', 'ensemble'])
    for (const [width, height] of [[1440, 900], [1024, 768], [390, 844]])
        for (const [locale, theme] of [['en', 'light'], ['en', 'dark'], ['zh-Hans', 'light'], ['zh-Hans', 'dark']]) {
            test(`layout ${mode} ${width} ${locale} ${theme}`, async ({ page }) => {
                await page.setViewportSize({ width: width!, height: height! });
                const { calls } = await fixture(page, mode, locale!, theme!);
                await ready(page);
                if (!BASELINE)
                    await expect(page.locator('.model-capacity-warning')).toHaveCount(0);
                await shot(page, `${mode}-${width}-${locale}-${theme}`);
                if (BASELINE)
                    return; // Baseline captures are evidence; current-code checks run below.
                const overflow = await page.locator('.settings-panel').evaluate(el => el.scrollWidth - el.clientWidth);
                if (overflow > 1)
                    console.log(await page.locator('.settings-panel').evaluate(el => Array.from(el.querySelectorAll<HTMLElement>('*')).filter(node => node.getBoundingClientRect().right > el.getBoundingClientRect().right + 1).slice(0, 12).map(node => ({ class: node.className, width: node.clientWidth, scroll: node.scrollWidth }))));
                expect(overflow).toBeLessThanOrEqual(1);
                if (mode === 'router')
                    expect(await page.locator('.setup-tier-table__row.is-head').innerText()).not.toMatch(/capacity|容量/i);
                const gear = page.locator('.model-capacity-trigger').first();
                await gear.scrollIntoViewIfNeeded();
                await gear.focus();
                await page.keyboard.press('Enter');
                const dialog = page.locator('.model-capacity-dialog');
                await expect(dialog).toBeVisible();
                await expect(dialog.locator('input').first()).toBeVisible();
                for (const control of await dialog.locator('.model-capacity-fields__control').all()) {
                    const inputBounds = await control.locator('input').boundingBox();
                    const unitBounds = await control.locator('.model-capacity-fields__unit').boundingBox();
                    expect(unitBounds!.x).toBeGreaterThan(inputBounds!.x);
                    expect(unitBounds!.x + unitBounds!.width).toBeLessThan(inputBounds!.x + inputBounds!.width);
                }
                const bounds = await dialog.boundingBox();
                expect(bounds!.x).toBeGreaterThanOrEqual(0);
                expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width! + 1);
                await shot(page, `editor-${mode}-${width}-${locale}-${theme}`);
                await page.keyboard.press('Escape');
                await expect(dialog).toBeHidden();
                await expect(gear).toBeFocused();
                expect(calls.filter(method => method === 'models.capacity.resolve').length).toBeLessThanOrEqual(4);
            });
        }
test('draft errors cancel save failure retry restore and keyboard', async ({ page }) => {
    test.skip(BASELINE);
    const { config, writes, calls, state } = await fixture(page, 'single', 'en', 'light');
    await page.setViewportSize({ width: 1440, height: 900 });
    await ready(page);
    const gear = page.locator('.model-capacity-trigger').first();
    await gear.click();
    const dialog = page.locator('.model-capacity-dialog');
    const context = dialog.getByLabel('Context window', { exact: true }), output = dialog.getByLabel('Maximum output length', { exact: true }), done = dialog.getByRole('button', { name: 'Done', exact: true });
    await context.fill('-1');
    await expect(done).toBeDisabled();
    await shot(page, 'invalid-input');
    await context.fill('262144');
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await gear.click();
    await expect(context).toHaveValue('');
    await context.fill('262144');
    await output.fill('65536');
    await done.click();
    await shot(page, 'unsaved');
    state.reject = true;
    state.delayed = true;
    const save = page.locator('.settings-dirtybar .btn--primary');
    await save.click();
    await expect(save).toBeDisabled();
    expect(writes).toHaveLength(1);
    state.release();
    state.delayed = false;
    await expect(save).toBeEnabled();
    await gear.click();
    await expect(context).toHaveValue('262144');
    await shot(page, 'save-failed-retained');
    await done.click();
    state.reject = false;
    await save.click();
    await expect(save).toBeHidden();
    expect(config.models.custom[MODEL]).toEqual({ context_window: 262144, max_output_tokens: 65536 });
    await gear.click();
    await expect(context).toHaveValue('262144');
    await shot(page, 'manual-saved');
    await dialog.getByRole('button', { name: 'Restore automatic values', exact: true }).click();
    await done.click();
    await save.click();
    await expect(save).toBeHidden();
    expect(config.models.custom[MODEL]).toEqual({});
    expect(calls.some(method => method.includes('configure') || method.includes('activate'))).toBe(false);
});
test('C3 opens shared lineup without changing routing mode', async ({ page }) => {
    test.skip(BASELINE);
    const { calls } = await fixture(page, 'router', 'en', 'light');
    await ready(page);
    const entry = page.getByTestId('tier-edit-shared-ensemble');
    await entry.click();
    await expect(page.getByTestId('ensemble-panel')).toBeVisible();
    await shot(page, 'router-shared-lineup');
    await page.getByRole('button', { name: 'Back to smart routing' }).click();
    await expect(entry).toBeFocused();
    expect(calls.some(method => method.includes('configure') || method === 'models.routing.set')).toBe(false);
});
for (const width of [1440, 390])
    for (const provider of ['custom', 'custom_anthropic']) {
        test(`provider capacity ${provider} ${width}`, async ({ page }) => {
            await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
            const { config, writes, calls } = await fixture(page, 'single', 'en', 'light');
            await page.goto('/control/settings/provider');
            const entry = page.locator(`[data-provider-id="${provider}"] .setup-provider-card__select`);
            await expect(entry).toBeVisible();
            await entry.click();
            const modal = page.locator('.setup-provider-modal');
            await expect(modal).toBeVisible();
            await shot(page, `provider-${provider}-${width}-collapsed`);
            if (BASELINE)
                return;
            const disclosure = modal.locator('.model-capacity-disclosure');
            await expect(disclosure).not.toHaveAttribute('open');
            await disclosure.locator('summary').click();
            const context = disclosure.getByLabel('Context window', { exact: true });
            await context.scrollIntoViewIfNeeded();
            await expect(context).toBeVisible();
            await shot(page, `provider-${provider}-${width}-expanded`);
            await context.fill('262144');
            const save = modal.locator('.setup-provider-modal__footer .btn--primary');
            await expect(save).toBeEnabled();
            await save.click();
            await expect.poll(() => writes.length).toBe(1);
            await expect.poll(() => config.models[provider]?.[MODEL]?.context_window).toBe(262144);
            expect(config.llm.provider).toBe('custom');
            expect(calls.some(method => method.includes('activate') || method.includes('configure'))).toBe(false);
        });
    }
test('capacity error target opens exact provider model editor and catalog is automatic', async ({ page }) => {
    test.skip(BASELINE);
    const { calls } = await fixture(page, 'router', 'en', 'light');
    await page.goto(`/control/settings/modelStrategy?capacityProvider=custom_anthropic&capacityModel=${encodeURIComponent(MODEL)}`);
    const dialog = page.locator('.model-capacity-dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(`custom_anthropic · ${MODEL}`);
    await expect(dialog).toContainText('System default');
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(page.locator('#settings-section-modelStrategy > .model-capacity-trigger')).toHaveCount(0);
    const known = page.locator('.setup-tier-table__row').filter({ has: page.locator(`input[value="${KNOWN}"]`) });
    await known.locator('.model-capacity-trigger').click();
    await expect(dialog).toContainText('Model catalog');
    await shot(page, 'automatic-catalog');
    expect(calls.some(method => method.includes('configure') || method.includes('activate'))).toBe(false);
});
test('fallback stays compact and configuration file lives in Advanced', async ({ page }) => {
    test.skip(BASELINE);
    await fixture(page, 'ensemble', 'en', 'light');
    await ready(page);
    const fallback = page.getByTestId('setup-model-strategy-fixed-section');
    const input = fallback.locator('input[name="setup_provider_model_strategy_fixed_model"]');
    await expect(fallback).not.toHaveAttribute('open');
    await expect(input).toBeHidden();
    const summary = fallback.locator('summary').first();
    await summary.scrollIntoViewIfNeeded();
    await summary.focus();
    await page.keyboard.press('Enter');
    await expect(input).toBeVisible();
    await shot(page, 'fallback-expanded');
    await summary.click();
    await expect(input).toBeHidden();
    await expect(page.locator('.settings-foot')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Copy config path', exact: true })).toHaveCount(0);
    await page.getByRole('tab', { name: /^Advanced/ }).click();
    const file = page.getByTestId('advanced-config-file');
    await expect(file).toBeVisible();
    await expect(file.locator('code')).toContainText('config.toml');
    await file.scrollIntoViewIfNeeded();
    await shot(page, 'advanced-config-file');
});

test('ensemble menu keyboard entry edits capacity without changing the lineup', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const { config, writes } = await fixture(page, 'ensemble', 'en', 'light');
    await ready(page);
    const lineup = JSON.stringify(config.llm_ensemble);
    const actions = page.locator('.setup-model-strategy__candidate-actions').first();
    await actions.evaluate(el => el.scrollIntoView({ block: 'center' }));
    await shot(page, 'ensemble-lineup');
    if (BASELINE) return;
    const summary = actions.locator('summary');
    await summary.focus();
    await page.keyboard.press('Enter');
    await page.keyboard.press('Tab');
    const entry = actions.locator('.model-capacity-menu');
    await expect(entry).toBeFocused();
    await shot(page, 'ensemble-capacity-menu');
    await page.keyboard.press('Enter');
    const dialog = page.locator('.model-capacity-dialog');
    await expect(dialog).toBeVisible();
    await dialog.getByLabel('Context window', { exact: true }).fill('131072');
    await dialog.getByRole('button', { name: 'Done', exact: true }).click();
    await expect(entry).toBeFocused();
    const inherited = page.getByTestId('ensemble-custom-aggregator-inherited').locator('.model-capacity-trigger');
    await inherited.click();
    await expect(dialog.getByLabel('Context window', { exact: true })).toHaveValue('131072');
    await expect(dialog).toContainText(`custom · ${MODEL}`);
    await shot(page, 'ensemble-inherited-editor');
    await page.keyboard.press('Escape');
    await expect(inherited).toBeFocused();
    expect(JSON.stringify(config.llm_ensemble)).toBe(lineup);
    expect(writes).toHaveLength(0);
});
