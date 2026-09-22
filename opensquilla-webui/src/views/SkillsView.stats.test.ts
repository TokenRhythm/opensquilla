// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

async function mountSkillsView(reloadResult: Record<string, unknown> | Promise<Record<string, unknown>> = {
  success: true,
  changed: true,
  partial: false,
  generation: 2,
  added: ['new-skill'],
  removed: [],
  modified: [],
  errors: [],
}, loadDataResult: boolean | undefined = undefined, queueBusy = false) {
  vi.resetModules()

  const { createApp, defineComponent, h, KeepAlive, nextTick, ref } = await import('vue')
  const { createPinia, setActivePinia } = await import('pinia')
  const i18n = (await import('@/i18n')).default

  const setStatusFilter = vi.fn()
  const loadData = vi.fn(async () => loadDataResult)
  const scrollIntoView = vi.fn()
  const rpcCall = vi.fn(async (_method: string) => reloadResult)
  const ready = vi.fn(async () => {})
  const pushToast = vi.fn()
  const routeState = ref<{ query: { skill?: string } }>({ query: {} })
  const allSkills = ref<Array<{ name: string; active?: boolean; install_id?: string }>>([])
  const detail = vi.fn(async (skill: { name: string }) => ({ ...skill, content: 'Synthetic content' }))
  const push = vi.fn(async () => undefined)
  const listCandidates = vi.fn(async () => ({ generation: 1, candidates: [{ name: 'synthetic-target', instanceId: 'skill:synthetic', digest: 'a'.repeat(64), kind: 'skill', disabled: false, ready: true }] }))
  vi.doMock('vue-router', () => ({ useRoute: () => routeState.value, useRouter: () => ({ push }) }))

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
    default: defineComponent({
      props: ['skill', 'canUseInTask', 'mutationDisabled', 'loadingContent'],
      emits: ['useInTask', 'close'],
      setup(props, { emit }) {
        return () => h('div', { 'data-testid': 'skill-detail-dialog' }, props.skill ? [
          props.canUseInTask ? h('button', { 'data-testid': 'use-skill', disabled: props.mutationDisabled || props.loadingContent, onClick: () => emit('useInTask', props.skill) }, 'Use in new task') : null,
          h('button', { 'data-testid': 'close-skill', onClick: () => emit('close') }, 'Close'),
        ] : [])
      },
    }),
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
  vi.doMock('@/components/skills/SkillsAddDrawer.vue', () => ({
    default: defineComponent({
      name: 'SkillsAddDrawerStub',
      props: { open: Boolean },
      emits: ['viewDetails'],
      setup(props, { emit }) {
        return () => props.open
          ? h('section', { 'data-testid': 'skills-add-drawer' }, [
              'add skill',
              h('button', {
                'data-testid': 'registry-view-details',
                type: 'button',
                onClick: () => emit('viewDetails', 'install:synthetic', 'clawhub', 'synthetic-installed'),
              }, 'View details'),
            ])
          : null
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
    useRpcStore: () => ({ call: rpcCall, ready }),
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
    useSkillRegistry: (_rpc: unknown, _loadData: unknown, mutationGate: {
      acquire: (owner: string) => boolean
      busy: { value: boolean }
    }) => {
      const queueRunning = ref(queueBusy)
      if (queueBusy) mutationGate.acquire('install_queue')
      return {
        registryQuery: ref(''),
        githubUrl: ref(''),
        registryResults: ref([]),
        registryLoading: ref(false),
        registryDiagnostics: ref([]),
        registrySearchError: ref(''),
        installingId: ref(null),
        installActivities: ref({
          clawhub: { items: [], refreshWarning: '' },
          github: { items: [], refreshWarning: '' },
        }),
        runningSource: ref(queueBusy ? 'clawhub' : null),
        queueRunning,
        mutationBusy: mutationGate.busy,
        installingDepsId: ref(null),
        uninstallingName: ref(null),
        searchRegistry: vi.fn(async () => {}),
        installGithub: vi.fn(async () => {}),
        installSkill: vi.fn(async () => {}),
        retryQueueItem: vi.fn(async () => {}),
        clearInstallActivity: vi.fn(),
        installDeps: vi.fn(async () => true),
        uninstallSkill: vi.fn(async () => true),
      }
    },
  }))
  vi.doMock('@/composables/skills/useSkillsCatalog', () => ({
    normalizeSkill: (skill: unknown) => skill,
    skillCatalogKey: (skill: { name: string }) => skill.name,
    skillLayerHelp: (key: string) => `help:${key}`,
    skillLayerLabel: (key: string) => `label:${key}`,
    isMetaSkill: (skill: { kind?: string }) => skill.kind === 'meta' || skill.kind === 'meta_sop',
    useSkillsCatalog: () => ({
      allSkills,
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

  const viewActive = ref(true)
  const Root = defineComponent({
    setup() {
      return () => h(KeepAlive, null, {
        default: () => viewActive.value ? h(Component) : null,
      })
    },
  })
  const app = createApp(Root)
  app.use(pinia)
  app.use(i18n)
  const { SKILL_CATALOG_KEY } = await import('@/modules/skillCatalog')
  app.provide(SKILL_CATALOG_KEY, {
    reload: () => rpcCall('skills.reload'),
    detail,
    supportsCandidates: () => true,
    listCandidates,
  } as never)
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
    ready,
    pushToast,
    viewActive,
    routeState,
    allSkills,
    detail,
    push,
    listCandidates,
  }
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.doUnmock('vue-router')
  vi.doUnmock('@/components/Icon.vue')
  vi.doUnmock('@/components/ControlSwitch.vue')
  vi.doUnmock('@/components/skills/AutoEnabledSkills.vue')
  vi.doUnmock('@/components/skills/SkillDetailDialog.vue')
  vi.doUnmock('@/components/skills/SkillGroup.vue')
  vi.doUnmock('@/components/skills/PendingSkillProposals.vue')
  vi.doUnmock('@/components/skills/SkillsAddDrawer.vue')
  vi.doUnmock('@/components/skills/SkillsStats.vue')
  vi.doUnmock('@/composables/skills/useSkillProposals')
  vi.doUnmock('@/composables/skills/useSkillRegistry')
  vi.doUnmock('@/composables/skills/useSkillsCatalog')
  vi.doUnmock('@/composables/useToasts')
  vi.doUnmock('@/stores/rpc')
})

describe('SkillsView stats navigation', () => {
  it('opens the catalog detail dialog from an installed registry result', async () => {
    const { app, el, allSkills, detail, nextTick } = await mountSkillsView()
    allSkills.value = [{ name: 'synthetic-installed', install_id: 'install:synthetic', active: true }]

    el.querySelector<HTMLButtonElement>('[data-testid="skills-add-trigger"]')!.click()
    await nextTick()
    el.querySelector<HTMLButtonElement>('[data-testid="registry-view-details"]')!.click()
    await nextTick()
    await nextTick()

    expect(detail).toHaveBeenCalledWith(expect.objectContaining({
      name: 'synthetic-installed',
      install_id: 'install:synthetic',
    }))
    expect(el.querySelector('[data-testid="skill-detail-dialog"]')?.textContent)
      .toContain('Close')
    app.unmount()
  })

  it('resolves candidates only on launch and hands the selected skill to an unsent draft', async () => {
    const { app, el, routeState, allSkills, listCandidates, push, nextTick } = await mountSkillsView()
    allSkills.value = [{ name: 'synthetic-target', active: true }]
    routeState.value.query.skill = 'synthetic-target'
    await nextTick()
    await nextTick()
    await vi.waitFor(() => expect(el.querySelector<HTMLButtonElement>('[data-testid="use-skill"]')?.disabled).toBe(false))
    expect(listCandidates).not.toHaveBeenCalled()
    el.querySelector<HTMLButtonElement>('[data-testid="use-skill"]')!.click()
    await nextTick()
    await nextTick()
    expect(listCandidates).toHaveBeenCalledOnce()
    expect(push).toHaveBeenCalledExactlyOnceWith({ path: '/chat/new', query: { agent: 'main' }, state: {
      prefill: '', autosend: false,
      selectedSkillPrefill: [{ name: 'synthetic-target', instanceId: 'skill:synthetic', digest: 'a'.repeat(64) }],
    } })
    app.unmount()
  })

  it.each(['close', 'deactivate'] as const)('ignores a pending launch after %s', async action => {
    const { app, el, routeState, allSkills, listCandidates, push, pushToast, viewActive, nextTick } = await mountSkillsView()
    const candidates = await listCandidates()
    listCandidates.mockClear()
    let resolveCandidates!: (value: typeof candidates) => void
    listCandidates.mockReturnValueOnce(new Promise(resolve => { resolveCandidates = resolve }))
    allSkills.value = [{ name: 'synthetic-target', active: true }]
    routeState.value.query.skill = 'synthetic-target'
    await vi.waitFor(() => expect(el.querySelector<HTMLButtonElement>('[data-testid="use-skill"]')?.disabled).toBe(false))
    el.querySelector<HTMLButtonElement>('[data-testid="use-skill"]')!.click()
    await nextTick()
    expect(el.querySelector<HTMLButtonElement>('[data-testid="use-skill"]')?.disabled).toBe(true)
    if (action === 'close') el.querySelector<HTMLButtonElement>('[data-testid="close-skill"]')!.click()
    else viewActive.value = false
    await nextTick()

    resolveCandidates(candidates)
    await nextTick()
    await nextTick()
    expect(listCandidates).toHaveBeenCalledOnce()
    expect(push).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
    app.unmount()
  })

  it('allows retry after a candidate lookup fails without navigating', async () => {
    const { app, el, routeState, allSkills, listCandidates, push, pushToast, nextTick } = await mountSkillsView()
    listCandidates.mockRejectedValueOnce(new Error('Synthetic lookup failed'))
    allSkills.value = [{ name: 'synthetic-target', active: true }]
    routeState.value.query.skill = 'synthetic-target'
    await vi.waitFor(() => expect(el.querySelector<HTMLButtonElement>('[data-testid="use-skill"]')?.disabled).toBe(false))
    el.querySelector<HTMLButtonElement>('[data-testid="use-skill"]')!.click()
    await nextTick()
    await nextTick()
    expect(push).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledWith('Synthetic lookup failed', { tone: 'danger' })
    expect(el.querySelector<HTMLButtonElement>('[data-testid="use-skill"]')?.disabled).toBe(false)

    el.querySelector<HTMLButtonElement>('[data-testid="use-skill"]')!.click()
    await nextTick()
    await nextTick()
    expect(push).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('opens the requested skill when a slash-menu management link updates the route', async () => {
    const { app, routeState, allSkills, detail, nextTick } = await mountSkillsView()
    allSkills.value = [{ name: 'synthetic-target', active: true }]
    routeState.value.query.skill = 'synthetic-target'
    await nextTick()
    await nextTick()
    expect(detail).toHaveBeenCalledWith(expect.objectContaining({ name: 'synthetic-target' }))
    app.unmount()
  })

  it('keeps the catalog visible when a status tile is selected', async () => {
    const { app, el, nextTick, setStatusFilter } = await mountSkillsView()
    const catalog = el.querySelector<HTMLElement>('[data-testid="skills-catalog"]')

    expect(catalog).not.toBeNull()
    expect(el.querySelector('[role="tablist"]')).toBeNull()
    expect(document.querySelector('.sk-add-drawer')).toBeNull()
    expect(el.querySelector('[data-testid="skills-add-trigger"]')?.getAttribute('aria-expanded')).toBe('false')

    el.querySelector<HTMLButtonElement>('[data-testid="skills-overview"]')?.click()
    await nextTick()
    el.querySelector<HTMLButtonElement>('[data-testid="stat-needs-setup"]')?.click()
    await nextTick()

    expect(setStatusFilter).toHaveBeenCalledWith('needs-setup')
    expect(catalog?.isConnected).toBe(true)
    app.unmount()
  })

  it('scrolls to proposed skills without changing surfaces', async () => {
    const { app, el, nextTick, scrollIntoView } = await mountSkillsView()

    el.querySelector<HTMLButtonElement>('[data-testid="skills-overview"]')?.click()
    await nextTick()
    el.querySelector<HTMLButtonElement>('[data-testid="stat-proposals"]')?.click()
    await nextTick()
    await nextTick()

    expect(el.querySelector('[data-testid="skills-catalog"]')).not.toBeNull()
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    app.unmount()
  })
})

describe('SkillsView catalog reload', () => {
  it('defers activation refreshes while an install batch owns the catalog', async () => {
    const { app, loadData, nextTick, viewActive } = await mountSkillsView(
      undefined,
      undefined,
      true,
    )

    expect(loadData).not.toHaveBeenCalled()
    viewActive.value = false
    await nextTick()
    viewActive.value = true
    await nextTick()
    expect(loadData).not.toHaveBeenCalled()
    app.unmount()
  })

  it('disables manual catalog reload while the install queue owns mutations', async () => {
    const { app, el, nextTick, rpcCall } = await mountSkillsView(undefined, undefined, true)

    el.querySelector<HTMLButtonElement>('[data-testid="skills-overview"]')?.click()
    await nextTick()
    const reload = el.querySelector<HTMLButtonElement>('[data-testid="skills-reload"]')
    expect(reload?.disabled).toBe(true)
    expect(el.querySelector<HTMLButtonElement>('[data-testid="skills-add-trigger"]')?.disabled)
      .toBe(false)
    reload?.click()
    expect(rpcCall).not.toHaveBeenCalled()
    app.unmount()
  })

  it('blocks Add Skill while manual reload owns mutations', async () => {
    let finishReload: ((value: Record<string, unknown>) => void) | undefined
    const reloadPending = new Promise<Record<string, unknown>>((resolve) => {
      finishReload = resolve
    })
    const { app, el, nextTick, rpcCall } = await mountSkillsView(reloadPending)

    el.querySelector<HTMLButtonElement>('[data-testid="skills-overview"]')?.click()
    await nextTick()
    el.querySelector<HTMLButtonElement>('[data-testid="skills-reload"]')?.click()
    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith('skills.reload'))
    expect(el.querySelector<HTMLButtonElement>('[data-testid="skills-add-trigger"]')?.disabled)
      .toBe(true)

    finishReload?.({
      success: true,
      changed: false,
      partial: false,
      generation: 2,
    })
    await vi.waitFor(() => {
      expect(el.querySelector<HTMLButtonElement>('[data-testid="skills-add-trigger"]')?.disabled)
        .toBe(false)
    })
    app.unmount()
  })

  it('does not force reload when the view is displayed', async () => {
    const { app, rpcCall } = await mountSkillsView()

    expect(rpcCall).not.toHaveBeenCalled()
    app.unmount()
  })

  it('calls reload before listing the published catalog', async () => {
    const { app, el, loadData, rpcCall, pushToast } = await mountSkillsView()
    loadData.mockClear()

    el.querySelector<HTMLButtonElement>('[data-testid="skills-overview"]')?.click()
    await vi.waitFor(() => expect(el.querySelector('[data-testid="skills-reload"]')).not.toBeNull())
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

    el.querySelector<HTMLButtonElement>('[data-testid="skills-overview"]')?.click()
    await vi.waitFor(() => expect(el.querySelector('[data-testid="skills-reload"]')).not.toBeNull())
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

    el.querySelector<HTMLButtonElement>('[data-testid="skills-overview"]')?.click()
    await vi.waitFor(() => expect(el.querySelector('[data-testid="skills-reload"]')).not.toBeNull())
    el.querySelector<HTMLButtonElement>('[data-testid="skills-reload"]')?.click()
    await vi.waitFor(() => expect(pushToast).toHaveBeenCalled())

    expect(pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'danger' })
    app.unmount()
  })
})
