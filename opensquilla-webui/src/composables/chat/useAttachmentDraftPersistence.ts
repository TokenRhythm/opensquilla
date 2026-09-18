import { getCurrentScope, nextTick, onScopeDispose, ref, watch, type Ref } from 'vue'
import type { Attachment } from '@/types/chat'
import { createClientRequestId } from '@/utils/chat/messageIdentity'
import { attachmentDraftKey, createAttachmentDraftStore, type AttachmentDraftConsumption, type AttachmentDraftScope, type AttachmentDraftStore } from '@/utils/chat/attachmentDrafts'

/** Draft-only storage. It never removes staged uploads, accepted queue material or WAL records. */
export function useAttachmentDraftPersistence(options: {
  attachments: Ref<Attachment[]>
  scope: () => AttachmentDraftScope | null
  ownerState?: () => unknown
  store?: AttachmentDraftStore | null
  beforeScopeChange: () => void
  restore: (attachments: Attachment[]) => Promise<void | boolean>
  onError: (message: string) => void
}) {
  const store = options.store === undefined ? createAttachmentDraftStore() : options.store
  const restoring = ref(false)
  let scope: AttachmentDraftScope | null = null
  let scopeKey: string | null = null
  let epoch = 0
  let revision = 0
  let contentRevision = createClientRequestId()
  let ownerRevision = 0
  let ownerDirty = false
  let clean = false
  let restoredGeneration = 0
  let suppressed = false
  let retired = false
  let failureReported = false
  let writes = Promise.resolve()
  let restoration = Promise.resolve()
  let nextWrite = 0
  const latestWrite = new Map<string, number>()
  function report(error: unknown) {
    if (failureReported) return
    failureReported = true
    options.onError(error instanceof Error ? error.message : 'Attachment draft recovery is unavailable')
  }
  function save(
    target = scope,
    attachments = options.attachments.value,
    retireSource?: AttachmentDraftScope,
  ): void {
    if (!target) return
    if (!store) { if (attachments.length) report(new Error('Attachment draft recovery is unavailable in this browser')); return }
    const snapshot = attachments.map(attachment => ({ ...attachment,
      ...(attachment.workspaceFile ? { workspaceFile: { ...attachment.workspaceFile } } : {}),
    }))
    let key: string
    try { key = attachmentDraftKey(target) } catch (error) { report(error); return }
    const version = ++nextWrite
    const savedContentRevision = contentRevision
    latestWrite.set(key, version)
    writes = writes.then(async () => {
      if (latestWrite.get(key) !== version) return
      await store.save(target, snapshot, savedContentRevision)
      if (latestWrite.get(key) === version) latestWrite.delete(key)
      if (retireSource) {
        const previousKey = attachmentDraftKey(retireSource)
        // Destination storage must acknowledge ownership before the source is
        // removed. A quota failure retains the old durable draft for recovery.
        // A return to the source or a newer source write also keeps it alive.
        if (scopeKey !== previousKey && !latestWrite.has(previousKey)) {
          await store.save(retireSource, [])
        }
      }
    }).catch(report)
  }
  const stopAttachments = watch(options.attachments, () => {
    revision += 1
    if (!suppressed && !retired) {
      clean = false
      contentRevision = createClientRequestId()
      save()
    }
  }, { deep: true, flush: 'sync' })
  const stopOwner = watch(options.ownerState ?? (() => null), () => {
    ownerRevision += 1
    if (suppressed) return
    clean = false
    if (ownerDirty) return
    ownerDirty = true
    contentRevision = createClientRequestId()
    if (!retired && !restoring.value && !suppressed) save()
  }, { deep: true, flush: 'sync' })
  async function restoreScope(key: string | null): Promise<void> {
    const next = options.scope()
    const previousScope = scope
    const wasScoped = scope !== null
    const preserveHandoff = scope?.identity === next?.identity && !retired
      && options.attachments.value.length > 0
    if (scope && !retired && !clean) save()
    clean = false
    ownerDirty = false
    if (!preserveHandoff) contentRevision = createClientRequestId()
    scope = key && next ? { ...next } : null
    scopeKey = key
    const currentEpoch = ++epoch
    failureReported = false
    suppressed = true
    if ((wasScoped || retired) && !preserveHandoff) options.beforeScopeChange()
    retired = false
    suppressed = false
    if (!scope || !store) { restoring.value = false; return }
    // A newly authenticated identity must not overwrite files the operator just
    // selected while its proof was arriving.
    if (options.attachments.value.length) {
      save(scope, options.attachments.value, preserveHandoff && previousScope ? previousScope : undefined)
      restoring.value = false
      return
    }
    const currentRevision = revision
    restoring.value = true
    try {
      // The text/skills draft watcher restores the destination in this flush.
      // Later edits, including editing back to the same value, change ownership.
      // A synchronous scope watcher runs before the other watchers are queued.
      await Promise.resolve()
      await nextTick()
      if (epoch !== currentEpoch || scopeKey !== key) return
      const currentOwnerRevision = ownerRevision
      await writes
      const loaded = store.loadSnapshot ? await store.loadSnapshot(scope) : { attachments: await store.load(scope) }
      if (epoch !== currentEpoch || revision !== currentRevision || scopeKey !== key) return
      suppressed = true
      const unchanged = await options.restore(loaded.attachments)
      if (epoch === currentEpoch) {
        clean = unchanged !== false && currentOwnerRevision === ownerRevision
        if (clean) {
          if (loaded.revision) {
            contentRevision = loaded.revision
            restoredGeneration += 1
          }
          ownerDirty = false
        }
        suppressed = false
        // An unchanged restore must not overwrite a newer draft from another
        // window while file preparation was awaiting completion.
        if (!clean) save()
      }
    } catch (error) {
      if (epoch === currentEpoch) report(error)
    } finally {
      if (epoch === currentEpoch) { suppressed = false; restoring.value = false }
    }
  }
  const stopScope = watch(() => {
    const next = options.scope()
    try { return next ? attachmentDraftKey(next) : null } catch (error) { report(error); return null }
  }, key => { restoration = restoreScope(key) }, { immediate: true, flush: 'sync' })
  function retire(): void {
    if (!clean) save()
    retired = true
    epoch += 1
    restoring.value = false
  }
  function resume(): void {
    if (!retired) return
    retired = false
    suppressed = false
  }
  function captureConsumption(attachments: readonly Attachment[]): AttachmentDraftConsumption | undefined {
    if (!scope || !store?.consume || retired) return undefined
    const target = { ...scope }
    const acceptedRevision = contentRevision
    ownerDirty = false
    const capturedRestore = restoredGeneration
    const targetKey = attachmentDraftKey(target)
    const acceptedIds = new Set(attachments.map(attachment => attachment.local_id))
    const indexes = options.attachments.value.flatMap((attachment, index) => (
      acceptedIds.has(attachment.local_id) ? [index] : []
    ))
    if (!indexes.length) return undefined
    return {
      consume() {
        // Serialize behind navigation's final save. IndexedDB checks the version
        // atomically, including writes from other tabs under the same scope.
        writes = writes.then(async () => { await store.consume!(target, acceptedRevision, indexes) }).catch(report)
        return writes
      },
      isRestoredCurrent() {
        return !restoring.value && !retired && scopeKey === targetKey
          && contentRevision === acceptedRevision && restoredGeneration > capturedRestore
      },
      async consumeCurrent(isCurrent, onConsumed, isOriginal) {
        const ownsCurrent = () => !restoring.value && !retired && scopeKey === targetKey
          && contentRevision === acceptedRevision && isCurrent()
        const consumeVisible = () => {
          // Never follow a CAS with an unconditional empty save: another
          // window may already have written its next draft.
          suppressed = true
          try {
            options.attachments.value = options.attachments.value.filter((_item, index) => !indexes.includes(index))
            contentRevision = createClientRequestId()
            clean = true
            onConsumed()
          } finally { suppressed = false }
        }
        if (ownsCurrent() && isOriginal?.()) {
          // Preserve synchronous acceptance for the unchanged live composer.
          // Recovery-storage failure must not leave sent files ready to resend.
          writes = writes.then(async () => { await store.consume!(target, acceptedRevision, indexes) }).catch(report)
          consumeVisible()
          return writes
        }
        // Restore itself awaits pending writes. Wait outside the write chain
        // so an ACK during restoration cannot deadlock or miss the draft.
        await restoration
        writes = writes.then(async () => {
          if (!ownsCurrent() || !await store.consume!(target, acceptedRevision, indexes) || !ownsCurrent()) return
          consumeVisible()
        }).catch(report)
        return writes
      },
    }
  }
  function dispose(): void {
    if (!retired && !clean) save()
    epoch += 1
    stopAttachments()
    stopOwner()
    stopScope()
  }
  if (getCurrentScope()) onScopeDispose(dispose)
  return { restoring, retire, resume, captureConsumption, dispose, flush: async () => { await restoration; await writes } }
}
