import { computed, createApp, defineComponent, h, ref } from 'vue'
import UsageChart from '@/components/usage/UsageChart.vue'
import UsageModelBreakdown from '@/components/usage/UsageModelBreakdown.vue'
import UsageSummaryStats from '@/components/usage/UsageSummaryStats.vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import { getManifest } from '@/themes/registry'
import type { BreakdownRow, ChartRow } from '@/types/usage'
import '@/assets/base.css'
import '@/themes/tokens'
import '@/styles/control-visual-system.css'
import '@/styles/apple-modern.css'
// Load the shipped UsageView style block without mounting its live data providers.
import '@/views/UsageView.vue?vue&type=style&index=0&lang.css'

// Only fixed example data: no gateway, model, profile, or localStorage access.
const query = new URLSearchParams(location.search)
const selectedTheme = query.get('theme')
const theme = selectedTheme && ['miami', 'light', 'dark'].includes(selectedTheme) ? selectedTheme : 'miami'
const locale = query.get('lang') === 'en' ? 'en' : 'zh-Hans'
await loadLocaleMessages(locale)
i18n.global.locale.value = locale
document.documentElement.lang = locale
document.documentElement.dataset.theme = theme
const manifest = getManifest(theme)
await Promise.all([
  manifest?.world?.styles?.(),
  ...(manifest?.world?.fonts?.map(font => font.load()) ?? []),
])
const english = locale === 'en'
const t = i18n.global.t
const fmtCost = (value: number | null | undefined) => value == null ? '—' : `$${value.toFixed(4)}`
const modelRows: BreakdownRow[] = [
  {
    model: 'example-provider/long-model-name-with-dated-release-and-extended-context-window-2026-10-10-validation-only',
    provider: 'example-provider',
    name: 'long-model-name-with-dated-release-and-extended-context-window-2026-10-10-validation-only',
    tokens: 987654321, cost: 1234.5678, share: 50, costSource: 'provider_billed',
  },
  { model: 'example-provider/model-with-estimated-cost', provider: 'example-provider', name: 'model-with-estimated-cost', tokens: 1234567, cost: 12.3456, share: 30, costSource: 'opensquilla_estimate' },
  { model: 'example-provider/model-mixed', provider: 'example-provider', name: 'model-mixed', tokens: 456789, cost: 3.4567, share: 20, costSource: 'mixed' },
]
function costKind(row: BreakdownRow): 'actual' | 'estimated' | 'mixed' {
  return row.costSource === 'provider_billed' ? 'actual' : row.costSource === 'opensquilla_estimate' ? 'estimated' : 'mixed'
}

const Preview = defineComponent({
  setup() {
    const chartMode = ref<'tokens' | 'cost'>('tokens')
    const range = ref('7')
    const rows = computed<ChartRow[]>(() => [
      [english ? 'Balanced input / output' : '输入输出各半', 50, 50],
      [english ? 'Mostly input' : '输入占九成', 90, 10],
      [english ? 'Input only' : '只有输入', 100, 0],
      [english ? 'Output only' : '只有输出', 0, 100],
    ].map(([label, input, output]) => ({
      sessionKey: null, label: String(label),
      inputPct: chartMode.value === 'tokens' ? Number(input) : 100,
      outputPct: chartMode.value === 'tokens' ? Number(output) : 0,
      totalPct: 100,
      valueLabel: chartMode.value === 'tokens' ? '1,000' : '$1.2500',
    })))
    return () => h('main', { class: 'main' }, [
      h('div', { class: 'content' }, [
        h('div', { class: 'usage-stage control-stage control-stage--spacious', 'data-fixture': 'ui-convergence' }, [
          h('header', { class: 'usage-stage__header control-stage__header' }, [
            h('div', { class: 'control-stage__title-block' }, [
              h('h1', { class: 'usage-stage__title control-stage__title' }, t('usageLogs.usage.title')),
              h('p', { class: 'usage-stage__subtitle control-stage__subtitle' }, english
                ? `Read-only example · ${theme} · Four token ratios and long model names`
                : `只读示例 · ${theme} · 四种 Token 比例与长模型名`),
            ]),
          ]),
          h(UsageSummaryStats, {
            totalTokens: '989,345,677', tokenParts: [{ label: t('usageLogs.metrics.input'), value: '900M' }, { label: t('usageLogs.metrics.output'), value: '89M' }],
            totalCost: '$1,250.3701',
            sessionCount: '4', avgCost: '$312.5925',
          }),
          h(UsageChart, {
            chartMode: chartMode.value, range: range.value,
            caption: english ? 'Fixed example values' : '固定示例数据', rows: rows.value,
            'onUpdate:chartMode': value => { chartMode.value = value },
            onSetRange: value => { range.value = value },
          }),
          h('section', { class: 'usage-sessions' }, [
            h('div', { class: 'usage-section-head' }, [
              h('h2', { class: 'usage-section-title' }, t('usageLogs.breakdown.title')),
            ]),
            h('div', { class: 'usage-table-wrap' }, [h(UsageModelBreakdown, {
              rows: modelRows, totalTokens: 989345677, totalCost: 1250.3701,
              nativeSource: { costSource: 'mixed' }, anyProrated: false, fmtCost,
              costSourceClasses: row => ({ [`usage-source--${row.costSource}`]: true }),
              costSourceLabel: row => t(`usageLogs.costSource.${costKind(row)}.label`),
              costSourceTooltip: row => t(`usageLogs.costSource.${costKind(row)}.tooltip`),
            })]),
          ]),
        ]),
      ]),
    ])
  },
})

createApp(Preview).use(i18n).mount('#app')
