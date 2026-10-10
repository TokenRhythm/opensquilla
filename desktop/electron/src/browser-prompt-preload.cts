import { contextBridge, ipcRenderer } from 'electron'

const CHANNEL = 'opensquilla-browser-prompt'
const MAX_MESSAGE = 2048
const MAX_VALUE = 4096

contextBridge.exposeInMainWorld('__opensquillaBrowserPrompt', {
  request(message: string, defaultValue: string): string | null {
    if (typeof message !== 'string' || typeof defaultValue !== 'string') return null
    const result: unknown = ipcRenderer.sendSync(CHANNEL, {
      message: message.slice(0, MAX_MESSAGE),
      defaultValue: defaultValue.slice(0, MAX_VALUE),
    })
    return result === null || typeof result === 'string' ? result : null
  },
})

contextBridge.executeInMainWorld({
  func: () => {
    const bridge = (window as unknown as Window & {
      __opensquillaBrowserPrompt: { request(message: string, defaultValue: string): string | null }
    }).__opensquillaBrowserPrompt
    window.prompt = (message?: string, defaultValue?: string): string | null =>
      bridge.request(message === undefined ? '' : String(message),
        defaultValue === undefined ? '' : String(defaultValue))
  },
})
