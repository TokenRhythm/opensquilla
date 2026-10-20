import { createApp, defineComponent, h, KeepAlive } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import UsageView from '@/views/UsageView.vue'
import { createV4Observability } from '@/adapters/gateway/observabilityV4'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import { SESSION_DIRECTORY_KEY, type SessionDirectory } from '@/modules/sessionDirectory'
import i18n, { loadLocaleMessages } from '@/i18n'
import { getManifest } from '@/themes/registry'
import '@/assets/base.css'
import '@/themes/tokens'
import '@/styles/control-visual-system.css'
import '@/styles/apple-modern.css'

// Production UsageView + v4 wire normalizer, synthetic in-memory transport.
// This fixture never contacts a provider/Gateway or accesses a real profile.
const query = new URLSearchParams(location.search)
const language = query.get('lang') === 'en' ? 'en' : 'zh-Hans'
await loadLocaleMessages(language)
i18n.global.locale.value = language
document.documentElement.lang = language
const theme = getManifest(query.get('theme') || 'light')!
document.documentElement.dataset.theme = theme.id
document.documentElement.style.colorScheme = theme.capabilities.colorScheme
await Promise.all([theme.world?.styles?.(), ...(theme.world?.fonts?.map(font => font.load()) || [])])
localStorage.setItem('opensquilla-usage-range', 'all')
localStorage.setItem('opensquilla-currency', 'USD')

function totals(cost: string, events = 1, estimated = '0', missing = 0) {
  const amount = Number(cost)
  const estimate = Number(estimated)
  return { inputTokens: events * 10, outputTokens: events * 2, totalTokens: events * 12,
    costNanos: Math.round(amount * 1e9), costUsdExact: cost,
    billedCostNanos: Math.round((amount - estimate) * 1e9), estimatedCostNanos: Math.round(estimate * 1e9),
    eventCount: events, sessionCount: 1, taskCount: 1, missingCostEntries: missing,
    estimatedEventCount: estimate > 0 ? 1 : 0,
    costSource: missing ? (amount ? 'mixed' : 'unavailable')
      : estimate > 0 ? (amount === estimate ? 'opensquilla_estimate' : 'mixed') : 'provider_billed' }
}
const native = (currency: string, amount: string, usd: string, receipts = 1) => ({
  [currency]: { amount, amountNanos: String(Math.round(Number(amount) * 1e9)),
    usdEquivalentNanos: String(Math.round(Number(usd) * 1e9)), usdEquivalentExact: usd,
    receiptCount: receipts, normalizationRatesNativePerUsd: [currency === 'CNY' ? '6.975' : '1'] },
})
const receiptCoverage = (count: number) => ({ nativeBillingExpectedReceiptCount: count,
  nativeBillingMissingConfirmedReceiptCount: 0, pendingBillingReceiptCount: 0 })
const meter = (metric: string, unit: string, quantity: string, unknown = false) => ({
  metric, unit, quantity, unknownCount: unknown ? 1 : 0, measurementCount: 1, dimensions: {},
})
const mediaTotals = (cost: string, missing = 0) => ({
  ...totals(cost, 1, '0', missing), inputTokens: 0, outputTokens: 0, totalTokens: 0,
})
const imageUsd = '0.028673835125448029'
const videoUsd = '0.172043010752688172'
const rootUsd = '0.400716845878136201'
const allUsd = '0.440716845878136201'
const imageUsage = { provider: 'tokenrhythm', model: 'qwen-image-2.0',
  capability: 'image_generation', tokenUnknownItemCount: 1,
  totals: { ...mediaTotals(imageUsd), ...receiptCoverage(1),
    nativeBilledByCurrency: native('CNY', '0.2', imageUsd) },
  meters: [meter('output_images', 'image', '1')] }
const searchUsage = { provider: 'tavily', model: 'web_search', capability: 'web_search',
  totals: mediaTotals('0', 1), tokenUnknownItemCount: 1,
  meters: [meter('requests', 'request', '1')] }
const videoUsage = { provider: 'tokenrhythm', model: 'wan3.0-video',
  capability: 'video_generation', tokenUnknownItemCount: 1,
  totals: { ...mediaTotals(videoUsd), ...receiptCoverage(1),
    nativeBilledByCurrency: native('CNY', '1.2', videoUsd) },
  meters: [meter('requested_video_seconds', 'second', '2'), meter('video_seconds', 'second', '0', true)] }
const mainTotals = { ...totals('0.1'), ...receiptCoverage(1), nativeBilledByCurrency: native('USD', '0.1', '0.1') }
const inlineTotals = totals('0.1', 1, '0.1')
const textUsage = { provider: 'fixture', model: 'fixture/text', capability: 'llm',
  totals: { ...totals('0.2', 2, '0.1'), ...receiptCoverage(1),
    nativeBilledByCurrency: native('USD', '0.1', '0.1') }, tokenUnknownItemCount: 0, meters: [] }
const loneUsage = { ...textUsage, totals: { ...totals('0.04'), ...receiptCoverage(1),
  nativeBilledByCurrency: native('USD', '0.04', '0.04') } }
const sources = { provider_billed: 3, opensquilla_estimate: 1, mixed: 0, unavailable: 1, none: 0 }
const root = { ...totals(rootUsd, 5, '0.1', 1), inputTokens: 20, outputTokens: 4, totalTokens: 24,
  sessionCount: 4, costSourceCounts: sources, ...receiptCoverage(3),
  nativeBilledByCurrency: { ...native('CNY', '1.4', '0.200716845878136201', 2), ...native('USD', '0.1', '0.1') } }
const all = { ...root, ...totals(allUsd, 6, '0.1', 1), inputTokens: 30, outputTokens: 6, totalTokens: 36,
  sessionCount: 5, taskCount: 2, ...receiptCoverage(4), costSourceCounts: { ...sources, provider_billed: 4 },
  nativeBilledByCurrency: { ...native('CNY', '1.4', '0.200716845878136201', 2), ...native('USD', '0.14', '0.14', 2) } }
const runs = [
  { runId: 'main', sessionId: 'root', relation: 'self', parentRunId: null, title: '调研主任务', totals: mainTotals },
  { runId: 'inline', sessionId: 'root', relation: 'inline_subagent', parentRunId: 'main', title: '资料核查', totals: inlineTotals },
  { runId: 'child', sessionId: 'child', relation: 'subsession', parentRunId: 'main', title: '生成配图', totals: imageUsage.totals },
  { runId: 'side', sessionId: 'side', relation: 'sidechat', parentRunId: null, title: '侧边讨论', totals: searchUsage.totals },
  { runId: 'side-child', sessionId: 'side-child', relation: 'sidechat', parentRunId: 'side', title: '侧边视频子任务', totals: videoUsage.totals },
].map(run => ({ ...run, sessionEpoch: 0 }))
const now = Date.now()
const emptyBranches = { inlineSubagents: totals('0', 0), subsessions: totals('0', 0),
  sideChats: totals('0', 0), unattributed: totals('0', 0) }
const payload = {
  schemaVersion: 1, source: 'usage_ledger', asOfMs: now, accountingRevision: 10,
  range: { preset: 'all', fromMs: null, toMs: now, timezone: 'UTC' },
  fxRatesNativePerUsd: { CNY: '6.975' }, totals: all,
  coverage: { status: 'partial', pricing: 'partial', timeAttribution: 'complete',
    attribution: { status: 'complete', missingEventCount: 0, reasonCodes: [] },
    measurements: { tokenUnknownItemCount: 3, unknownMeterCount: 1 },
    nativeBilling: { status: 'complete', missingConfirmedReceiptCount: 0, pendingReceiptCount: 0 } },
  taskGroups: [
    { taskGroupId: 'task-group', rootSessionId: 'root', rootSessionEpoch: 0,
      sessionKey: 'fixture-root', title: '调研主任务', attributionStatus: 'complete', totals: root,
      firstUsageAtMs: now - 3600_000, lastUsageAtMs: now,
      modelBreakdown: [textUsage, imageUsage, searchUsage, videoUsage],
      branches: { self: mainTotals, inlineSubagents: inlineTotals, subsessions: imageUsage.totals,
        sideChats: { ...videoUsage.totals, eventCount: 2, missingCostEntries: 1, costSource: 'mixed' },
        unattributed: totals('0', 0) }, runs },
    { taskGroupId: 'lone-task', rootSessionId: 'lone', rootSessionEpoch: 0,
      sessionKey: 'fixture-lone', title: '独立单模型任务', attributionStatus: 'complete', totals: loneUsage.totals,
      firstUsageAtMs: now - 1000, lastUsageAtMs: now - 1000, modelBreakdown: [loneUsage],
      branches: { self: loneUsage.totals, ...emptyBranches },
      runs: [{ runId: 'lone-run', sessionId: 'lone', sessionEpoch: 0, relation: 'self', parentRunId: null,
        title: '独立单模型任务', totals: loneUsage.totals }] },
  ],
  capabilities: [
    { capability: 'llm', totals: totals('0.24', 3, '0.1'), tokenUnknownItemCount: 0, meters: [] },
    ...[imageUsage, searchUsage, videoUsage],
  ],
  meters: [...imageUsage.meters, ...searchUsage.meters, ...videoUsage.meters],
  settlementSummary: { provider_confirmed: 4, provider_estimate: 0, local_estimate: 1, unknown: 1 },
  // These repeat the same physical calls as taskGroups; the UI must not add or show them as extra tasks.
  sessions: [
    { sessionId: 'root', sessionKey: 'fixture-root', title: '调研主任务', totals: textUsage.totals,
      modelBreakdown: [textUsage], taskGroupId: 'task-group' },
    { sessionId: 'child', sessionKey: 'fixture-child', title: '生成配图', totals: imageUsage.totals,
      modelBreakdown: [imageUsage], taskGroupId: 'task-group' },
    { sessionId: 'side', sessionKey: 'fixture-side', title: '侧边讨论', totals: searchUsage.totals,
      modelBreakdown: [searchUsage], taskGroupId: 'task-group' },
    { sessionId: 'side-child', sessionKey: null, title: '侧边视频子任务（已删除）', totals: videoUsage.totals,
      modelBreakdown: [videoUsage], taskGroupId: 'task-group' },
    { sessionId: 'lone', sessionKey: 'fixture-lone', title: '独立单模型任务', totals: loneUsage.totals,
      modelBreakdown: [loneUsage], taskGroupId: 'lone-task' },
  ].map(session => ({ ...session, sessionEpoch: 0, attributionStatus: 'complete', lastUsageAtMs: now })),
  models: [
    { ...textUsage, sessionCount: 2, taskCount: 2, totals: totals('0.24', 3, '0.1') },
    ...[imageUsage, searchUsage, videoUsage].map(usage => ({ ...usage, sessionCount: 1, taskCount: 1 })),
  ],
  days: [{ date: new Date(now).toISOString().slice(0, 10), fromMs: now - 3600_000, toMs: now, totals: all }],
}
const unused = async (): Promise<never> => { throw new Error('Unexpected fixture operation') }
const observability = createV4Observability({
  ready: async () => {}, supports: () => true, markUnsupported: () => {},
  request: async <T>(method: string) => {
    if (method !== 'usage.query') throw new Error(`Unexpected fixture method: ${method}`)
    return structuredClone(payload) as T
  },
}, { requestJson: unused, requestBinary: unused }, () => null)
const directory: SessionDirectory = { listPage: async () => ({ items: [], hasMore: false, nextCursor: null }),
  count: async () => null, resolve: unused, search: unused }
const router = createRouter({ history: createMemoryHistory(), routes: [
  { path: '/', component: UsageView }, { path: '/chat', component: UsageView },
] })
const Preview = defineComponent({ setup: () => () => h('main', { class: 'usage-fixture' }, [
  h('p', '正式 UsageView 与 v4 适配器 · 内存模拟账目 · 不连接真实网关或供应商'),
  h(KeepAlive, null, { default: () => h(UsageView) }),
]) })
const app = createApp(Preview)
app.use(i18n).use(router).provide(OBSERVABILITY_KEY, observability).provide(SESSION_DIRECTORY_KEY, directory)
await router.isReady()
app.mount('#app')
