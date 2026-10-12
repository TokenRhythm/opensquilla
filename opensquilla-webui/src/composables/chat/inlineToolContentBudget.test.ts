// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactContentAccess, type ArtifactWorkbench, type AttachmentTextPreview } from '@/modules/artifactWorkbench'
import { SESSION_INSPECTION_KEY, type ExecutionLogPage, type SessionInspection } from '@/modules/sessionInspection'
import { saveByteStream } from '@/utils/streamDownload'
import { useInlineExecutionLog } from './useInlineExecutionLog'
import { useInlineFileSnapshot } from './useInlineFileSnapshot'

vi.mock('@/utils/streamDownload', () => ({ saveByteStream: vi.fn() }))

const cleanups: Array<() => void> = []
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.restoreAllMocks() })
const fileBody: AttachmentTextPreview = { content: 'all body', totalBytes: 8, truncated: false }
const fullBody = `${'x'.repeat(511)}\n`.repeat(512)
const fullFile: AttachmentTextPreview = { content: fullBody, totalBytes: fullBody.length, truncated: false }
function logPage(content = 'saved', patch: Partial<ExecutionLogPage> = {}): ExecutionLogPage {
  return { handle: 'log', content, offset: 0, nextOffset: null, chars: content.length, complete: true, ...patch }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function mountFile(readAttachmentPreview: NonNullable<ArtifactContentAccess['readAttachmentPreview']>, enabled = true, bytes = 8) {
  const state = reactive({ enabled })
  let result!: ReturnType<typeof useInlineFileSnapshot>
  const app = createApp({ setup() {
    result = useInlineFileSnapshot({ sessionKey: () => 'session', toolId: () => 'file', enabled: () => state.enabled,
      snapshot: () => ({ kind: 'write', path: 'a.txt', content: '', sha256_ref: 'a'.repeat(64), bytes }) })
    return () => h('div')
  } })
  app.provide(ARTIFACT_WORKBENCH_KEY, { content: { readAttachmentPreview } } as ArtifactWorkbench)
  app.mount(document.createElement('div'))
  let mounted = true
  const unmount = () => { if (mounted) { mounted = false; app.unmount() } }
  cleanups.push(unmount)
  return { state, result, unmount }
}
function mountLog(readExecutionLog: SessionInspection['readExecutionLog'], enabled = true) {
  const state = reactive({ enabled })
  let result!: ReturnType<typeof useInlineExecutionLog>
  const app = createApp({ setup() {
    result = useInlineExecutionLog({ sessionKey: () => 'session', toolId: () => 'command', handle: () => 'log', enabled: () => state.enabled })
    return () => h('div')
  } })
  app.provide(SESSION_INSPECTION_KEY, { readExecutionLog } as SessionInspection)
  app.mount(document.createElement('div'))
  let mounted = true
  const unmount = () => { if (mounted) { mounted = false; app.unmount() } }
  cleanups.push(unmount)
  return { state, result, unmount }
}

describe('shared file and execution log limits', () => {
  it('allows two combined reads and removes closed queued cards before they issue requests', async () => {
    const fileResponse = deferred<AttachmentTextPreview>()
    const logResponse = deferred<ExecutionLogPage>()
    const readFile = vi.fn().mockReturnValue(fileResponse.promise)
    const readLog = vi.fn<SessionInspection['readExecutionLog']>().mockReturnValue(logResponse.promise)
    const firstFile = mountFile(readFile)
    const firstLog = mountLog(readLog)
    const queuedLog = mountLog(readLog)
    const queuedFile = mountFile(readFile)
    expect(readFile).toHaveBeenCalledTimes(1)
    expect(readLog).toHaveBeenCalledTimes(1)
    queuedLog.unmount()
    expect(await queuedLog.result.ensureComplete()).toBe(false)
    firstFile.unmount()
    expect(readFile.mock.calls[0]![1].signal.aborted).toBe(true)
    expect(readFile).toHaveBeenCalledTimes(2)
    expect(readLog).toHaveBeenCalledTimes(1)
    fileResponse.resolve(fileBody)
    logResponse.resolve(logPage())
    expect(await queuedFile.result.ensurePreview()).toBe(true)
    expect(await firstLog.result.ensureComplete()).toBe(true)
    expect(firstFile.result.content.value).toBeNull()
    expect(queuedLog.result.content.value).toBeNull()
  })

  it('charges actual retained text so many small file and log cards remain readable', async () => {
    const readFile = vi.fn().mockResolvedValue(fileBody)
    const readLog = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValue(logPage())
    const files = Array.from({ length: 20 }, () => mountFile(readFile))
    const logs = Array.from({ length: 20 }, () => mountLog(readLog))
    expect(await Promise.all([...files.map(item => item.result.ensurePreview()), ...logs.map(item => item.result.ensureComplete())]))
      .toEqual(Array(40).fill(true))
    expect(readFile).toHaveBeenCalledTimes(20)
    expect(readLog).toHaveBeenCalledTimes(20)
    expect(files.every(item => !item.result.error.value)).toBe(true)
    expect(logs.every(item => !item.result.error.value)).toBe(true)
  })

  it('waits at the retained byte budget, cancels closed waiters and resumes automatically', async () => {
    const readFile = vi.fn().mockResolvedValue(fullFile)
    const readLog = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValue(logPage(fullBody))
    const files = Array.from({ length: 8 }, () => mountFile(readFile, true, fullBody.length))
    const logs = Array.from({ length: 8 }, () => mountLog(readLog))
    await Promise.all([...files.map(item => item.result.ensurePreview()), ...logs.map(item => item.result.ensureComplete())])
    const closed = mountFile(readFile, true, fullBody.length)
    const waiting = mountLog(readLog)
    expect(closed.result.error.value).toBe(false)
    expect(waiting.result.error.value).toBeNull()
    expect(waiting.result.loading.value).toBe(true)
    expect(readFile).toHaveBeenCalledTimes(8)
    expect(readLog).toHaveBeenCalledTimes(8)
    closed.unmount()
    files[0]!.state.enabled = false
    await nextTick()
    expect(await waiting.result.ensureComplete()).toBe(true)
    expect(waiting.result.content.value).toBe(fullBody)
    expect(files[0]!.result.content.value).toBeNull()
    expect(readFile).toHaveBeenCalledTimes(8)
    expect(readLog).toHaveBeenCalledTimes(9)
  })

  it('releases an empty failed read without double-releasing its reservation on close', async () => {
    const readFile = vi.fn().mockResolvedValue(fullFile)
    const heldFiles = Array.from({ length: 15 }, () => mountFile(readFile, true, fullBody.length))
    await Promise.all(heldFiles.map(item => item.result.ensurePreview()))
    const failedLog = mountLog(vi.fn<SessionInspection['readExecutionLog']>().mockRejectedValue(new Error('unavailable')))
    await vi.waitFor(() => expect(failedLog.result.error.value).toBe('failed'))
    expect(failedLog.result.content.value).toBeNull()
    const replacement = mountFile(readFile, true, fullBody.length)
    expect(await replacement.result.ensurePreview()).toBe(true)
    const readLog = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValue(logPage())
    const waiting = mountLog(readLog)
    failedLog.unmount()
    await nextTick()
    expect(readLog).not.toHaveBeenCalled()
    expect(waiting.result.error.value).toBeNull()
    heldFiles[0]!.unmount()
    expect(await waiting.result.ensureComplete()).toBe(true)
    expect(readLog).toHaveBeenCalledTimes(1)
  })

  it('charges the visible log prefix after a tail error until that card closes', async () => {
    const readFile = vi.fn().mockResolvedValue(fullFile)
    const files = Array.from({ length: 15 }, () => mountFile(readFile, true, fullBody.length))
    await Promise.all(files.map(item => item.result.ensurePreview()))
    const readLog = vi.fn<SessionInspection['readExecutionLog']>()
      .mockResolvedValueOnce(logPage('a', { chars: 2, nextOffset: 1 }))
      .mockRejectedValueOnce(new Error('tail unavailable'))
    const log = mountLog(readLog)
    await vi.waitFor(() => expect(log.result.content.value).toBe('a'))
    expect(await log.result.loadMore()).toBe(false)
    expect(log.result.content.value).toBe('a')
    const waiting = mountFile(readFile, true, fullBody.length)
    expect(waiting.result.error.value).toBe(false)
    expect(waiting.result.loading.value).toBe(true)
    expect(readFile).toHaveBeenCalledTimes(15)
    log.unmount()
    expect(log.result.content.value).toBeNull()
    expect(await waiting.result.ensurePreview()).toBe(true)
    expect(readFile).toHaveBeenCalledTimes(16)
  })

  it('does not read while cards are closed or continue after their close', async () => {
    const readFile = vi.fn().mockResolvedValue(fileBody)
    const readLog = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValue(logPage())
    const file = mountFile(readFile, false)
    const log = mountLog(readLog, false)
    await file.result.retry()
    await log.result.loadMore()
    expect(readFile).not.toHaveBeenCalled()
    expect(readLog).not.toHaveBeenCalled()
    file.unmount()
    log.unmount()
    await file.result.retry()
    await log.result.retry()
    expect(readFile).not.toHaveBeenCalled()
    expect(readLog).not.toHaveBeenCalled()
  })

  it('makes streaming log downloads share read slots without changing the retained preview', async () => {
    const readLog = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValue(logPage())
    const log = mountLog(readLog)
    expect(await log.result.ensureComplete()).toBe(true)
    const blocked = deferred<AttachmentTextPreview>()
    const readFile = vi.fn().mockReturnValue(blocked.promise)
    const first = mountFile(readFile)
    const second = mountFile(readFile)
    let downloaded = ''
    vi.mocked(saveByteStream).mockImplementation(async (_name, source) => {
      const reader = (await source()).getReader()
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        downloaded += new TextDecoder().decode(value)
      }
      return 'saved'
    })
    const download = log.result.download()
    await nextTick()
    expect(readLog).toHaveBeenCalledTimes(1)
    first.unmount()
    expect(await download).toBe('saved')
    expect(readLog).toHaveBeenCalledTimes(2)
    expect(downloaded).toBe('saved')
    expect(log.result.content.value).toBe('saved')
    second.unmount()
  })

  it('removes a queued download when its card closes without issuing a later log request', async () => {
    const readLog = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValue(logPage())
    const log = mountLog(readLog)
    expect(await log.result.ensureComplete()).toBe(true)
    const blocked = deferred<AttachmentTextPreview>()
    const readFile = vi.fn().mockReturnValue(blocked.promise)
    const first = mountFile(readFile)
    const second = mountFile(readFile)
    vi.mocked(saveByteStream).mockImplementation(async (_name, source) => {
      const reader = (await source()).getReader()
      await reader.read()
      return 'saved'
    })
    const download = log.result.download()
    const rejection = expect(download).rejects.toMatchObject({ name: 'AbortError' })
    await nextTick()
    log.unmount()
    await rejection
    first.unmount()
    second.unmount()
    expect(readLog).toHaveBeenCalledTimes(1)
    expect(log.result.content.value).toBeNull()
  })
})
