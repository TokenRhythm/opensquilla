import { computed, type ComputedRef } from 'vue'
import i18n from '@/i18n'
import type { ModelCard, UsageSession } from '@/types/usage'

const t = i18n.global.t

export function useUsageModelCards(options: {
  visibleSessions: ComputedRef<UsageSession[]>
  serverModels?: ComputedRef<ModelCard[] | null>
}) {
  const modelCards = computed((): ModelCard[] => {
    if (options.serverModels?.value) return options.serverModels.value
    const map: Record<string, ModelCard> = {}
    // Aggregation-only bookkeeping (not part of the ModelCard shape): the set
    // of distinct cost sources contributing to a model, and whether any
    // contributing item was priced with the cache-blind fallback estimator.
    const costSources: Record<string, Set<string>> = {}
    const cacheBlind: Record<string, boolean> = {}

    options.visibleSessions.value.forEach(row => {
      const breakdown = Array.isArray(row.modelBreakdown) ? row.modelBreakdown : []
      const items = breakdown.length > 0 ? breakdown : [{
        model: row.model || 'unknown',
        inputTokens: row.inputTokens,
        outputTokens: row.outputTokens,
        cacheReadTokens: row.cacheReadTokens,
        cacheWriteTokens: row.cacheWriteTokens,
        costUsd: row.costUsd,
        // Fallback (whole-session) items carry the row's own provenance so
        // the aggregate costSource/anyCacheBlind below still reflect it.
        costSource: row.costSource,
        estimateBasis: row.estimateBasis,
      }]
      const modelsSeenInSession = new Set<string>()
      items.forEach(item => {
        const model = item.model || row.model || 'unknown'
        if (!map[model]) {
          map[model] = {
            model,
            provider: '',
            name: '',
            inputTokens: 0,
            outputTokens: 0,
            cacheReadTokens: 0,
            cacheWriteTokens: 0,
            costUsd: 0,
            sessions: 0,
            share: 0,
            totalTokens: 0,
            costSource: 'none',
            anyCacheBlind: false,
          }
          costSources[model] = new Set()
          cacheBlind[model] = false
        }
        map[model].inputTokens += Number(item.inputTokens || 0)
        map[model].outputTokens += Number(item.outputTokens || 0)
        map[model].cacheReadTokens += Number(item.cacheReadTokens || 0)
        map[model].cacheWriteTokens += Number(item.cacheWriteTokens || 0)
        map[model].costUsd += Number(item.costUsd || 0)
        const itemCostSource = item.costSource
        if (itemCostSource != null && itemCostSource !== '') {
          costSources[model].add(String(itemCostSource))
        }
        const itemEstimateBasis = item.estimateBasis
        if (itemEstimateBasis === 'cache_blind') {
          cacheBlind[model] = true
        }
        if (!modelsSeenInSession.has(model)) {
          map[model].sessions += 1
          modelsSeenInSession.add(model)
        }
      })
    })

    Object.keys(map).forEach(model => {
      const sources = costSources[model]
      map[model].costSource = sources.size === 1 ? [...sources][0] : sources.size > 1 ? 'mixed' : 'none'
      map[model].anyCacheBlind = cacheBlind[model] || false
    })

    const models = Object.values(map).sort((a, b) => b.costUsd - a.costUsd)
    const totalCost = models.reduce((acc, m) => acc + m.costUsd, 0)

    return models.map(m => {
      const provider = (m.model || '').split('/')[0] || ''
      const name = (m.model || '').split('/').slice(1).join('/') || m.model || 'unknown'
      return {
        ...m,
        provider,
        name,
        share: totalCost > 0 ? (m.costUsd / totalCost) * 100 : 0,
        totalTokens: m.inputTokens + m.outputTokens,
      }
    })
  })

  const modelsMeta = computed(() => {
    const n = modelCards.value.length
    return t('usageLogs.models.count', { count: n })
  })

  return { modelCards, modelsMeta }
}
