import { afterEach, describe, expect, it, vi } from 'vitest'

import { useChatAttachments } from '@/composables/chat/useChatAttachments'
import {
  createChatMetaDraftRecovery,
  type MetaDraftListResult,
} from '@/composables/chat/useChatMetaDraftRecovery'
import type { DurableMetaDraft } from '@/composables/chat/useChatSlashCommands'
import chatViewSource from './ChatView.vue?raw'

const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

class ControlledFileReader {
  static instances: ControlledFileReader[] = []

  result: string | ArrayBuffer | null = null
  onload: ((event: ProgressEvent<FileReader>) => void) | null = null
  onerror: ((event: ProgressEvent<FileReader>) => void) | null = null

  readAsDataURL(_blob: Blob) {
    ControlledFileReader.instances.push(this)
  }

  succeed(dataUrl: string) {
    this.result = dataUrl
    this.onload?.({ target: this } as unknown as ProgressEvent<FileReader>)
  }
}

function serverDraft(sessionKey: string): DurableMetaDraft {
  return {
    sessionKey,
    clientRequestId: 'request-meta-recovery',
    name: 'meta-draft-recovery',
    launchText: '/meta run meta-draft-recovery',
    createdAt: 1,
    expiresAt: 2,
    sessionExists: false,
  }
}

describe('ChatView Meta draft attachment recovery fence', () => {
  afterEach(() => {
    ControlledFileReader.instances = []
    pushToast.mockClear()
    vi.unstubAllGlobals()
  })

  it('includes in-flight attachment work in the pristine draft boundary', () => {
    const start = chatViewSource.indexOf('function isPristineDraftForRecovery(')
    const end = chatViewSource.indexOf('\nconst metaDraftRecovery =', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThanOrEqual(0)
    expect(end).toBeGreaterThan(start)
    expect(source).toContain('&& !attachmentWorkBusy.value')
    expect(source.indexOf('!attachmentWorkBusy.value')).toBeLessThan(
      source.indexOf('pendingAttachments.value.length === 0'),
    )
  })

  it('rejects Meta rebind while unknown MIME sniffing keeps the file on its source draft', async () => {
    vi.stubGlobal('FileReader', ControlledFileReader)
    const attachments = useChatAttachments()
    const discovery = deferred<MetaDraftListResult>()
    const sniff = deferred<ArrayBuffer>()
    const sourceSessionKey = 'agent:main:webchat:local-draft'
    const recoveredSessionKey = 'agent:main:webchat:server-draft'
    let currentSessionKey = sourceSessionKey
    const pristine = vi.fn((sessionKey: string) => (
      sessionKey === currentSessionKey
      && !attachments.attachmentWorkBusy.value
      && attachments.pendingAttachments.value.length === 0
    ))
    const rebindDraftSession = vi.fn(async (sessionKey: string) => {
      currentSessionKey = sessionKey
      return { authoritative: true, live: false, backgroundOnly: false }
    })
    const restore = vi.fn()
    const recovery = createChatMetaDraftRecovery({
      currentSessionKey: () => currentSessionKey,
      listDrafts: () => discovery.promise,
      isPristineDraft: pristine,
      rebindDraftSession,
      onAuthoritativeSubscription: restore,
    })

    recovery.start('main')

    const file = new File(
      ['source draft attachment'],
      'source-draft.unknown',
      { type: 'application/x-unknown' },
    )
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(() => sniff.promise),
    })
    const adding = attachments.addAttachment(file)

    expect(attachments.pendingAttachments.value).toEqual([])
    expect(attachments.attachmentWorkBusy.value).toBe(true)

    discovery.resolve({ drafts: [serverDraft(recoveredSessionKey)], retryable: false })
    await vi.waitFor(() => expect(pristine).toHaveBeenCalledTimes(2))

    expect(rebindDraftSession).not.toHaveBeenCalled()
    expect(restore).not.toHaveBeenCalled()
    expect(currentSessionKey).toBe(sourceSessionKey)

    sniff.resolve(new TextEncoder().encode('source draft attachment').buffer)
    await adding
    expect(ControlledFileReader.instances).toHaveLength(1)
    ControlledFileReader.instances[0]!.succeed(
      'data:text/plain;base64,c291cmNlIGRyYWZ0IGF0dGFjaG1lbnQ=',
    )

    expect(currentSessionKey).toBe(sourceSessionKey)
    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'inline', name: 'source-draft.unknown' },
    ])
    expect(attachments.attachmentWorkBusy.value).toBe(false)
    expect(pushToast).not.toHaveBeenCalled()
  })
})
