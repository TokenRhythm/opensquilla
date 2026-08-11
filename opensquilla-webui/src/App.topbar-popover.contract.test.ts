import { describe, expect, it } from 'vitest'

import appSource from './App.vue?raw'

describe('App chat topbar popover contract', () => {
  it('provides chat-only arbitration and registers the App-owned theme menu', () => {
    expect(appSource).toContain(
      'provideChatTopbarPopoverCoordinator(isChatRoute)',
    )
    expect(appSource).toContain(
      "'theme',\n  themeMenuOpen,\n  chatTopbarPopoverCoordinator,",
    )
    expect(appSource).toContain('data-chat-topbar-popover="theme"')
  })

  it('lets an open dialog layer own Escape before the mobile drawer fallback', () => {
    const keydownStart = appSource.indexOf('function handleKeydown')
    const keydownEnd = appSource.indexOf('function errorMessage', keydownStart)
    const keydown = appSource.slice(keydownStart, keydownEnd)
    const layerGuard = keydown.indexOf("e.key === 'Escape' && hasOpenDialogLayer()")
    const drawerFallback = keydown.indexOf("e.key === 'Escape' && appStore.sidebarOpen")

    expect(layerGuard).toBeGreaterThan(-1)
    expect(drawerFallback).toBeGreaterThan(layerGuard)
  })
})
