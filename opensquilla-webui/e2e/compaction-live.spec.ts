import { expect, test, type Page } from '@playwright/test'
import { readFile, writeFile, rm } from 'node:fs/promises'
import { join } from 'node:path'

// Runs against the existing Python live harness's --serve-gateway mode.
// Requests, sessions, summaries and continuation replies all use production paths.
const LIVE = process.env.OPENSQUILLA_COMPACTION_LIVE === '1'
const reportPath = process.env.OPENSQUILLA_COMPACTION_REPORT || ''
const stateRoot = process.env.OPENSQUILLA_COMPACTION_ROOT || ''
const profile = process.env.OPENSQUILLA_COMPACTION_TASK || 'coding'
const resumePath = process.env.OPENSQUILLA_COMPACTION_SESSION_PATH || ''
const recallOnly = process.env.OPENSQUILLA_COMPACTION_RECALL_ONLY === '1'
const executionMode = process.env.OPENSQUILLA_COMPACTION_EXECUTION || 'direct'
const routerTiers = (process.env.OPENSQUILLA_COMPACTION_ROUTER_TIERS || '').split(',').filter(Boolean)

type Member = { provider: string; model: string; role?: string; enabled?: boolean }
type Execution = Member & {
  routed_model?: string; routed_tier?: string; baseline_model?: string; routing_applied?: boolean
  effective_context_window_tokens?: number; effective_max_tokens?: number
  ensemble_trace?: {
    successful_proposers: number; min_successful_proposers: number; fallback_used: boolean
    final_request_role: string; llm_request_count: number
    fallback_code?: string; selected_candidate_count?: number
    candidates: Array<Member & { ok: boolean; request_started: boolean; execution: Execution }>
    final_request: { request_started: boolean; execution: Execution }
  }
}

type Evidence = {
  status: string
  layout: string
  model: string
  provider: string
  window_mode: string
  request_models_by_call: string[]
  physical_deployments: Array<Member & {
    physical_window_known: boolean; physical_context_window_tokens: number | null
  }>
  execution_overlay: {
    squilla_router?: { enabled: boolean; tiers: Record<string, Member> }
    llm_ensemble?: { enabled: boolean; candidates: Member[]; min_successful_proposers: number }
  }
  physical_fault_injections?: Array<Member & {
    kind: string; http_status: number; network_bypassed: boolean; once: boolean
  }>
  task_profile: string
  task_facts: Record<string, string>
  task_instructions: string
  transport_kind: string
  blocked_unobserved_generation_requests: number
  blocked_tool_attempts: number
  tools_mode: string
  large_paste_chars: number
  compaction_events: Array<{ status?: string; durability?: string }>
  storage: {
    counts?: Record<string, number>
    canonical_message_digests?: Record<string, string>
    duplicate_canonical_ids?: number
    summaries?: Array<{ id: number; sha256: string; source: string }>
    archived_fact_source_ids?: string[]
    turn_executions?: Array<{ message_sha256: string; execution: Execution }>
  }
  calls: Array<{
    summary_request: boolean; injected_fault: string | null
    http_status: number; finish_reason: string; completed: boolean; tool_count: number
    provider: string; generation_budget: number
  }>
  wire_summary_matches: Array<{
    summary_request: boolean; summary_occurrences: number[]
    response_facts: Record<string, boolean>; outage_continued: boolean
    temporary_window_notice: boolean
    generated_paste_placeholder: boolean
  }>
  summary_fact_checks: Array<Record<string, boolean>>
}

// Existing operator RPC on the same isolated Gateway; prompts still go through UI.
async function routingRpc(page: Page, method: string, params: Record<string, unknown>) {
  return page.evaluate(async ({ method, params }) => {
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`)
      const timer = setTimeout(() => { socket.close(); reject(new Error('routing RPC timeout')) }, 30_000)
      const finish = (value?: Record<string, unknown>, error?: string) => {
        clearTimeout(timer)
        socket.close()
        if (error) reject(new Error(error))
        else resolve(value || {})
      }
      socket.onerror = () => finish(undefined, 'routing RPC connection failed')
      socket.onmessage = ({ data }) => {
        const frame = JSON.parse(String(data))
        if (frame.type === 'event' && frame.event === 'connect.challenge') {
          socket.send(JSON.stringify({ type: 'req', id: 'acceptance-connect', method: 'connect',
            params: { minProtocol: 1, maxProtocol: 3, role: 'operator', scopes: ['operator.admin'] } }))
        } else if (frame.type === 'hello-ok') {
          socket.send(JSON.stringify({ type: 'req', id: 'acceptance-routing', method, params }))
        } else if (frame.type === 'res' && frame.id === 'acceptance-routing') {
          finish(frame.payload, frame.ok ? undefined : 'routing RPC rejected')
        }
      }
    })
  }, { method, params })
}

function physicalWindow(proof: Evidence, member: Member) {
  const physical = proof.physical_deployments.find(row =>
    row.provider === member.provider && row.model === member.model)
  expect(physical?.physical_window_known, `Known physical W for ${member.provider}/${member.model}`).toBe(true)
  expect(physical?.physical_context_window_tokens).toBeGreaterThan(0)
  return physical!.physical_context_window_tokens!
}

function assertEnsembleTurn(proof: Evidence, before: Evidence) {
  const lineup = proof.execution_overlay.llm_ensemble!
  expect(lineup.enabled).toBe(true)
  const members = lineup.candidates.filter(row => row.enabled !== false)
  const proposers = members.filter(row => row.role === 'proposer')
  const aggregator = members.find(row => row.role === 'aggregator')!
  expect(aggregator).toBeDefined()
  const priorIds = new Set((before.storage.turn_executions || []).map(row => row.message_sha256))
  const turns = (proof.storage.turn_executions || []).filter(row => !priorIds.has(row.message_sha256))
  expect(turns, 'One persisted final answer for this UI turn').toHaveLength(1)
  const trace = turns[0]!.execution.ensemble_trace!
  expect(trace, 'Actual ensemble execution, not only enabled config').toBeDefined()
  expect(trace.successful_proposers).toBe(proposers.length)
  expect(trace.min_successful_proposers).toBe(lineup.min_successful_proposers)
  expect(trace.fallback_used).toBe(false)
  expect(trace.final_request_role).toBe('aggregator')
  expect(trace.llm_request_count).toBe(proposers.length + 1)
  expect(trace.candidates).toHaveLength(proposers.length)
  for (const proposer of proposers) {
    const candidate = trace.candidates.find(row =>
      row.provider === proposer.provider && row.model === proposer.model)
    expect(candidate?.ok).toBe(true)
    expect(candidate?.request_started).toBe(true)
    expect(candidate?.execution.effective_max_tokens).toBe(8192)
    expect(candidate?.execution.effective_context_window_tokens).toBe(physicalWindow(proof, proposer))
  }
  const final = trace.final_request
  expect(final.request_started).toBe(true)
  expect(final.execution.provider).toBe(aggregator.provider)
  expect(final.execution.model).toBe(aggregator.model)
  expect(final.execution.effective_max_tokens).toBe(8192)
  expect(final.execution.effective_context_window_tokens).toBe(physicalWindow(proof, aggregator))
  const indices = proof.calls.map((call, index) => ({ call, index }))
    .slice(before.calls.length).filter(({ call }) => !call.summary_request)
  expect(indices).toHaveLength(proposers.length + 1)
  const actual = indices.map(({ call, index }) => `${call.provider}/${proof.request_models_by_call[index]}`)
  expect(actual.slice(0, -1).sort()).toEqual(proposers.map(row => `${row.provider}/${row.model}`).sort())
  expect(actual.at(-1)).toBe(`${aggregator.provider}/${aggregator.model}`)
  expect(indices.every(({ call }) => call.http_status === 200 && call.completed
    && call.finish_reason === 'stop' && call.generation_budget === 8192)).toBe(true)
}

function assertFixedFallbackTurn(proof: Evidence, before: Evidence, target: Member) {
  const lineup = proof.execution_overlay.llm_ensemble!
  const proposers = lineup.candidates.filter(row => row.enabled !== false && row.role === 'proposer')
  expect(proposers).toHaveLength(2)
  expect(lineup.min_successful_proposers).toBe(2)
  const previousIds = new Set((before.storage.turn_executions || []).map(row => row.message_sha256))
  const turns = (proof.storage.turn_executions || []).filter(row => !previousIds.has(row.message_sha256))
  expect(turns).toHaveLength(1)
  const trace = turns[0]!.execution.ensemble_trace!
  expect(trace, 'A real fixed fallback must be persisted').toBeDefined()
  expect(trace.successful_proposers).toBe(1)
  expect(trace.min_successful_proposers).toBe(2)
  expect(trace.fallback_used).toBe(true)
  expect(trace.fallback_code).toBe('ensemble_insufficient_proposers')
  expect(trace.final_request_role).toBe('fixed_aggregator')
  expect(trace.selected_candidate_count).toBe(1)
  expect(trace.llm_request_count).toBe(3)
  expect(trace.candidates).toHaveLength(2)
  for (const proposer of proposers) {
    const candidate = trace.candidates.find(row =>
      row.provider === proposer.provider && row.model === proposer.model)
    expect(candidate?.request_started).toBe(true)
    expect(candidate?.ok).toBe(!(proposer.provider === target.provider && proposer.model === target.model))
  }
  const fixed = { provider: proof.provider, model: proof.model }
  expect(trace.final_request.request_started).toBe(true)
  expect(trace.final_request.execution.provider).toBe(fixed.provider)
  expect(trace.final_request.execution.model).toBe(fixed.model)
  expect(trace.final_request.execution.effective_max_tokens).toBe(8192)
  expect(trace.final_request.execution.effective_context_window_tokens).toBe(physicalWindow(proof, fixed))
  const calls = proof.calls.map((call, index) => ({ call, index })).slice(before.calls.length)
  expect(calls, 'Exactly two proposer attempts plus the real fixed aggregator').toHaveLength(3)
  expect(calls.every(({ call }) => !call.summary_request)).toBe(true)
  const failures = calls.filter(({ call }) => call.injected_fault === 'proposer_error')
  expect(failures).toHaveLength(1)
  expect(failures[0]!.call.http_status).toBe(503)
  expect(failures[0]!.call.provider).toBe(target.provider)
  expect(proof.request_models_by_call[failures[0]!.index]).toBe(target.model)
  const realCalls = calls.filter(({ call }) => call.injected_fault === null)
  expect(realCalls).toHaveLength(2)
  expect(realCalls.every(({ call }) => call.http_status === 200 && call.completed
    && call.finish_reason === 'stop' && call.generation_budget === 8192)).toBe(true)
  const identities = calls.map(({ call, index }) => `${call.provider}/${proof.request_models_by_call[index]}`)
  expect(identities.slice(0, 2).sort()).toEqual(proposers.map(row => `${row.provider}/${row.model}`).sort())
  expect(identities.at(-1)).toBe(`${fixed.provider}/${fixed.model}`)
  const injections = (proof.physical_fault_injections || []).slice(before.physical_fault_injections?.length || 0)
  expect(injections).toEqual([{ provider: target.provider, model: target.model,
    kind: 'proposer_error', http_status: 503, network_bypassed: true, once: true }])
}

async function evidence(): Promise<Evidence> {
  return JSON.parse(await readFile(reportPath, 'utf8')) as Evidence
}

function assertTaskFacts(answer: string, facts: Record<string, string>) {
  const objectText = answer.match(/\{[\s\S]*\}/)?.[0]
  expect(objectText, 'Continuation must return the original key/value associations').toBeTruthy()
  const returned = JSON.parse(objectText!) as Record<string, unknown>
  for (const [key, value] of Object.entries(facts)) expect(returned[key], key).toBe(value)
}

test('real compaction preserves task facts and continues after a summary outage', async ({ page }) => {
  test.skip(!LIVE, 'Opt-in paid compaction acceptance; requires the isolated live Gateway.')
  expect(reportPath).not.toBe('')
  expect(stateRoot).not.toBe('')
  const fixture = await evidence()
  expect(['direct', 'router', 'ensemble']).toContain(executionMode)
  if (executionMode !== 'direct') {
    expect(fixture.window_mode).toBe('deployment_auto')
    expect(process.env.OPENSQUILLA_COMPACTION_MANUAL,
      'Execution compatibility cases isolate summary-only manual requests').toBe('1')
  }
  if (executionMode === 'router') {
    expect(fixture.execution_overlay.squilla_router?.enabled).toBe(true)
    expect(routerTiers.length).toBeGreaterThanOrEqual(2)
    expect(routerTiers.every(tier => /^c[0-3]$/.test(tier))).toBe(true)
  }
  expect(fixture.task_profile).toBe(profile)
  expect(fixture.transport_kind).toBe('real')
  expect(fixture.tools_mode).toBe('none')
  const facts = fixture.task_facts
  expect(Object.keys(facts)).toHaveLength(12)
  test.setTimeout(900_000)
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  if (resumePath) expect(resumePath).toMatch(/^\/control\/chat(?:\/[^?#]*)?(?:\?[^#]*)?$/)
  await page.goto(resumePath || '/control/chat/new')
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 30_000 })
  if (executionMode !== 'direct') {
    const mode = executionMode === 'router' ? 'squilla_router' : 'llm_ensemble'
    await page.locator('.chat-model-routing-btn').click()
    await page.locator(`.composer-model-routing__option--${mode}`).click()
    await expect(page.locator(`.chat-model-routing-btn--${mode}`)).toBeVisible()
    await page.locator('.composer-model-routing__close').click()
    await expect(page.locator('.composer-model-routing')).toBeHidden()
  }
  const composer = page.locator('.chat-textarea')
  const send = async (text: string, expectedMarker?: string, fallbackTarget?: Member) => {
    expect(text.length, 'Keep synthetic history inline instead of converting it to an attachment')
      .toBeLessThan(fixture.large_paste_chars)
    const prior = await page.locator('.msg-ai').count()
    const before = await evidence()
    const priorCalls = before.calls.length
    await composer.fill(text)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => page.locator('.msg-ai').count(), { timeout: 240_000 }).toBeGreaterThan(prior)
    await expect(page.locator('.work-card')).toHaveCount(0, { timeout: 240_000 })
    await expect.poll(async () => {
      const current = await evidence()
      const call = current.calls.at(-1)
      return current.calls.length > priorCalls && call?.summary_request === false
        && call.http_status === 200 && call.finish_reason === 'stop' && call.completed
    }, { timeout: 240_000 }).toBe(true)
    if (executionMode !== 'direct') {
      await expect.poll(async () => (await evidence()).storage.turn_executions?.length || 0,
        { timeout: 30_000 }).toBeGreaterThan(before.storage.turn_executions?.length || 0)
    }
    const observed = await evidence()
    expect(observed.blocked_unobserved_generation_requests).toBe(0)
    expect(observed.blocked_tool_attempts).toBe(0)
    expect(observed.calls.every(call => call.tool_count === 0)).toBe(true)
    expect(observed.wire_summary_matches.some(call => call.generated_paste_placeholder)).toBe(false)
    if (expectedMarker) {
      expect(await page.locator('.msg-ai').last().innerText()).toContain(expectedMarker)
      expect((await evidence()).wire_summary_matches.at(-1)?.outage_continued).toBe(true)
    }
    if (fallbackTarget) assertFixedFallbackTurn(observed, before, fallbackTarget)
    else if (executionMode === 'ensemble') assertEnsembleTurn(observed, before)
    return { before, observed }
  }
  const holdTier = async (tier: string) => {
    const sessionKey = new URL(page.url()).searchParams.get('session')
      || await page.evaluate(() => localStorage.getItem('opensquilla_active_session'))
    expect(sessionKey, 'Hold must target the actual UI-created session').toMatch(/^agent:/)
    const selected = fixture.execution_overlay.squilla_router!.tiers[tier]!
    const result = await routingRpc(page, 'routing.hold.set', {
      sessionKey, target: tier, turns: 0, ttlSeconds: 1800,
    })
    const hold = result.hold as { targetId: string; model: string; provider: string; source: string }
    expect(hold.targetId).toBe(`tier:${tier}`)
    expect(hold.provider).toBe(selected.provider)
    expect(hold.model).toBe(selected.model)
    expect(hold.source).toBe('routing_hold_rpc')
    return selected
  }
  const verifyRecallEvidence = async () => {
    const proof = await evidence()
    const wire = proof.wire_summary_matches.at(-1)
    expect(wire?.summary_request).toBe(false)
    expect(wire?.summary_occurrences.at(-1)).toBe(1)
    expect((proof.storage.archived_fact_source_ids || []).length).toBeGreaterThan(0)
    expect(Object.values(wire?.response_facts || {})).toHaveLength(12)
    expect(Object.values(wire?.response_facts || {}).every(Boolean)).toBe(true)
  }
  const filler = 'Disposable background: sort colored paper into boxes and discard duplicate notes. '
  const turns = Number(process.env.OPENSQUILLA_COMPACTION_HISTORY_TURNS || '12')
  const repetitions = Number(process.env.OPENSQUILLA_COMPACTION_FILLER_REPETITIONS || '150')
  expect(Number.isSafeInteger(turns) && turns >= 2).toBe(true)
  expect(Number.isSafeInteger(repetitions) && repetitions > 0).toBe(true)
  const initialSummaryCount = (await evidence()).storage.summaries?.length || 0
  if (!resumePath) await send(`${fixture.task_instructions}\nRemember these twelve synthetic task facts. Do not alter their values or states.
${JSON.stringify(facts)}
Reply only FACTS_RECORDED.`)
  const selectedWindows: number[] = []
  const historyTurns = executionMode === 'router' ? Math.max(turns, routerTiers.length) : turns
  for (let i = 0; !recallOnly && i < historyTurns; i += 1) {
    const tier = routerTiers[Math.min(i, routerTiers.length - 1)]
    const selected = executionMode === 'router' ? await holdTier(tier!) : null
    const { before: previous, observed } = await send(`SYNTHETIC_BROWSER_HISTORY_${i}
${filler.repeat(repetitions)}
This background is disposable. Keep the original task facts. Reply only BACKGROUND_RECORDED.`)
    if (selected) {
      const indices = observed.calls.map((call, index) => ({ call, index }))
        .slice(previous.calls.length).filter(({ call }) => !call.summary_request)
      expect(indices).toHaveLength(1)
      expect(indices[0]!.call.provider).toBe(selected.provider)
      expect(observed.request_models_by_call[indices[0]!.index]).toBe(selected.model)
      expect(indices[0]!.call.generation_budget).toBe(8192)
      selectedWindows.push(physicalWindow(observed, selected))
      const priorIds = new Set((previous.storage.turn_executions || []).map(row => row.message_sha256))
      const executions = (observed.storage.turn_executions || []).filter(row => !priorIds.has(row.message_sha256))
      expect(executions).toHaveLength(1)
      expect(executions[0]!.execution.routed_tier).toBe(tier)
      expect(executions[0]!.execution.routed_model).toBe(selected.model)
      expect(executions[0]!.execution.baseline_model).toBe(fixture.model)
      expect(executions[0]!.execution.routing_applied).toBe(true)
      expect(observed.storage.summaries).toEqual(previous.storage.summaries)
      expect(observed.storage.counts?.compacted_transcript_entries)
        .toBe(previous.storage.counts?.compacted_transcript_entries)
    }
    if (((await evidence()).storage.summaries?.length || 0) > initialSummaryCount) break
  }
  if (executionMode === 'router') {
    expect(selectedWindows.length).toBeGreaterThanOrEqual(routerTiers.length)
    expect(new Set(selectedWindows).size, 'Actually switch distinct physical context windows').toBeGreaterThan(1)
  }
  // Manual triggering is explicitly configurable; automatic pressure remains the default.
  if (!recallOnly && process.env.OPENSQUILLA_COMPACTION_MANUAL === '1') {
    const beforeManual = await evidence()
    await composer.fill('/compact')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    if (executionMode !== 'direct') {
      await expect.poll(async () => (await evidence()).storage.summaries?.length || 0,
        { timeout: 240_000 }).toBeGreaterThan(beforeManual.storage.summaries?.length || 0)
      const afterManual = await evidence()
      const calls = afterManual.calls.slice(beforeManual.calls.length)
      expect(calls.length).toBeGreaterThan(0)
      expect(calls.every(call => call.summary_request && call.http_status === 200
        && call.completed && call.finish_reason === 'stop' && call.generation_budget === 8192)).toBe(true)
      if (executionMode === 'ensemble') {
        const aggregator = fixture.execution_overlay.llm_ensemble!.candidates
          .find(row => row.enabled !== false && row.role === 'aggregator')!
        expect(calls.every(call => call.provider === aggregator.provider)).toBe(true)
        expect(afterManual.request_models_by_call.slice(beforeManual.calls.length)
          .every(model => model === aggregator.model)).toBe(true)
        expect(afterManual.storage.turn_executions?.length)
          .toBe(beforeManual.storage.turn_executions?.length)
      } else {
        const finalTier = fixture.execution_overlay.squilla_router!.tiers[routerTiers.at(-1)!]!
        expect(calls.every(call => call.provider === finalTier.provider)).toBe(true)
        expect(afterManual.request_models_by_call.slice(beforeManual.calls.length)
          .every(model => model === finalTier.model)).toBe(true)
      }
    }
  }
  if (!recallOnly) await expect.poll(async () => (await evidence()).storage.summaries?.length || 0,
    { timeout: 240_000 }).toBeGreaterThan(initialSummaryCount)
  const recall = `Return one JSON object with these exact keys: ${Object.keys(facts).join(', ')}. Return every value as a JSON string, including numeric-looking values. Recover the original values from our task. Keep completed, pending, rejected and next states distinct. Do not invent values.`
  await send(recall)
  const answer = await page.locator('.msg-ai').last().innerText()
  assertTaskFacts(answer, facts)
  await verifyRecallEvidence()
  const before = await evidence()
  const sessionUrl = new URL(page.url())
  await writeFile(join(stateRoot, 'browser-session-path.txt'), sessionUrl.pathname + sessionUrl.search, 'utf8')
  expect(before.storage.duplicate_canonical_ids).toBe(0)
  const finalFacts = Object.values(before.summary_fact_checks.at(-1) || {})
  expect(finalFacts).toHaveLength(12)
  expect(finalFacts.every(Boolean)).toBe(true)
  if (process.env.OPENSQUILLA_COMPACTION_ENSEMBLE_FALLBACK === '1') {
    expect(executionMode).toBe('ensemble')
    const target = fixture.execution_overlay.llm_ensemble!.candidates
      .find(row => row.enabled !== false && row.role === 'proposer')!
    const faultPath = join(stateRoot, 'ensemble-proposer-fault.json')
    await writeFile(faultPath, JSON.stringify({ kind: 'error', provider: target.provider,
      model: target.model, once: 'fixed-fallback' }), { encoding: 'utf8', flag: 'wx' })
    try {
      const fallback = await send(recall, undefined, target)
      assertTaskFacts(await page.locator('.msg-ai').last().innerText(), facts)
      await verifyRecallEvidence()
      expect(fallback.observed.storage.summaries).toEqual(fallback.before.storage.summaries)
      expect(fallback.observed.storage.counts?.compacted_transcript_entries)
        .toBe(fallback.before.storage.counts?.compacted_transcript_entries)
    } finally {
      await rm(faultPath, { force: true })
    }
  }
  if (executionMode === 'router') {
    const sessionKey = new URL(page.url()).searchParams.get('session')
      || await page.evaluate(() => localStorage.getItem('opensquilla_active_session'))
    await routingRpc(page, 'routing.hold.clear', { sessionKey })
  }

  if (process.env.OPENSQUILLA_COMPACTION_FAULT === '1') {
    const faultFile = join(stateRoot, 'summary-fault-mode')
    const faultMode = process.env.OPENSQUILLA_COMPACTION_FAULT_MODE || 'error'
    expect(['error', 'empty', 'length', 'timeout']).toContain(faultMode)
    await writeFile(faultFile, faultMode, 'utf8')
    try {
      for (let i = 0; i < Math.max(10, turns); i += 1) {
        await send(`SYNTHETIC_OUTAGE_HISTORY_${i}\n${filler.repeat(repetitions)}\nReply OUTAGE_CONTINUED.`, 'OUTAGE_CONTINUED')
      }
      await expect.poll(async () => (await evidence()).calls.some(call => call.injected_fault === faultMode),
        { timeout: 240_000 }).toBe(true)
      const during = await evidence()
      const windowCovered = during.wire_summary_matches.slice(before.calls.length)
        .some(call => !call.summary_request && call.temporary_window_notice)
        || during.compaction_events.slice(before.compaction_events.length)
          .some(event => event.durability === 'request_scoped')
      expect(windowCovered, 'Outage acceptance requires actual request-scoped recovery').toBe(true)
      expect(during.storage.summaries).toEqual(before.storage.summaries)
      expect(during.storage.counts?.compacted_transcript_entries)
        .toBe(before.storage.counts?.compacted_transcript_entries)
      for (const [id, hash] of Object.entries(before.storage.canonical_message_digests || {})) {
        expect(during.storage.canonical_message_digests?.[id]).toBe(hash)
      }
      await page.reload()
      await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 30_000 })
      expect(await page.locator('body').innerText()).not.toContain('Synthetic summary-only outage')
    } finally {
      await rm(faultFile, { force: true })
    }
    // A normal manual operation verifies service recovery without waiting out automatic cooldown.
    await composer.fill('/compact')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(async () => (await evidence()).storage.summaries?.length || 0,
      { timeout: 240_000 }).toBeGreaterThan(before.storage.summaries?.length || 0)
    await send(recall)
    const recovered = await page.locator('.msg-ai').last().innerText()
    assertTaskFacts(recovered, facts)
    await verifyRecallEvidence()
  }
})
