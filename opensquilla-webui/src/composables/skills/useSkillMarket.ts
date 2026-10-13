import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'
import type { SkillCatalog } from '@/modules/skillCatalog'
import type { RegistryResult, SkillDiagnostic } from '@/types/skills'

export type MarketSource = 'all' | 'clawhub' | 'skillhub' | 'github'
export type MarketSort = 'relevance' | 'downloads' | 'installs' | 'name'

/** Registry rows per query; sources cap far above this, so one page is enough. */
const MARKET_LIMIT = 40

export interface SkillMarket {
  query: Ref<string>
  source: Ref<MarketSource>
  category: Ref<string>
  sort: Ref<MarketSort>
  results: Ref<RegistryResult[]>
  visibleResults: ComputedRef<RegistryResult[]>
  categories: ComputedRef<string[]>
  diagnostics: Ref<SkillDiagnostic[]>
  message: Ref<string>
  loading: Ref<boolean>
  loaded: Ref<boolean>
  search: (options?: { readonly silent?: boolean }) => Promise<void>
}

/**
 * Market browse state. An empty query is a valid "browse the shelf" request:
 * the Gateway forwards it to every source and each source that cannot answer a
 * keywordless query returns an empty page instead of an error.
 */
export function useSkillMarket(catalog: SkillCatalog): SkillMarket {
  const query = ref('')
  const source = ref<MarketSource>('all')
  const category = ref('')
  const sort = ref<MarketSort>('relevance')
  const results = ref<RegistryResult[]>([])
  const diagnostics = ref<SkillDiagnostic[]>([])
  const message = ref('')
  const loading = ref(false)
  const loaded = ref(false)
  let requestId = 0

  async function search(options?: { readonly silent?: boolean }) {
    const id = ++requestId
    if (!options?.silent) loading.value = true
    message.value = ''
    try {
      const data = await catalog.search(query.value.trim(), {
        limit: MARKET_LIMIT,
        source: source.value === 'all' ? null : source.value,
      })
      if (id !== requestId) return
      results.value = [...data.results]
      diagnostics.value = [...data.diagnostics]
      message.value = data.message || ''
      loaded.value = true
    } catch (err) {
      if (id !== requestId) return
      results.value = []
      diagnostics.value = []
      message.value = (err as Error).message || String(err)
      loaded.value = true
    } finally {
      if (id === requestId) loading.value = false
    }
  }

  watch(source, () => {
    category.value = ''
    void search()
  })

  const categories = computed(() => {
    const seen = new Set<string>()
    for (const row of results.value) {
      const value = (row.category || '').trim()
      if (value) seen.add(value)
    }
    return [...seen].sort((a, b) => a.localeCompare(b))
  })

  const visibleResults = computed(() => {
    let rows = results.value
    if (category.value) {
      rows = rows.filter(row => (row.category || '').trim() === category.value)
    }
    const numeric = (key: 'downloads' | 'installs') =>
      (a: RegistryResult, b: RegistryResult) => (b[key] || 0) - (a[key] || 0)
    switch (sort.value) {
      case 'downloads':
        return [...rows].sort((a, b) => numeric('downloads')(a, b)
          || a.name.localeCompare(b.name))
      case 'installs':
        return [...rows].sort((a, b) => numeric('installs')(a, b)
          || a.name.localeCompare(b.name))
      case 'name':
        return [...rows].sort((a, b) => a.name.localeCompare(b.name))
      default:
        return rows
    }
  })

  return {
    query,
    source,
    category,
    sort,
    results,
    visibleResults,
    categories,
    diagnostics,
    message,
    loading,
    loaded,
    search,
  }
}
