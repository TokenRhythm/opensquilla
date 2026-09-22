// @vitest-environment happy-dom

import { createApp, defineComponent, h, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { useSkillRegistry, type SkillInstallSource } from '@/composables/skills/useSkillRegistry'
import { useToasts } from '@/composables/useToasts'
import type { SkillCatalog, SkillRegistrySearchResult } from '@/modules/skillCatalog'
import SkillsAddDrawer from './SkillsAddDrawer.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
  const { toasts, dismissToast } = useToasts()
  for (const toast of [...toasts.value]) dismissToast(toast.id)
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function mountSearchDrawer(search: SkillCatalog['search']) {
  const searches: Promise<void>[] = []
  const catalog = {
    search,
    supportsInstallCancellation: () => false,
  } as SkillCatalog
  const Root = defineComponent({
    setup() {
      const registry = useSkillRegistry(catalog, async () => true)
      return () => h(SkillsAddDrawer, {
        open: true,
        registryQuery: registry.registryQuery.value,
        githubUrl: registry.githubUrl.value,
        results: registry.registryResults.value,
        loading: registry.registryLoading.value,
        registryDiagnostics: registry.registryDiagnostics.value,
        registrySearchError: registry.registrySearchError.value,
        activities: registry.installActivities.value,
        runningSource: registry.runningSource.value,
        cancellableInstallSource: registry.cancellableInstallSource.value,
        cancellingSource: registry.cancellingSource.value,
        'onUpdate:registryQuery': (query: string) => { registry.registryQuery.value = query },
        'onUpdate:githubUrl': (url: string) => { registry.githubUrl.value = url },
        onSourceChange: registry.resetRegistrySearch,
        onSearch: (source?: SkillInstallSource) => { searches.push(registry.searchRegistry(source)) },
      })
    },
  })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(Root)
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  return { searches }
}

async function switchSource(source: SkillInstallSource) {
  document.querySelector<HTMLButtonElement>(`#skills-add-tab-${source}`)!.click()
  await nextTick()
}

async function enterQuery(value: string) {
  const input = document.querySelector<HTMLInputElement>('.sk-add-search-row input')!
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}

function searchButton() {
  return document.querySelector<HTMLButtonElement>('.sk-add-search-row button')!
}

const skillHubResult: SkillRegistrySearchResult = {
  results: [{ name: 'SkillHub paper', identifier: 'paper', source: 'skillhub' }],
  diagnostics: [],
  message: '',
}

describe('SkillsAddDrawer registry search integration', () => {
  it('releases search controls on a market switch and ignores late source diagnostics', async () => {
    const clawHub = deferred<SkillRegistrySearchResult>()
    const search = vi.fn<SkillCatalog['search']>()
      .mockReturnValueOnce(clawHub.promise)
      .mockResolvedValueOnce(skillHubResult)
    const { searches } = mountSearchDrawer(search)
    await switchSource('clawhub')
    await enterQuery('paper')
    searchButton().click()
    await nextTick()
    expect(searchButton().disabled).toBe(true)

    await switchSource('skillhub')
    expect(searchButton().disabled).toBe(false)
    expect(document.querySelector<HTMLInputElement>('.sk-add-search-row input')!.value).toBe('paper')

    clawHub.resolve({
      results: [],
      message: 'ClawHub search failed',
      diagnostics: [{
        code: 'SOURCE_RATE_LIMITED',
        message: 'ClawHub rate limited the request',
        phase: 'source',
        severity: 'warning',
        blocking: false,
      }],
    })
    await searches[0]
    await nextTick()
    expect(document.querySelector('.sk-add-callout')).toBeNull()
    expect(searchButton().disabled).toBe(false)

    searchButton().click()
    await searches[1]
    await nextTick()
    expect(search).toHaveBeenNthCalledWith(1, 'paper', { limit: 20, source: 'clawhub' })
    expect(search).toHaveBeenNthCalledWith(2, 'paper', { limit: 20, source: 'skillhub' })
    expect(document.querySelector('.sk-add-result')?.textContent).toContain('SkillHub paper')

    await switchSource('clawhub')
    expect(document.querySelector<HTMLInputElement>('.sk-add-search-row input')!.value).toBe('paper')
    expect(document.querySelector('.sk-add-result')).toBeNull()
    expect(document.querySelector('.sk-add-callout')).toBeNull()
    expect(searchButton().disabled).toBe(false)
  })

  it('keeps the new search pending when the old market request rejects', async () => {
    const clawHub = deferred<SkillRegistrySearchResult>()
    const skillHub = deferred<SkillRegistrySearchResult>()
    const search = vi.fn<SkillCatalog['search']>()
      .mockReturnValueOnce(clawHub.promise)
      .mockReturnValueOnce(skillHub.promise)
    const { searches } = mountSearchDrawer(search)
    await switchSource('clawhub')
    await enterQuery('paper')
    searchButton().click()
    await nextTick()

    await switchSource('skillhub')
    expect(searchButton().disabled).toBe(false)
    searchButton().click()
    await nextTick()
    expect(searchButton().disabled).toBe(true)

    clawHub.reject(new Error('ClawHub timed out'))
    await searches[0]
    await nextTick()
    expect(document.querySelector('.sk-add-callout')).toBeNull()
    expect(useToasts().toasts.value).toEqual([])
    expect(searchButton().disabled).toBe(true)

    skillHub.resolve(skillHubResult)
    await searches[1]
    await nextTick()
    expect(searchButton().disabled).toBe(false)
    expect(document.querySelector('.sk-add-result')?.textContent).toContain('SkillHub paper')
    expect(document.querySelector('.sk-add-callout')).toBeNull()
  })
})
