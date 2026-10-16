import { describe, expect, it } from 'vitest'

import appSource from './App.vue?raw'

describe('App topbar popover contract', () => {
  it('retains popover arbitration while preferences are reached through Settings', () => {
    expect(appSource).toContain(
      'const topbarPopoverCoordinationEnabled = ref(true)',
    )
    expect(appSource).toMatch(/provideChatTopbarPopoverCoordinator\(\s+topbarPopoverCoordinationEnabled,\s+\)/)
    expect(appSource).not.toContain('data-chat-topbar-popover="theme"')
    expect(appSource).not.toContain('<LanguageSwitcher')
  })

  it('lets an open dialog layer own Escape before the mobile drawer fallback', () => {
    const keydownStart = appSource.indexOf('function handleKeydown')
    const keydownEnd = appSource.indexOf('function errorMessage', keydownStart)
    const keydown = appSource.slice(keydownStart, keydownEnd)
    const layerGuard = keydown.indexOf("e.key === 'Escape' && hasOpenDialogLayer()")
    const drawerFallback = keydown.indexOf("e.key === 'Escape' && effectiveSidebarOpen.value")

    expect(layerGuard).toBeGreaterThan(-1)
    expect(drawerFallback).toBeGreaterThan(layerGuard)
  })
})
