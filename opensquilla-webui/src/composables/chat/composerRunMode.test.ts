import { describe, expect, it, vi } from 'vitest'

import {
  completeComposerSafeSetup,
  composerRunModeSelectionAction,
  effectiveComposerRunMode,
} from './composerRunMode'

describe('effectiveComposerRunMode', () => {
  it.each(['not_setup', 'setting_up', 'failed', 'unavailable'] as const)(
    'soft-lands a stale Safe preference in Full Access while setup is %s',
    (state) => {
      expect(effectiveComposerRunMode(
        'safe',
        { state, platform: 'win32', message: '', requiresAdmin: true },
        null,
      )).toBe('full')
    },
  )

  it('keeps Safe when setup is ready', () => {
    expect(effectiveComposerRunMode(
      'safe',
      { state: 'ready', platform: 'win32', message: '', requiresAdmin: false },
      null,
    )).toBe('safe')
  })

  it('does not invent failure while setup status is unknown', () => {
    expect(effectiveComposerRunMode('safe', null, null)).toBe('safe')
  })

  it('shows Full Access until the initial setup check resolves', () => {
    expect(effectiveComposerRunMode('safe', null, null, false)).toBe('full')
  })

  it('preserves an active task lock even if setup status changes', () => {
    expect(effectiveComposerRunMode(
      'full',
      { state: 'not_setup', platform: 'win32', message: '', requiresAdmin: true },
      'safe',
    )).toBe('safe')
  })

  it('routes a repairable Safe selection into setup instead of persistence', () => {
    const status = { state: 'not_setup', platform: 'win32', message: '', requiresAdmin: true } as const

    expect(composerRunModeSelectionAction('safe', status, true)).toBe('setup')
    expect(composerRunModeSelectionAction('safe', status, false)).toBe('ignore')
    expect(composerRunModeSelectionAction('full', status, true)).toBe('persist')
    expect(composerRunModeSelectionAction('safe', { ...status, state: 'ready' }, false)).toBe('persist')
  })

  it('ignores Safe selection until the initial setup check resolves', () => {
    expect(composerRunModeSelectionAction('safe', null, false, false)).toBe('ignore')
    expect(composerRunModeSelectionAction('full', null, false, false)).toBe('persist')
  })

  it('persists Safe only after setup succeeds', async () => {
    const persist = vi.fn().mockResolvedValue(undefined)

    await expect(completeComposerSafeSetup(async () => false, persist)).resolves.toBe(false)
    expect(persist).not.toHaveBeenCalled()

    await expect(completeComposerSafeSetup(async () => true, persist)).resolves.toBe(true)
    expect(persist).toHaveBeenCalledOnce()
    expect(persist).toHaveBeenCalledWith('safe')
  })
})
