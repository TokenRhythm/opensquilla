// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import ConfirmModal from '@/components/ConfirmModal.vue'
import { useConfirm } from '@/composables/useConfirm'
import { SetupWorkflowError } from '@/modules/setupWorkflow'
import { submitPrimaryProviderTransition } from './primaryProviderTransition'

const conflict = (allowedRouterActions = ['use_recommended', 'enable_cross_provider', 'disable']) => (
  new SetupWorkflowError('conflict', 'opaque server message', 'router-provider-conflict', undefined, {
    reason: 'router_provider_conflict', providerId: 'tokenrhythm',
    conflictProviders: ['openrouter'], allowedRouterActions,
  })
)

afterEach(() => {
  useConfirm().resolveConfirm(false)
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('shared primary provider transition', () => {
  it('submits a clean request once without opening a dialog', async () => {
    const submit = vi.fn(async () => ({ changed: true }))
    await expect(submitPrimaryProviderTransition({ providerId: 'tokenrhythm' }, submit))
      .resolves.toEqual({ result: { changed: true } })
    expect(submit).toHaveBeenCalledTimes(1)
    expect(useConfirm().confirmState.value).toBeNull()
  })

  it.each(['primary', 'secondary'] as const)('retries the frozen credential snapshot with the explicit %s choice', async choice => {
    const command = { providerId: 'tokenrhythm', apiKey: 'synthetic-original', tiers: { c0: 'original' } }
    const submit = vi.fn().mockRejectedValueOnce(conflict()).mockResolvedValueOnce({ changed: true })
    const pending = submitPrimaryProviderTransition(command, submit)
    await vi.waitFor(() => expect(useConfirm().confirmState.value).not.toBeNull())
    command.apiKey = 'synthetic-later-draft'
    command.tiers.c0 = 'later'
    useConfirm().resolveConfirmChoice(choice)
    await pending
    expect(submit).toHaveBeenCalledTimes(2)
    expect(submit.mock.calls[1]?.[0]).toEqual({
      providerId: 'tokenrhythm', apiKey: 'synthetic-original', tiers: { c0: 'original' },
      routerAction: choice === 'primary' ? 'use_recommended' : 'disable',
    })
  })

  it('shows only backend-allowed actions and never offers cross-provider routing', async () => {
    const submit = vi.fn().mockRejectedValueOnce(conflict(['disable', 'enable_cross_provider']))
    const pending = submitPrimaryProviderTransition({ providerId: 'tokenrhythm' }, submit)
    await vi.waitFor(() => expect(useConfirm().confirmState.value).not.toBeNull())
    expect(useConfirm().confirmState.value?.primaryLabel).toBe('Keep routes and turn Router off')
    expect(useConfirm().confirmState.value?.secondaryLabel).toBeUndefined()
    useConfirm().resolveConfirm(false)
    await expect(pending).resolves.toBeNull()
    expect(submit).toHaveBeenCalledTimes(1)
  })

  it('focuses Cancel and supports keyboard cancellation with long Chinese text', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ConfirmModal)
    app.use(i18n)
    app.mount(host)
    try {
      const submit = vi.fn().mockRejectedValueOnce(conflict())
      const pending = submitPrimaryProviderTransition({ providerId: 'tokenrhythm' }, submit)
      await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).not.toBeNull())
      await nextTick()
      await vi.waitFor(() => expect(document.activeElement?.textContent).toBe('取消'))
      expect(document.querySelector('[role="dialog"]')?.textContent).toContain('替换已保存和未保存的 Router 分层编辑')
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await expect(pending).resolves.toBeNull()
      expect(submit).toHaveBeenCalledTimes(1)
    } finally { app.unmount() }
  })

  it('does not infer conflicts from text or retry an uncertain result', async () => {
    const error = new SetupWorkflowError('unavailable', 'router_provider_conflict: timed out')
    const submit = vi.fn().mockRejectedValue(error)
    await expect(submitPrimaryProviderTransition({ providerId: 'tokenrhythm' }, submit)).rejects.toBe(error)
    expect(submit).toHaveBeenCalledTimes(1)
    expect(useConfirm().confirmState.value).toBeNull()
  })
})
