// @vitest-environment happy-dom

import { createApp, h, nextTick, ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SkillDetailDialog from './SkillDetailDialog.vue'
import i18n from '@/i18n'
import en from '@/locales/en.json'
import type { Proposal, Skill } from '@/types/skills'

const apps: ReturnType<typeof createApp>[] = []

beforeEach(() => {
  i18n.global.locale.value = 'en'
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.setAttribute('open', '')
    },
  })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.removeAttribute('open')
    },
  })
})

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

function mountDialog(initial: Skill | null, initialProposal: Proposal | null = null, canUseInTask = false) {
  const skill = ref<Skill | null>(initial)
  const proposal = ref<Proposal | null>(initialProposal)
  const close = vi.fn(() => {
    skill.value = null
    proposal.value = null
  })
  const installDeps = vi.fn()
  const setEnabled = vi.fn()
  const useInTask = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    setup: () => () => h(SkillDetailDialog, {
      skill: skill.value,
      proposal: proposal.value,
      loadingContent: false,
      contentError: '',
      installFeedback: '',
      installingDepsId: null,
      uninstallingName: null,
      onClose: close,
      onInstallDeps: installDeps,
      canSetEnabled: true,
      onSetEnabled: setEnabled,
      canUseInTask,
      onUseInTask: useInTask,
    }),
  })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.mount(host)
  apps.push(app)
  return { skill, proposal, close, installDeps, setEnabled, useInTask, host, dialog: host.querySelector('dialog')! }
}

describe('SkillDetailDialog behavior contract', () => {
  it('hands an eligible skill to the draft action without toggling or installing it', async () => {
    const skill = { name: 'synthetic-ready', status: 'ready' }
    const mounted = mountDialog(skill, null, true)
    await nextTick()
    mounted.dialog.querySelector<HTMLButtonElement>('.sk-detail__launch button')!.click()
    expect(mounted.useInTask).toHaveBeenCalledExactlyOnceWith(skill)
    expect(mounted.installDeps).not.toHaveBeenCalled()
    expect(mounted.setEnabled).not.toHaveBeenCalled()
  })

  it('does not advertise a draft launch when the catalog says it is unavailable', async () => {
    const mounted = mountDialog({ name: 'synthetic-disabled', disabled: true })
    await nextTick()
    expect(mounted.dialog.querySelector('.sk-detail__launch')).toBeNull()
  })
  it('allows a disabled skill without implicitly installing its dependencies', async () => {
    const mounted = mountDialog({ name: 'synthetic', disabled: true, missing_bins: ['synthetic-bin'] })
    await nextTick()
    const toggle = mounted.dialog.querySelector<HTMLButtonElement>('[role="switch"]')!
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    toggle.click()
    expect(mounted.setEnabled).toHaveBeenCalledExactlyOnceWith('synthetic', true)
    expect(mounted.installDeps).not.toHaveBeenCalled()
    expect(mounted.dialog.textContent).toContain('Use is disabled')
  })

  it('routes native cancel through the parent close path and can reopen', async () => {
    const alpha = { name: 'alpha', description: 'Alpha skill' }
    const mounted = mountDialog(alpha)
    await nextTick()
    expect(mounted.dialog.open).toBe(true)

    const cancel = new Event('cancel', { cancelable: true })
    mounted.dialog.dispatchEvent(cancel)
    await nextTick()
    expect(cancel.defaultPrevented).toBe(true)
    expect(mounted.close).toHaveBeenCalledTimes(1)
    expect(mounted.skill.value).toBeNull()

    mounted.skill.value = alpha
    await nextTick()
    expect(mounted.dialog.open).toBe(true)
  })

  it('synchronizes an independent native close with parent selection', async () => {
    const mounted = mountDialog({ name: 'alpha' })
    await nextTick()
    mounted.dialog.removeAttribute('open')
    mounted.dialog.dispatchEvent(new Event('close'))
    await nextTick()

    expect(mounted.close).toHaveBeenCalledTimes(1)
    expect(mounted.skill.value).toBeNull()
  })

  it('reopens a closed dialog when a different card is selected', async () => {
    const mounted = mountDialog(null)
    mounted.skill.value = { name: 'alpha' }
    await nextTick()
    mounted.dialog.removeAttribute('open')

    mounted.skill.value = { name: 'beta' }
    await nextTick()

    expect(mounted.dialog.open).toBe(true)
    expect(mounted.dialog.textContent).toContain('beta')
  })

  it('shows only install actions that match current missing dependencies', async () => {
    const mounted = mountDialog({
      name: 'render',
      status: 'needs_setup',
      missing_bins: ['ffmpeg'],
      install: [
        { id: 'ffmpeg', kind: 'brew', label: 'Current FFmpeg', bins: ['ffmpeg'] },
        { id: 'stale', kind: 'brew', label: 'Stale ImageMagick', bins: ['imagemagick'] },
      ],
    })
    await nextTick()

    expect(mounted.host.textContent).toContain('Current FFmpeg')
    expect(mounted.host.textContent).not.toContain('Stale ImageMagick')
  })

  it.each([
    ['shadowed', 'loaded'],
    ['disabled', 'loaded'],
    ['hidden', 'loaded'],
    ['active', 'not_discovered'],
  ] as const)('hides dependency mutations for a %s/%s lifecycle candidate', async (
    selectionState,
    loadState,
  ) => {
    const mounted = mountDialog({
      name: 'shared',
      active: false,
      status: 'needs_setup',
      missing_bins: ['ffmpeg'],
      install: [{ id: 'ffmpeg', kind: 'brew', label: 'Install candidate FFmpeg', bins: ['ffmpeg'] }],
      lifecycle: {
        install_state: 'tracked',
        load_state: loadState,
        selection_state: selectionState,
        compatibility_state: 'instruction_only',
        readiness_state: 'needs_setup',
      },
    })
    await nextTick()

    expect(mounted.dialog.textContent).not.toContain('Install candidate FFmpeg')
    expect(mounted.dialog.querySelector('.sk-detail__install-row button')).toBeNull()
    expect(mounted.installDeps).not.toHaveBeenCalled()
  })

  it('updates its accessible name for the selected skill or proposal', async () => {
    const mounted = mountDialog({ name: 'alpha' })
    await nextTick()
    expect(mounted.dialog.getAttribute('aria-label')).toBe('Skill details: alpha')

    mounted.skill.value = null
    mounted.proposal.value = { proposal_id: 'proposal-7' }
    await nextTick()

    expect(mounted.dialog.getAttribute('aria-label')).toBe('Proposal details: proposal-7')
  })

  it('routes toolchain setup through the MetaSkill flow while preserving other installers', async () => {
    const mounted = mountDialog({
      name: 'meta-paper-write',
      kind: 'meta',
      status: 'needs_setup',
      missing_bins: ['xelatex', 'bibtex'],
      install: [
        {
          id: 'paper-tex',
          kind: 'toolchain',
          label: 'Install managed TeX toolchain',
          bins: ['xelatex', 'bibtex'],
        },
        { id: 'paper-helper', kind: 'uv', label: 'Install paper helper', bins: ['bibtex'] },
      ],
    })
    await nextTick()

    expect(mounted.dialog.textContent).toContain(
      'Managed toolchains can only be installed through that setup flow.',
    )
    const installRows = Array.from(mounted.dialog.querySelectorAll<HTMLElement>('.sk-detail__install-row'))
    const toolchainRow = installRows.find((row) => row.textContent?.includes('managed TeX toolchain'))
    expect(toolchainRow?.querySelector('button')).toBeNull()

    const helperRow = installRows.find((row) => row.textContent?.includes('paper helper'))
    const helperButton = helperRow?.querySelector<HTMLButtonElement>('button')
    expect(helperButton).toBeDefined()
    expect(helperButton?.textContent).toContain('Install via uv')
    helperButton?.click()
    expect(mounted.installDeps).toHaveBeenCalledOnce()
    expect(mounted.installDeps).toHaveBeenCalledWith('meta-paper-write', 'paper-helper')
  })

  it('keeps ordinary managed toolchain installs actionable', async () => {
    const mounted = mountDialog({
      name: 'short-drama-delivery-audit',
      status: 'needs_setup',
      missing_bins: ['ffmpeg'],
      install: [{
        id: 'managed-media',
        kind: 'toolchain',
        label: 'Install managed media toolchain',
        bins: ['ffmpeg'],
      }],
    })
    await nextTick()

    expect(mounted.dialog.textContent).not.toContain(
      'Managed toolchains can only be installed through that setup flow.',
    )
    const button = mounted.dialog.querySelector<HTMLButtonElement>('.sk-detail__install-row button')
    expect(button).toBeDefined()
    expect(button?.textContent).toContain('Install via toolchain')

    button?.click()
    expect(mounted.installDeps).toHaveBeenCalledOnce()
    expect(mounted.installDeps).toHaveBeenCalledWith(
      'short-drama-delivery-audit',
      'managed-media',
    )
  })

  it('shows provider-backed local readiness as a launch-time provider check', async () => {
    const mounted = mountDialog({
      name: 'meta-short-drama',
      status: 'ready',
      status_detail: 'Ready — 0/0 dependencies satisfied',
      provider_check_at_launch: true,
    })
    await nextTick()

    const statusChip = Array.from(mounted.dialog.querySelectorAll<HTMLElement>('.sk-chip'))
      .find((chip) => chip.textContent?.includes('Provider will be checked at launch'))
    expect(statusChip).toBeDefined()
    expect(statusChip?.classList.contains('sk-chip--unverified')).toBe(true)
    expect(statusChip?.classList.contains('sk-chip--ok')).toBe(false)
  })
})
