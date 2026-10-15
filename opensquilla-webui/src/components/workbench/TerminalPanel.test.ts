// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import en from '@/locales/en.json'
import { TERMINAL_ACCESS_KEY, type TerminalAccess, type TerminalExit, type TerminalOutput } from '@/modules/terminal'
import TerminalPanel from './TerminalPanel.vue'

vi.mock('@xterm/xterm', () => {
  class Terminal {
    cols = 80
    rows = 24
    writes: string[] = []
    private dataHandler: ((value: string) => void) | null = null
    open() {}
    loadAddon() {}
    focus() {}
    write(value: string) { this.writes.push(value) }
    onData(handler: (value: string) => void) { this.dataHandler = handler; return { dispose: () => { this.dataHandler = null } } }
    onResize() { return { dispose: () => undefined } }
    dispose() {}
    emitData(value: string) { this.dataHandler?.(value) }
  }
  return { Terminal }
})
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }))

const cleanups: Array<() => void> = []
afterEach(() => { for (const cleanup of cleanups.splice(0)) cleanup(); vi.restoreAllMocks() })

function mountTerminal(access: TerminalAccess) {
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp({ render: () => h(TerminalPanel, { sessionKey: 'session-a' }) })
  app.provide(TERMINAL_ACCESS_KEY, access)
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount(element)
  const unmount = () => { app.unmount(); element.remove() }
  cleanups.push(unmount)
  return { element, unmount }
}

describe('TerminalPanel', () => {
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
