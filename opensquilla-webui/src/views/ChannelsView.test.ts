// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const channelRows = [
  {
    name: 'ops-slack',
    type: 'slack',
    status: 'connected',
    connected: true,
    enabled: true,
    configured: true,
    connected_since: '2026-07-13T08:00:00Z',
    restart_attempts: 1,
    bot_user_id: 'U-BOT',
    capability_profile: {
      transports: ['webhook'],
      maturity: 'YELLOW-experimental',
      evidence: {
        reply: {
          declared: true,
          implemented: true,
          effective: true,
          evidence_kind: 'method',
          methods: ['build_reply_message'],
          proof_status: 'unverified',
        },
        group_chat: {
          declared: true,
          implemented: true,
          effective: true,
          evidence_kind: 'declaration',
          methods: [],
          proof_status: 'unverified',
        },
      },
    },
    diagnostics: {
      network_probe: 'not_run',
      delivery: {
        ingress: { accepted: { count: 1 } },
        outbox: { sent: { count: 2 } },
        leases: [],
      },
    },
  },
  {
    name: 'alerts-telegram',
    type: 'telegram',
    status: 'stopped',
    connected: false,
    enabled: true,
    configured: true,
    capability_profile: {
      transports: ['polling'],
      maturity: 'YELLOW-experimental',
      evidence: {},
    },
    diagnostics: { network_probe: 'not_run' },
  },
  {
    name: 'dead-telegram',
    type: 'telegram',
    status: 'dead',
    connected: false,
    enabled: true,
    configured: true,
    capability_profile: {
      transports: ['polling'],
      maturity: 'YELLOW-experimental',
      evidence: {},
    },
    diagnostics: {
      network_probe: 'not_run',
      last_error: { message: '401 Unauthorized — bot token rejected', error_class: 'auth_invalid' },
    },
  },
  {
    name: 'off-discord',
    type: 'discord',
    status: 'disabled',
    connected: false,
    enabled: false,
    configured: true,
    diagnostics: { network_probe: 'not_run' },
  },
  {
    // Failed during gateway start: wire status is "stopped" but the
    // diagnostics carry the startup error (source: start_error).
    name: 'boot-failed-slack',
    type: 'slack',
    status: 'stopped',
    connected: false,
    enabled: true,
    configured: true,
    diagnostics: {
      network_probe: 'not_run',
      last_error: { message: 'invalid app credentials', error_class: 'auth_invalid', source: 'start_error' },
    },
  },
  {
    name: 'ghost-runtime',
    type: 'slack',
    status: 'connected',
    connected: true,
    enabled: true,
    configured: false,
    diagnostics: { network_probe: 'not_run' },
  },
]

function buttonWithText(root: ParentNode, label: string): HTMLButtonElement {
  const button = Array.from(root.querySelectorAll<HTMLButtonElement>('button'))
    .find(candidate => candidate.textContent?.trim() === label)
  if (!button) throw new Error(`button not found: ${label}`)
  return button
}

function channelCard(root: ParentNode, name: string): HTMLElement {
  const card = Array.from(root.querySelectorAll<HTMLElement>('.chb-story'))
    .find(candidate => candidate.querySelector('.chb-story__name')?.textContent === name)
  if (!card) throw new Error(`channel card not found: ${name}`)
  return card
}

async function mountChannelsView(options: {
  loadPairings?: (params?: Record<string, unknown>) => Promise<unknown>
  adminSenders?: Record<string, string[]>
  /** Override the initial pairing rows served by the channels.pairings mock. */
  initialPairings?: Array<Record<string, unknown>>
  /** When set, config.get rejects (admin counts become unknown). */
  configGetError?: boolean
  /** When set, approve commits but the asAdmin grant fails non-fatally. */
  adminGrantFails?: boolean
  /** When set, onboarding.channel.probe rejects with this message. */
  draftProbeError?: { message: string }
  /** Initial route query (deep-link scenarios). */
  routeQuery?: Record<string, string>
  /** Override the configured channel rows (drives the home mode: 0 → inline
   *  gallery, >=1 → fleet front page + enroll strip). Defaults to the shared
   *  fixture. */
  channelRows?: Array<Record<string, unknown>>
  /** Override the channels.get response (drill configuration seeding). */
  channelsGet?: (params?: Record<string, unknown>) => unknown
  locale?: string
} = {}) {
  vi.resetModules()

  const { KeepAlive, createApp, defineComponent, h, nextTick, ref } = await import('vue')
  const i18nModule = await import('@/i18n')
  const i18n = i18nModule.default
  const locale = (options.locale || 'en') as 'en'
  if (locale !== 'en') await i18nModule.loadLocaleMessages(locale)
  i18n.global.locale.value = locale

  // The drill-in page scrolls sections into view; happy-dom needs a stub.
  const scrollIntoView = vi.fn()
  Element.prototype.scrollIntoView = scrollIntoView

  const push = vi.fn(async (_to: { query?: Record<string, unknown> }) => {})
  const pushToast = vi.fn()
  const confirm = vi.fn(async () => true)
  interface PairingRow {
    pairingId: string
    pairingCode?: string
    channelName: string
    senderId: string
    senderName: string
    status: string
    createdAt?: string
    approvedAt?: string
  }
  let pairings: PairingRow[] = options.initialPairings
    ? options.initialPairings.map(pairing => ({ ...pairing }) as unknown as PairingRow)
    : [
    {
      pairingId: 'pair-pending',
      pairingCode: 'AB12CD34',
      channelName: 'ops-slack',
      senderId: 'U-PENDING',
      senderName: 'Pending User',
      status: 'pending',
      createdAt: '2026-07-13T08:30:00Z',
    },
    {
      pairingId: 'pair-approved',
      channelName: 'ops-slack',
      senderId: 'U-APPROVED',
      senderName: 'Approved User',
      status: 'approved',
      approvedAt: '2026-07-13T09:00:00Z',
    },
    {
      pairingId: 'pair-revoked',
      channelName: 'ops-slack',
      senderId: 'U-REVOKED',
      senderName: 'Revoked User',
      status: 'revoked',
    },
  ]
  const adminSendersMap: Record<string, string[]> = { ...(options.adminSenders || {}) }
  const rows = options.channelRows ?? channelRows
  const execute = vi.fn(async () => ({ channels: rows }))
  const refresh = vi.fn(async () => ({ channels: rows }))
  // Catalog slice mirroring the backend field-spec shape (groups, secrets,
  // show_when, advanced) for the in-place configuration editor.
  const slackSpec = {
    type: 'slack',
    label: 'Slack',
    description: 'Slack workspace bot.',
    transport: 'mixed',
    setupAids: [],
    fields: [
      { name: 'name', label: 'Channel name', type: 'text', required: true },
      { name: 'connection_mode', label: 'Connection mode', type: 'select', default: 'socket', choices: ['socket', 'webhook'] },
      { name: 'slack_channel_id', label: 'Default channel id', type: 'text', default: '' },
      { name: 'token', label: 'Bot token', type: 'password', required: true, secret: true, group: 'credentials' },
      { name: 'signing_secret', label: 'Signing secret', type: 'password', required: true, secret: true, group: 'credentials', showWhen: { connection_mode: 'webhook' } },
      { name: 'reply_in_thread', label: 'Reply in thread', type: 'bool', default: false, advanced: true },
    ],
  }
  const feishuSpec = {
    type: 'feishu',
    label: 'Feishu / Lark',
    description: 'Feishu (or Lark) bot.',
    transport: 'mixed',
    setupAids: [
      { id: 'scopes_json', kind: 'copy', content: '{"scopes":{}}' },
      { id: 'credentials_link', kind: 'link', content: 'https://open.feishu.cn/app/{app_id}/baseinfo' },
      { id: 'ws_order_note', kind: 'note' },
    ],
    fields: [
      { name: 'name', label: 'Channel name', type: 'text', required: true },
      { name: 'app_id', label: 'App id', type: 'text', required: true, group: 'credentials' },
      { name: 'app_secret', label: 'App secret', type: 'password', required: true, secret: true, group: 'credentials' },
      { name: 'connection_mode', label: 'Connection mode', type: 'select', default: 'websocket', choices: ['webhook', 'websocket'] },
      { name: 'domain', label: 'Domain', type: 'select', default: 'feishu', choices: ['feishu', 'lark'] },
    ],
  }
  // No dedicated credential/secret fields: the gallery footnote must fall
  // back to the required fields instead of rendering blank.
  const matrixSpec = {
    type: 'matrix',
    label: 'Matrix',
    description: 'Matrix homeserver client.',
    transport: 'http_sync',
    setupAids: [],
    fields: [
      { name: 'name', label: 'Channel name', type: 'text', required: true },
      { name: 'homeserver_url', label: 'Homeserver URL', type: 'text', required: true },
      { name: 'user_id', label: 'User id (@user:server)', type: 'text', required: true },
      { name: 'access_token', label: 'Access token', type: 'password', required: false, secret: true, default: '' },
    ],
  }
  const rpcCall = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'onboarding.catalog') {
      return { channels: [slackSpec, feishuSpec, matrixSpec] }
    }
    if (method === 'onboarding.channel.probe') {
      if (options.draftProbeError) throw new Error(options.draftProbeError.message)
      return { status: 'validated', connected: false, restartRequired: true, warnings: [] }
    }
    if (method === 'onboarding.channel.upsert') {
      const entry = params?.entry as Record<string, unknown> | undefined
      return { changed: true, restartRequired: true, entry: { name: entry?.name } }
    }
    if (method === 'onboarding.channel.enable' || method === 'onboarding.channel.disable') {
      return { changed: true, restartRequired: true }
    }
    if (method === 'channels.probe') {
      return { status: 'verified', connected: true, latencyMs: 17 }
    }
    if (method === 'channels.restart') {
      return { status: 'restarted', channel: params?.name }
    }
    if (method === 'channels.get') {
      if (options.channelsGet) return options.channelsGet(params)
      return {
        entry: {
          name: 'ops-slack',
          type: 'slack',
          token: '***',
          signing_secret: '***',
          connection_mode: 'webhook',
        },
        secretFields: ['token', 'signing_secret'],
      }
    }
    if (method === 'config.get') {
      // Card facts and the members panel read only the channel_admin_senders
      // map (bounded path read), mirroring the registry's dot-path navigation.
      if (options.configGetError) throw new Error('config read failed')
      if (params?.path === 'channel_admin_senders') return { ...adminSendersMap }
      return {}
    }
    if (method === 'channels.pairings') {
      if (options.loadPairings) return options.loadPairings(params)
      return { pairings: pairings.map(pairing => ({ ...pairing })) }
    }
    if (method === 'channels.pairing.approve') {
      pairings = pairings.map(pairing => pairing.pairingId === params?.pairingId
        ? { ...pairing, status: 'approved', approvedAt: '2026-07-13T10:00:00Z' }
        : pairing)
      // Mirrors the backend contract: the approval COMMITS even when the
      // admin grant fails, reported non-fatally via adminGranted + warnings.
      if (params?.asAdmin && options.adminGrantFails) {
        return { status: 'approved', adminGranted: false, warnings: ['admin grant failed'] }
      }
      if (params?.asAdmin) {
        const target = pairings.find(pairing => pairing.pairingId === params?.pairingId)
        if (target) {
          const set = new Set(adminSendersMap['ops-slack'] || [])
          set.add(target.senderId)
          adminSendersMap['ops-slack'] = [...set]
        }
      }
      return { status: 'approved', adminGranted: Boolean(params?.asAdmin) }
    }
    if (method === 'channels.pairing.revoke') {
      pairings = pairings.filter(pairing => pairing.pairingId !== params?.pairingId)
      return { status: 'revoked' }
    }
    if (method === 'channels.admin.set') {
      const channel = String(params?.channelName)
      const senderId = String(params?.senderId)
      const admin = Boolean(params?.admin)
      const set = new Set(adminSendersMap[channel] || [])
      if (admin) set.add(senderId)
      else set.delete(senderId)
      if (set.size) adminSendersMap[channel] = [...set]
      else delete adminSendersMap[channel]
      return { channelName: channel, senderId, admin: set.has(senderId), admins: adminSendersMap[channel] || [] }
    }
    throw new Error(`unexpected rpc method: ${method}`)
  })

  const iconStub = defineComponent({
    name: 'IconStub',
    setup() {
      return () => h('span', { 'data-testid': 'icon' })
    },
  })
  const emptyStub = (name: string) => defineComponent({
    name,
    setup() {
      return () => h('div', { 'data-testid': name })
    },
  })

  const replace = vi.fn(async (_to: { query?: Record<string, unknown> }) => {})
  // Mutable status snapshot: tests drive the 30s-poll divergence path by
  // swapping this ref's value.
  const channelsData = ref<{ channels: Array<Record<string, unknown>> }>({ channels: rows })
  vi.doMock('vue-router', () => ({
    useRouter: () => ({ push, replace }),
    useRoute: () => ({ path: '/channels', query: { ...(options.routeQuery || {}) }, hash: '' }),
    onBeforeRouteLeave: vi.fn(),
  }))
  vi.doMock('@/stores/rpc', () => ({
    useRpcStore: () => ({
      call: rpcCall,
      on: vi.fn(() => () => {}),
      waitForConnection: vi.fn(async () => {}),
    }),
  }))
  vi.doMock('@/composables/useRequest', () => ({
    useRequest: () => ({
      data: channelsData,
      loading: ref(false),
      error: ref(null),
      execute,
      refresh,
    }),
  }))
  vi.doMock('@/composables/useToasts', () => ({
    useToasts: () => ({ pushToast }),
  }))
  vi.doMock('@/composables/useConfirm', () => ({
    useConfirm: () => ({ confirm }),
  }))
  vi.doMock('@/components/Icon.vue', () => ({ default: iconStub }))
  vi.doMock('@/components/ErrorState.vue', () => ({ default: emptyStub('error-state') }))
  vi.doMock('@/components/LoadingSpinner.vue', () => ({ default: emptyStub('loading-spinner') }))

  const Component = (await import('./ChannelsView.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  // KeepAlive matches the production mount (SkillsChannelsHubView) and makes
  // onActivated run — the document Esc listener and catalog refresh live there.
  const app = createApp(defineComponent({
    setup() {
      return () => h(KeepAlive, null, [h(Component)])
    },
  }))
  app.use(i18n)
  app.mount(el)
  await nextTick()

  const flush = async () => {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }

  return { app, el, flush, nextTick, rpcCall, refresh, confirm, pushToast, push, replace, scrollIntoView, channelsData }
}

type Ctx = Awaited<ReturnType<typeof mountChannelsView>>

async function openDrill(ctx: Ctx, name: string): Promise<HTMLElement> {
  channelCard(ctx.el, name).click()
  await ctx.flush()
  const page = ctx.el.querySelector<HTMLElement>('.chd')
  if (!page) throw new Error(`drill-in page not found for ${name}`)
  return page
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.doUnmock('vue-router')
  vi.doUnmock('@/stores/rpc')
  vi.doUnmock('@/composables/useRequest')
  vi.doUnmock('@/composables/useToasts')
  vi.doUnmock('@/composables/useConfirm')
  vi.doUnmock('@/components/Icon.vue')
  vi.doUnmock('@/components/ErrorState.vue')
  vi.doUnmock('@/components/LoadingSpinner.vue')
})

describe('ChannelsView dashboard home', () => {
  it('renders configured channels as cards with real member facts', async () => {
    const { app, el, flush, rpcCall } = await mountChannelsView({
      adminSenders: { 'ops-slack': ['U-APPROVED'] },
    })
    try {
      await flush()
      expect(el.querySelector('.control-stage--hub-actions')).not.toBeNull()
      expect(el.querySelector('.control-stage__header--hub-actions')).not.toBeNull()
      // One card per configured channel, no table.
      expect(el.querySelector('table')).toBeNull()
      const ops = channelCard(el, 'ops-slack')
      expect(channelCard(el, 'alerts-telegram')).toBeTruthy()
      expect(channelCard(el, 'dead-telegram')).toBeTruthy()
      expect(channelCard(el, 'off-discord')).toBeTruthy()

      // Facts come from channels.pairings (per channel) + ONE bounded
      // config.get for channel_admin_senders.
      for (const name of ['ops-slack', 'alerts-telegram', 'dead-telegram', 'off-discord']) {
        expect(rpcCall).toHaveBeenCalledWith('channels.pairings', { channelName: name })
      }
      expect(rpcCall).toHaveBeenCalledWith('config.get', { path: 'channel_admin_senders' })
      const factValues = Array.from(ops.querySelectorAll('.chb-figure dd')).map(node => node.textContent)
      // Ledger figures: Uptime, Members, Admins, then the Awaiting figure that
      // joins the row only because ops-slack has a pending pairing.
      expect(factValues).toHaveLength(4)
      expect(factValues[1]).toBe('1') // approved members
      expect(factValues[2]).toBe('1') // channel admins

      // The pending request surfaces on the card: alert figure + inline banner.
      expect(ops.querySelector('.chb-figure--alert dd')?.textContent).toContain('1')
      expect(ops.querySelector('.chb-story__alerts .chal--pending')).not.toBeNull()
      expect(ops.textContent).toContain('Pending User')
      expect(ops.textContent).toContain('AB12CD34')

      // The shared pairings response is filtered per channel: ops-slack's
      // rows must not leak onto other cards as members or pending banners.
      for (const name of ['alerts-telegram', 'dead-telegram', 'off-discord']) {
        const other = channelCard(el, name)
        const values = Array.from(other.querySelectorAll('.chb-figure dd')).map(node => node.textContent)
        expect(values[1]).toBe('0')
        expect(other.textContent).not.toContain('Pending User')
        expect(other.querySelector('.chb-figure--alert')).toBeNull()
        expect(other.querySelector('[aria-label="Approve access for Pending User"]')).toBeNull()
        if (name === 'alerts-telegram' || name === 'off-discord') {
          expect(other.querySelector('.chb-story__alerts')).toBeNull()
        }
      }

      // Unconfigured runtime channel renders as a muted, non-drillable card.
      const ghost = channelCard(el, 'ghost-runtime')
      expect(ghost.classList.contains('is-muted')).toBe(true)
      expect(ghost.classList.contains('is-static')).toBe(true)
      expect(ghost.textContent).toContain('Running but not in config')

      // The enroll strip is the single add entry closing the ledger; there is
      // never an add-card row.
      expect(el.querySelector('.chb-enroll__title')).toBeTruthy()
      expect(el.querySelector('.chb-card--add')).toBeNull()
    } finally {
      app.unmount()
    }
  })

  it('runs Test and Restart from the card without drilling in', async () => {
    const { app, el, flush, rpcCall, push } = await mountChannelsView()
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      buttonWithText(ops, 'Test').click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.probe', { name: 'ops-slack' })
      buttonWithText(ops, 'Restart').click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.restart', { name: 'ops-slack' })
      // The quick actions never navigate: still on the dashboard.
      expect(el.querySelector('.chd')).toBeNull()
      expect(push).not.toHaveBeenCalled()
    } finally {
      app.unmount()
    }
  })

  it('approves a pending pairing on the card without asAdmin when unchecked', async () => {
    const { app, el, flush, rpcCall, push } = await mountChannelsView()
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      // ops-slack already has an approved member → bootstrap default is off.
      const checkbox = ops.querySelector<HTMLInputElement>(
        '[aria-label="Approve Pending User as a channel admin"]')!
      expect(checkbox.checked).toBe(false)
      // The plain label, not the first-pairing bootstrap wording.
      const label = ops.querySelector<HTMLElement>('.chal__asadmin span')!
      expect(label.textContent).toContain('as admin')
      expect(label.textContent).not.toContain('This is me')

      ops.querySelector<HTMLButtonElement>('[aria-label="Approve access for Pending User"]')!.click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.approve', {
        channelName: 'ops-slack',
        pairingId: 'pair-pending',
      })
      await flush()
      // The banner clears once the facts reload shows no pending request.
      expect(el.querySelector('[aria-label="Approve access for Pending User"]')).toBeNull()
      expect(el.querySelector('.chd')).toBeNull()
      expect(push).not.toHaveBeenCalled()
    } finally {
      app.unmount()
    }
  })

  it('passes asAdmin through when the card checkbox is ticked', async () => {
    const { app, el, flush, nextTick, rpcCall } = await mountChannelsView()
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      const checkbox = ops.querySelector<HTMLInputElement>(
        '[aria-label="Approve Pending User as a channel admin"]')!
      checkbox.checked = true
      checkbox.dispatchEvent(new Event('change', { bubbles: true }))
      await nextTick()
      ops.querySelector<HTMLButtonElement>('[aria-label="Approve access for Pending User"]')!.click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.approve', {
        channelName: 'ops-slack',
        pairingId: 'pair-pending',
        asAdmin: true,
      })
    } finally {
      app.unmount()
    }
  })

  it('defaults the card checkbox to admin for a channel with no members or admins', async () => {
    const loadPairings = vi.fn(async () => ({
      pairings: [{
        pairingId: 'pair-first',
        channelName: 'ops-slack',
        senderId: 'U-FIRST',
        senderName: 'First User',
        status: 'pending',
      }],
    }))
    const { app, el, flush, rpcCall } = await mountChannelsView({ loadPairings })
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      const checkbox = ops.querySelector<HTMLInputElement>(
        '[aria-label="Approve First User as a channel admin"]')!
      expect(checkbox.checked).toBe(true)
      // First pairing on an empty channel: the label says who the grant is for.
      expect(ops.querySelector('.chal__asadmin span')?.textContent)
        .toContain('This is me — make channel admin')
      ops.querySelector<HTMLButtonElement>('[aria-label="Approve access for First User"]')!.click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.approve', {
        channelName: 'ops-slack',
        pairingId: 'pair-first',
        asAdmin: true,
      })
    } finally {
      app.unmount()
    }
  })

  it('rejects a pending pairing right on the card', async () => {
    const { app, el, flush, rpcCall, pushToast } = await mountChannelsView()
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      ops.querySelector<HTMLButtonElement>(
        '[aria-label="Reject the pairing request from Pending User"]')!.click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.revoke', {
        channelName: 'ops-slack',
        pairingId: 'pair-pending',
      })
      expect(pushToast).toHaveBeenCalledWith('Rejected the pairing request from Pending User', { tone: 'ok' })
    } finally {
      app.unmount()
    }
  })

  it('offers Enable on a disabled channel card', async () => {
    const { app, el, flush, rpcCall } = await mountChannelsView()
    try {
      await flush()
      const card = channelCard(el, 'off-discord')
      expect(card.classList.contains('is-muted')).toBe(true)
      buttonWithText(card, 'Enable').click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('onboarding.channel.enable', { name: 'off-discord' })
    } finally {
      app.unmount()
    }
  })

  it('shows the error banner on a failed channel and drills into credentials', async () => {
    const { app, el, flush, rpcCall, push } = await mountChannelsView()
    try {
      await flush()
      const card = channelCard(el, 'dead-telegram')
      expect(card.textContent).toContain('401 Unauthorized — bot token rejected')
      expect(card.querySelector('.chb-story__alerts .chal--error')).not.toBeNull()
      buttonWithText(card, 'Fix credentials').click()
      await flush()
      // The escape hatch is a drill (history PUSH) straight into edit mode.
      expect(push).toHaveBeenCalledWith(expect.objectContaining({
        query: expect.objectContaining({ channel: 'dead-telegram', tab: 'configuration', edit: '1' }),
      }))
      expect(el.querySelector('.chd')).toBeTruthy()
      expect(rpcCall).toHaveBeenCalledWith('channels.get', { name: 'dead-telegram' })
    } finally {
      app.unmount()
    }
  })

  it('presents a startup-failed stopped channel as danger with its error', async () => {
    const { app, el, flush } = await mountChannelsView()
    try {
      await flush()
      const card = channelCard(el, 'boot-failed-slack')
      // Wire status is "stopped", but the start_error diagnostics escalate
      // the presentation to Failed — never a muted "Not running".
      expect(card.querySelector('.chs')?.textContent).toContain('Failed')
      expect(card.querySelector('.chs.is-danger')).toBeTruthy()
      expect(card.textContent).toContain('invalid app credentials')
      expect(buttonWithText(card, 'Fix credentials')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('degrades member facts to — when the pairings fetch fails and falls back to the status pending count', async () => {
    const loadPairings = vi.fn(async () => {
      throw new Error('store offline')
    })
    const ctx = await mountChannelsView({
      loadPairings,
      adminSenders: { 'ops-slack': ['U-APPROVED'] },
    })
    const { app, el, flush, channelsData } = ctx
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      const values = Array.from(ops.querySelectorAll('.chb-figure dd')).map(node => node.textContent)
      // Unknown member data reads as —, never as a hard zero; the admin
      // count still renders because config.get succeeded.
      expect(values[1]).toBe('—')
      expect(values[2]).toBe('1')
      expect(ops.querySelector('.chb-figure--alert')).toBeNull()

      // channels.status still reports a pending count → the badge falls back
      // to it instead of hiding the requests.
      channelsData.value = {
        channels: channelRows.map(ch =>
          ch.name === 'ops-slack' ? { ...ch, pendingPairings: 3 } : ch),
      }
      await flush()
      await flush()
      expect(channelCard(el, 'ops-slack').querySelector('.chb-figure--alert dd')?.textContent)
        .toContain('3')
    } finally {
      app.unmount()
    }
  })

  it('keeps the as-admin bootstrap OFF when the admin list is unknown', async () => {
    const loadPairings = vi.fn(async () => ({
      pairings: [{
        pairingId: 'pair-first',
        channelName: 'ops-slack',
        senderId: 'U-FIRST',
        senderName: 'First User',
        status: 'pending',
      }],
    }))
    const { app, el, flush } = await mountChannelsView({ loadPairings, configGetError: true })
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      const values = Array.from(ops.querySelectorAll('.chb-figure dd')).map(node => node.textContent)
      expect(values[2]).toBe('—')
      // No approved members, but the admin list is UNKNOWN — the bootstrap
      // default must not arm the grant on a failed read.
      const checkbox = ops.querySelector<HTMLInputElement>(
        '[aria-label="Approve First User as a channel admin"]')!
      expect(checkbox.checked).toBe(false)
    } finally {
      app.unmount()
    }
  })

  it('refetches facts when the status poll reports a diverging pending count', async () => {
    const { app, el, flush, rpcCall, channelsData } = await mountChannelsView()
    try {
      await flush()
      const callsFor = (name: string) => rpcCall.mock.calls
        .filter(([method, params]) => method === 'channels.pairings' && params?.channelName === name)
        .length
      const opsBefore = callsFor('ops-slack')
      const telegramBefore = callsFor('alerts-telegram')

      channelsData.value = {
        channels: channelRows.map(ch =>
          ch.name === 'ops-slack' ? { ...ch, pendingPairings: 2 } : ch),
      }
      await flush()
      await flush()
      // Only the diverging channel refetches; the fresh status count drives
      // the badge immediately.
      expect(callsFor('ops-slack')).toBeGreaterThan(opsBefore)
      expect(callsFor('alerts-telegram')).toBe(telegramBefore)
      expect(channelCard(el, 'ops-slack').querySelector('.chb-figure--alert dd')?.textContent)
        .toContain('2')
    } finally {
      app.unmount()
    }
  })

  it('refreshes the open members panel when status reports a new pairing', async () => {
    let hasPendingPairing = false
    const loadPairings = vi.fn(async (params?: Record<string, unknown>) => {
      if (params?.channelName !== 'ops-slack' || !hasPendingPairing) return { pairings: [] }
      return {
        pairings: [{
          pairingId: 'pair-new',
          pairingCode: 'NEWPAIR1',
          channelName: 'ops-slack',
          senderId: 'U-NEW',
          senderName: 'New Pending User',
          status: 'pending',
        }],
      }
    })
    const initialRows = channelRows.map(ch =>
      ch.name === 'ops-slack' ? { ...ch, pendingPairings: 0 } : ch)
    const ctx = await mountChannelsView({ channelRows: initialRows, loadPairings })
    try {
      await ctx.flush()
      const page = await openDrill(ctx, 'ops-slack')
      expect(page.querySelector('.chal--pending')).toBeNull()

      hasPendingPairing = true
      ctx.channelsData.value = {
        channels: initialRows.map(ch =>
          ch.name === 'ops-slack' ? { ...ch, pendingPairings: 1 } : ch),
      }
      await ctx.flush()
      await ctx.flush()

      expect(page.querySelector('.chal--pending')?.textContent).toContain('New Pending User')
      expect(page.querySelector('.chal--pending')?.textContent).toContain('NEWPAIR1')
    } finally {
      ctx.app.unmount()
    }
  })

  it('resets the as-admin override when the banner moves to the next pairing', async () => {
    const { app, el, flush, nextTick, rpcCall } = await mountChannelsView({
      initialPairings: [
        { pairingId: 'pair-alpha', channelName: 'ops-slack', senderId: 'U-ALPHA', senderName: 'Alpha', status: 'pending' },
        { pairingId: 'pair-beta', channelName: 'ops-slack', senderId: 'U-BETA', senderName: 'Beta', status: 'pending' },
        { pairingId: 'pair-approved', channelName: 'ops-slack', senderId: 'U-OK', senderName: 'Existing', status: 'approved' },
      ],
    })
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      // Tick as-admin for Alpha explicitly (bootstrap is off: a member exists).
      const alphaBox = ops.querySelector<HTMLInputElement>(
        '[aria-label="Approve Alpha as a channel admin"]')!
      expect(alphaBox.checked).toBe(false)
      alphaBox.checked = true
      alphaBox.dispatchEvent(new Event('change', { bubbles: true }))
      await nextTick()
      ops.querySelector<HTMLButtonElement>('[aria-label="Approve access for Alpha"]')!.click()
      await flush()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.approve', {
        channelName: 'ops-slack',
        pairingId: 'pair-alpha',
        asAdmin: true,
      })
      // The banner now shows Beta — Alpha's explicit override must NOT leak.
      const betaBox = el.querySelector<HTMLInputElement>(
        '[aria-label="Approve Beta as a channel admin"]')!
      expect(betaBox.checked).toBe(false)
    } finally {
      app.unmount()
    }
  })

  it('keeps the last-good pending banner and override across a transient facts failure', async () => {
    let fail = false
    const loadPairings = vi.fn(async () => {
      if (fail) throw new Error('store offline')
      return {
        pairings: [
          { pairingId: 'pair-pending', pairingCode: 'AB12CD34', channelName: 'ops-slack', senderId: 'U-PENDING', senderName: 'Pending User', status: 'pending' },
          { pairingId: 'pair-approved', channelName: 'ops-slack', senderId: 'U-APPROVED', senderName: 'Approved User', status: 'approved' },
        ],
      }
    })
    const { app, el, flush, nextTick } = await mountChannelsView({ loadPairings })
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      const box = ops.querySelector<HTMLInputElement>(
        '[aria-label="Approve Pending User as a channel admin"]')!
      box.checked = true
      box.dispatchEvent(new Event('change', { bubbles: true }))
      await nextTick()

      // One failed poll must NOT blip the banner out (which would wipe the
      // operator's explicit as-admin choice): last-good facts stand in.
      fail = true
      buttonWithText(el, 'Refresh').click()
      await flush()
      await flush()
      const after = channelCard(el, 'ops-slack')
      expect(after.textContent).toContain('Pending User')
      const values = Array.from(after.querySelectorAll('.chb-figure dd')).map(node => node.textContent)
      expect(values[1]).toBe('1')
      expect(after.querySelector<HTMLInputElement>(
        '[aria-label="Approve Pending User as a channel admin"]')!.checked).toBe(true)

      // The next healthy poll re-resolves the SAME request — still overridden.
      fail = false
      buttonWithText(el, 'Refresh').click()
      await flush()
      await flush()
      expect(channelCard(el, 'ops-slack').querySelector<HTMLInputElement>(
        '[aria-label="Approve Pending User as a channel admin"]')!.checked).toBe(true)
    } finally {
      app.unmount()
    }
  })

  it('surfaces a failed admin grant after an as-admin card approve', async () => {
    const { app, el, flush, nextTick, pushToast } = await mountChannelsView({ adminGrantFails: true })
    try {
      await flush()
      const ops = channelCard(el, 'ops-slack')
      const box = ops.querySelector<HTMLInputElement>(
        '[aria-label="Approve Pending User as a channel admin"]')!
      box.checked = true
      box.dispatchEvent(new Event('change', { bubbles: true }))
      await nextTick()
      ops.querySelector<HTMLButtonElement>('[aria-label="Approve access for Pending User"]')!.click()
      await flush()
      // The approval committed but the grant failed: plain approve success
      // plus an actionable warning — never a false admin-success toast.
      expect(pushToast).toHaveBeenCalledWith('Approved pairing access for Pending User', { tone: 'ok' })
      expect(pushToast).toHaveBeenCalledWith(
        'Approved Pending User, but the admin grant failed — retry from the members list.',
        { tone: 'danger' },
      )
      expect(pushToast).not.toHaveBeenCalledWith('Approved Pending User as a channel admin', { tone: 'ok' })
    } finally {
      app.unmount()
    }
  })
})

describe('ChannelsView drill-in page', () => {
  it('drills in with a history push and hydrates every section at once', async () => {
    const ctx = await mountChannelsView()
    const { app, flush, rpcCall, push, refresh } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      expect(push).toHaveBeenCalledWith(expect.objectContaining({
        query: expect.objectContaining({ channel: 'ops-slack', tab: 'overview' }),
      }))
      expect(page.querySelector('h2')?.textContent).toBe('ops-slack')
      // The restarts fact pluralizes: 1 → singular.
      expect(page.textContent).toContain('1 restart')
      expect(page.textContent).not.toContain('1 restarts')

      // All sections mount together: members and configuration hydrate on entry.
      expect(rpcCall).toHaveBeenCalledWith('channels.pairings', { channelName: 'ops-slack' })
      expect(rpcCall).toHaveBeenCalledWith('channels.get', { name: 'ops-slack' })
      expect(page.querySelector('.ch-pairings')).toBeTruthy()
      expect(page.querySelectorAll('.cfge [data-field]').length).toBeGreaterThan(0)
      const storedSecrets = page.querySelectorAll<HTMLElement>('.cfge__value--secret')
      expect(storedSecrets).toHaveLength(2)

      // Header quick actions stay wired to the live channel.
      buttonWithText(page, 'Test').click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.probe', { name: 'ops-slack' })
      expect(page.textContent).toContain('Connection verified')

      buttonWithText(page, 'Restart').click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.restart', { name: 'ops-slack' })
      expect(refresh).toHaveBeenCalled()

      // Capability evidence lives folded inside diagnostics now.
      const reference = page.querySelector<HTMLElement>('.ch-tech')!
      expect(reference.textContent).toContain('Capability reference')
      expect(reference.textContent).toContain('Backed by: build_reply_message')
      expect(reference.textContent).toContain('Implemented')
    } finally {
      app.unmount()
    }
  })

  it('omits the restarts fact when there have been no restart attempts', async () => {
    const ctx = await mountChannelsView()
    const { app, flush, channelsData } = ctx
    try {
      await flush()
      channelsData.value = {
        channels: [{ ...channelRows[0], restart_attempts: 0 }, ...channelRows.slice(1)],
      }
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const facts = page.querySelector<HTMLElement>('.chd__factsline')!
      expect(facts.textContent).not.toMatch(/restart/i)
    } finally {
      app.unmount()
    }
  })

  it('middle-truncates a long bot id in the header and copies the full id', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const ctx = await mountChannelsView()
    const { app, flush, pushToast, channelsData } = ctx
    try {
      await flush()
      channelsData.value = {
        channels: [{ ...channelRows[0], bot_user_id: 'U-BOT-FEED-0123456789' }, ...channelRows.slice(1)],
      }
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const botButton = page.querySelector<HTMLButtonElement>('.chd__botid')!
      // 7-char prefix … 4-char suffix, never the raw 21-char id.
      expect(botButton.textContent).toContain('Bot U-BOT-F…6789')
      expect(botButton.textContent).not.toContain('U-BOT-FEED-0123456789')

      botButton.click()
      await flush()
      // The clipboard always receives the FULL id, not the display form.
      expect(writeText).toHaveBeenCalledWith('U-BOT-FEED-0123456789')
      expect(pushToast).toHaveBeenCalledWith('Bot ID copied', { tone: 'ok' })
    } finally {
      app.unmount()
    }
  })

  it('holds the scrollspy during a section jump and re-arms after the window', async () => {
    const ctx = await mountChannelsView()
    const { app, flush, nextTick } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      await flush()
      const activeNav = () =>
        page.querySelector<HTMLElement>('.chd__nav button.is-active')?.textContent || ''
      // Fake only Date: the spy hold compares Date.now(), while flush() keeps
      // relying on real setTimeout.
      vi.useFakeTimers({ toFake: ['Date'] })
      try {
        const navButtons = Array.from(page.querySelectorAll<HTMLButtonElement>('.chd__nav button'))
        navButtons.find(button => button.textContent?.includes('Configuration'))!.click()
        await nextTick()
        expect(activeNav()).toContain('Configuration')

        // A scroll inside the 800ms hold must not move the highlight off the
        // clicked section (happy-dom rects all sit above the fold line, so a
        // live spy would immediately jump to the last section).
        window.dispatchEvent(new Event('scroll'))
        await nextTick()
        expect(activeNav()).toContain('Configuration')

        // Past the hold the spy re-arms and tracks the viewport again.
        vi.advanceTimersByTime(801)
        window.dispatchEvent(new Event('scroll'))
        await nextTick()
        expect(activeNav()).toContain('Diagnostics')
      } finally {
        vi.useRealTimers()
      }
    } finally {
      app.unmount()
    }
  })

  it('returns home through the breadcrumb', async () => {
    const ctx = await mountChannelsView()
    const { app, el, flush, replace } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      page.querySelector<HTMLButtonElement>('.chd__back')!.click()
      await flush()
      expect(el.querySelector('.chd')).toBeNull()
      expect(el.querySelector('.chb-ledger')).toBeTruthy()
      const lastQuery = replace.mock.calls[replace.mock.calls.length - 1]?.[0]?.query
      expect(lastQuery?.channel).toBeUndefined()
    } finally {
      app.unmount()
    }
  })

  it('maps tab=pairings to a section scroll on a cold deep link', async () => {
    const ctx = await mountChannelsView({
      routeQuery: { channel: 'ops-slack', tab: 'pairings' },
    })
    const { app, el, flush, rpcCall, scrollIntoView } = ctx
    try {
      await flush()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.pairings', { channelName: 'ops-slack' })
      const page = el.querySelector<HTMLElement>('.chd')!
      expect(page.textContent).toContain('Pending User')
      expect(page.textContent).toContain('Approved User')
      // The deep link scrolled the members section into view.
      expect(scrollIntoView).toHaveBeenCalled()
    } finally {
      app.unmount()
    }
  })

  it('hydrates the editor on a cold tab=configuration deep link', async () => {
    const ctx = await mountChannelsView({
      routeQuery: { channel: 'ops-slack', tab: 'configuration' },
    })
    const { app, el, flush, rpcCall } = ctx
    try {
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.get', { name: 'ops-slack' })
      const page = el.querySelector<HTMLElement>('.chd')!
      expect(page.querySelectorAll('.cfge [data-field]').length).toBeGreaterThan(0)
    } finally {
      app.unmount()
    }
  })

  it('lands editable from a cold edit deep link', async () => {
    const ctx = await mountChannelsView({
      routeQuery: { channel: 'ops-slack', tab: 'configuration', edit: '1' },
    })
    const { app, el, flush, rpcCall } = ctx
    try {
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.get', { name: 'ops-slack' })
      const page = el.querySelector<HTMLElement>('.chd')!
      expect(page.querySelector('[data-field="slack_channel_id"] input')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('folds the legacy tab=capabilities deep link into diagnostics', async () => {
    const ctx = await mountChannelsView({
      routeQuery: { channel: 'ops-slack', tab: 'capabilities' },
    })
    const { app, el, flush, replace } = ctx
    try {
      await flush()
      expect(el.querySelector('.chd')).toBeTruthy()
      const writes = replace.mock.calls.map(call => call[0]?.query).filter(Boolean)
      expect(writes.some(query => query?.tab === 'diagnostics')).toBe(true)
      expect(writes.every(query => query?.tab !== 'capabilities')).toBe(true)
    } finally {
      app.unmount()
    }
  })
})

describe('ChannelsView members section', () => {
  function membersPanel(page: HTMLElement): HTMLElement {
    const panel = page.querySelector<HTMLElement>('.ch-pairings')
    if (!panel) throw new Error('members panel not found')
    return panel
  }

  it('administers channel-local pairing access with confirmations and refreshes', async () => {
    const ctx = await mountChannelsView()
    const { app, flush, rpcCall, confirm, pushToast } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const panel = membersPanel(page)
      expect(panel.textContent).toContain('Request AB12CD34')
      expect(panel.textContent).toContain('Pending User')
      expect(panel.textContent).toContain('Approved User')
      expect(panel.textContent).toContain('Pending requests')
      expect(panel.textContent).toContain('Approved access')

      panel.querySelector<HTMLButtonElement>('[aria-label="Approve access for Pending User"]')!.click()
      await flush()
      expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
        title: 'Approve pairing request?',
        primaryClass: 'btn--primary',
      }))
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.approve', {
        channelName: 'ops-slack',
        pairingId: 'pair-pending',
      })
      expect(pushToast).toHaveBeenCalledWith('Approved pairing access for Pending User', { tone: 'ok' })
      expect(panel.querySelector('[aria-label="Approve access for Pending User"]')).toBeNull()

      panel.querySelector<HTMLButtonElement>('[aria-label="Revoke access for Approved User"]')!.click()
      await flush()
      expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
        title: 'Revoke pairing access?',
      }))
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.revoke', {
        channelName: 'ops-slack',
        pairingId: 'pair-approved',
      })
      expect(pushToast).toHaveBeenCalledWith('Revoked pairing access for Approved User', { tone: 'ok' })
      expect(panel.querySelector('[aria-label="Revoke access for Approved User"]')).toBeNull()

      // A revoked request can be re-approved via the same approve RPC.
      expect(panel.textContent).toContain('Revoked access')
      panel.querySelector<HTMLButtonElement>('[aria-label="Re-approve access for Revoked User"]')!.click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.approve', {
        channelName: 'ops-slack',
        pairingId: 'pair-revoked',
      })
    } finally {
      app.unmount()
    }
  })

  it('ignores stale pairing loads when switching channels', async () => {
    let resolveFirst: ((value: unknown) => void) | undefined
    const firstLoad = new Promise(resolve => { resolveFirst = resolve })
    const loadPairings = vi.fn(async (params?: Record<string, unknown>) => {
      if (params?.channelName === 'ops-slack') return firstLoad
      return {
        pairings: [{
          pairingId: 'pair-telegram',
          pairingCode: 'TG12CD34',
          channelName: 'alerts-telegram',
          senderId: 'TG-USER',
          senderName: 'Telegram User',
          status: 'pending',
        }],
      }
    })
    const ctx = await mountChannelsView({ loadPairings })
    const { app, flush } = ctx
    try {
      await flush()
      const first = await openDrill(ctx, 'ops-slack')
      first.querySelector<HTMLButtonElement>('.chd__back')!.click()
      await flush()
      const page = await openDrill(ctx, 'alerts-telegram')

      expect(page.textContent).toContain('Telegram User')

      resolveFirst?.({
        pairings: [{
          pairingId: 'pair-slack',
          channelName: 'ops-slack',
          senderId: 'SLACK-USER',
          senderName: 'Slack User',
          status: 'pending',
        }],
      })
      await flush()

      expect(page.textContent).toContain('Telegram User')
      expect(page.textContent).not.toContain('Slack User')
    } finally {
      app.unmount()
    }
  })

  it('promotes an approved member to channel admin', async () => {
    const ctx = await mountChannelsView()
    const { app, flush, rpcCall, confirm } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const panel = membersPanel(page)

      panel.querySelector<HTMLButtonElement>('[aria-label="Make Approved User a channel admin"]')!.click()
      await flush()
      expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
        title: 'Make this member an admin?',
      }))
      expect(rpcCall).toHaveBeenCalledWith('channels.admin.set', {
        channelName: 'ops-slack',
        senderId: 'U-APPROVED',
        admin: true,
      })
      // After the grant + reload the row flips to the admin controls.
      expect(panel.querySelector('[aria-label="Remove admin from Approved User"]')).toBeTruthy()
      expect(panel.querySelector('[aria-label="Make Approved User a channel admin"]')).toBeNull()
    } finally {
      app.unmount()
    }
  })

  it('shows the Admin pill and demotes a channel admin', async () => {
    const ctx = await mountChannelsView({
      adminSenders: { 'ops-slack': ['U-APPROVED'] },
    })
    const { app, flush, rpcCall, confirm } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const panel = membersPanel(page)

      // The approved admin renders the Admin pill instead of the plain badge.
      const adminPill = Array.from(panel.querySelectorAll<HTMLElement>('.ch-pairing-status.is-admin'))
        .find(node => node.textContent?.trim() === 'Admin')
      expect(adminPill).toBeTruthy()

      panel.querySelector<HTMLButtonElement>('[aria-label="Remove admin from Approved User"]')!.click()
      await flush()
      expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
        title: 'Remove admin access?',
      }))
      expect(rpcCall).toHaveBeenCalledWith('channels.admin.set', {
        channelName: 'ops-slack',
        senderId: 'U-APPROVED',
        admin: false,
      })
      // Back to member controls after the demotion + reload.
      expect(panel.querySelector('[aria-label="Make Approved User a channel admin"]')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('approves a pending member as admin from the panel when ticked', async () => {
    const ctx = await mountChannelsView()
    const { app, flush, nextTick, rpcCall } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const panel = membersPanel(page)

      const checkbox = panel.querySelector<HTMLInputElement>(
        '[aria-label="Approve Pending User as a channel admin"]')!
      // There is already an approved member, so the bootstrap default is off.
      expect(checkbox.checked).toBe(false)
      // And the label keeps the plain wording, not the bootstrap string.
      const label = panel.querySelector<HTMLElement>('.ch-pairing-asadmin span')!
      expect(label.textContent).toContain('as admin')
      expect(label.textContent).not.toContain('This is me')
      checkbox.checked = true
      checkbox.dispatchEvent(new Event('change', { bubbles: true }))
      await nextTick()

      panel.querySelector<HTMLButtonElement>('[aria-label="Approve access for Pending User"]')!.click()
      await flush()
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.approve', {
        channelName: 'ops-slack',
        pairingId: 'pair-pending',
        asAdmin: true,
      })
    } finally {
      app.unmount()
    }
  })

  it('single-sources the as-admin override between the drill banner and the members row', async () => {
    const ctx = await mountChannelsView()
    const { app, flush, nextTick } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const banner = page.querySelector<HTMLElement>('.chal--pending')!
      const panel = membersPanel(page)
      const bannerBox = banner.querySelector<HTMLInputElement>('input[type="checkbox"]')!
      const rowBox = panel.querySelector<HTMLInputElement>(
        '[aria-label="Approve Pending User as a channel admin"]')!
      expect(bannerBox.checked).toBe(false)
      expect(rowBox.checked).toBe(false)

      // Tick the members row: the banner reads the same store-backed value.
      rowBox.checked = true
      rowBox.dispatchEvent(new Event('change', { bubbles: true }))
      await nextTick()
      expect(bannerBox.checked).toBe(true)

      // Untick on the banner: the row follows — ONE override, two surfaces.
      bannerBox.checked = false
      bannerBox.dispatchEvent(new Event('change', { bubbles: true }))
      await nextTick()
      expect(rowBox.checked).toBe(false)
    } finally {
      app.unmount()
    }
  })

  it('routes the drill banner approve through the members confirmation gate', async () => {
    const ctx = await mountChannelsView()
    const { app, flush, nextTick, rpcCall, confirm, pushToast } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const banner = page.querySelector<HTMLElement>('.chal--pending')!
      const box = banner.querySelector<HTMLInputElement>('input[type="checkbox"]')!
      box.checked = true
      box.dispatchEvent(new Event('change', { bubbles: true }))
      await nextTick()

      banner.querySelector<HTMLButtonElement>('[aria-label="Approve access for Pending User"]')!.click()
      await flush()
      // Same gate as the Members row (the admin-aware confirm) — the drill
      // page never offers two different risk gates for the same grant. Only
      // the dashboard card quick-approve stays confirmation-free.
      expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
        title: 'Approve pairing request?',
        body: expect.stringContaining('admin access'),
      }))
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.approve', {
        channelName: 'ops-slack',
        pairingId: 'pair-pending',
        asAdmin: true,
      })
      expect(pushToast).toHaveBeenCalledWith('Approved Pending User as a channel admin', { tone: 'ok' })
    } finally {
      app.unmount()
    }
  })

  it('surfaces a failed admin grant after an as-admin approve from the panel', async () => {
    const ctx = await mountChannelsView({ adminGrantFails: true })
    const { app, flush, nextTick, pushToast } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const panel = membersPanel(page)
      const box = panel.querySelector<HTMLInputElement>(
        '[aria-label="Approve Pending User as a channel admin"]')!
      box.checked = true
      box.dispatchEvent(new Event('change', { bubbles: true }))
      await nextTick()
      panel.querySelector<HTMLButtonElement>('[aria-label="Approve access for Pending User"]')!.click()
      await flush()
      expect(pushToast).toHaveBeenCalledWith('Approved pairing access for Pending User', { tone: 'ok' })
      expect(pushToast).toHaveBeenCalledWith(
        'Approved Pending User, but the admin grant failed — retry from the members list.',
        { tone: 'danger' },
      )
      // The members list still reloads: the approval itself committed.
      expect(panel.querySelector('[aria-label="Approve access for Pending User"]')).toBeNull()
    } finally {
      app.unmount()
    }
  })

  it('anchors the bootstrap default to the unfiltered first request, not the search match', async () => {
    const ctx = await mountChannelsView({
      initialPairings: [
        { pairingId: 'pair-alpha', channelName: 'ops-slack', senderId: 'U-ALPHA', senderName: 'Alpha', status: 'pending' },
        { pairingId: 'pair-beta', channelName: 'ops-slack', senderId: 'U-BETA', senderName: 'Beta', status: 'pending' },
      ],
    })
    const { app, flush, nextTick } = ctx
    try {
      await flush()
      const page = await openDrill(ctx, 'ops-slack')
      const panel = membersPanel(page)
      // No members, no admins: the FIRST request carries the bootstrap default.
      expect(panel.querySelector<HTMLInputElement>(
        '[aria-label="Approve Alpha as a channel admin"]')!.checked).toBe(true)
      expect(panel.querySelector<HTMLInputElement>(
        '[aria-label="Approve Beta as a channel admin"]')!.checked).toBe(false)

      // Filtering to Beta must NOT move the pre-checked default onto it.
      const search = panel.querySelector<HTMLInputElement>('.ch-pairing-search input')!
      search.value = 'Beta'
      search.dispatchEvent(new Event('input', { bubbles: true }))
      await nextTick()
      expect(panel.querySelector('[aria-label="Approve Alpha as a channel admin"]')).toBeNull()
      expect(panel.querySelector<HTMLInputElement>(
        '[aria-label="Approve Beta as a channel admin"]')!.checked).toBe(false)
    } finally {
      app.unmount()
    }
  })
})

describe('ChannelsView in-place configuration editor', () => {
  async function openConfiguration(ctx: Ctx): Promise<HTMLElement> {
    await ctx.flush()
    return openDrill(ctx, 'ops-slack')
  }

  async function enterEditMode(ctx: Ctx, page: HTMLElement): Promise<void> {
    buttonWithText(page, 'Edit').click()
    await ctx.flush()
  }

  function fieldNames(page: HTMLElement): string[] {
    return Array.from(page.querySelectorAll<HTMLElement>('.cfge [data-field]'))
      .map(node => node.getAttribute('data-field') || '')
  }

  function setField(page: HTMLElement, name: string, value: string): void {
    const input = page.querySelector<HTMLInputElement>(`[data-field="${name}"] input`)!
    input.value = value
    input.dispatchEvent(new Event('input', { bubbles: true }))
  }

  function bar(el: HTMLElement): HTMLElement {
    const node = el.querySelector<HTMLElement>('.ceb')
    if (!node) throw new Error('editor action bar not found')
    return node
  }

  it('flips read → edit onto the same field skeleton, name locked', async () => {
    const ctx = await mountChannelsView()
    try {
      const page = await openConfiguration(ctx)
      const readFields = fieldNames(page)
      // Spec-driven read rows: same set the edit form will own, secrets
      // masked, with the credentials group leading the rail.
      expect(readFields).toEqual(
        ['token', 'signing_secret', 'name', 'connection_mode', 'slack_channel_id', 'reply_in_thread'])
      expect(page.querySelectorAll('.cfge input')).toHaveLength(0)

      await enterEditMode(ctx, page)
      expect(fieldNames(page)).toEqual(readFields)
      // Rows became live fields in place; the name stays locked text.
      expect(page.querySelector('[data-field="slack_channel_id"] input')).toBeTruthy()
      expect(page.querySelector('[data-field="name"] input')).toBeNull()
      expect(page.querySelector('[data-field="name"] .cfge__value--locked')).toBeTruthy()
    } finally {
      ctx.app.unmount()
    }
  })

  it('owns the edit=1 query contract: enter, two-stage Esc exit, never carries across channels', async () => {
    const ctx = await mountChannelsView()
    const { el, flush, replace } = ctx
    const lastQuery = () => {
      const calls = replace.mock.calls
      return calls[calls.length - 1]?.[0]?.query
    }
    try {
      const page = await openConfiguration(ctx)
      await enterEditMode(ctx, page)
      expect(lastQuery()).toMatchObject({
        channel: 'ops-slack', tab: 'configuration', edit: '1',
      })

      // First Esc only blurs the autofocused field — still editing.
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await flush()
      expect(page.querySelector('[data-field="slack_channel_id"] input')).toBeTruthy()

      // Second Esc cancels back to read mode and drops edit=1.
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await flush()
      expect(page.querySelector('[data-field="slack_channel_id"] input')).toBeNull()
      const afterExit = lastQuery()
      expect(afterExit).toMatchObject({ channel: 'ops-slack', tab: 'configuration' })
      expect(afterExit?.edit).toBeUndefined()

      // Re-enter edit (clean draft), go home, drill another channel: its
      // query must never inherit edit=1.
      await enterEditMode(ctx, page)
      page.querySelector<HTMLButtonElement>('.chd__back')!.click()
      await flush()
      channelCard(el, 'alerts-telegram').click()
      await flush()
      // Entering the drill lands as a push; later state writes replace — the
      // query must never inherit edit=1 on either channel.
      const telegramWrites = [...replace.mock.calls, ...ctx.push.mock.calls]
        .map(call => call[0]?.query)
        .filter(query => query?.channel === 'alerts-telegram')
      expect(telegramWrites.length).toBeGreaterThan(0)
      expect(telegramWrites.every(query => query?.edit === undefined)).toBe(true)
    } finally {
      ctx.app.unmount()
    }
  })

  it('names the changed fields in the floating dirty bar and probes the current draft', async () => {
    const ctx = await mountChannelsView()
    const { el, flush, rpcCall } = ctx
    try {
      const page = await openConfiguration(ctx)
      await enterEditMode(ctx, page)
      expect(el.querySelector('.ceb')).toBeNull()

      setField(page, 'slack_channel_id', 'C123')
      await flush()
      expect(bar(el).textContent).toContain('Unsaved — Default channel id')
      expect(page.querySelector('.ch-tab-dirty')).toBeTruthy()
      // The bar floats fixed at bottom-center now that the page scrolls as a
      // whole — it rides in its own overlay wrapper, not inside a section.
      expect(bar(el).parentElement?.classList.contains('chd-dirtybar')).toBe(true)

      buttonWithText(bar(el), 'Test connection').click()
      await flush()
      const probeCall = rpcCall.mock.calls.find(([method]) => method === 'onboarding.channel.probe')
      expect(probeCall).toBeTruthy()
      const entry = (probeCall![1] as { entry: Record<string, unknown> }).entry
      expect(entry).toMatchObject({
        type: 'slack', name: 'ops-slack', connection_mode: 'webhook', slack_channel_id: 'C123',
      })
      // Untouched stored secrets stay out of the draft probe entirely.
      expect('token' in entry).toBe(false)
      expect(JSON.stringify(entry)).not.toContain('***')
      expect(page.textContent).toContain('Configuration checks passed')
    } finally {
      ctx.app.unmount()
    }
  })

  it('saves via probe → upsert, resets the baseline, and drops edit mode', async () => {
    const ctx = await mountChannelsView()
    const { el, flush, rpcCall, pushToast } = ctx
    try {
      const page = await openConfiguration(ctx)
      await enterEditMode(ctx, page)
      setField(page, 'slack_channel_id', 'C123')
      await flush()

      buttonWithText(bar(el), 'Save changes').click()
      await flush()
      await flush()

      const methods = rpcCall.mock.calls.map(([method]) => method)
      expect(methods.indexOf('onboarding.channel.probe'))
        .toBeLessThan(methods.indexOf('onboarding.channel.upsert'))
      const upsertCall = rpcCall.mock.calls.find(([method]) => method === 'onboarding.channel.upsert')!
      const entry = (upsertCall[1] as { entry: Record<string, unknown> }).entry
      expect(entry).toMatchObject({ name: 'ops-slack', slack_channel_id: 'C123' })
      expect(JSON.stringify(entry)).not.toContain('***')
      expect(pushToast).toHaveBeenCalledWith(
        'Channel saved — configuration validated locally; use Test connection to verify credentials',
        { tone: 'ok' },
      )
      // Baseline reset from the reseed: read mode, no dirty dot, no bar.
      expect(page.querySelector('[data-field="slack_channel_id"] input')).toBeNull()
      expect(page.querySelector('.ch-tab-dirty')).toBeNull()
      expect(el.querySelector('.ceb')).toBeNull()
    } finally {
      ctx.app.unmount()
    }
  })

  it('blocks Save on a failed probe with inline rows and offers Save anyway', async () => {
    const ctx = await mountChannelsView({
      draftProbeError: { message: 'invalid channel entry: token: Field required' },
    })
    const { el, flush, rpcCall } = ctx
    try {
      const page = await openConfiguration(ctx)
      await enterEditMode(ctx, page)
      setField(page, 'slack_channel_id', 'C123')
      await flush()

      buttonWithText(bar(el), 'Save changes').click()
      await flush()
      expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(false)
      expect(page.textContent).toContain('invalid channel entry: token: Field required')

      buttonWithText(page, 'Save anyway').click()
      await flush()
      await flush()
      expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(true)
    } finally {
      ctx.app.unmount()
    }
  })

  it('guards leaving the drill-in page behind the inline discard confirm', async () => {
    const ctx = await mountChannelsView()
    const { el, flush, rpcCall } = ctx
    try {
      const page = await openConfiguration(ctx)
      await enterEditMode(ctx, page)
      setField(page, 'slack_channel_id', 'C123')
      await flush()

      page.querySelector<HTMLButtonElement>('.chd__back')!.click()
      await flush()
      // The exit waited: still drilled in, confirm pair raised in the bar.
      expect(el.querySelector('.chd h2')?.textContent).toBe('ops-slack')
      expect(bar(el).textContent).toContain('Discard unsaved changes?')

      buttonWithText(bar(el), 'Keep editing').click()
      await flush()
      expect(el.querySelector('.chd h2')?.textContent).toBe('ops-slack')
      const input = page.querySelector<HTMLInputElement>('[data-field="slack_channel_id"] input')!
      expect(input.value).toBe('C123')
      expect(bar(el).textContent).toContain('Unsaved — Default channel id')

      page.querySelector<HTMLButtonElement>('.chd__back')!.click()
      await flush()
      buttonWithText(bar(el), 'Discard').click()
      await flush()
      expect(el.querySelector('.chd')).toBeNull()
      expect(el.querySelector('.chb-ledger')).toBeTruthy()
      expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(false)
    } finally {
      ctx.app.unmount()
    }
  })

  it('never round-trips the redaction sentinel through secret Replace/Cancel', async () => {
    const ctx = await mountChannelsView()
    const { el, flush, nextTick, rpcCall } = ctx
    try {
      const page = await openConfiguration(ctx)
      await enterEditMode(ctx, page)

      // Replace → type → cancel: the stored value stays authoritative.
      const tokenRow = page.querySelector<HTMLElement>('[data-field="token"]')!
      buttonWithText(tokenRow, 'Replace').click()
      await nextTick()
      setField(page, 'token', 'xoxb-typed-then-cancelled')
      await nextTick()
      buttonWithText(tokenRow, 'Keep stored value').click()
      await nextTick()
      expect(tokenRow.textContent).toContain('Stored ······')

      // Replace the other secret for real and save.
      const signingRow = page.querySelector<HTMLElement>('[data-field="signing_secret"]')!
      buttonWithText(signingRow, 'Replace').click()
      await nextTick()
      setField(page, 'signing_secret', 'shhh-new-secret')
      await flush()

      buttonWithText(bar(el), 'Save changes').click()
      await flush()
      await flush()

      for (const method of ['onboarding.channel.probe', 'onboarding.channel.upsert']) {
        const call = rpcCall.mock.calls.find(([m]) => m === method)!
        const entry = (call[1] as { entry: Record<string, unknown> }).entry
        expect(entry.signing_secret).toBe('shhh-new-secret')
        expect('token' in entry).toBe(false)
        expect(JSON.stringify(entry)).not.toContain('***')
      }
      // The reseed masks the replaced secret again.
      expect(page.querySelector<HTMLElement>('[data-field="signing_secret"]')?.textContent)
        .toContain('Stored ······')
    } finally {
      ctx.app.unmount()
    }
  })
})

describe('ChannelsView add entry', () => {
  const twoChannels = [
    { name: 'ops-slack', type: 'slack', status: 'connected', connected: true, enabled: true, configured: true, diagnostics: { network_probe: 'not_run' } },
    { name: 'alerts-telegram', type: 'telegram', status: 'stopped', connected: false, enabled: true, configured: true, diagnostics: { network_probe: 'not_run' } },
  ]

  // The header toolbar now carries ONLY the Refresh ghost button — there is no
  // primary "Add channel" entry at any fleet count. The single add affordance
  // is the enroll strip's title button.
  function headerPrimaryButtons(root: ParentNode): HTMLButtonElement[] {
    return Array.from(root.querySelectorAll<HTMLButtonElement>('.ch-stage__actions .btn--primary'))
  }

  it('0 channels: the inline platform gallery is the page, no ledger or enroll strip', async () => {
    const { app, el, flush } = await mountChannelsView({ channelRows: [] })
    try {
      await flush()
      await flush()
      // The catalog gallery renders inline (same specs the compose gallery uses).
      expect(el.querySelector('.ctg__grid')).toBeTruthy()
      expect(el.querySelector('[data-channel-type="slack"]')).toBeTruthy()
      expect(el.querySelector('[data-channel-type="feishu"]')).toBeTruthy()
      // The gallery IS the single entry: no header add button, and neither the
      // fleet ledger nor the enroll strip (both belong to the fleet page only).
      expect(headerPrimaryButtons(el)).toHaveLength(0)
      expect(el.querySelector('.chb-ledger')).toBeNull()
      expect(el.querySelector('.chb-enroll')).toBeNull()
    } finally {
      app.unmount()
    }
  })

  it('0 channels: a gallery tile enters compose pre-picked (?compose=1&type=)', async () => {
    const ctx = await mountChannelsView({ channelRows: [] })
    const { app, el, flush, push } = ctx
    try {
      await flush()
      await flush()
      el.querySelector<HTMLButtonElement>('[data-channel-type="slack"]')!.click()
      await flush()
      // One history PUSH carries compose=1 AND the picked type in a single write.
      expect(push).toHaveBeenCalledWith(expect.objectContaining({
        query: expect.objectContaining({ compose: '1', type: 'slack' }),
      }))
      expect(el.querySelector('.chc')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('fleet (1..3 channels): the enroll strip is the add entry and skips used platforms', async () => {
    const { app, el, flush } = await mountChannelsView({ channelRows: twoChannels })
    try {
      await flush()
      await flush()
      // The ledger keeps the channel stories; there is never a trailing add-card.
      expect(channelCard(el, 'ops-slack')).toBeTruthy()
      expect(channelCard(el, 'alerts-telegram')).toBeTruthy()
      expect(el.querySelector('.chb-card--add')).toBeNull()
      // No header add button — the enroll strip is the single entry.
      expect(headerPrimaryButtons(el)).toHaveLength(0)
      const enroll = el.querySelector<HTMLElement>('.chb-enroll')
      expect(enroll).toBeTruthy()
      expect(enroll!.querySelector('.chb-enroll__title')).toBeTruthy()
      // Chips only for UNCONFIGURED platform types: slack (used) is skipped;
      // feishu/matrix (catalog, unused) surface.
      expect(enroll!.querySelector('[data-channel-type="slack"]')).toBeNull()
      expect(enroll!.querySelector('[data-channel-type="feishu"]')).toBeTruthy()
      expect(enroll!.querySelector('[data-channel-type="matrix"]')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('fleet: an enroll chip enters compose pre-picked', async () => {
    const ctx = await mountChannelsView({ channelRows: twoChannels })
    const { app, el, flush, push } = ctx
    try {
      await flush()
      await flush()
      el.querySelector<HTMLButtonElement>('.chb-enroll [data-channel-type="feishu"]')!.click()
      await flush()
      expect(push).toHaveBeenCalledWith(expect.objectContaining({
        query: expect.objectContaining({ compose: '1', type: 'feishu' }),
      }))
      expect(el.querySelector('.chc')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('fleet (>=4 channels): the enroll strip stays the add entry, never an add-card', async () => {
    // The shared fixture carries five configured channels.
    const { app, el, flush } = await mountChannelsView()
    try {
      await flush()
      expect(el.querySelector('.chb-enroll__title')).toBeTruthy()
      expect(el.querySelector('.chb-card--add')).toBeNull()
      expect(headerPrimaryButtons(el)).toHaveLength(0)
    } finally {
      app.unmount()
    }
  })
})

describe('ChannelsView compose takeover', () => {
  async function enterCompose(ctx: Ctx): Promise<HTMLElement> {
    ctx.el.querySelector<HTMLButtonElement>('.chb-enroll__title')!.click()
    await ctx.flush()
    const surface = ctx.el.querySelector<HTMLElement>('.chc')
    if (!surface) throw new Error('compose surface not found')
    return surface
  }

  async function pickType(ctx: Ctx, surface: HTMLElement, type: string): Promise<void> {
    surface.querySelector<HTMLButtonElement>(`[data-channel-type="${type}"]`)!.click()
    await settle(ctx)
  }

  // The gallery ⇄ form swap rides a <Transition mode="out-in">, whose enter
  // side waits on animation frames — give the DOM real frame time to settle.
  async function settle(ctx: Ctx): Promise<void> {
    for (let i = 0; i < 4; i += 1) {
      await new Promise(resolve => setTimeout(resolve, 20))
      await ctx.nextTick()
    }
  }

  function setComposeField(surface: HTMLElement, name: string, value: string): void {
    const input = surface.querySelector<HTMLInputElement>(`[data-field="${name}"] input`)!
    input.value = value
    input.dispatchEvent(new Event('input', { bubbles: true }))
  }

  function lastQuery(ctx: Ctx): Record<string, unknown> | undefined {
    const calls = ctx.replace.mock.calls
    return calls[calls.length - 1]?.[0]?.query
  }

  it('enters compose with history PUSH and exits clean via back', async () => {
    const ctx = await mountChannelsView()
    const { el, flush } = ctx
    try {
      await flush()
      const surface = await enterCompose(ctx)
      // The PUSH transition: Back returns to the dashboard.
      expect(ctx.push).toHaveBeenCalledWith(
        expect.objectContaining({ query: expect.objectContaining({ compose: '1' }) }))
      // Gallery shows the catalog platforms as real buttons.
      expect(surface.querySelector('[data-channel-type="slack"]')).toBeTruthy()
      expect(surface.querySelector('[data-channel-type="feishu"]')).toBeTruthy()

      surface.querySelector<HTMLButtonElement>('.chc__back')!.click()
      await flush()
      expect(el.querySelector('.chc')).toBeNull()
      const query = lastQuery(ctx)
      expect(query?.compose).toBeUndefined()
      expect(query?.type).toBeUndefined()
    } finally {
      ctx.app.unmount()
    }
  })

  it('moves focus into the takeover on open and returns it to the invoker on exit', async () => {
    const ctx = await mountChannelsView()
    const { el, flush } = ctx
    try {
      await flush()
      const addButton = el.querySelector<HTMLButtonElement>('.chb-enroll__title')!
      addButton.focus()
      addButton.click()
      await flush()
      // aria-modal demands managed focus: the surface takes it on open…
      const surface = el.querySelector<HTMLElement>('.chc__surface')!
      expect(document.activeElement).toBe(surface)

      // …and the invoker gets it back when the takeover dismisses.
      surface.querySelector<HTMLButtonElement>('.chc__back')!.click()
      await flush()
      expect(el.querySelector('.chc')).toBeNull()
      expect(document.activeElement).toBe(addButton)
    } finally {
      ctx.app.unmount()
    }
  })

  it('renders a recognition-first gallery: brand-mark cards with credential footnotes', async () => {
    const ctx = await mountChannelsView()
    try {
      await ctx.flush()
      const surface = await enterCompose(ctx)
      // Latin-locale order leads with Slack; the title names the step.
      const types = Array.from(surface.querySelectorAll('[data-channel-type]'))
        .map(node => node.getAttribute('data-channel-type'))
      expect(types).toEqual(['slack', 'matrix', 'feishu'])
      expect(surface.querySelector('.chc__title')?.textContent).toContain('Choose a platform')

      const slackCard = surface.querySelector<HTMLElement>('[data-channel-type="slack"]')!
      expect(slackCard.querySelector('.brand-mark')).toBeTruthy()
      // The footnote honors show_when against the spec defaults: the
      // webhook-only signing secret is not part of the default (socket)
      // setup path, so it must not join the credential mix.
      expect(slackCard.querySelector('.ctg__cred')?.textContent).toBe('Bot token')
      // Matrix has no dedicated credential fields — the footnote derives from
      // its required fields instead of rendering blank.
      const matrixCard = surface.querySelector<HTMLElement>('[data-channel-type="matrix"]')!
      expect(matrixCard.querySelector('.ctg__cred')?.textContent)
        .toBe('Homeserver URL · User id (@user:server)')
      // Transport badges and descriptions are gone from the gallery.
      expect(surface.querySelector('.ctg__transport')).toBeNull()
      expect(surface.querySelector('.ctg__desc')).toBeNull()

      await pickType(ctx, surface, 'slack')
      expect(surface.querySelector('.chc__title')?.textContent).toContain('Add Slack')
    } finally {
      ctx.app.unmount()
    }
  })

  it('orders the gallery for zh locales with the CN platforms first, names localized', async () => {
    const ctx = await mountChannelsView({ locale: 'zh-Hans' })
    try {
      await ctx.flush()
      ctx.el.querySelector<HTMLButtonElement>('.chb-enroll__title')!.click()
      await ctx.flush()
      const surface = ctx.el.querySelector<HTMLElement>('.chc')!
      const types = Array.from(surface.querySelectorAll('[data-channel-type]'))
        .map(node => node.getAttribute('data-channel-type'))
      expect(types).toEqual(['feishu', 'slack', 'matrix'])
      // Platform display names come through the catalog i18n overlay.
      expect(surface.querySelector('[data-channel-type="feishu"] .ctg__name')?.textContent)
        .toBe('飞书（Lark）')
    } finally {
      ctx.app.unmount()
    }
  })

  it('collapses the gallery to a receipt chip on pick and restores it via Change', async () => {
    const ctx = await mountChannelsView()
    try {
      await ctx.flush()
      const surface = await enterCompose(ctx)
      await pickType(ctx, surface, 'slack')

      expect(surface.querySelector('.ctg__grid')).toBeNull()
      expect(surface.querySelector('.chc__chipname')?.textContent).toBe('Slack')
      // The picked-type form is the shared editor in compose mode: name editable.
      expect(surface.querySelector('[data-field="name"] input')).toBeTruthy()
      expect(lastQuery(ctx)).toMatchObject({ compose: '1', type: 'slack' })

      buttonWithText(surface, 'Change').click()
      await settle(ctx)
      expect(surface.querySelector('[data-channel-type="slack"]')).toBeTruthy()
      const query = lastQuery(ctx)
      expect(query?.compose).toBe('1')
      expect(query?.type).toBeUndefined()
    } finally {
      ctx.app.unmount()
    }
  })

  it('guards exit behind the inline confirm while the compose draft is dirty', async () => {
    const ctx = await mountChannelsView()
    const { el, flush, rpcCall } = ctx
    try {
      await flush()
      const surface = await enterCompose(ctx)
      await pickType(ctx, surface, 'slack')
      setComposeField(surface, 'name', 'my-slack')
      await flush()

      surface.querySelector<HTMLButtonElement>('.chc__back')!.click()
      await flush()
      expect(surface.querySelector('.chc__footer--confirm')?.textContent)
        .toContain('Discard the Slack draft?')
      expect(el.querySelector('.chc')).toBeTruthy()

      buttonWithText(surface, 'Keep editing').click()
      await flush()
      expect(el.querySelector('.chc')).toBeTruthy()
      expect(surface.querySelector<HTMLInputElement>('[data-field="name"] input')?.value)
        .toBe('my-slack')

      surface.querySelector<HTMLButtonElement>('.chc__back')!.click()
      await flush()
      buttonWithText(surface, 'Discard').click()
      await flush()
      expect(el.querySelector('.chc')).toBeNull()
      expect(lastQuery(ctx)?.compose).toBeUndefined()
      expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(false)
    } finally {
      ctx.app.unmount()
    }
  })

  it('saves a new channel sentinel-free and selects it on success', async () => {
    const ctx = await mountChannelsView()
    const { el, flush, rpcCall, pushToast } = ctx
    try {
      await flush()
      const surface = await enterCompose(ctx)
      await pickType(ctx, surface, 'slack')
      setComposeField(surface, 'name', 'new-slack')
      // Socket mode: token is the one required secret, rendered as a password box.
      const tokenInput = surface.querySelector<HTMLInputElement>('[data-field="token"] input')!
      expect(tokenInput.type).toBe('password')
      setComposeField(surface, 'token', 'xoxb-fresh-token')
      await flush()

      buttonWithText(surface, 'Save Channel').click()
      await flush()
      await flush()

      const methods = rpcCall.mock.calls.map(([method]) => method)
      expect(methods.indexOf('onboarding.channel.probe'))
        .toBeLessThan(methods.indexOf('onboarding.channel.upsert'))
      for (const method of ['onboarding.channel.probe', 'onboarding.channel.upsert']) {
        const call = rpcCall.mock.calls.find(([m]) => m === method)!
        const entry = (call[1] as { entry: Record<string, unknown> }).entry
        expect(entry).toMatchObject({ type: 'slack', name: 'new-slack', token: 'xoxb-fresh-token' })
        expect(JSON.stringify(entry)).not.toContain('***')
      }
      expect(pushToast).toHaveBeenCalledWith(
        'Channel saved — configuration validated locally; use Test connection to verify credentials',
        { tone: 'ok' },
      )
      // Takeover dismissed; the new channel is selected on its page.
      expect(el.querySelector('.chc')).toBeNull()
      expect(lastQuery(ctx)).toMatchObject({ channel: 'new-slack', tab: 'overview' })
      expect(lastQuery(ctx)?.compose).toBeUndefined()
    } finally {
      ctx.app.unmount()
    }
  })

  it('offers Save anyway inline when the compose probe fails', async () => {
    const ctx = await mountChannelsView({
      draftProbeError: { message: 'invalid channel entry: token: Field required' },
    })
    const { el, flush, rpcCall } = ctx
    try {
      await flush()
      const surface = await enterCompose(ctx)
      await pickType(ctx, surface, 'slack')
      setComposeField(surface, 'name', 'new-slack')
      setComposeField(surface, 'token', 'xoxb-fresh-token')
      await flush()

      buttonWithText(surface, 'Save Channel').click()
      await flush()
      expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(false)
      expect(surface.textContent).toContain('invalid channel entry: token: Field required')
      expect(el.querySelector('.chc')).toBeTruthy()

      buttonWithText(surface, 'Save anyway').click()
      await flush()
      await flush()
      expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(true)
      expect(el.querySelector('.chc')).toBeNull()
    } finally {
      ctx.app.unmount()
    }
  })

  it('direct-lands in the picked-type form from ?compose=1&type=feishu', async () => {
    const ctx = await mountChannelsView({ routeQuery: { compose: '1', type: 'feishu' } })
    const { el } = ctx
    try {
      await settle(ctx)
      const surface = el.querySelector<HTMLElement>('.chc')!
      expect(surface).toBeTruthy()
      expect(surface.querySelector('.ctg__grid')).toBeNull()
      expect(surface.querySelector('.chc__chipname')?.textContent).toBe('Feishu / Lark')
      // Empty draft, editable fields, aids beside the credentials they unblock.
      expect(surface.querySelector('[data-field="app_id"] input')).toBeTruthy()
      const appSecret = surface.querySelector<HTMLInputElement>('[data-field="app_secret"] input')!
      expect(appSecret.type).toBe('password')
      // API credentials, not login passwords: opted out of password managers
      // so autofill prompts never claim an app secret.
      expect(appSecret.getAttribute('autocomplete')).toBe('off')
      expect(appSecret.hasAttribute('data-1p-ignore')).toBe(true)
      expect(appSecret.getAttribute('data-lpignore')).toBe('true')
      expect(appSecret.value).toBe('')
      expect(surface.textContent).toContain('Feishu console shortcuts')
    } finally {
      ctx.app.unmount()
    }
  })
})

describe('feishu event-subscription guidance', () => {
  const FEISHU_ROW = {
    name: 'fs-main',
    type: 'feishu',
    status: 'connected',
    connected: true,
    enabled: true,
    configured: true,
    connected_since: '2026-07-13T08:00:00Z',
    diagnostics: { delivery: { ingress: {}, outbox: {}, leases: [] } },
  }

  function feishuGet(connectionMode: string) {
    return () => ({
      entry: {
        name: 'fs-main',
        type: 'feishu',
        app_id: 'cli_dummy',
        app_secret: '***',
        connection_mode: connectionMode,
        domain: 'feishu',
      },
      secretFields: ['app_secret'],
    })
  }

  it('shows on a connected websocket channel with no inbound events yet', async () => {
    const ctx = await mountChannelsView({
      channelRows: [FEISHU_ROW],
      channelsGet: feishuGet('websocket'),
    })
    try {
      await ctx.flush()
      const page = await openDrill(ctx, 'fs-main')
      const step = page.querySelector<HTMLElement>('.ch-alert--step')
      expect(step).toBeTruthy()
      expect(step!.textContent).toContain('Check Feishu event subscription')
      expect(step!.textContent).not.toContain('Final step')
      expect(step!.textContent).not.toContain('one step left')
      // The retuned ws_order_note is neutral console guidance, not a diagnosis.
      expect(step!.textContent).toContain('事件与回调')
      expect(step!.textContent).toContain('does not by itself mean the console is misconfigured')
    } finally {
      ctx.app.unmount()
    }
  })

  it.each([
    { status: 'connected', connected: false, reason: 'transport evidence is false' },
    { status: 'restarting', connected: true, reason: 'wire status is not connected' },
  ])('stays hidden when $reason', async ({ status, connected }) => {
    const ctx = await mountChannelsView({
      channelRows: [{ ...FEISHU_ROW, status, connected }],
      channelsGet: feishuGet('websocket'),
    })
    try {
      await ctx.flush()
      const page = await openDrill(ctx, 'fs-main')
      expect(page.querySelector('.ch-alert--step')).toBeNull()
    } finally {
      ctx.app.unmount()
    }
  })

  it('self-resolves durably: completed ingress rows (the steady state) count', async () => {
    const ctx = await mountChannelsView({
      channelRows: [{
        ...FEISHU_ROW,
        // The ledger lifecycle is accepted → processing → completed and
        // completed rows persist — a healthy channel with its backlog drained
        // reports ONLY completed counts, and must stay resolved.
        diagnostics: { delivery: { ingress: { completed: { count: 2 } }, outbox: {}, leases: [] } },
      }],
      channelsGet: feishuGet('websocket'),
    })
    try {
      await ctx.flush()
      const page = await openDrill(ctx, 'fs-main')
      expect(page.querySelector('.ch-alert--step')).toBeNull()
    } finally {
      ctx.app.unmount()
    }
  })

  it('stays resolved while an event is still in flight', async () => {
    const ctx = await mountChannelsView({
      channelRows: [{
        ...FEISHU_ROW,
        diagnostics: { delivery: { ingress: { accepted: { count: 2 } }, outbox: {}, leases: [] } },
      }],
      channelsGet: feishuGet('websocket'),
    })
    try {
      await ctx.flush()
      const page = await openDrill(ctx, 'fs-main')
      expect(page.querySelector('.ch-alert--step')).toBeNull()
    } finally {
      ctx.app.unmount()
    }
  })

  it('never shows for a webhook-mode channel', async () => {
    const ctx = await mountChannelsView({
      channelRows: [FEISHU_ROW],
      channelsGet: feishuGet('webhook'),
    })
    try {
      await ctx.flush()
      const page = await openDrill(ctx, 'fs-main')
      expect(page.querySelector('.ch-alert--step')).toBeNull()
    } finally {
      ctx.app.unmount()
    }
  })

  it('fails closed when the channel config cannot be loaded', async () => {
    const ctx = await mountChannelsView({
      channelRows: [FEISHU_ROW],
      channelsGet: () => {
        throw new Error('config read failed')
      },
    })
    try {
      await ctx.flush()
      const page = await openDrill(ctx, 'fs-main')
      // Mode unknown → no websocket guidance rather than guessing: a webhook
      // channel must never see long-connection subscription guidance.
      expect(page.querySelector('.ch-alert--step')).toBeNull()
    } finally {
      ctx.app.unmount()
    }
  })
})
