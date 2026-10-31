import type { Page } from '@playwright/test'
import { helloOkResponse } from './gateway-fixture'

type Provider = 'openrouter' | 'tokenrhythm'
type Kind = 'router' | 'ensemble'
type Row = { role: string; provider: string; model: string; thinkingLevel: string | null }

/** Offline Gateway serving the real settings surface; all model IDs are synthetic. */
export async function installRecommendationsGateway(page: Page, provider: Provider = 'tokenrhythm', kind: Kind = 'router', supported = true) {
  const rows = (p: Provider, k: Kind, revision: number): Row[] => (k === 'router' ? ['c0', 'c1', 'c2', 'c3'] : ['proposer_1', 'proposer_2', 'proposer_3', 'proposer_4', 'aggregator']).map(role => ({ role, provider: p, model: `synthetic-${p}-${role}-v${revision}`, thinkingLevel: 'high' }))
  const record = (p: Provider, k: Kind) => `${p}:${k}`
  const plans = Object.fromEntries((['openrouter', 'tokenrhythm'] as const).flatMap(p => (['router', 'ensemble'] as const).map(k => [record(p, k), { providerId: p, kind: k, revision: 'revision-2', source: 'remote', rows: rows(p, k, 2), currentRows: rows(p, k, 1), hasUpdate: true, notified: false }])))
  const profiles = Object.fromEntries((['openrouter', 'tokenrhythm'] as const).map(p => [p, {
    revision: 'revision-1', proposers: rows(p, 'ensemble', 1).filter(row => row.role !== 'aggregator').map(row => ({ model: row.model, thinking_level: row.thinkingLevel })), aggregator: { model: rows(p, 'ensemble', 1).at(-1)!.model, thinking_level: 'high' },
  }]))
  const config = {
    llm: { provider: provider as string, model: 'synthetic-fixed-model' },
    squilla_router: { enabled: kind === 'router', preset_binding: 'follow_primary', default_tier: 'c1', visual_mode: 'real_candidates', tiers: Object.fromEntries(rows(provider, 'router', 1).map(row => [row.role, { provider, model: row.model, thinking_level: row.thinkingLevel, ensemble_enabled: false }])) },
    llm_ensemble: { enabled: kind === 'ensemble', selection_mode: `static_${provider}_b5`, recommended_profiles: profiles, candidates: [], model_options: [] },
    memory: {}, models: {}, permissions: {}, skills: {},
  }
  const reads: string[] = []
  let readFailure = false
  let rejectApply = false
  let applyDelay = 0
  const applyAttempts: unknown[] = []
  const writes: Array<{ providerId: Provider; kind: Kind; revision: string }> = []
  let mode: string = kind
  const methods = ['config.get', 'config.effective', 'onboarding.catalog', 'onboarding.status', 'onboarding.models.discover', 'models.list', 'models.routing.get', 'models.routing.set', 'models.routing.resetRecommended', 'providers.status', 'sessions.list', 'agents.list', 'commands.list_for_surface', ...(supported ? ['models.recommendations.get', 'models.recommendations.apply', 'models.recommendations.acknowledge'] : [])]
  await page.addInitScript(() => { localStorage.setItem('opensquilla-locale', 'en'); localStorage.setItem('opensquilla-theme', 'dark') })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(async message => {
      const frame = JSON.parse(String(message)); if (frame.type !== 'req') return
      const respond = (payload: unknown, ok = true) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok, ...(ok ? { payload } : { error: payload }) }))
      if (frame.method === 'connect') { ws.send(helloOkResponse({ features: { methods, events: [] } })); return }
      if (frame.method === 'models.recommendations.get') { reads.push(plans[record(provider, kind)].revision); if (readFailure) { respond({ code: 'INTERNAL_ERROR', message: 'Synthetic cache read failure' }, false); return }; respond({ plans: Object.values(plans) }); return }
      if (frame.method === 'models.recommendations.acknowledge') { plans[record(frame.params.providerId, frame.params.kind)].notified = true; respond({ acknowledged: true }); return }
      if (frame.method === 'models.recommendations.apply') {
        if (applyDelay > 0) {
          const milliseconds = applyDelay
          applyDelay = 0
          await new Promise(resolve => setTimeout(resolve, milliseconds))
        }
        const command = frame.params; applyAttempts.push(command); if (rejectApply) { rejectApply = false; respond({ code: 'INVALID_REQUEST', message: 'Synthetic save rejected' }, false); return }; const plan = plans[record(command.providerId, command.kind)]
        if (command.revision !== plan.revision) { respond({ code: 'CONFLICT', message: 'Synthetic revision conflict' }, false); return }
        writes.push(command); plan.currentRows = structuredClone(plan.rows); plan.hasUpdate = false
        if (command.kind === 'router') config.squilla_router.tiers = Object.fromEntries(plan.rows.map(row => [row.role, { provider: command.providerId, model: row.model, thinking_level: row.thinkingLevel!, ensemble_enabled: false }]))
        else {
          config.llm_ensemble.selection_mode = `static_${command.providerId}_b5`
          config.llm_ensemble.recommended_profiles[command.providerId] = { revision: command.revision, proposers: plan.rows.filter(row => row.role !== 'aggregator').map(row => ({ model: row.model, thinking_level: row.thinkingLevel })), aggregator: { model: plan.rows.find(row => row.role === 'aggregator')!.model, thinking_level: 'high' } }
        }
        respond({ mode, patched: [command.kind === 'router' ? 'squilla_router.tiers' : 'llm_ensemble.recommended_profiles'], restart_required: false }); return
      }
      if (frame.method === 'models.routing.set') { mode = frame.params.mode; config.squilla_router.enabled = mode === 'router'; config.llm_ensemble.enabled = mode === 'ensemble'; respond({ mode, patched: [], restart_required: false }); return }
      if (frame.method.includes('discover')) { respond({ ok: true, source: 'live', models: [...Object.values(plans).flatMap(plan => [...plan.rows, ...plan.currentRows]).map(row => row.model), 'synthetic-fixed-model'].map(id => ({ id, name: id, capabilities: ['chat'] })) }); return }
      const payloads: Record<string, unknown> = {
        'config.get': config,
        'config.effective': { fields: { 'llm.provider': { value: provider, source: 'config' } } },
        'onboarding.catalog': { providers: ['tokenrhythm', 'openrouter', 'openai'].map(providerId => ({ providerId, label: providerId === 'tokenrhythm' ? 'TokenRhythm' : providerId === 'openrouter' ? 'OpenRouter' : 'OpenAI', runtimeSupported: true, requiresApiKey: true, routerSupported: true, fields: [{ name: 'model', label: 'Model', required: true }] })) },
        'onboarding.status': { hasConfig: true, llmConfigured: true, llmCredentialStatus: { provider: config.llm.provider, available: true, source: 'explicit' }, sectionDetails: { router: { enabled: config.squilla_router.enabled, routerBinding: 'follow_primary' }, ensemble: { enabled: config.llm_ensemble.enabled } } },
        'models.routing.get': { mode }, 'models.list': { models: [], errors: [] }, 'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] }, 'sessions.list': { sessions: [], count: 0, ts: 1800000000, has_more: false },
      }
      respond(payloads[frame.method] ?? {})
    })
  })
  return { config, plans, writes, reads, applyAttempts, delayNextApply: (milliseconds: number) => { applyDelay = milliseconds }, failReads: () => { readFailure = true }, rejectNextApply: () => { rejectApply = true }, get mode() { return mode }, publish(p: Provider, k: Kind, revision: number) {
    const plan = plans[record(p, k)]; plan.rows = rows(p, k, revision); plan.revision = `revision-${revision}`; plan.hasUpdate = true; plan.notified = false
  } }
}
