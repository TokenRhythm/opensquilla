// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

async function mountSkillsView(reloadResult: Record<string, unknown> = {
  success: true,
  changed: true,
  partial: false,
  generation: 2,
  added: ['new-skill'],
  removed: [],
  modified: [],
  errors: [],
}, loadDataResult: boolean | undefined = undefined) {
  vi.resetModules()

  const { createApp, defineComponent, h, nextTick, ref } = await import('vue')
  const { createPinia, setActivePinia } = await import('pinia')
  const i18n = (await import('@/i18n')).default

  const setStatusFilter = vi.fn()
  const loadData = vi.fn(async () => loadDataResult)
  const scrollIntoView = vi.fn()
  const rpcCall = vi.fn(async () => reloadResult)
  const waitForConnection = vi.fn(async () => {})
  const pushToast = vi.fn()

  const iconStub = defineComponent({
    name: 'IconStub',
    setup() {
      return () => h('span')
    },
  })
  const emptyStub = (name: string) => defineComponent({
    name,
    setup(_, { slots }) {
      return () => h('div', { 'data-testid': name }, slots.default?.())
    },
  })

  vi.doMock('@/components/Icon.vue', () => ({ default: iconStub }))
  vi.doMock('@/components/ControlSwitch.vue', () => ({ default: emptyStub('control-switch') }))
  vi.doMock('@/components/skills/AutoEnabledSkills.vue', () => ({
    default: emptyStub('auto-enabled-skills'),
  }))
  vi.doMock('@/components/skills/SkillDetailDialog.vue', () => ({
    default: emptyStub('skill-detail-dialog'),
  }))
  vi.doMock('@/components/skills/SkillGroup.vue', () => ({
    default: defineComponent({
      name: 'SkillGroupStub',
      props: {
        title: String,
      },
      setup(props) {
        return () => h('section', { 'data-testid': 'skill-group' }, props.title)
      },
    }),
  }))
  vi.doMock('@/components/skills/PendingSkillProposals.vue', () => ({
    default: defineComponent({
      name: 'PendingSkillProposalsStub',
      setup(_, { expose }) {
        expose({ scrollIntoView })
        return () => h('section', { 'data-testid': 'pending-proposals' })
      },
    }),
  }))
  vi.doMock('@/components/skills/SkillsRegistryPanel.vue', () => ({
    default: defineComponent({
      name: 'SkillsRegistryPanelStub',
      setup() {
        return () => h('section', { 'data-testid': 'registry-panel' }, 'registry')
      },
    }),
  }))
  vi.doMock('@/components/skills/SkillsStats.vue', () => ({
    default: defineComponent({
      name: 'SkillsStatsStub',
      props: {
        tiles: { type: Array, required: true },
        proposalCount: { type: Number, default: 0 },
      },
      emits: ['select', 'show-proposals'],
      setup(props, { emit }) {
        return () => h('div', { 'data-testid': 'skills-stats' }, [
          ...(props.tiles as Array<{ key: string; label: string }>).map((tile) => h(
            'button',
            {
              'data-testid': `stat-${tile.key}`,
              type: 'button',
              onClick: () => emit('select', tile.key),
            },
            tile.label,
          )),
          props.proposalCount > 0
            ? h(
              'button',
              {
                'data-testid': 'stat-proposals',
                type: 'button',
                onClick: () => emit('show-proposals'),
              },
              'Proposals',
            )
            : null,
        ])
      },
    }),
  }))
  vi.doMock('@/stores/rpc', () => ({
    useRpcStore: () => ({ call: rpcCall, waitForConnection }),
  }))
  vi.doMock('@/composables/useToasts', () => ({
    useToasts: () => ({ pushToast }),
  }))

  vi.doMock('@/composables/skills/useSkillProposals', () => ({
    useSkillProposals: () => ({
      proposals: ref([{ id: 'proposal-1' }]),
      autoEnabledSkills: ref([]),
      proposalsSettings: ref({ available: false }),
      proposalsSettingsOn: ref(false),
      loadProposals: vi.fn(async () => {}),
      toggleAutoPropose: vi.fn(),
      setAutoEnableRisk: vi.fn(),
      showProposal: vi.fn(async () => null),
      acceptProposal: vi.fn(),
      rejectProposal: vi.fn(),
      disableAutoEnabled: vi.fn(),
    }),
  }))
  vi.doMock('@/composables/skills/useSkillRegistry', () => ({
    useSkillRegistry: () => ({
      registryQuery: ref(''),
      githubUrl: ref(''),
      registryResults: ref([]),
      registryLoading: ref(false),
      installingId: ref(null),
      installingDepsId: ref(null),
      uninstallingName: ref(null),
      searchRegistry: vi.fn(async () => {}),
      installGithub: vi.fn(async () => {}),
      installSkill: vi.fn(async () => {}),
      installDeps: vi.fn(async () => true),
      uninstallSkill: vi.fn(async () => true),
    }),
  }))
  vi.doMock('@/composables/skills/useSkillsCatalog', () => ({
    skillLayerHelp: (key: string) => `help:${key}`,
    skillLayerLabel: (key: string) => `label:${key}`,
    useSkillsCatalog: () => ({
      filterText: ref(''),
      statusFilter: ref('all'),
      metaSkills: ref([]),
      visibleLayerGroups: ref([{ key: 'community', skills: [] }]),
      installedEmpty: ref(false),
      emptyMessage: ref(''),
      statTiles: ref([
        { key: 'all', label: 'All skills', value: '51', hint: 'all' },
        { key: 'ready', label: 'Ready', value: '20', hint: 'ready' },
        { key: 'needs-setup', label: 'Needs setup', value: '7', hint: 'awaiting deps' },
      ]),
      setStatusFilter,
      loadData,
    }),
  }))

  const pinia = createPinia()
  setActivePinia(pinia)
  i18n.global.locale.value = 'en'

  const Component = (await import('./SkillsView.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)

  const app = createApp(Component)
  app.use(pinia)
  app.use(i18n)
  app.mount(el)
  await nextTick()

  return {
    app,
    el,
    nextTick,
    setStatusFilter,
    scrollIntoView,
    loadData,
    rpcCall,
    waitForConnection,
    pushToast,
  }
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.doUnmock('@/components/Icon.vue')
  vi.doUnmock('@/components/ControlSwitch.vue')
  vi.doUnmock('@/components/skills/AutoEnabledSkills.vue')
  vi.doUnmock('@/components/skills/SkillDetailDialog.vue')
  vi.doUnmock('@/components/skills/SkillGroup.vue')
  vi.doUnmock('@/components/skills/PendingSkillProposals.vue')
  vi.doUnmock('@/components/skills/SkillsRegistryPanel.vue')
  vi.doUnmock('@/components/skills/SkillsStats.vue')
  vi.doUnmock('@/composables/skills/useSkillProposals')
  vi.doUnmock('@/composables/skills/useSkillRegistry')
  vi.doUnmock('@/composables/skills/useSkillsCatalog')
  vi.doUnmock('@/composables/useToasts')
  vi.doUnmock('@/stores/rpc')
})

describe('SkillsView stats navigation', () => {
  it('returns to Installed when a status tile is clicked from Community', async () => {
    const { app, el, nextTick, setStatusFilter } = await mountSkillsView()
    const installedPanel = el.querySelector<HTMLElement>('#sk-panel-installed')
    const registryPanel = el.querySelector<HTMLElement>('#sk-panel-registry')

    el.querySelector<HTMLButtonElement>('#sk-tab-registry')?.click()
    await nextTick()
    expect(installedPanel?.style.display).toBe('none')
    expect(registryPanel?.style.display).not.toBe('none')

    el.querySelector<HTMLButtonElement>('[data-testid="stat-needs-setup"]')?.click()
    await nextTick()

    expect(setStatusFilter).toHaveBeenCalledWith('needs-setup')
    expect(installedPanel?.style.display).not.toBe('none')
    expect(registryPanel?.style.display).toBe('none')
    expect(el.querySelector('#sk-tab-installed')?.getAttribute('aria-selected')).toBe('true')
    app.unmount()
  })

  it('returns to Installed before scrolling to proposed skills', async () => {
    const { app, el, nextTick, scrollIntoView } = await mountSkillsView()
    const installedPanel = el.querySelector<HTMLElement>('#sk-panel-installed')
    const registryPanel = el.querySelector<HTMLElement>('#sk-panel-registry')

    el.querySelector<HTMLButtonElement>('#sk-tab-registry')?.click()
    await nextTick()

    el.querySelector<HTMLButtonElement>('[data-testid="stat-proposals"]')?.click()
    await nextTick()
    await nextTick()

    expect(installedPanel?.style.display).not.toBe('none')
    expect(registryPanel?.style.display).toBe('none')
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    app.unmount()
  })
})

describe('SkillsView catalog reload', () => {
  it('does not force reload when the view is displayed', async () => {
    const { app, rpcCall } = await mountSkillsView()

    expect(rpcCall).not.toHaveBeenCalled()
    app.unmount()
  })

  it('calls reload before listing the published catalog', async () => {
    const { app, el, loadData, rpcCall, pushToast } = await mountSkillsView()
    loadData.mockClear()

    el.querySelector<HTMLButtonElement>('[data-testid="skills-reload"]')?.click()
    await vi.waitFor(() => expect(loadData).toHaveBeenCalledTimes(1))

    expect(rpcCall).toHaveBeenCalledExactlyOnceWith('skills.reload')
    expect(rpcCall.mock.invocationCallOrder[0]).toBeLessThan(
      loadData.mock.invocationCallOrder[0],
    )
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining('generation 2'), {
      tone: 'ok',
    })
    app.unmount()
  })

  it('shows a warning when reload publishes a partial catalog', async () => {
    const { app, el, pushToast } = await mountSkillsView({
      success: true,
      changed: true,
      partial: true,
      generation: 3,
      added: [],
      removed: [],
      modified: ['existing-skill'],
      errors: [{ message: 'invalid frontmatter', kept_previous: true }],
    })

    el.querySelector<HTMLButtonElement>('[data-testid="skills-reload"]')?.click()
    await vi.waitFor(() => expect(pushToast).toHaveBeenCalled())

    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining('1 error'), {
      tone: 'warn',
    })
    app.unmount()
  })

  it('does not report success when the refreshed list fails to load', async () => {
    const { app, el, pushToast } = await mountSkillsView({
      success: true,
      changed: true,
      partial: false,
      generation: 4,
      added: ['new-skill'],
      removed: [],
      modified: [],
      errors: [],
    }, false)

    el.querySelector<HTMLButtonElement>('[data-testid="skills-reload"]')?.click()
    await vi.waitFor(() => expect(pushToast).toHaveBeenCalled())

    expect(pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'danger' })
    app.unmount()
  })
})
