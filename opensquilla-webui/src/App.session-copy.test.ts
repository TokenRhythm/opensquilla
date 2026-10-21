import { describe, expect, it, vi } from 'vitest'
import { ScriptTarget, transpileModule } from '@typescript/typescript6'
import appSource from './App.vue?raw'

const start = appSource.indexOf('let sidebarCopySequence =')
const end = appSource.indexOf('// Only the current rename', start)
if (start < 0 || end < 0) throw new Error('App sidebar copy handler boundary changed')
const handler = transpileModule(appSource.slice(start, end), {
  compilerOptions: { target: ScriptTarget.ES2022 },
}).outputText

function setup() {
  const gatewayAccess = { subscriptionEpoch: 1 }
  const platform = {}
  const link = vi.fn().mockImplementation(async (key: string) => `https://qa.example/control/chat?session=${key}`)
  const copy = vi.fn().mockResolvedValue(undefined)
  const toast = vi.fn()
  const run = new Function('gatewayAccess', 'platform', 'currentSessionLink', 'copyTextWithFallback', 'pushToast', 't',
    `${handler}; return onCopySidebarSessionLink`)(gatewayAccess, platform, link, copy, toast, (key: string) => key)
  return { gatewayAccess, platform, link, copy, toast, run }
}

describe('App sidebar session link action', () => {
  it('copies the clicked row without navigating or showing success feedback', async () => {
    const app = setup()
    await app.run('agent:main:webchat:other-row')
    expect(app.link).toHaveBeenCalledExactlyOnceWith('agent:main:webchat:other-row', app.platform)
    expect(app.copy).toHaveBeenCalledExactlyOnceWith('https://qa.example/control/chat?session=agent:main:webchat:other-row')
    expect(app.toast).not.toHaveBeenCalled()
  })

  it('does not copy a result resolved after changing Gateway', async () => {
    const app = setup()
    let resolve!: (link: string) => void
    app.link.mockReturnValueOnce(new Promise<string>(done => { resolve = done }))
    const pending = app.run('old-gateway-session')
    app.gatewayAccess.subscriptionEpoch += 1
    resolve('https://old.example/control/chat')
    await pending
    expect(app.copy).not.toHaveBeenCalled()
    expect(app.toast).not.toHaveBeenCalled()
  })

  it('does not overwrite a newer copied row when an older lookup resolves late', async () => {
    const app = setup()
    let resolve!: (link: string) => void
    app.link.mockReturnValueOnce(new Promise<string>(done => { resolve = done }))
    const pending = app.run('older')
    await app.run('newer')
    resolve('https://old.example/older')
    await pending
    expect(app.copy).toHaveBeenCalledExactlyOnceWith('https://qa.example/control/chat?session=newer')
  })

  it('reports a copy failure rather than a success', async () => {
    const app = setup()
    app.copy.mockRejectedValueOnce(new Error('Clipboard denied'))
    await app.run('target')
    expect(app.toast).toHaveBeenCalledExactlyOnceWith('chat.toast.copyFailed', { tone: 'danger' })
  })

  it('serializes an older pending clipboard fallback before the newest link', async () => {
    const app = setup()
    let finishOlder!: () => void
    const written: string[] = []
    app.copy.mockImplementationOnce((link: string) => new Promise<void>(resolve => {
      finishOlder = () => { written.push(link); resolve() }
    })).mockImplementationOnce(async (link: string) => { written.push(link) })
    const older = app.run('older')
    await vi.waitFor(() => expect(app.copy).toHaveBeenCalledTimes(1))
    const newer = app.run('newer')
    await Promise.resolve()
    expect(app.copy).toHaveBeenCalledTimes(1)
    finishOlder()
    await Promise.all([older, newer])
    expect(written).toEqual([
      'https://qa.example/control/chat?session=older',
      'https://qa.example/control/chat?session=newer',
    ])
    expect(app.toast).not.toHaveBeenCalled()
  })
})
