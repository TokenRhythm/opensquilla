// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useToasts } from '@/composables/useToasts'
import { _resetSkillMarketFavoritesForTests } from '@/composables/skills/useSkillMarketFavorites'
import type { SkillCatalog, SkillRegistrySearchResult } from '@/modules/skillCatalog'
import type { RegistryResult } from '@/types/skills'

const apps: ReturnType<typeof import('vue').createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
  localStorage.clear()
  _resetSkillMarketFavoritesForTests()
  const { toasts, dismissToast } = useToasts()
  for (const toast of [...toasts.value]) dismissToast(toast.id)
})

function makeRow(overrides: Partial<RegistryResult> = {}): RegistryResult {
  return {
    name: 'pdf-toolkit',
    description: 'Comprehensive PDF manipulation toolkit',
    version: '1.2.0',
    author: 'awspace',
    source: 'skillhub',
    trust_level: 'community',
    identifier: 'pdf-toolkit',
    installReference: 'pdf-toolkit',
    installed: false,
    category: 'Documents',
    tags: ['pdf', 'extract'],
    downloads: 50448,
    installs: 1200,
    ...overrides,
  }
}

async function mountMarket(
  search: SkillCatalog['search'],
  install?: SkillCatalog['install'],
) {
  const { createApp, defineComponent, h, provide } = await import('vue')
  const { createPinia, setActivePinia } = await import('pinia')
  const i18n = (await import('@/i18n')).default
  const { SKILL_CATALOG_KEY } = await import('@/modules/skillCatalog')

  setActivePinia(createPinia())

  const iconStub = defineComponent({
    name: 'IconStub',
    setup() {
      return () => h('span')
    },
  })
  vi.doMock('@/components/Icon.vue', () => ({ default: iconStub }))

  const catalog = {
    search,
    ...(install ? { install } : {}),
    supportsInstallCancellation: () => false,
    supportsInstallStatus: () => false,
  } as unknown as SkillCatalog

  const { default: SkillsMarketView } = await import('./SkillsMarketView.vue')

  const Root = defineComponent({
    setup() {
      provide(SKILL_CATALOG_KEY, catalog)
      return () => h(SkillsMarketView)
    },
  })

  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(Root)
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  const { nextTick } = await import('vue')
  await nextTick()
  await nextTick()
  return host
}

describe('SkillsMarketView', () => {
  it('browses with an empty query on mount and renders market cards', async () => {
    const search = vi.fn(async (): Promise<SkillRegistrySearchResult> => ({
      results: [makeRow(), makeRow({
        name: 'deep-research',
        identifier: 'deep-research',
        installReference: 'deep-research',
        category: 'Research',
        downloads: 900,
      })],
      diagnostics: [],
      message: '',
    }))

    const host = await mountMarket(search as SkillCatalog['search'])

    expect(search).toHaveBeenCalledWith('', expect.objectContaining({
      source: null,
      limit: 40,
    }))
    expect(host.querySelector('[data-testid="market-grid"]')).toBeTruthy()
    expect(host.querySelector('[data-testid="market-card-pdf-toolkit"]')).toBeTruthy()
    expect(host.querySelector('[data-testid="market-card-deep-research"]')).toBeTruthy()
    expect(host.querySelector('[data-testid="market-installed-badge"]')).toBeNull()
    // 50448 renders as compact "50k"
    expect(host.querySelector('[data-testid="market-card-pdf-toolkit"]')?.textContent)
      .toContain('50k')
  })

  it('marks installed rows and does not offer a second install', async () => {
    const install = vi.fn()
    const search = vi.fn(async (): Promise<SkillRegistrySearchResult> => ({
      results: [makeRow({ installed: true })],
      diagnostics: [],
      message: '',
    }))

    const host = await mountMarket(search as SkillCatalog['search'], install as SkillCatalog['install'])

    expect(host.querySelector('[data-testid="market-installed-badge"]')).toBeTruthy()
    const button = host.querySelector<HTMLButtonElement>('[data-testid="market-install-pdf-toolkit"]')
    expect(button?.disabled).toBe(true)
    expect(button?.textContent).toContain('Installed')
    expect(install).not.toHaveBeenCalled()
  })

  it('installs a row through the registry', async () => {
    const install = vi.fn(async () => ({
      success: true,
      installed: true,
      name: 'pdf-toolkit',
      installId: 'install:pdf',
    }))
    const search = vi.fn(async (): Promise<SkillRegistrySearchResult> => ({
      results: [makeRow()],
      diagnostics: [],
      message: '',
    }))

    const host = await mountMarket(search as SkillCatalog['search'], install as unknown as SkillCatalog['install'])

    const button = host.querySelector<HTMLButtonElement>('[data-testid="market-install-pdf-toolkit"]')
    expect(button).toBeTruthy()
    button!.click()

    const { nextTick } = await import('vue')
    await nextTick()
    await nextTick()
    expect(install).toHaveBeenCalledWith(expect.objectContaining({
      identifier: 'pdf-toolkit',
      source: 'skillhub',
    }))
  })

  it('surfaces gateway messages as an alert', async () => {
    const search = vi.fn(async (): Promise<SkillRegistrySearchResult> => ({
      results: [],
      diagnostics: [],
      message: 'No skill sources configured',
    }))

    const host = await mountMarket(search as SkillCatalog['search'])
    const alert = host.querySelector('[data-testid="market-error"]')
    expect(alert?.textContent).toContain('No skill sources configured')
    expect(host.querySelector('[data-testid="market-empty"]')).toBeTruthy()
  })

  it('opens a detail preview from a card and closes it', async () => {
    const search = vi.fn(async (): Promise<SkillRegistrySearchResult> => ({
      results: [makeRow()],
      diagnostics: [],
      message: '',
    }))

    const host = await mountMarket(search as SkillCatalog['search'])
    expect(document.querySelector('[data-testid="market-detail-dialog"]')).toBeNull()

    const opener = host.querySelector<HTMLButtonElement>('[data-testid="market-detail-pdf-toolkit"]')
    expect(opener).toBeTruthy()
    opener!.click()

    const { nextTick } = await import('vue')
    await nextTick()
    await nextTick()

    // The dialog teleports to body, outside the view host.
    const dialog = document.querySelector('[data-testid="market-detail-dialog"]')
    expect(dialog).toBeTruthy()
    expect(dialog?.textContent).toContain('Comprehensive PDF manipulation toolkit')
    expect(dialog?.textContent).toContain('awspace')
    expect(dialog?.querySelector('[data-testid="market-detail-install"]')).toBeTruthy()

    dialog?.querySelector<HTMLButtonElement>('.sk-mdlg__close')?.click()
    // The leave transition unmounts on the next animation frame.
    await vi.waitFor(() => {
      expect(document.querySelector('[data-testid="market-detail-dialog"]')).toBeNull()
    }, { timeout: 2000 })
  })

  it('shows a trending shelf in browse mode, ordered by downloads', async () => {
    const search = vi.fn(async (): Promise<SkillRegistrySearchResult> => ({
      results: [
        makeRow({ name: 'small', identifier: 'small', installReference: 'small', downloads: 100 }),
        makeRow({ name: 'big', identifier: 'big', installReference: 'big', downloads: 99999 }),
        makeRow({ name: 'mid', identifier: 'mid', installReference: 'mid', downloads: 5000 }),
      ],
      diagnostics: [],
      message: '',
    }))

    const host = await mountMarket(search as SkillCatalog['search'])
    const featured = host.querySelector('[data-testid="market-featured"]')
    expect(featured).toBeTruthy()
    const order = [...(featured?.querySelectorAll('button[data-testid^="market-featured-"]') || [])]
      .map(el => el.getAttribute('data-testid'))
    expect(order).toEqual(['market-featured-big', 'market-featured-mid', 'market-featured-small'])

    // The shelf opens the same detail preview.
    ;(featured?.querySelector('[data-testid="market-featured-big"]') as HTMLButtonElement).click()
    const { nextTick } = await import('vue')
    await nextTick()
    expect(document.querySelector('[data-testid="market-detail-dialog"]')?.textContent)
      .toContain('Comprehensive PDF manipulation toolkit')
  })

  it('hides the trending shelf while a search query is active', async () => {
    const search = vi.fn(async (): Promise<SkillRegistrySearchResult> => ({
      results: [makeRow()],
      diagnostics: [],
      message: '',
    }))

    const host = await mountMarket(search as SkillCatalog['search'])
    const { nextTick } = await import('vue')
    const input = host.querySelector<HTMLInputElement>('[data-testid="market-search-input"]')!
    input.value = 'pdf'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    host.querySelector<HTMLButtonElement>('[data-testid="market-search-button"]')!.click()
    await nextTick()
    await nextTick()

    expect(host.querySelector('[data-testid="market-featured"]')).toBeNull()
    expect(host.querySelector('[data-testid="market-grid"]')).toBeTruthy()
  })

  it('stars a card, persists the favorite, and filters the grid to favorites', async () => {
    const search = vi.fn(async (): Promise<SkillRegistrySearchResult> => ({
      results: [makeRow(), makeRow({
        name: 'big-thing',
        identifier: 'big-thing',
        installReference: 'big-thing',
        downloads: 900,
      })],
      diagnostics: [],
      message: '',
    }))

    const host = await mountMarket(search as SkillCatalog['search'])
    const { nextTick } = await import('vue')

    expect(host.querySelector('[data-testid="market-favorites-filter"]')).toBeNull()
    const star = host.querySelector<HTMLButtonElement>('[data-testid="market-favorite-pdf-toolkit"]')!
    expect(star.getAttribute('aria-pressed')).toBe('false')
    star.click()
    await nextTick()

    expect(star.getAttribute('aria-pressed')).toBe('true')
    expect(localStorage.getItem('opensquilla.skillMarket.favorites')).toBe('["pdf-toolkit"]')

    const filter = host.querySelector<HTMLButtonElement>('[data-testid="market-favorites-filter"]')!
    filter.click()
    await nextTick()

    expect(host.querySelector('[data-testid="market-card-pdf-toolkit"]')).toBeTruthy()
    expect(host.querySelector('[data-testid="market-card-big-thing"]')).toBeNull()
    expect(host.querySelector('[data-testid="market-featured"]')).toBeNull()

    filter.click()
    await nextTick()
    expect(host.querySelector('[data-testid="market-card-big-thing"]')).toBeTruthy()
  })

  it('toggles the favorite from the detail dialog footer', async () => {
    const search = vi.fn(async (): Promise<SkillRegistrySearchResult> => ({
      results: [makeRow()],
      diagnostics: [],
      message: '',
    }))

    const host = await mountMarket(search as SkillCatalog['search'])
    const { nextTick } = await import('vue')

    host.querySelector<HTMLButtonElement>('[data-testid="market-detail-pdf-toolkit"]')!.click()
    await nextTick()
    const dialogStar = document.querySelector<HTMLButtonElement>(
      '[data-testid="market-detail-favorite"]',
    )!
    expect(dialogStar.getAttribute('aria-pressed')).toBe('false')
    dialogStar.click()
    await nextTick()

    expect(dialogStar.getAttribute('aria-pressed')).toBe('true')
    expect(localStorage.getItem('opensquilla.skillMarket.favorites')).toBe('["pdf-toolkit"]')
    expect(host.querySelector('[data-testid="market-favorite-pdf-toolkit"]')
      ?.getAttribute('aria-pressed')).toBe('true')
  })
})
