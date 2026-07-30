// @vitest-environment happy-dom

import { createApp, h, nextTick, ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SkillDetailDialog from './SkillDetailDialog.vue'
import type { Skill } from '@/types/skills'

const apps: ReturnType<typeof createApp>[] = []

beforeEach(() => {
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

function mountDialog(initial: Skill | null) {
  const skill = ref<Skill | null>(initial)
  const close = vi.fn(() => {
    skill.value = null
  })
  const install = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    setup: () => () => h(SkillDetailDialog, {
      skill: skill.value,
      proposal: null,
      loadingContent: false,
      contentError: '',
      installFeedback: '',
      installingDepsId: null,
      uninstallingName: null,
      onClose: close,
      onInstallDeps: install,
    }),
  })
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    missingWarn: false,
    fallbackWarn: false,
    messages: { en: {} },
  }))
  app.mount(host)
  apps.push(app)
  return { skill, close, install, host, dialog: host.querySelector('dialog')! }
}

describe('SkillDetailDialog behavior contract', () => {
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
})
