// @vitest-environment happy-dom

import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import type {
  SkillInstallActivities,
  SkillInstallQueueItem,
  SkillInstallSource,
} from '@/composables/skills/useSkillRegistry'
import type { RegistryResult, SkillDiagnostic } from '@/types/skills'
import SkillsAddDrawer from './SkillsAddDrawer.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

function mountDrawer(options: {
  queue?: SkillInstallQueueItem[]
  activities?: SkillInstallActivities
  runningSource?: SkillInstallSource | null
  cancellableInstallSource?: SkillInstallSource | null
  cancellingSource?: SkillInstallSource | null
  mutationBlocked?: boolean
  results?: RegistryResult[]
  registryDiagnostics?: SkillDiagnostic[]
} = {}) {
  const open = ref(false)
  const githubUrl = ref('https://github.com/acme/demo')
  const registryQuery = ref('demo')
  const queue = ref(options.queue || [])
  const queueSource: SkillInstallSource = queue.value[0]?.source === 'clawhub'
    ? 'clawhub'
    : 'github'
  const activities = ref<SkillInstallActivities>(options.activities || {
    clawhub: {
      items: queueSource === 'clawhub' ? queue.value : [],
      refreshWarning: '',
    },
    github: {
      items: queueSource === 'github' ? queue.value : [],
      refreshWarning: '',
    },
  })
  const runningSource = ref<SkillInstallSource | null>(options.runningSource ?? null)
  const cancellingSource = ref<SkillInstallSource | null>(options.cancellingSource ?? null)
  const installed: Array<[string, string, string]> = []
  const retried: Array<[string, boolean | undefined]> = []
  const cleared: SkillInstallSource[] = []
  const cancelled: SkillInstallSource[] = []

  const Root = defineComponent({
    setup() {
      return () => h('div', [
        h('button', {
          id: 'drawer-trigger',
          onClick: () => { open.value = true },
        }, 'Open drawer'),
        h(SkillsAddDrawer, {
          open: open.value,
          registryQuery: registryQuery.value,
          githubUrl: githubUrl.value,
          results: options.results || [],
          loading: false,
          registryDiagnostics: options.registryDiagnostics || [],
          registrySearchError: '',
          activities: activities.value,
          runningSource: runningSource.value,
          cancellableInstallSource: options.cancellableInstallSource ?? null,
          cancellingSource: cancellingSource.value,
          mutationBlocked: options.mutationBlocked || false,
          'onUpdate:registryQuery': (value: string) => { registryQuery.value = value },
          'onUpdate:githubUrl': (value: string) => { githubUrl.value = value },
          onClose: () => { open.value = false },
          onInstall: (identifier: string, source: string, name: string) => {
            installed.push([identifier, source, name])
          },
          onRetry: (id: string, acknowledgeRisk?: boolean) => {
            retried.push([id, acknowledgeRisk])
          },
          onCancelInstall: (source: SkillInstallSource) => {
            cancelled.push(source)
          },
          onClearActivity: (source: SkillInstallSource) => {
            cleared.push(source)
            activities.value[source] = { items: [], refreshWarning: '' }
          },
        }),
      ])
    },
  })

  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(Root)
  app.use(i18n)
  app.mount(host)
  apps.push(app)

  return {
    host,
    open,
    githubUrl,
    queue,
    activities,
    runningSource,
    cancellingSource,
    installed,
    retried,
    cleared,
    cancelled,
  }
}

describe('SkillsAddDrawer', () => {
  it('is absent by default, opens on GitHub, closes by scrim, and restores focus', async () => {
    const { open } = mountDrawer()
    const trigger = document.querySelector<HTMLButtonElement>('#drawer-trigger')!

    expect(document.querySelector('.sk-add-drawer')).toBeNull()
    trigger.focus()
    trigger.click()
    await nextTick()
    await nextTick()

    const drawer = document.querySelector<HTMLElement>('.sk-add-drawer')
    expect(drawer).not.toBeNull()
    expect(drawer?.getAttribute('role')).toBe('dialog')
    const sourceGroup = document.querySelector<HTMLElement>('.sk-add-source-tabs')
    const githubButton = document.querySelector<HTMLElement>('#skills-add-tab-github')
    const clawhubButton = document.querySelector<HTMLElement>('#skills-add-tab-clawhub')
    expect(sourceGroup?.getAttribute('role')).toBe('group')
    expect(githubButton?.getAttribute('aria-pressed')).toBe('true')
    expect(clawhubButton?.getAttribute('aria-pressed')).toBe('false')
    expect(githubButton?.hasAttribute('aria-selected')).toBe(false)
    expect(githubButton?.hasAttribute('aria-controls')).toBe(false)
    expect(document.querySelector('#skills-add-panel-github')?.hasAttribute('role')).toBe(false)
    expect(document.activeElement).toBe(drawer?.querySelector('.sk-add-drawer__close'))

    document.querySelector<HTMLElement>('[data-testid="skills-add-scrim"]')?.click()
    await nextTick()
    expect(open.value).toBe(false)
    await new Promise(resolve => setTimeout(resolve, 320))
    expect(document.querySelector('.sk-add-drawer')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('switches sources as pressed buttons and renders only safe retry-after text', async () => {
    mountDrawer({
      registryDiagnostics: [
        {
          code: 'SOURCE_RATE_LIMITED',
          severity: 'warning',
          phase: 'source',
          blocking: false,
          message: 'ClawHub rate limited the request.',
          details: {
            retryAfter: '<b>30</b>\nseconds',
            ignored: { secret: 'not rendered' },
          },
        },
      ],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const githubButton = document.querySelector<HTMLButtonElement>('#skills-add-tab-github')!
    const clawhubButton = document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')!
    clawhubButton.click()
    await nextTick()

    expect(clawhubButton.getAttribute('aria-pressed')).toBe('true')
    expect(githubButton.getAttribute('aria-pressed')).toBe('false')
    expect(document.querySelector('#skills-add-panel-github')).toBeNull()
    expect(document.querySelector('#skills-add-panel-clawhub')?.hasAttribute('role')).toBe(false)
    const callout = document.querySelector<HTMLElement>('.sk-add-callout')!
    expect(callout.textContent).toContain('Retry after: <b>30</b> seconds')
    expect(callout.innerHTML).not.toContain('<b>30</b>')
    expect(callout.textContent).not.toContain('secret')
  })

  it('explains newline batching and reports only exact duplicate references', async () => {
    const mounted = mountDrawer()
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const input = document.querySelector<HTMLTextAreaElement>('#skills-add-github-input')!
    const formatHint = document.querySelector<HTMLElement>('#skills-add-github-format-hint')!
    const batchHint = document.querySelector<HTMLElement>('#skills-add-github-batch-hint')!
    expect(formatHint.textContent).toContain('Commas, semicolons, and spaces do not separate references')
    expect(batchHint.textContent).toContain('Ordinary failures do not stop later references')
    expect(batchHint.textContent).toContain('a GitHub rate limit pauses the remaining batch')
    expect(input.getAttribute('aria-describedby'))
      .toBe('skills-add-github-format-hint skills-add-github-batch-hint')
    expect(document.querySelector('#skills-add-github-duplicates-hint')).toBeNull()

    mounted.githubUrl.value = [
      'acme/demo@abc:skills/one/SKILL.md',
      'acme/demo@abc:skills/one/SKILL.md',
      'acme/demo@abc:skills/two/SKILL.md',
    ].join('\n')
    await nextTick()

    expect(document.querySelector('[data-testid="skills-install-github"]')?.textContent)
      .toContain('Install 2 Skill(s)')
    expect(document.querySelector('#skills-add-github-duplicates-hint')?.textContent)
      .toContain('Exact duplicate references ignored: 1')
    expect(input.getAttribute('aria-describedby')).toBe([
      'skills-add-github-format-hint',
      'skills-add-github-batch-hint',
      'skills-add-github-duplicates-hint',
    ].join(' '))

    mounted.githubUrl.value = [
      'acme/demo@abc:skills/one/SKILL.md',
      'acme/demo@abc:skills/two/SKILL.md',
    ].join('\n')
    await nextTick()

    expect(document.querySelector('#skills-add-github-duplicates-hint')).toBeNull()
    expect(input.getAttribute('aria-describedby'))
      .toBe('skills-add-github-format-hint skills-add-github-batch-hint')
  })

  it('blocks an oversized GitHub batch with its current count and split-batch guidance', async () => {
    const mounted = mountDrawer()
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    mounted.githubUrl.value = Array.from({ length: 11 }, (_, index) =>
      `acme/demo@abc:skills/skill-${index + 1}/SKILL.md`).join('\n')
    await nextTick()

    const input = document.querySelector<HTMLTextAreaElement>('#skills-add-github-input')!
    const installButton = document.querySelector<HTMLButtonElement>(
      '[data-testid="skills-install-github"]',
    )!
    expect(document.querySelector('#skills-add-github-batch-hint')?.textContent)
      .toContain('11 / 10 unique references')
    expect(document.querySelector('#skills-add-github-limit-hint')?.textContent)
      .toContain('batches of 10 or fewer')
    expect(installButton.disabled).toBe(true)
    expect(input.getAttribute('aria-invalid')).toBe('true')
    expect(input.getAttribute('aria-describedby')).toContain('skills-add-github-limit-hint')
  })

  it('keeps queue results across close and reopen and tolerates an old Gateway payload', async () => {
    const queue: SkillInstallQueueItem[] = [{
      id: '["github","legacy"]',
      identifier: 'legacy',
      source: 'github',
      displayName: 'legacy-skill',
      status: 'installed',
      result: { success: true, name: 'legacy-skill' },
    }]
    mountDrawer({ queue })
    const trigger = document.querySelector<HTMLButtonElement>('#drawer-trigger')!
    trigger.click()
    await nextTick()
    expect(document.querySelector('.sk-add-queue-item')?.textContent).toContain('legacy-skill')
    expect((document.querySelector('.sk-add-activity-body') as HTMLElement)?.style.display)
      .toBe('none')

    document.querySelector<HTMLButtonElement>('.sk-add-drawer__close')?.click()
    await nextTick()
    trigger.click()
    await nextTick()

    expect(document.querySelector('.sk-add-queue-item')?.getAttribute('data-status')).toBe('installed')
    expect(document.querySelector('.sk-add-queue-item')?.textContent).toContain('legacy-skill')
  })

  it('offers backend cancellation only for a cancellable active install', async () => {
    const queue: SkillInstallQueueItem[] = [{
      id: '["github","active"]',
      identifier: 'active',
      source: 'github',
      displayName: 'active-skill',
      status: 'installing',
    }]
    const mounted = mountDrawer({
      queue,
      runningSource: 'github',
      cancellableInstallSource: 'github',
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const cancel = document.querySelector<HTMLButtonElement>('[data-testid="skills-cancel-install"]')!
    expect(cancel.textContent).toContain('Cancel install')
    expect(cancel.disabled).toBe(false)
    cancel.click()
    expect(mounted.cancelled).toEqual(['github'])

    mounted.cancellingSource.value = 'github'
    await nextTick()
    expect(cancel.textContent).toContain('Cancelling')
    expect(cancel.disabled).toBe(true)
  })

  it('counts cancelled items as processed and allows a safe retry', async () => {
    const queue: SkillInstallQueueItem[] = [{
      id: '["github","cancelled"]',
      identifier: 'cancelled',
      source: 'github',
      displayName: 'cancelled-skill',
      status: 'cancelled',
    }]
    const mounted = mountDrawer({ queue })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    expect(document.querySelector('.sk-add-section-title')?.textContent)
      .toContain('1 / 1 processed · 1 cancelled')
    const retry = document.querySelector<HTMLButtonElement>('.sk-add-retry')!
    expect(retry.disabled).toBe(false)
    retry.click()
    expect(mounted.retried).toEqual([['["github","cancelled"]', false]])
    expect(document.querySelector('[data-testid="skills-cancel-install"]')).toBeNull()
  })

  it('renders upstream diagnostic details as text and disables mutations while running', async () => {
    const queue: SkillInstallQueueItem[] = [{
      id: '["github","failed"]',
      identifier: 'failed',
      source: 'github',
      displayName: 'failed-skill',
      status: 'failed',
      error: 'Rejected',
      result: {
        success: false,
        diagnostics: [{
          code: 'DIALECT_FIELD_UNSUPPORTED',
          severity: 'error',
          phase: 'compatibility',
          blocking: true,
          message: 'Unsupported field.',
          details: { upstreamText: '<em data-e2e="must-stay-text">literal text</em>' },
        }, {
          code: 'NO_DETAILS',
          severity: 'warning',
          phase: 'archive',
          blocking: false,
          message: 'No structured details.',
          details: {},
        }],
      },
    }]
    mountDrawer({ queue, runningSource: 'github' })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const input = document.querySelector<HTMLTextAreaElement>('#skills-add-github-input')
    const installButton = document.querySelector<HTMLButtonElement>('[data-testid="skills-install-github"]')
    const retry = document.querySelector<HTMLButtonElement>('.sk-add-retry')
    expect(input?.disabled).toBe(true)
    expect(installButton?.disabled).toBe(true)
    expect(installButton?.getAttribute('aria-busy')).toBe('true')
    expect(installButton?.querySelector('.sk-spinner')).toBeNull()
    expect(retry?.disabled).toBe(true)

    document.querySelector<HTMLDetailsElement>('.sk-add-diagnostics')!.open = true
    await nextTick()
    expect(document.querySelector('.sk-add-diagnostics')?.textContent).toContain('literal text')
    expect(document.querySelector('[data-e2e="must-stay-text"]')).toBeNull()
    expect(document.querySelectorAll('.sk-add-diagnostics pre')).toHaveLength(1)
  })

  it('disables install entry points without showing queue progress when another mutation owns the surface', async () => {
    mountDrawer({
      mutationBlocked: true,
      results: [{
        name: 'Demo',
        installReference: '@acme/demo',
        source: 'clawhub',
      }],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const githubInstall = document.querySelector<HTMLButtonElement>('[data-testid="skills-install-github"]')
    expect(document.querySelector<HTMLTextAreaElement>('#skills-add-github-input')?.disabled).toBe(true)
    expect(githubInstall?.disabled).toBe(true)
    expect(githubInstall?.getAttribute('aria-busy')).toBe('false')

    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()
    expect(document.querySelector<HTMLButtonElement>('.sk-add-result .btn--primary')?.disabled).toBe(true)
  })

  it('installs the exact ClawHub installReference returned by search', async () => {
    const { installed } = mountDrawer({
      results: [{
        name: 'Demo',
        identifier: 'demo',
        installReference: '@verified/demo@1.2.3',
        source: 'clawhub',
        author: 'Verified',
        version: '1.2.3',
      }],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('.sk-add-result .btn--primary')?.click()

    expect(installed).toEqual([['@verified/demo@1.2.3', 'clawhub', 'Demo']])
  })

  it('shows install activity before long ClawHub search results', async () => {
    const queue: SkillInstallQueueItem[] = [{
      id: '["clawhub","@verified/demo"]',
      identifier: '@verified/demo',
      source: 'clawhub',
      displayName: 'demo',
      status: 'failed',
      error: 'Rejected',
    }]
    mountDrawer({
      queue,
      results: [{
        name: 'Demo',
        installReference: '@verified/demo',
        source: 'clawhub',
      }],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    const results = document.querySelector<HTMLElement>('.sk-add-results')!
    expect(activity.compareDocumentPosition(results) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
  })

  it('keeps result progress compact and reveals Activity as the only failure owner', async () => {
    const operationKey = '["clawhub","@verified/demo"]'
    const queue: SkillInstallQueueItem[] = [{
      id: operationKey,
      identifier: '@verified/demo',
      source: 'clawhub',
      displayName: 'Demo',
      status: 'installing',
    }]
    const mounted = mountDrawer({
      queue,
      runningSource: 'clawhub',
      results: [{
        name: 'Demo',
        installReference: '@verified/demo',
        source: 'clawhub',
      }],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    const result = document.querySelector<HTMLElement>('.sk-add-result')!
    const action = result.querySelector<HTMLButtonElement>('button')!
    expect(result.dataset.status).toBe('installing')
    expect(action.textContent).toContain('Installing')
    expect(action.getAttribute('aria-busy')).toBe('true')
    expect(action.querySelector('.sk-spinner')).toBeNull()
    expect(document.querySelector('#skills-add-tab-clawhub .sk-spinner')).toBeNull()
    expect(document.querySelectorAll('.sk-add-queue-item .sk-spinner')).toHaveLength(1)
    expect(document.querySelector('.sk-add-queue')?.hasAttribute('aria-live')).toBe(false)
    const announcement = document.querySelector<HTMLElement>('.sk-add-install-announcement')!
    expect(announcement.getAttribute('aria-atomic')).toBe('true')
    expect(announcement.textContent).toContain('Installing Demo')

    mounted.queue.value[0].status = 'failed'
    mounted.queue.value[0].error = 'Manifest rejected'
    mounted.runningSource.value = null
    await nextTick()

    expect(result.dataset.status).toBe('failed')
    expect(action.textContent).toContain('View details')
    expect(result.textContent).toContain('Failed')
    expect(result.textContent).not.toContain('Manifest rejected')
    expect(result.textContent).not.toContain('Not installed')
    expect(result.textContent).not.toContain('Retry')
    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    expect(activity.textContent).toContain('Manifest rejected')
    expect(activity.textContent).toContain('Not installed')
    activity.querySelector<HTMLButtonElement>('.sk-add-activity-toggle')?.click()
    await nextTick()
    expect(activity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display).toBe('none')
    action.click()
    await nextTick()
    expect(activity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .not.toBe('none')
    expect(mounted.retried).toEqual([])
  })

  it('keeps source activity isolated and exposes inactive failures on the source tab only', async () => {
    const activities: SkillInstallActivities = {
      clawhub: {
        items: [{
          id: '["clawhub","@acme/failed"]',
          identifier: '@acme/failed',
          source: 'clawhub',
          displayName: 'Claw failure',
          status: 'failed',
          error: 'Manifest rejected',
          result: { success: false, installed: false },
        }],
        refreshWarning: '',
      },
      github: {
        items: [{
          id: '["github","acme/ready"]',
          identifier: 'acme/ready',
          source: 'github',
          displayName: 'GitHub success',
          status: 'installed',
          result: { success: true, installed: true },
        }],
        refreshWarning: '',
      },
    }
    mountDrawer({ activities })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const githubActivity = document.querySelector<HTMLElement>('.sk-add-queue[data-source="github"]')!
    expect(githubActivity.textContent).toContain('GitHub success')
    expect(githubActivity.textContent).not.toContain('Claw failure')
    expect(githubActivity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .toBe('none')
    expect(document.querySelector('#skills-add-tab-clawhub .sk-add-source-failures')?.textContent)
      .toBe('1')

    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()
    const clawActivity = document.querySelector<HTMLElement>('.sk-add-queue[data-source="clawhub"]')!
    expect(clawActivity.textContent).toContain('Claw failure')
    expect(clawActivity.textContent).not.toContain('GitHub success')
    expect(clawActivity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .not.toBe('none')
  })

  it('shows background progress on its source tab and keeps read-only search available', async () => {
    const activities: SkillInstallActivities = {
      clawhub: { items: [], refreshWarning: '' },
      github: {
        items: [{
          id: '["github","acme/running"]',
          identifier: 'acme/running',
          source: 'github',
          displayName: 'running-skill',
          status: 'installing',
        }],
        refreshWarning: '',
      },
    }
    mountDrawer({ activities, runningSource: 'github' })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    const runningActivity = document.querySelector<HTMLElement>('.sk-add-queue')!
    expect(runningActivity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .not.toBe('none')
    expect(runningActivity.querySelector<HTMLButtonElement>('.sk-add-activity-toggle')?.disabled)
      .toBe(true)
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    const githubTab = document.querySelector<HTMLElement>('#skills-add-tab-github')!
    expect(githubTab.querySelector('.sk-spinner')).not.toBeNull()
    expect(githubTab.textContent).toContain('Installing running-skill')
    expect(document.querySelector('.sk-add-queue')).toBeNull()
    expect(document.querySelector<HTMLInputElement>('#skills-add-clawhub-query')?.disabled)
      .toBe(false)
    expect(document.querySelector<HTMLButtonElement>('.sk-add-search-row button')?.disabled)
      .toBe(false)
    expect(document.querySelector<HTMLButtonElement>('.sk-add-result button')).toBeNull()
  })

  it('shows one canonical spinner and truthful copy while refreshing the active source', async () => {
    const activities: SkillInstallActivities = {
      clawhub: { items: [], refreshWarning: '', phase: 'terminal' },
      github: {
        items: [{
          id: '["github","acme/ready"]',
          identifier: 'acme/ready',
          source: 'github',
          displayName: 'ready-skill',
          status: 'installed',
        }],
        refreshWarning: '',
        phase: 'refreshing',
      },
    }
    mountDrawer({ activities, runningSource: 'github' })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    const installButton = document.querySelector<HTMLButtonElement>(
      '[data-testid="skills-install-github"]',
    )!
    expect(activity.textContent).toContain('Reloading')
    expect(installButton.textContent).toContain('Reloading')
    expect(installButton.textContent).not.toContain('Installing')
    expect(installButton.querySelector('.sk-spinner')).toBeNull()
    expect(document.querySelector('#skills-add-tab-github .sk-spinner')).toBeNull()
    expect(activity.querySelectorAll('.sk-spinner')).toHaveLength(1)
    expect(activity.querySelector('.sk-add-section-title .sk-spinner')).not.toBeNull()
  })

  it('renders rate-limited remainder items as not attempted instead of failed', async () => {
    const deferredNote = 'Not attempted because GitHub rate limited an earlier reference.'
    const activities: SkillInstallActivities = {
      clawhub: { items: [], refreshWarning: '', phase: 'terminal' },
      github: {
        items: [{
          id: '["github","acme/limited"]',
          identifier: 'acme/limited',
          source: 'github',
          displayName: 'limited',
          status: 'failed',
          error: 'GitHub rate limited this batch.',
        }, {
          id: '["github","acme/later-one"]',
          identifier: 'acme/later-one',
          source: 'github',
          displayName: 'later-one',
          status: 'deferred',
        }, {
          id: '["github","acme/later-two"]',
          identifier: 'acme/later-two',
          source: 'github',
          displayName: 'later-two',
          status: 'deferred',
        }],
        refreshWarning: '',
        phase: 'terminal',
      },
    }
    mountDrawer({ activities })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    expect(activity.textContent).toContain('1 / 3 processed')
    expect(activity.textContent).toContain('1 failed')
    expect(activity.textContent).toContain('2 not attempted')
    const deferred = activity.querySelectorAll<HTMLElement>('[data-status="deferred"]')
    expect(deferred).toHaveLength(2)
    expect(deferred[0].textContent).toContain('Not attempted')
    expect(deferred[0].querySelector('.sk-add-queue-item__error')).toBeNull()
    expect(deferred[0].querySelector('.sk-add-queue-item__note')?.textContent)
      .toContain(deferredNote)
  })

  it('summarizes terminal outcomes, allows manual disclosure, and clears only terminal activity', async () => {
    const activities: SkillInstallActivities = {
      clawhub: { items: [], refreshWarning: '' },
      github: {
        items: [{
          id: '["github","acme/installed"]',
          identifier: 'acme/installed',
          source: 'github',
          displayName: 'installed',
          status: 'installed',
        }, {
          id: '["github","acme/current"]',
          identifier: 'acme/current',
          source: 'github',
          displayName: 'current',
          status: 'unchanged',
        }],
        refreshWarning: '',
      },
    }
    const mounted = mountDrawer({ activities })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    expect(activity.textContent).toContain('2 / 2 processed')
    expect(activity.textContent).toContain('1 installed')
    expect(activity.textContent).toContain('1 already current')
    expect(activity.textContent).not.toContain('0 failed')
    expect(activity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .toBe('none')

    activity.querySelector<HTMLButtonElement>('.sk-add-activity-toggle')?.click()
    await nextTick()
    expect(activity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .not.toBe('none')

    const clear = Array.from(activity.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Clear activity'))!
    clear.click()
    await nextTick()
    expect(mounted.cleared).toEqual(['github'])
    expect(document.querySelector('.sk-add-queue')).toBeNull()
  })

  it('renders failed operation truth without misleading lifecycle or publication metadata', async () => {
    const missingLifecycle = {
      install_state: 'missing' as const,
      load_state: 'not_discovered' as const,
      selection_state: 'active' as const,
      compatibility_state: 'native' as const,
      readiness_state: 'unknown' as const,
    }
    const activities: SkillInstallActivities = {
      clawhub: {
        items: [{
          id: '["clawhub","@acme/new"]',
          identifier: '@acme/new',
          source: 'clawhub',
          displayName: 'new-skill',
          status: 'failed',
          error: 'Security scan blocked installation',
          result: {
            success: false,
            installed: false,
            effectiveFrom: 'next_turn',
            catalogGeneration: 0,
            lifecycle: missingLifecycle,
            resolution: {
              publisher: 'acme',
              version: '1.1.0',
              immutableRevision: '1.1.0',
            },
            diagnostics: [{
              code: 'SCAN_CONFIRMATION_REQUIRED',
              severity: 'warning',
              phase: 'security',
              blocking: true,
              message: 'Review the scanner findings before continuing.',
              details: {
                confirmationToken: 'reviewed-artifact-confirmation',
                resolvedIdentifier: '@acme/new@1.1.0',
                artifactDigest: 'artifact-digest',
                treeDigest: 'tree-digest',
              },
            }],
          },
        }],
        refreshWarning: '',
      },
      github: { items: [], refreshWarning: '' },
    }
    const mounted = mountDrawer({ activities })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    expect(activity.textContent).toContain('Not installed')
    expect(activity.textContent).not.toContain('Installed files missing')
    expect(activity.textContent).not.toContain('Available next turn')
    expect(activity.textContent).not.toContain('Catalog generation')
    expect(Array.from(activity.querySelectorAll('.sk-add-queue-item__meta span'))
      .filter(node => node.textContent === '1.1.0')).toHaveLength(1)
    const installAnyway = activity.querySelector<HTMLButtonElement>(
      '[data-testid="skills-install-acknowledge-risk"]',
    )!
    expect(installAnyway.textContent).toContain('Install anyway')
    installAnyway.click()
    await nextTick()
    expect(mounted.retried).toEqual([['["clawhub","@acme/new"]', true]])
  })

  it('reports a preserved installation when a reinstall fails', async () => {
    const activities: SkillInstallActivities = {
      clawhub: {
        items: [{
          id: '["clawhub","@acme/existing"]',
          identifier: '@acme/existing',
          source: 'clawhub',
          displayName: 'existing-skill',
          status: 'failed',
          error: 'Update rejected',
          result: {
            success: false,
            installed: true,
            lifecycle: {
              install_state: 'tracked',
              load_state: 'loaded',
              selection_state: 'active',
              compatibility_state: 'instruction_only',
              readiness_state: 'ready',
            },
          },
        }],
        refreshWarning: '',
      },
      github: { items: [], refreshWarning: '' },
    }
    mountDrawer({ activities })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    expect(document.querySelector('.sk-add-queue')?.textContent)
      .toContain('Existing installation preserved')
    expect(document.querySelector('.sk-add-queue')?.textContent).toContain('Active')
  })

  it('presents an interrupted response as unknown and marks its source for attention', async () => {
    const activities: SkillInstallActivities = {
      clawhub: {
        items: [{
          id: '["clawhub","@acme/uncertain"]',
          identifier: '@acme/uncertain',
          source: 'clawhub',
          displayName: 'uncertain-skill',
          status: 'unknown',
          error: 'connection closed',
        }],
        refreshWarning: '',
        phase: 'terminal',
      },
      github: { items: [], refreshWarning: '', phase: 'terminal' },
    }
    mountDrawer({
      activities,
      results: [{
        name: 'uncertain-skill',
        installReference: '@acme/uncertain',
        source: 'clawhub',
      }],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    expect(document.querySelector('#skills-add-tab-clawhub .sk-add-source-failures')?.textContent)
      .toBe('1')
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    const result = document.querySelector<HTMLElement>('.sk-add-result')!
    expect(activity.textContent).toContain('Installation result unknown')
    expect(activity.textContent).toContain('connection closed')
    expect(activity.textContent).not.toContain('Not installed')
    expect(activity.querySelector('.sk-add-retry')).toBeNull()
    expect(activity.querySelector('.sk-spinner')).toBeNull()
    expect(result.textContent).toContain('Installation result unknown')
    expect(result.textContent).toContain('View details')
    expect(result.textContent).not.toContain('connection closed')
  })
})
