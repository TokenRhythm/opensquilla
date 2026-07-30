// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

// Mounted coverage for the Overview diagnostics actions: the conditional
// "diagnose with agent" hand-off, finding→settings deep links, and the
// active-provider latency readout (with null guards for older gateways).

interface MountOptions {
  report?: Record<string, unknown> | null
  providers?: unknown
  failProviders?: boolean
  desktop?: boolean
  connectionUrl?: string
  doctorHandler?: (
    params: unknown,
    callIndex: number,
  ) => Record<string, unknown> | Promise<Record<string, unknown>>
}

interface PushArg {
  path: string
  query?: Record<string, string>
  hash?: string
  state?: { prefill?: string; autosend?: boolean }
}

const mountedApps: Array<{ app: App; el: HTMLElement }> = []

function baseReport(): Record<string, unknown> {
  return {
    status: 'degraded',
    ready: true,
    summary: 'Config at /Users/dummyuser/dir/opensquilla.toml',
    gatewayUrl: 'ws://127.0.0.1:18791/ws',
    configPath: '/Users/dummyuser/dir/opensquilla.toml',
    agentId: 'main',
    counts: { warn: 1 },
    impactCounts: { degrades: 1 },
    findings: [
      {
        id: 'memory.degraded',
        surface: 'memory',
        severity: 'warn',
        readinessImpact: 'degrades',
        title: 'Memory index <stale> & behind',
        detail: 'Index at /Users/dummyuser/state/memory',
      },
    ],
  }
}

async function mountOverview(options: MountOptions = {}) {
  vi.resetModules()
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = options.desktop
    ? {}
    : undefined
  window.localStorage.clear()
  if (options.connectionUrl) {
    window.localStorage.setItem('opensquilla.wsUrl', options.connectionUrl)
  }

  const { KeepAlive, createApp, defineComponent, h, nextTick, ref } = await import('vue')
  const { createPinia, setActivePinia } = await import('pinia')
  const i18n = (await import('@/i18n')).default

  const push = vi.fn((_to: PushArg) => Promise.resolve())
  const pushToast = vi.fn()
  const copyText = vi.fn(async (_text: string) => {})
  const rpcOn = vi.fn(() => () => {})
  let doctorCallIndex = 0
  const rpcCall = vi.fn(async (method: string, params?: unknown) => {
    if (method === 'doctor.status') {
      if (options.report === null) throw new Error('doctor unavailable')
      if (options.doctorHandler) {
        return options.doctorHandler(params, doctorCallIndex++)
      }
      doctorCallIndex++
      return JSON.parse(JSON.stringify(options.report ?? baseReport()))
    }
    if (method === 'providers.status') {
      if (options.failProviders) throw new Error('providers unavailable')
      return options.providers ?? { providers: [] }
    }
    throw new Error(`unexpected rpc method: ${method}`)
  })

  vi.doMock('vue-router', () => ({ useRouter: () => ({ push }) }))
  vi.doMock('@/stores/rpc', () => ({
    useRpcStore: () => ({
      isConnected: true,
      isConnecting: false,
      on: rpcOn,
      waitForConnection: vi.fn(async () => {}),
      call: rpcCall,
    }),
  }))
  const useRequestMethods: string[] = []
  vi.doMock('@/composables/useRequest', async () => {
    const { ref } = await import('vue')
    return {
      useRequest: (method: string) => {
        useRequestMethods.push(method)
        return {
          data: ref(null),
          error: ref(null),
          loading: ref(false),
          execute: vi.fn(async () => null),
          refresh: vi.fn(async () => null),
        }
      },
    }
  })
  vi.doMock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))
  vi.doMock('@/utils/browser', () => ({ copyTextWithFallback: copyText }))
  vi.doMock('@/components/Icon.vue', () => ({
    default: defineComponent({
      name: 'IconStub',
      props: { name: { type: String, default: '' } },
      setup(props) {
        return () => h('span', { 'data-icon': props.name })
      },
    }),
  }))
  const pinia = createPinia()
  setActivePinia(pinia)
  i18n.global.locale.value = 'en'

  const Component = (await import('./OverviewView.vue')).default
  const active = ref(true)
  const TestHost = defineComponent({
    name: 'OverviewTestHost',
    setup() {
      return () => h(KeepAlive, null, active.value ? [h(Component)] : [])
    },
  })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(TestHost)
  app.component('RouterLink', defineComponent({
    name: 'RouterLinkStub',
    setup(_, { slots }) {
      return () => h('a', slots.default?.())
    },
  }))
  app.use(pinia)
  app.use(i18n)
  app.mount(el)
  mountedApps.push({ app, el })

  async function flush() {
    for (let i = 0; i < 8; i++) await Promise.resolve()
    await nextTick()
  }
  await flush()

  async function setActive(value: boolean) {
    active.value = value
    await flush()
  }

  return {
    el,
    push,
    pushToast,
    copyText,
    rpcCall,
    rpcOn,
    useRequestMethods,
    flush,
    setActive,
  }
}

beforeEach(() => {
  document.body.innerHTML = ''
  window.localStorage.clear()
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = undefined
})

afterEach(() => {
  while (mountedApps.length) {
    const { app, el } = mountedApps.pop()!
    app.unmount()
    el.remove()
  }
  vi.doUnmock('vue-router')
  vi.doUnmock('@/stores/rpc')
  vi.doUnmock('@/composables/useRequest')
  vi.doUnmock('@/composables/useToasts')
  vi.doUnmock('@/utils/browser')
  vi.doUnmock('@/components/Icon.vue')
  window.localStorage.clear()
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = undefined
  vi.restoreAllMocks()
  vi.useRealTimers()
})

// The buttons carry resolved translations in their title attributes; the
// suite pins locale 'en' in mountOverview, so select by the en strings.
const DIAGNOSE_SELECTOR = '[title="Diagnose with agent"]'

describe('OverviewView status lifecycle', () => {
  it('drops the old activity panels and their data sources', async () => {
    const { el, rpcCall, rpcOn, useRequestMethods } = await mountOverview()

    expect(el.querySelector('.ov-grid')).toBeNull()
    expect(el.querySelector('.ov-recent')).toBeNull()
    expect(el.querySelector('.conn-pill')).toBeNull()
    expect(el.querySelector('.ov-event-log')).toBeNull()
    expect(useRequestMethods).toEqual(['status'])
    expect(rpcCall.mock.calls.some(([method]) => method === 'sessions.list')).toBe(false)
    expect(rpcOn).not.toHaveBeenCalled()
  })

  it('runs one initial deep check, silent shallow refreshes, and stops while inactive', async () => {
    vi.useFakeTimers()
    let resolveShallow!: (report: Record<string, unknown>) => void
    const shallowReport = new Promise<Record<string, unknown>>((resolve) => {
      resolveShallow = resolve
    })
    const { el, rpcCall, flush, setActive } = await mountOverview({
      doctorHandler: (_params, callIndex) => (
        callIndex === 0 ? baseReport() : shallowReport
      ),
    })
    const doctorParams = () => rpcCall.mock.calls
      .filter(([method]) => method === 'doctor.status')
      .map(([, params]) => params)

    expect(doctorParams()).toEqual([{ agentId: 'main', deep: true }])

    await vi.advanceTimersByTimeAsync(30000)
    expect(doctorParams()).toEqual([
      { agentId: 'main', deep: true },
      { agentId: 'main', deep: false },
    ])
    expect(el.querySelector('.ov-statusline')?.classList.contains('is-loading')).toBe(false)

    resolveShallow(baseReport())
    await flush()
    await setActive(false)
    await vi.advanceTimersByTimeAsync(60000)
    expect(doctorParams()).toHaveLength(2)

    await setActive(true)
    expect(doctorParams()).toHaveLength(3)
    let calls = doctorParams()
    expect(calls[calls.length - 1]).toEqual({ agentId: 'main', deep: false })

    el.querySelector<HTMLButtonElement>('.ov-status-actions .btn--ghost')!.click()
    await flush()
    calls = doctorParams()
    expect(calls[calls.length - 1]).toEqual({ agentId: 'main', deep: true })
  })
})

describe('OverviewView diagnose-with-agent hand-off', () => {
  it('does not render an old gateway migration finding or degraded summary', async () => {
    const { el } = await mountOverview({
      report: {
        status: 'ready',
        ready: true,
        summary: 'Ready, 1 optional setup item',
        counts: { error: 0, warn: 0, info: 1, ok: 0 },
        impactCounts: { blocks_ready: 0, degrades: 0, optional: 1, none: 0 },
        findings: [{
          id: 'migration.legacy_home_detected',
          surface: 'migration',
          severity: 'info',
          readinessImpact: 'optional',
          title: 'Legacy data found',
        }],
      },
    })
    expect(el.textContent).not.toContain('Legacy data found')
    expect(el.textContent).not.toContain('optional setup item')
    expect(el.textContent).toContain('Ready')
  })

  it('shows the button and routes a sanitized, escaped report into a new chat', async () => {
    const { el, push, flush } = await mountOverview()
    const button = el.querySelector<HTMLButtonElement>(DIAGNOSE_SELECTOR)
    expect(button).toBeTruthy()

    button!.click()
    await flush()

    expect(push).toHaveBeenCalledTimes(1)
    const arg = push.mock.calls[0][0]
    expect(arg.path).toBe('/chat/new')
    expect(arg.query).toEqual({ agent: 'main' })
    expect(arg.state?.autosend).toBe(true)

    const prefill = String(arg.state?.prefill)
    expect(prefill).toContain('Please troubleshoot this OpenSquilla configuration')
    expect(prefill).toContain('<context source="client:diagnostic-context">')
    expect(prefill).toContain('"platform":"web"')
    expect(prefill).toContain('"hasTerminalWorkflow":true')
    expect(prefill).toContain('<untrusted source="doctor:report">')
    expect(prefill).toContain('</untrusted>')
    // Home paths are normalized and the report body is XML-escaped.
    expect(prefill).toContain('~/dir/opensquilla.toml')
    expect(prefill).not.toContain('dummyuser')
    expect(prefill).toContain('Memory index &lt;stale&gt; &amp; behind')
    // Only the minimal report ships — no env fields like configPath.
    expect(prefill).not.toContain('"configPath"')
  })

  it('hides the button when a provider finding blocks the agent', async () => {
    const report = baseReport()
    report.findings = [
      {
        id: 'provider.key.missing',
        surface: 'provider',
        severity: 'error',
        readinessImpact: 'blocks_ready',
        title: 'Provider API key missing',
      },
    ]
    const { el } = await mountOverview({ report })
    expect(el.querySelector(DIAGNOSE_SELECTOR)).toBeNull()
  })

  it('filters an old migration finding from local Desktop diagnostics', async () => {
    const report = baseReport()
    report.findings = [
      {
        id: 'migration.legacy_home_detected',
        surface: 'migration',
        severity: 'info',
        readinessImpact: 'optional',
        title: 'Legacy data found',
        fixSteps: [
          {
            label: 'Preview the import',
            command: 'opensquilla migrate opensquilla --source /Users/dummyuser/.opensquilla',
          },
        ],
      },
    ]
    const { el, push, flush } = await mountOverview({ report, desktop: true })

    el.querySelector<HTMLButtonElement>(DIAGNOSE_SELECTOR)!.click()
    await flush()

    const prefill = String(push.mock.calls[0][0].state?.prefill)
    expect(prefill).toContain('"platform":"desktop"')
    expect(prefill).toContain('"hasTerminalWorkflow":false')
    expect(prefill).toContain('"ownsGateway":true')
    expect(prefill).toContain('"connectionScope":"local_owned"')
    expect(prefill).not.toContain('/settings/runtime')
    expect(prefill).not.toContain('migration.legacy_home_detected')
    expect(prefill).not.toContain('"command":"opensquilla migrate')
    expect(prefill).not.toContain('dummyuser')
  })

  it('marks a remote Desktop gateway and omits the local Runtime remediation', async () => {
    const report = baseReport()
    report.findings = [
      {
        id: 'migration.legacy_home_detected',
        surface: 'migration',
        severity: 'info',
        readinessImpact: 'optional',
        title: 'Legacy data found',
      },
    ]
    const { el, push, flush } = await mountOverview({
      report,
      desktop: true,
      connectionUrl: 'ws://remote.example:18791/ws',
    })

    expect(el.querySelector('.health-settings-link')).toBeNull()
    el.querySelector<HTMLButtonElement>(DIAGNOSE_SELECTOR)!.click()
    await flush()

    const prefill = String(push.mock.calls[0][0].state?.prefill)
    expect(prefill).toContain('"ownsGateway":false')
    expect(prefill).toContain('"connectionScope":"remote"')
    expect(prefill).toContain('"remoteGatewayActions":"handle_on_gateway_host"')
    expect(prefill).not.toContain('/settings/runtime')
  })

  it('filters old migration commands from a Web hand-off too', async () => {
    const report = baseReport()
    report.findings = [
      {
        id: 'migration.legacy_home_detected',
        surface: 'migration',
        fixSteps: [{ label: 'Preview', command: 'opensquilla migrate opensquilla --source /tmp/old' }],
      },
    ]
    const { el, push, flush } = await mountOverview({ report })

    el.querySelector<HTMLButtonElement>(DIAGNOSE_SELECTOR)!.click()
    await flush()

    const prefill = String(push.mock.calls[0][0].state?.prefill)
    expect(prefill).not.toContain('migration.legacy_home_detected')
    expect(prefill).not.toContain('opensquilla migrate opensquilla')
  })
})

describe('OverviewView recovery activation copy', () => {
  it('keeps restart guidance on the concrete step without a finding-level restart claim', async () => {
    const report = baseReport()
    report.findings = [
      {
        id: 'provider.active.not_configured',
        surface: 'provider',
        severity: 'error',
        readinessImpact: 'blocks_ready',
        title: 'Active provider is not configured',
        restartRequired: true,
        fixSteps: [
          {
            label: 'Set provider environment variable',
            detail: 'Set TOKENRHYTHM_API_KEY, then restart OpenSquilla.',
          },
          {
            label: 'Restart gateway',
            command: 'opensquilla gateway restart',
          },
        ],
      },
    ]

    const { el } = await mountOverview({ report })

    expect(el.textContent).not.toContain('Recovery requires restart')
    expect(el.textContent).toContain('then restart OpenSquilla')
    expect(el.textContent).toContain('Restart gateway')
  })

  it('hides old migration commands when the current target already has data', async () => {
    const report = baseReport()
    report.findings = [
      {
        id: 'migration.legacy_home_detected',
        surface: 'migration',
        severity: 'info',
        readinessImpact: 'optional',
        title: 'Legacy data found',
        evidence: { target_fresh: false },
        restartRequired: true,
        fixSteps: [
          {
            label: 'Preview the import',
            command: 'opensquilla migrate opensquilla --source /tmp/old',
          },
          {
            label: 'Apply the import',
            command: 'opensquilla migrate opensquilla --source /tmp/old --apply',
          },
        ],
      },
    ]

    const { el } = await mountOverview({ report })

    expect(el.textContent).not.toContain('Preview the import')
    expect(el.textContent).not.toContain('Apply the import')
    expect(el.textContent).not.toContain('--apply')
  })

  it('hides old migration commands for a fresh target too', async () => {
    const report = baseReport()
    report.findings = [
      {
        id: 'migration.legacy_home_detected',
        surface: 'migration',
        severity: 'warn',
        readinessImpact: 'degrades',
        title: 'Legacy data found',
        evidence: { targetFresh: true },
        fixSteps: [
          {
            label: 'Preview the import',
            command: 'opensquilla migrate opensquilla --source /tmp/old',
          },
          {
            label: 'Apply the import',
            command: 'opensquilla migrate opensquilla --source /tmp/old --apply',
          },
        ],
      },
    ]

    const { el } = await mountOverview({ report })

    expect(el.textContent).not.toContain('Preview the import')
    expect(el.textContent).not.toContain('Apply the import')
    expect(el.textContent).not.toContain('--apply')
  })
})

describe('OverviewView finding settings links', () => {
  it('links mapped surfaces to their settings section and skips the rest', async () => {
    const report = baseReport()
    report.findings = [
      {
        id: 'provider.model.unknown',
        surface: 'provider',
        severity: 'warn',
        readinessImpact: 'degrades',
        title: 'Model not in catalog',
        evidence: { providerId: 'openrouter' },
      },
      {
        id: 'memory.degraded',
        surface: 'memory',
        severity: 'warn',
        readinessImpact: 'degrades',
        title: 'Memory degraded',
      },
    ]
    const { el, push, flush } = await mountOverview({ report })

    const links = el.querySelectorAll<HTMLButtonElement>('.health-settings-link')
    expect(links.length).toBe(1)

    links[0].click()
    await flush()
    expect(push).toHaveBeenCalledWith({ path: '/settings/provider', hash: '#provider-openrouter' })
  })

  it('filters old Desktop migration findings while retaining capability links', async () => {
    const report = baseReport()
    report.findings = [
      {
        id: 'migration.legacy_home_detected',
        surface: 'migration',
        severity: 'info',
        readinessImpact: 'optional',
        title: 'Legacy data found',
      },
      {
        id: 'image_generation.credentials.missing',
        surface: 'image_generation',
        severity: 'info',
        readinessImpact: 'optional',
        title: 'Image generation key missing',
      },
    ]
    const { el, push, flush } = await mountOverview({ report, desktop: true })

    const links = el.querySelectorAll<HTMLButtonElement>('.health-settings-link')
    expect(links.length).toBe(1)
    links[0].click()
    await flush()
    expect(push).toHaveBeenCalledWith({ path: '/settings/capabilities' })
  })
})

describe('OverviewView provider latency line', () => {
  const latencyProviders = {
    providers: [
      {
        providerId: 'anthropic',
        active: false,
        latency: { p50TtftMs: 100, p95TtftMs: 200, samples: 5, windowMinutes: 60 },
      },
      {
        providerId: 'openrouter',
        active: true,
        latency: { p50TtftMs: 380, p95TtftMs: 1200, samples: 87, windowMinutes: 60 },
      },
    ],
  }

  it('renders the compact line for the active provider only', async () => {
    const { el } = await mountOverview({ providers: latencyProviders })
    const line = el.querySelector('.ov-readout__latency code')
    expect(line?.textContent).toBe('p50 380ms · p95 1.2s · 87 samples/60min')
  })

  it('skips the line when the active row has no latency payload', async () => {
    const { el } = await mountOverview({
      providers: { providers: [{ providerId: 'openrouter', active: true, latency: null }] },
    })
    expect(el.querySelector('.ov-readout__latency')).toBeNull()
  })

  it('tolerates a providers.status failure without breaking the view', async () => {
    const { el } = await mountOverview({ failProviders: true })
    expect(el.querySelector('.ov-readout__latency')).toBeNull()
    // The rest of the overview still rendered.
    expect(el.querySelector('.ov-statusline')).toBeTruthy()
    expect(el.querySelector(DIAGNOSE_SELECTOR)).toBeTruthy()
  })

  it('fetches providers.status on mount only, not on health reruns', async () => {
    const { el, rpcCall, flush } = await mountOverview({ providers: latencyProviders })
    const providerCalls = () =>
      rpcCall.mock.calls.filter(([method]) => method === 'providers.status').length
    expect(providerCalls()).toBe(1)

    // "Rerun checks" repeats the deep doctor pass but must not re-instantiate
    // a provider client per registered spec just for the latency line.
    el.querySelector<HTMLButtonElement>('.ov-rerun')!.click()
    await flush()
    expect(rpcCall.mock.calls.filter(([method]) => method === 'doctor.status').length).toBe(2)
    expect(providerCalls()).toBe(1)
  })
})

describe('OverviewView config path readout', () => {
  it('abbreviates Linux home config paths too', async () => {
    const report = baseReport()
    report.configPath = '/home/dummyuser/dir/opensquilla.toml'
    const { el } = await mountOverview({ report })
    const codes = Array.from(el.querySelectorAll('.ov-readout__kv code'))
      .map(code => code.textContent)
    expect(codes).toContain('~/dir/opensquilla.toml')
  })
})
