// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { ScriptTarget, transpileModule } from '@typescript/typescript6'
import appSource from './App.vue?raw'

// Exercise App's actual preference/presentation handlers without booting its
// unrelated Gateway services. Responsive host E2E covers the rendered shell.
function sourceBetween(startMarker: string, endMarker: string): string {
  const start = appSource.indexOf(startMarker)
  const end = appSource.indexOf(endMarker, start)
  if (start < 0 || end < 0) throw new Error(`Missing App sidebar boundary: ${startMarker}`)
  return appSource.slice(start, end)
}

const handlers = transpileModule([
  'const sidebarDrawerOpen = ref(false)',
  sourceBetween('const effectiveSidebarOpen = computed(', 'const workspaceAvailableWidth ='),
  sourceBetween('type SidebarToggleSource =', 'function handleSidebarResizeStart()'),
  sourceBetween('watch(sidebarLayoutMode,', 'watch(sidebarDynamicMaximum,'),
].join('\n'), { compilerOptions: { target: ScriptTarget.ES2022 } }).outputText

const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
})

function setup(preference = true, initialMode = 'resizable') {
  const appStore = reactive({ sidebarOpen: preference, setSidebarOpen: vi.fn((open: boolean) => { appStore.sidebarOpen = open }) })
  const sidebarLayoutMode = ref(initialMode)
  const isSidebarDrawer = computed(() => sidebarLayoutMode.value === 'drawer')
  const sidebarYieldsToWorkbench = ref(false)
  const sidebarRevealOverride = ref(false)
  const dependencies = {
    computed, nextTick, ref, watch, appStore, sidebarLayoutMode, isSidebarDrawer,
    sidebarYieldsToWorkbench, sidebarRevealOverride,
    sidebarResizerRef: ref({ cancel: vi.fn() }),
    sidebarResizeActive: ref(false),
    sidebarEffectiveWidth: ref(280),
    sidebarRef: ref(null),
    sidebarDockToggleRef: ref({ focus: vi.fn() }),
    topbarSidebarToggleRef: ref({ focus: vi.fn() }),
    setSidebarCssWidth: vi.fn(),
  }
  const scope = effectScope()
  scopes.push(scope)
  const controls = scope.run(() => new Function(...Object.keys(dependencies), `${handlers}; return {
    effectiveSidebarOpen, toggleDock, openSidebarDrawer, closeSidebarDrawer, handleNavClick
  }`)(...Object.values(dependencies)))!
  return { ...dependencies, ...controls }
}

describe('App sidebar presentation and desktop preference', () => {
  it.each([true, false])('restores desktop preference %s after passing through compact and phone widths', async preference => {
    const app = setup(preference)
    for (const mode of ['compact', 'drawer', 'compact', 'resizable']) {
      app.sidebarLayoutMode.value = mode
      await nextTick()
      expect(app.effectiveSidebarOpen.value).toBe(mode === 'drawer' ? false : preference)
      expect(app.appStore.sidebarOpen).toBe(preference)
    }
    expect(app.appStore.setSidebarOpen).not.toHaveBeenCalled()
  })

  it('starts with the phone drawer closed even when the desktop dock is preferred open', () => {
    const app = setup(true, 'drawer')
    expect(app.effectiveSidebarOpen.value).toBe(false)
    expect(app.appStore.sidebarOpen).toBe(true)
    expect(app.appStore.setSidebarOpen).not.toHaveBeenCalled()
  })

  it.each([true, false])('drawer open, dismiss, and navigation do not rewrite desktop preference %s', preference => {
    const app = setup(preference, 'drawer')
    app.openSidebarDrawer()
    expect(app.effectiveSidebarOpen.value).toBe(true)
    app.closeSidebarDrawer()
    expect(app.effectiveSidebarOpen.value).toBe(false)
    app.toggleDock('shortcut')
    expect(app.effectiveSidebarOpen.value).toBe(true)
    app.handleNavClick()
    expect(app.effectiveSidebarOpen.value).toBe(false)
    expect(app.appStore.sidebarOpen).toBe(preference)
    expect(app.appStore.setSidebarOpen).not.toHaveBeenCalled()
  })

  it('clears an open drawer when returning wide and does not reopen it on a later shrink', async () => {
    const app = setup(true, 'drawer')
    app.openSidebarDrawer()
    app.sidebarLayoutMode.value = 'resizable'
    await nextTick()
    expect(app.effectiveSidebarOpen.value).toBe(true)
    app.sidebarLayoutMode.value = 'drawer'
    await nextTick()
    expect(app.effectiveSidebarOpen.value).toBe(false)
    expect(app.appStore.sidebarOpen).toBe(true)
  })

  it('persists explicit desktop toggles while temporary yielding only changes presentation', () => {
    const app = setup()
    app.sidebarYieldsToWorkbench.value = true
    expect(app.effectiveSidebarOpen.value).toBe(false)
    expect(app.appStore.sidebarOpen).toBe(true)
    app.toggleDock('topbar-button')
    expect(app.effectiveSidebarOpen.value).toBe(true)
    expect(app.sidebarRevealOverride.value).toBe(true)
    app.toggleDock('sidebar-button')
    expect(app.effectiveSidebarOpen.value).toBe(false)
    expect(app.appStore.sidebarOpen).toBe(false)
    expect(app.appStore.setSidebarOpen.mock.calls).toEqual([[true], [false]])
  })
})
