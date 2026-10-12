import { describe, expect, it } from 'vitest'
import appSource from './App.vue?raw'

describe('App status and navigation ownership', () => {
  it('owns one update entry in the settings footer, outside the route header', () => {
    const footer = appSource.slice(appSource.indexOf('<div class="sidebar-foot">'), appSource.indexOf('<SidebarResizer'))
    expect(footer).toContain('@click="openSettings"')
    expect(footer).toContain('<SidebarDesktopUpdate @open="openDesktopUpdateSettings" />')
    expect(appSource.match(/<SidebarDesktopUpdate\b/g)).toHaveLength(1)
    expect(appSource).not.toContain('<DesktopUpdateIndicator')
    expect(appSource).toContain("router.push('/settings/gateway#updates')")
  })
  it('keeps the updater discoverable through the collapsed sidebar opener', () => {
    const opener = appSource.slice(appSource.indexOf('ref="topbarSidebarToggleRef"'), appSource.indexOf('<!-- App owns the route header'))
    expect(opener).toContain(':aria-label="sidebarExpandLabel"')
    expect(opener).toContain('v-if="desktopUpdate.visible.value"')
    expect(opener).toContain('data-testid="sidebar-update-dot"')
  })
  it('uses the same independent connection and approval controls across routes', () => {
    const status = appSource.match(/<ChatSystemStatus\b[\s\S]*?\/>/)?.[0]
    expect(appSource.match(/<ChatSystemStatus\b/g)).toHaveLength(1)
    expect(status).not.toContain('v-if="isChatRoute"')
    expect(status).toContain('@open-connection="openConnectionSettings"')
    expect(status).toContain('@open-approval="openBlockedApprovalSession"')
    expect(appSource).toContain('<WorkbenchToggle')
    expect(appSource).toContain('@open-deliverables="chatRouteHeader.invoke(\'openDeliverables\')"')
  })
})
