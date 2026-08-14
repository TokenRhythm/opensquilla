// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive, type App } from 'vue'

import i18n from '@/i18n'
import type { MetaSetupState } from '@/types/metaSetup'
import MetaSkillSetupCard from './MetaSkillSetupCard.vue'

function state(overrides: Partial<MetaSetupState> = {}): MetaSetupState {
  return {
    name: 'meta-paper-write',
    sessionKey: 'agent:main:webchat:setup-card',
    phase: 'confirm',
    readiness: {
      missing_bins: ['xelatex', 'bibtex'],
      missing_capabilities: ['paper-tex'],
      setup_actions: [{
        id: 'meta-paper-write:paper-toolchain',
        install_id: 'paper-tex',
        label: 'Paper tools',
        bins: ['xelatex', 'bibtex'],
        available: true,
        version: '1.0.0',
        download_size_bytes: 42 * 1024 * 1024,
        source: 'https://example.test/paper-tools',
        license: 'MIT',
      }],
    },
    actionIds: ['meta-paper-write:paper-toolchain'],
    completedActions: [],
    ...overrides,
  }
}

async function mountCard(
  setupState: MetaSetupState,
  handlers: {
    onConfirm?: () => void
    onCancel?: () => void
    onRetry?: () => void
    onConfigure?: (providerId: string) => void
    providerNavigationPending?: boolean
  } = {},
) {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp(MetaSkillSetupCard, { state: setupState, ...handlers })
  app.use(i18n)
  app.mount(root)
  await nextTick()
  return { app, root }
}

let apps: App[] = []

beforeEach(() => {
  i18n.global.locale.value = 'en'
  i18n.global.mergeLocaleMessage('en', {
    chat: {
      metaSetup: {
        title: 'Set up {skill}',
        badgeConfirm: 'Setup needed',
        badgeInstalling: 'Installing',
        badgeVerifying: 'Verifying',
        badgeFailed: 'Setup failed',
        badgeBlocked: 'Action needed',
        intro: 'Review local tools and service connections required by this MetaSkill.',
        missingDependencies: 'Missing dependencies',
        installActions: 'OpenSquilla will install',
        source: 'Source',
        version: 'Version {version}',
        license: 'License',
        downloadSize: 'Download',
        paperToolchain: 'Managed TeX paper toolchain',
        mediaToolchain: 'Compatible FFmpeg media toolchain',
        missingRuntimeCapabilities: 'Runtime capabilities still missing: {capabilities}',
        requiresAdmin: 'Administrator approval may be required.',
        confirmHint: 'OpenSquilla will continue the MetaSkill when setup is ready.',
        homebrewHint: 'Homebrew resolves the current formula and installs it outside OpenSquilla. OpenSquilla does not uninstall it.',
        installAndContinue: 'Install and continue',
        notNow: 'Not now',
        hide: 'Hide',
        installingStatus: 'Installing tools',
        verifyingStatus: 'Verifying tools',
        downloadProgress: 'Download progress',
        completedActions: '{completed} of {total} setup steps complete',
        failedTitle: 'Setup failed',
        blockedTitle: 'More setup is needed',
        retry: 'Retry',
        checkAgain: 'Check again',
        providerTitle: 'Connect {provider} to continue',
        providerCapabilities: 'Used for: {capabilities}.',
        providerReasonMissingCredential: 'Add a credential for this provider.',
        providerReasonInvalidEndpoint: 'Review this provider endpoint.',
        providerReasonConnectionRequired: 'A valid provider connection is required.',
        providerNoChargeHint: 'Saving this connection does not start generation or incur generation charges. You will confirm generation separately.',
        configureProvider: 'Connect {provider}',
        close: 'Close',
        noAutomaticSetup: 'Automatic setup is unavailable.',
        sessionChanged: 'Return to the original conversation and run the MetaSkill again.',
        launchFailed: 'The MetaSkill could not start after setup.',
      },
    },
  })
  document.body.innerHTML = ''
})

afterEach(() => {
  for (const app of apps) app.unmount()
  apps = []
  document.body.innerHTML = ''
})

describe('MetaSkillSetupCard', () => {
  it('renders a labelled confirmation region with exact dependency and package details', async () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    const mounted = await mountCard(state(), { onConfirm, onCancel })
    apps.push(mounted.app)

    const card = mounted.root.querySelector('[data-testid="meta-setup-card"]')
    const titleId = card?.getAttribute('aria-labelledby') || ''
    const descriptionId = card?.getAttribute('aria-describedby') || ''
    expect(card?.getAttribute('role')).toBe('region')
    expect(card?.getAttribute('aria-busy')).toBe('false')
    expect(mounted.root.querySelector(`#${titleId}`)?.textContent).toContain('meta-paper-write')
    expect(mounted.root.querySelector(`#${descriptionId}`)).toBeTruthy()
    expect(mounted.root.querySelector('[data-testid="meta-setup-missing"]')?.textContent)
      .toContain('xelatex')
    expect(mounted.root.querySelector('[data-testid="meta-setup-missing"]')?.textContent)
      .toContain('paper-tex')
    expect(mounted.root.textContent).toContain('42 MB')
    expect(mounted.root.textContent).toContain('Managed TeX paper toolchain')
    expect(mounted.root.querySelector('a')?.getAttribute('rel')).toBe('noopener')
    expect(card?.getAttribute('tabindex')).toBe('-1')

    mounted.root.querySelector<HTMLButtonElement>('[data-testid="meta-setup-confirm"]')?.click()
    mounted.root.querySelector<HTMLButtonElement>('[data-testid="meta-setup-cancel"]')?.click()
    expect(onConfirm).toHaveBeenCalledOnce()
    expect(onCancel).toHaveBeenCalledOnce()
  })

  it('explains Homebrew only for an action using its formula source', async () => {
    const paper = await mountCard(state())
    apps.push(paper.app)
    expect(paper.root.textContent).not.toContain('Homebrew resolves the current formula')

    const media = await mountCard(state({
      readiness: {
        setup_actions: [{
          id: 'meta-short-drama:media-ffmpeg',
          install_id: 'media-ffmpeg',
          source: 'https://formulae.brew.sh/formula/ffmpeg-full',
          available: true,
        }],
      },
    }))
    apps.push(media.app)
    expect(media.root.textContent).toContain('Compatible FFmpeg media toolchain')
    expect(media.root.textContent).toContain('Homebrew resolves the current formula')
    expect(media.root.textContent).toContain('installs it outside OpenSquilla')
    expect(media.root.textContent).toContain('does not uninstall it')
  })

  it('explains administrator approval when an install action requires it', async () => {
    const setupState = state()
    setupState.readiness.setup_actions![0].requires_admin = true
    const mounted = await mountCard(setupState)
    apps.push(mounted.app)

    expect(mounted.root.textContent).toContain('Administrator approval may be required.')
  })

  it('focuses a newly shown confirmation once without refocusing during progress polls', async () => {
    const setupState = reactive(state()) as MetaSetupState
    const mounted = await mountCard(setupState)
    apps.push(mounted.app)
    await nextTick()

    const card = mounted.root.querySelector<HTMLElement>('[data-testid="meta-setup-card"]')
    expect(document.activeElement).toBe(card)

    const outsideButton = document.createElement('button')
    document.body.appendChild(outsideButton)
    outsideButton.focus()
    setupState.phase = 'installing'
    setupState.downloadedBytes = 1
    await nextTick()
    setupState.downloadedBytes = 2
    await nextTick()

    expect(document.activeElement).toBe(outsideButton)
  })

  it('keeps keyboard focus in the setup flow when confirm becomes install and then fails', async () => {
    const setupState = reactive(state()) as MetaSetupState
    const mounted = await mountCard(setupState)
    apps.push(mounted.app)

    const confirm = mounted.root.querySelector<HTMLButtonElement>(
      '[data-testid="meta-setup-confirm"]',
    )
    confirm?.focus()
    expect(document.activeElement).toBe(confirm)

    setupState.phase = 'installing'
    await nextTick()

    const card = mounted.root.querySelector<HTMLElement>('[data-testid="meta-setup-card"]')
    expect(document.activeElement).toBe(card)

    setupState.phase = 'failed'
    setupState.retryMode = 'install'
    setupState.error = 'Checksum mismatch'
    await nextTick()

    const retry = mounted.root.querySelector<HTMLButtonElement>('[data-testid="meta-setup-retry"]')
    expect(document.activeElement).toBe(retry)
  })

  it('announces installing and reports backend-provided byte progress', async () => {
    const mounted = await mountCard(state({
      phase: 'installing',
      currentAction: 'meta-paper-write:paper-toolchain',
      message: 'Downloading verified package',
      downloadedBytes: 21 * 1024 * 1024,
      downloadTotalBytes: 42 * 1024 * 1024,
    }))
    apps.push(mounted.app)

    const card = mounted.root.querySelector('[data-testid="meta-setup-card"]')
    const status = mounted.root.querySelector('[data-testid="meta-setup-status"]')
    const phaseStatus = mounted.root.querySelector('[data-testid="meta-setup-phase-status"]')
    expect(card?.getAttribute('aria-busy')).toBe('true')
    expect(status?.getAttribute('role')).toBeNull()
    expect(status?.getAttribute('aria-live')).toBeNull()
    expect(phaseStatus?.getAttribute('role')).toBe('status')
    expect(phaseStatus?.getAttribute('aria-live')).toBe('polite')
    expect(phaseStatus?.getAttribute('aria-atomic')).toBe('true')
    // Backend job messages are diagnostic and not localized; the visible
    // status stays on the locale-owned phase label.
    expect(status?.textContent).toContain('Installing tools')
    expect(status?.textContent).not.toContain('Downloading verified package')
    expect(status?.textContent).toContain('Managed TeX paper toolchain')
    expect(status?.textContent).not.toContain('Paper tools')
    expect(status?.textContent).toContain('21 MB / 42 MB')
    const progress = mounted.root.querySelector('[role="progressbar"]')
    expect(progress?.getAttribute('aria-valuenow')).toBe(String(21 * 1024 * 1024))
    expect(progress?.getAttribute('aria-valuemax')).toBe(String(42 * 1024 * 1024))
    expect(progress?.getAttribute('aria-valuetext')).toBe('21 MB / 42 MB')
    expect(progress?.querySelector<HTMLElement>('span')?.style.width).toBe('50%')
  })

  it('does not invent byte progress when the installer cannot report a total', async () => {
    const mounted = await mountCard(state({
      phase: 'installing',
      downloadedBytes: 0,
      downloadTotalBytes: 0,
    }))
    apps.push(mounted.app)

    expect(mounted.root.querySelector('[role="progressbar"]')).toBeNull()
    expect(mounted.root.textContent).not.toContain('%')
  })

  it('explains the post-download setup stage instead of appearing stuck at 100%', async () => {
    const mounted = await mountCard(state({
      phase: 'installing',
      downloadedBytes: 42 * 1024 * 1024,
      downloadTotalBytes: 42 * 1024 * 1024,
    }))
    apps.push(mounted.app)

    const status = mounted.root.querySelector('[data-testid="meta-setup-status"]')
    expect(status?.textContent).toContain('Download complete. Finishing local toolchain setup')
    expect(status?.textContent).not.toContain('Installing the selected components')
  })

  it('renders failures as alerts and exposes retry and close actions', async () => {
    const onRetry = vi.fn()
    const onCancel = vi.fn()
    const mounted = await mountCard(state({
      phase: 'failed',
      error: 'Checksum mismatch',
      retryMode: 'install',
    }), { onRetry, onCancel })
    apps.push(mounted.app)

    const alert = mounted.root.querySelector('[data-testid="meta-setup-error"]')
    expect(alert?.getAttribute('role')).toBe('alert')
    expect(alert?.textContent).toContain('Checksum mismatch')

    mounted.root.querySelector<HTMLButtonElement>('[data-testid="meta-setup-retry"]')?.click()
    mounted.root.querySelector<HTMLButtonElement>('[data-testid="meta-setup-cancel"]')?.click()
    expect(onRetry).toHaveBeenCalledOnce()
    expect(onCancel).toHaveBeenCalledOnce()
  })

  it('explains a non-installable blocked state without showing a retry button', async () => {
    const mounted = await mountCard(state({
      phase: 'blocked',
      actionIds: [],
      blockedReason: 'no_actions',
      error: 'No package is available for this platform',
      retryMode: undefined,
    }))
    apps.push(mounted.app)

    const alert = mounted.root.querySelector('[data-testid="meta-setup-blocked"]')
    expect(alert?.getAttribute('role')).toBe('alert')
    expect(alert?.textContent).toContain('No package is available for this platform')
    expect(mounted.root.querySelector('[data-testid="meta-setup-retry"]')).toBeNull()
  })

  it('keeps a provider-only setup actionable without hard-coding an environment key', async () => {
    const onConfigure = vi.fn()
    const onRetry = vi.fn()
    const mounted = await mountCard(state({
      phase: 'confirm',
      readiness: {
        ready: false,
        missing_env: ['ACME_MEDIA_TOKEN'],
        setup_actions: [],
        manual_setup_actions: [{
          id: 'provider:acme-media',
          kind: 'provider_connection',
          provider_id: 'acme-media',
          label: 'Acme Media',
          capability_ids: ['image.generate', 'video.generate'],
          recommended: true,
          available: true,
          reason_code: 'missing_credential',
          reason: 'unsafe raw backend detail must not render',
        }],
      },
      actionIds: [],
      retryMode: 'readiness',
    }), { onConfigure, onRetry })
    apps.push(mounted.app)

    const provider = mounted.root.querySelector('[data-testid="meta-setup-provider"]')
    expect(provider?.getAttribute('data-provider-id')).toBe('acme-media')
    expect(provider?.textContent).toContain('Connect Acme Media to continue')
    expect(provider?.textContent).toContain('image.generate, video.generate')
    expect(provider?.textContent).toContain('Add a credential for this provider')
    expect(provider?.textContent).not.toContain('unsafe raw backend detail')
    expect(provider?.textContent).toContain('does not start generation')
    expect(provider?.textContent).toContain('confirm generation separately')
    expect(mounted.root.querySelector('[data-testid="meta-setup-blocked"]')).toBeNull()
    expect(mounted.root.querySelector('[data-testid="meta-setup-confirm"]')).toBeNull()
    const configure = mounted.root.querySelector<HTMLButtonElement>(
      '[data-testid="meta-setup-configure-provider"]',
    )
    expect(configure?.classList.contains('btn--primary')).toBe(true)
    expect(configure?.textContent).toContain('Connect Acme Media')
    const retry = mounted.root.querySelector<HTMLButtonElement>('[data-testid="meta-setup-retry"]')
    expect(retry?.textContent).toContain('Check again')

    configure?.click()
    retry?.click()
    expect(onConfigure).toHaveBeenCalledWith('acme-media')
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('disables provider actions while navigation is in flight', async () => {
    const mounted = await mountCard(state({
      readiness: {
        setup_actions: [],
        manual_setup_actions: [{
          id: 'provider:acme-media',
          kind: 'provider_connection',
          provider_id: 'acme-media',
          available: true,
        }],
      },
      actionIds: [],
      retryMode: 'readiness',
    }), { providerNavigationPending: true })
    apps.push(mounted.app)

    expect(mounted.root.querySelector<HTMLButtonElement>(
      '[data-testid="meta-setup-configure-provider"]',
    )?.disabled).toBe(true)
    expect(mounted.root.querySelector<HTMLButtonElement>(
      '[data-testid="meta-setup-retry"]',
    )?.disabled).toBe(true)
  })

  it('does not infer a provider settings action from a raw missing environment key', async () => {
    const mounted = await mountCard(state({
      phase: 'blocked',
      readiness: {
        ready: false,
        missing_env: ['OPENROUTER_API_KEY'],
        setup_actions: [],
      },
      actionIds: [],
      blockedReason: 'no_actions',
      retryMode: 'readiness',
    }))
    apps.push(mounted.app)

    expect(mounted.root.querySelector('[data-testid="meta-setup-provider"]')).toBeNull()
    expect(mounted.root.querySelector('[data-testid="meta-setup-configure-provider"]')).toBeNull()
  })
})
