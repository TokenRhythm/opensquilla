// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import type { ITerminalOptions } from '@xterm/xterm'
import en from '@/locales/en.json'
import { TERMINAL_ACCESS_KEY, type TerminalAccess, type TerminalExit, type TerminalHandle, type TerminalOutput, type TerminalTarget } from '@/modules/terminal'
import TerminalPanel from './TerminalPanel.vue'
import { createTerminalWorkbenchDefinition } from './terminalWorkbenchProvider'
import { createTerminalWorkbenchItem } from '@/workbench/terminalItems'

const terminalInstances = vi.hoisted(() => [] as Array<{ options: ITerminalOptions; writes: string[]; disposed: boolean; focus: ReturnType<typeof vi.fn>; blur: ReturnType<typeof vi.fn>; callbacks: Array<() => void> }>)

vi.mock('@xterm/xterm', () => {
  class Terminal {
    cols = 80
    rows = 24
    writes: string[] = []
    disposed = false
    callbacks: Array<() => void> = []
    constructor(public options: ITerminalOptions) { terminalInstances.push(this) }
    private dataHandler: ((value: string) => void) | null = null
    open() {}
    loadAddon() {}
    focus = vi.fn()
    blur = vi.fn()
    write(value: string, callback?: () => void) { this.writes.push(value); if (callback) this.callbacks.push(callback) }
    onData(handler: (value: string) => void) { this.dataHandler = handler; return { dispose: () => { this.dataHandler = null } } }
    onResize() { return { dispose: () => undefined } }
    dispose() { this.disposed = true }
    emitData(value: string) { this.dataHandler?.(value) }
  }
  return { Terminal }
})
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }))

const cleanups: Array<() => void> = []
afterEach(() => { for (const cleanup of cleanups.splice(0)) cleanup(); terminalInstances.length = 0; vi.restoreAllMocks() })

function mountTerminal(access: TerminalAccess, props: { sessionKey?: string; target?: TerminalTarget; visible?: boolean } = { sessionKey: 'session-a' }) {
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp({ render: () => h(TerminalPanel, props) })
  app.provide(TERMINAL_ACCESS_KEY, access)
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount(element)
  const unmount = () => { app.unmount(); element.remove() }
  cleanups.push(unmount)
  return { element, unmount }
}

describe('TerminalPanel', () => {
  it('keeps the hidden parser and PTY, acknowledges consumption and avoids focus theft on return', async () => {
    let outputHandler!: (event: TerminalOutput) => void
    const access: TerminalAccess = {
      create: vi.fn(async () => ({ terminalId: 'pty-hidden', sessionKey: 'session-a', cols: 80, rows: 24, shell: 'bash', outputFlowControl: 'consumed-v1' as const })),
      consume: vi.fn(async (_id, seq) => ({ terminalId: 'pty-hidden', consumedSeq: seq })),
      write: vi.fn(async () => ({ terminalId: 'pty-hidden', accepted: true, bytes: 1 })),
      resize: vi.fn(async () => ({ terminalId: 'pty-hidden', cols: 80, rows: 24 })),
      close: vi.fn(async () => ({ terminalId: 'pty-hidden', closed: true })),
      onOutput: vi.fn(handler => { outputHandler = handler; return () => undefined }),
      onExit: vi.fn(() => () => undefined),
    }
    const props = reactive({ sessionKey: 'session-a', visible: true })
    mountTerminal(access, props)
    await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); await nextTick()
    const instance = terminalInstances[0]!
    expect(access.create).toHaveBeenCalledWith('session-a', { cols: 100, rows: 30, outputFlowControl: 'consumed-v1' })
    const focusCalls = instance.focus.mock.calls.length
    props.visible = false
    await nextTick()
    expect(instance.options.cursorBlink).toBe(false)
    expect(instance.blur).toHaveBeenCalledOnce()
    outputHandler({ terminalId: 'pty-hidden', seq: 1, data: '\x1b[?1049hfullscreen', encoding: 'utf8' })
    expect(instance.writes).toEqual(['\x1b[?1049hfullscreen'])
    expect(access.consume).not.toHaveBeenCalled()
    instance.callbacks.shift()?.()
    await new Promise(resolve => setTimeout(resolve, 25))
    expect(access.consume).toHaveBeenCalledWith('pty-hidden', 1, expect.any(AbortSignal))
    await new Promise(resolve => setTimeout(resolve, 90))
    expect(access.resize).not.toHaveBeenCalled()
    expect(access.close).not.toHaveBeenCalled()
    props.visible = true
    await nextTick(); await nextTick()
    expect(instance.options.cursorBlink).toBe(true)
    expect(instance.focus).toHaveBeenCalledTimes(focusCalls)
    expect(instance.disposed).toBe(false)
    expect(terminalInstances).toHaveLength(1)
    expect(access.create).toHaveBeenCalledTimes(1)
  })

  it('opens a standalone target and only replays its own early output', async () => {
    const target: TerminalTarget = { kind: 'project', workspaceId: 'project-a' }
    const item = createTerminalWorkbenchItem('draft-a', target)
    let resolveCreate!: (handle: TerminalHandle) => void
    let outputHandler!: (event: TerminalOutput) => void
    const access: TerminalAccess = {
      create: vi.fn(() => new Promise<TerminalHandle>(resolve => { resolveCreate = resolve })),
      write: vi.fn(async () => ({ terminalId: 'pty-target', accepted: true, bytes: 1 })),
      resize: vi.fn(async () => ({ terminalId: 'pty-target', cols: 80, rows: 24 })),
      close: vi.fn(async () => ({ terminalId: 'pty-target', closed: true })),
      onOutput: vi.fn(handler => { outputHandler = handler; return () => undefined }),
      onExit: vi.fn(() => () => undefined),
    }
    const definition = createTerminalWorkbenchDefinition(access, key => key)
    expect(definition.getProps?.(item, { active: true, hostAvailable: true, nativeSurface: false, runtimeState: {} })).toEqual({ target, visible: true })
    expect(definition.getProps?.(item, { active: true, visible: false, hostAvailable: true, nativeSurface: false, runtimeState: {} })).toEqual({ target, visible: false })
    const { element } = mountTerminal(access, { target })
    expect(access.create).toHaveBeenCalledWith(target, { cols: 100, rows: 30 })
    outputHandler({ terminalId: 'other-same-target', target, seq: 1, data: 'wrong shell', encoding: 'utf8' })
    outputHandler({ terminalId: 'other-target', target: { kind: 'default-agent', agentId: 'main' }, seq: 1, data: 'other project', encoding: 'utf8' })
    outputHandler({ terminalId: 'pty-target', target, seq: 1, data: 'my prompt', encoding: 'utf8' })
    expect(terminalInstances[0]!.writes).toEqual([])
    resolveCreate({ terminalId: 'pty-target', target, cwdPath: 'C:\\work\\project-a', cols: 80, rows: 24, shell: 'pwsh' })
    await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); await nextTick()
    expect(terminalInstances[0]!.writes).toEqual(['my prompt'])
    expect(element.querySelector('[role="status"]')?.textContent).toContain('Connected')
    expect(element.querySelector('.terminal-panel__cwd')?.textContent).toBe('project-a')
    expect(element.querySelector('.terminal-panel__cwd')?.getAttribute('title')).toBe('C:\\work\\project-a')
    expect(access.close).not.toHaveBeenCalled()
  })

  it('recolors an existing buffer on theme changes without restarting the shell', async () => {
    const root = document.documentElement
    const originalStyle = root.style.cssText
    const originalTheme = root.getAttribute('data-theme')
    cleanups.push(() => {
      root.style.cssText = originalStyle
      if (originalTheme === null) root.removeAttribute('data-theme')
      else root.setAttribute('data-theme', originalTheme)
    })
    root.style.setProperty('--bg', '#fafafa')
    root.style.setProperty('--text', '#262626')
    root.style.setProperty('--warn', '#8a6410')
    let outputHandler: ((event: TerminalOutput) => void) | null = null
    const access: TerminalAccess = {
      create: vi.fn(async () => ({ terminalId: 'pty-theme', sessionKey: 'session-a', cols: 80, rows: 24, shell: 'bash' })),
      write: vi.fn(async () => ({ terminalId: 'pty-theme', accepted: true, bytes: 1 })),
      resize: vi.fn(async () => ({ terminalId: 'pty-theme', cols: 80, rows: 24 })),
      close: vi.fn(async () => ({ terminalId: 'pty-theme', closed: true })),
      onOutput: vi.fn(handler => { outputHandler = handler; return () => undefined }),
      onExit: vi.fn(() => () => undefined),
    }
    const { element } = mountTerminal(access)
    await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); await nextTick()
    const instance = terminalInstances[0]!
    expect(instance.options.minimumContrastRatio).toBe(4.5)
    expect(instance.options.theme?.background).toBe('#fafafa')
    expect(instance.options.theme?.yellow).toBe('#8a6410')
    ;(outputHandler as ((event: TerminalOutput) => void) | null)?.({ terminalId: 'pty-theme', seq: 7, data: 'kept output', encoding: 'utf8' })

    root.style.setProperty('--bg', '#18181a')
    root.style.setProperty('--text', '#f5f5f7')
    root.style.setProperty('--warn', '#e8b23a')
    root.setAttribute('data-theme', 'dark')
    await new Promise(resolve => setTimeout(resolve, 0)); await nextTick()

    expect(instance.options.theme?.background).toBe('#18181a')
    expect(instance.options.theme?.yellow).toBe('#e8b23a')
    expect(instance.writes).toContain('kept output')
    expect(instance.disposed).toBe(false)
    expect(terminalInstances).toHaveLength(1)
    expect(access.create).toHaveBeenCalledTimes(1)
    expect(access.close).not.toHaveBeenCalled()
    expect(element.querySelector('[role="status"]')?.textContent).not.toContain('#7')
  })

  it('keeps the Gateway failure above the terminal surface and clears it when restart succeeds', async () => {
    const create = vi.fn<TerminalAccess['create']>()
      .mockRejectedValueOnce(Object.assign(new Error('The session workspace is unavailable'), {
        code: 'WORKSPACE_UNAVAILABLE', accepted: false,
      }))
      .mockResolvedValueOnce({ terminalId: 'pty-retry', sessionKey: 'session-a', cols: 80, rows: 24, shell: 'bash' })
    const access: TerminalAccess = {
      create,
      write: vi.fn(async () => ({ terminalId: 'pty-retry', accepted: true, bytes: 1 })),
      resize: vi.fn(async () => ({ terminalId: 'pty-retry', cols: 80, rows: 24 })),
      close: vi.fn(async () => ({ terminalId: 'pty-retry', closed: true })),
      onOutput: vi.fn(() => () => undefined),
      onExit: vi.fn(() => () => undefined),
    }
    const { element } = mountTerminal(access)
    await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); await nextTick()

    const alert = element.querySelector('[role="alert"]')
    expect(alert?.textContent).toBe('The session workspace is unavailable')
    expect(alert?.nextElementSibling?.classList.contains('terminal-panel__surface')).toBe(true)
    expect(element.querySelector('[role="status"]')?.textContent).toContain('Unavailable')
    const restart = element.querySelector<HTMLButtonElement>('.terminal-panel__status button')
    expect(restart?.textContent).toContain('Restart')
    restart?.click()
    await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); await nextTick()

    expect(create).toHaveBeenCalledTimes(2)
    expect(element.querySelector('[role="alert"]')).toBeNull()
    expect(element.querySelector('[role="status"]')?.textContent).toContain('Connected')
    expect(element.querySelector('.terminal-panel__status button')).toBeNull()
  })

  it('creates a session, forwards terminal input and closes on unmount', async () => {
    let outputHandler: ((event: TerminalOutput) => void) | null = null
    let exitHandler: ((event: TerminalExit) => void) | null = null
    const access: TerminalAccess = {
      create: vi.fn(async () => ({ terminalId: 'pty-1', sessionKey: 'session-a', cols: 80, rows: 24, shell: 'bash' })),
      write: vi.fn(async () => ({ terminalId: 'pty-1', accepted: true, bytes: 1 })),
      resize: vi.fn(async () => ({ terminalId: 'pty-1', cols: 80, rows: 24 })),
      close: vi.fn(async () => ({ terminalId: 'pty-1', closed: true })),
      onOutput: vi.fn((handler: (event: TerminalOutput) => void) => { outputHandler = handler; return () => { outputHandler = null } }),
      onExit: vi.fn((handler: (event: TerminalExit) => void) => { exitHandler = handler; return () => { exitHandler = null } }),
    }
    const { element, unmount } = mountTerminal(access)
    await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); await nextTick()
    expect(access.create).toHaveBeenCalledWith('session-a', { cols: 100, rows: 30 })
    ;(outputHandler as ((event: TerminalOutput) => void) | null)?.({ terminalId: 'pty-1', seq: 1, data: 'hello\r\n', encoding: 'utf8' })
    ;(exitHandler as ((event: TerminalExit) => void) | null)?.({ terminalId: 'pty-1', seq: 2, exitCode: 0, signal: null, reason: null })
    expect(element.querySelector('[data-testid="terminal-panel"]')).not.toBeNull()
    expect(access.close).not.toHaveBeenCalled()
    // The provider owns disposal; unmounting the Workbench tab closes the PTY.
    unmount()
    expect(access.close).toHaveBeenCalledWith('pty-1', 'component-unmount')
    expect(access.write).not.toHaveBeenCalled()
  })
})
