import { effectScope, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useLocalPathPicker } from './useLocalPathPicker'

const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()) })
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function fixture() {
  const available = ref(true)
  const session = ref('') // First message has no durable session.
  const intent = ref('new')
  const connection = ref('connection')
  const workspace = ref('workspace')
  const agent = ref('agent')
  const runMode = ref('safe')
  const revision = ref(0)
  const sendPending = ref(false)
  const historyState = ref({ loading: false, sessionMissing: false })
  const activeWorkspace = ref({ id: 'workspace', name: 'Workspace' })
  const text = ref('Please inspect')
  const result = deferred<string[]>()
  const choosePaths = vi.fn(() => result.promise)
  const getBinding = vi.fn(async () => ({ instanceId: 'instance', profileFingerprint: 'profile' }))
  const append = vi.fn((value: string) => { text.value += `\n${value}`; revision.value++ })
  const onError = vi.fn()
  const scope = effectScope()
  scopes.push(scope)
  const picker = scope.run(() => useLocalPathPicker({
    available: () => available.value,
    scope: () => [session.value, intent.value, connection.value, workspace.value, agent.value,
      runMode.value, revision.value, sendPending.value, historyState.value.sessionMissing,
      activeWorkspace.value.id],
    text: () => text.value, getBinding, choosePaths, append, onError,
  }))!
  return { picker, result, choosePaths, getBinding, append, onError, available, session, intent,
    connection, workspace, agent, runMode, revision, sendPending, historyState, activeWorkspace, text, scope }
}

describe('explicit local path picker', () => {
  it.each(['', 'existing-session'])('inserts local path references without attachment/session calls: %s', async session => {
    const f = fixture()
    f.session.value = session
    const pending = f.picker.choose()
    const paths = ['C:\\项目 外\\文 件 #` x.txt', "C:\\other\\single'quote.txt"]
    f.result.resolve(paths)
    await pending
    expect(f.choosePaths).toHaveBeenCalledWith({ gatewayInstanceId: 'instance' })
    expect(f.append).toHaveBeenCalledWith(paths.join('\n'))
    expect(f.picker.busy.value).toBe(false)
    expect(f.onError).not.toHaveBeenCalled()
  })

  it('browser/unowned/remote availability never calls native picker', async () => {
    const f = fixture(); f.available.value = false
    await f.picker.choose()
    expect(f.getBinding).not.toHaveBeenCalled()
    expect(f.choosePaths).not.toHaveBeenCalled()
  })

  it('keeps a pending selection when parent objects refresh without a scope value change', async () => {
    const f = fixture()
    const pending = f.picker.choose()
    await Promise.resolve()
    expect(f.choosePaths).toHaveBeenCalledOnce()

    // Restoring focus after the native dialog can refresh history/workspace data.
    f.historyState.value = { loading: true, sessionMissing: false }
    f.activeWorkspace.value = { id: 'workspace', name: 'Updated name' }
    expect(f.picker.busy.value).toBe(true)

    f.result.resolve(['C:\\file.txt'])
    await pending
    expect(f.append).toHaveBeenCalledExactlyOnceWith('C:\\file.txt')
    expect(f.onError).not.toHaveBeenCalled()
    expect(f.picker.busy.value).toBe(false)
  })

  it('discards a selection when a scope value within a replaced parent changes and changes back', async () => {
    const f = fixture()
    const pending = f.picker.choose()
    await Promise.resolve()

    f.historyState.value = { loading: false, sessionMissing: true }
    f.historyState.value = { loading: false, sessionMissing: false }
    f.result.resolve(['C:\\file.txt'])
    await pending

    expect(f.append).not.toHaveBeenCalled()
    expect(f.onError).not.toHaveBeenCalled()
    expect(f.picker.busy.value).toBe(false)
  })

  it.each(['session', 'intent', 'connection', 'workspace', 'agent', 'runMode', 'revision', 'sendPending', 'available'] as const)(
    'discards a late result after %s changes, even if it changes back', async key => {
      const f = fixture()
      const pending = f.picker.choose()
      await Promise.resolve()
      expect(f.choosePaths).toHaveBeenCalledOnce()
      const target = f[key] as { value: unknown }
      const old = target.value
      target.value = typeof old === 'boolean' ? !old : typeof old === 'number' ? 1 : 'changed'
      target.value = old
      f.result.resolve(['C:\\file.txt'])
      await pending
      expect(f.append).not.toHaveBeenCalled()
      expect(f.onError).not.toHaveBeenCalled()
      expect(f.picker.busy.value).toBe(false)
    },
  )

  it('discards after explicit cancellation or component disposal', async () => {
    for (const dispose of [false, true]) {
      const f = fixture(); const pending = f.picker.choose()
      await Promise.resolve()
      if (dispose) f.scope.stop(); else f.picker.cancel()
      f.result.resolve(['C:\\file.txt']); await pending
      expect(f.append).not.toHaveBeenCalled()
    }
  })

  it('rechecks main binding after dialog even before renderer connection event arrives', async () => {
    const f = fixture()
    f.getBinding.mockResolvedValueOnce({ instanceId: 'old', profileFingerprint: 'profile' })
    const pending = f.picker.choose()
    f.result.resolve(['C:\\file.txt']); await pending
    expect(f.append).not.toHaveBeenCalled()
  })

  it('cancellation and malformed paths do not insert text', async () => {
    for (const paths of [[], ['C:\\bad\nname']]) {
      const f = fixture(); const pending = f.picker.choose()
      f.result.resolve(paths); await pending
      expect(f.append).not.toHaveBeenCalled()
      expect(f.onError).toHaveBeenCalledTimes(paths.length ? 1 : 0)
    }
  })

  it('rejects the entire over-limit insertion, without truncation, and accepts the exact boundary', async () => {
    for (const extra of [0, 1]) {
      const f = fixture(); const path = 'C:\\file.txt'
      f.text.value = 'x'.repeat(100_000 - path.length - 1 + extra)
      const pending = f.picker.choose(); f.result.resolve([path]); await pending
      expect(f.append).toHaveBeenCalledTimes(extra ? 0 : 1)
      if (extra) expect(f.onError).toHaveBeenCalledWith('too-long')
    }
  })

  it('does not open two dialogs for repeated clicks', async () => {
    const f = fixture(); const pending = f.picker.choose()
    await f.picker.choose(); f.result.resolve([]); await pending
    expect(f.choosePaths).toHaveBeenCalledOnce()
  })

  it('turns native non-image drops into local path references and leaves images for byte attachments', async () => {
    const f = fixture()
    const resolveNativeFilePath = vi.fn(async (file: File) => file.name === 'large.bin' ? 'C:\\large.bin' : null)
    const picker = useLocalPathPicker({
      available: () => f.available.value,
      nativeDropAvailable: () => f.available.value,
      scope: () => [f.session.value, f.revision.value],
      getBinding: f.getBinding,
      choosePaths: f.choosePaths,
      resolveNativeFilePath,
      text: () => f.text.value,
      append: f.append,
      onError: f.onError,
    })
    const image = new File(['image'], 'photo.jpg', { type: 'image/jpeg' })
    const binary = new File(['large'], 'large.bin', { type: 'application/octet-stream' })
    await expect(picker.appendNativeDrop([image, binary], file => file.type.startsWith('image/')))
      .resolves.toEqual([image])
    expect(resolveNativeFilePath).toHaveBeenCalledTimes(1)
    expect(f.append).toHaveBeenCalledWith('C:\\large.bin')
  })

  it('drops an over-limit native batch without truncating or attaching its paths', async () => {
    const f = fixture()
    const picker = useLocalPathPicker({
      available: () => f.available.value,
      nativeDropAvailable: () => f.available.value,
      scope: () => [f.revision.value],
      getBinding: f.getBinding,
      choosePaths: f.choosePaths,
      resolveNativeFilePath: vi.fn(async (file: File) => `C:\\${file.name}`),
      text: () => f.text.value,
      append: f.append,
      onError: f.onError,
    })
    const files = Array.from({ length: 11 }, (_, i) => new File(['x'], `file-${i}.bin`))
    await expect(picker.appendNativeDrop(files, () => false)).resolves.toEqual([])
    expect(f.append).not.toHaveBeenCalled()
    expect(f.onError).toHaveBeenCalledWith('too-many')
  })

  it('discards a native drop when the chat scope changes while resolving', async () => {
    const f = fixture()
    const result = deferred<string | null>()
    const picker = useLocalPathPicker({
      available: () => f.available.value,
      nativeDropAvailable: () => f.available.value,
      scope: () => [f.session.value],
      getBinding: f.getBinding,
      choosePaths: f.choosePaths,
      resolveNativeFilePath: vi.fn(() => result.promise),
      text: () => f.text.value,
      append: f.append,
      onError: f.onError,
    })
    const pending = picker.appendNativeDrop([new File(['x'], 'large.bin')], () => false)
    await Promise.resolve()
    f.session.value = 'new-session'
    result.resolve('C:\\large.bin')
    await expect(pending).resolves.toBeNull()
    expect(f.append).not.toHaveBeenCalled()
  })

  it.each([false, true])('only invalidates a resolving native drop for a changed scope value: %s', async changed => {
    const f = fixture()
    const result = deferred<string | null>()
    const picker = f.scope.run(() => useLocalPathPicker({
      available: () => f.available.value,
      scope: () => [f.historyState.value.sessionMissing, f.activeWorkspace.value.id],
      getBinding: f.getBinding,
      choosePaths: f.choosePaths,
      resolveNativeFilePath: vi.fn(() => result.promise),
      text: () => f.text.value,
      append: f.append,
      onError: f.onError,
    }))!
    const image = new File(['image'], 'photo.jpg', { type: 'image/jpeg' })
    const pending = picker.appendNativeDrop([image, new File(['x'], 'large.bin')], file => file.type.startsWith('image/'))

    f.historyState.value = { loading: true, sessionMissing: changed }
    f.historyState.value = { loading: false, sessionMissing: false }
    f.activeWorkspace.value = { id: 'workspace', name: 'Updated name' }
    expect(picker.busy.value).toBe(!changed)
    result.resolve('C:\\large.bin')

    await expect(pending).resolves.toEqual(changed ? null : [image])
    expect(f.append).toHaveBeenCalledTimes(changed ? 0 : 1)
    if (!changed) expect(f.append).toHaveBeenCalledWith('C:\\large.bin')
    expect(f.onError).not.toHaveBeenCalled()
    expect(picker.busy.value).toBe(false)
  })
})
