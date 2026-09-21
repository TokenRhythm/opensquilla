import { describe, expect, it, vi } from 'vitest'
import type { ChatPendingItem } from '@/types/chat'
import { ParkedPendingQueueCache, PARKED_PENDING_QUEUE_LIMITS } from './parkedPendingQueueCache'

function item(id: string, text = id): ChatPendingItem {
  return { text, intent: null, attachments: [], pendingInputId: id,
    pendingUiId: id, ownerSessionKey: id, pendingPersistenceState: 'local_only' }
}

function cache(isPinned = (_item: ChatPendingItem) => false, isUrlRetained = (_url: string) => false) {
  return new ParkedPendingQueueCache({ isPinned, isUrlRetained })
}

function park(target: ParkedPendingQueueCache, value: ChatPendingItem) {
  target.rememberCommitted(value, target.snapshotForCommit(value))
  target.set(value.pendingInputId!, [value])
}

describe('durable parked queue resource budget', () => {
  it('measures 500 protected sessions with one eligibility pass per trim', () => {
    const pinned = vi.fn(() => true)
    const target = cache(pinned)
    const started = performance.now()
    let maxParkMs = 0
    for (let i = 0; i < 500; i++) {
      const step = performance.now()
      park(target, item(`protected-${i}`, 'Synthetic protected input '.repeat(40)))
      maxParkMs = Math.max(maxParkMs, performance.now() - step)
    }
    const usage = target.usage()
    expect(usage).toMatchObject({ sessions: 500, protectedSessions: 500,
      reclaimableSessions: 0, reclaimablePayloadBytes: 0, reclaimableBlobBytes: 0 })
    expect(usage.protectedPayloadBytes).toBe(usage.payloadBytes)
    expect(usage.protectedBlobBytes).toBe(usage.blobBytes)
    pinned.mockClear()
    target.trim()
    expect(pinned).toHaveBeenCalledTimes(500)
    console.log('Protected parked queue pressure sample:', JSON.stringify({
      elapsedMs: Math.round(performance.now() - started), maxParkMs: Math.round(maxParkMs * 10) / 10,
      ...usage,
    }))
  })

  it('keeps the most recently parked durable sessions within both count and payload budgets', () => {
    const target = cache()
    for (let i = 0; i < 500; i++) park(target, item(`session-${i}`, 'x'.repeat(1024)))
    expect([...target.keys()]).toEqual(Array.from({ length: 16 }, (_, i) => `session-${484 + i}`))
    expect(target.usage().sessions).toBe(16)
    expect(target.usage().payloadBytes).toBeLessThan(PARKED_PENDING_QUEUE_LIMITS.payloadBytes)
    park(target, item('large', 'x'.repeat(9 * 1024 * 1024)))
    expect(target.usage().payloadBytes).toBeLessThanOrEqual(PARKED_PENDING_QUEUE_LIMITS.payloadBytes)
    expect(target.has('large')).toBe(false)
  })

  it('releases Blob handles only after commit and preserves shared active handles', () => {
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    try {
      const target = cache()
      const draft = item('attachment')
      draft.attachments = [{ kind: 'staged', local_id: 1, name: 'fixture.bin', mime: 'application/octet-stream',
        file: new File([new Uint8Array(33 * 1024 * 1024)], 'fixture.bin'), dataUrl: 'blob:synthetic-attachment' }]
      target.set('attachment', [draft])
      expect(target.usage().protectedSessions).toBe(1)
      expect(target.usage().protectedBlobBytes).toBe(33 * 1024 * 1024)
      expect(target.usage().reclaimableBlobBytes).toBe(0)
      expect(revoke).not.toHaveBeenCalled()
      target.rememberCommitted(draft, target.snapshotForCommit(draft))
      expect(target.usage().blobBytes).toBe(0)
      expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:synthetic-attachment')
      const shared = cache(() => false, url => url === 'blob:synthetic-attachment')
      park(shared, draft)
      expect(shared.size).toBe(0)
      expect(revoke).toHaveBeenCalledTimes(1)
    } finally { revoke.mockRestore() }
  })

  it('pins in-flight owners, unsaved edits and handles without durable bytes', () => {
    const active = item('active', 'x'.repeat(9 * 1024 * 1024))
    const target = cache(value => value === active)
    park(target, active)
    const changed = item('changed')
    target.rememberCommitted(changed, target.snapshotForCommit(changed))
    changed.text = 'not committed'
    target.set('changed', [changed])
    const volatile = item('volatile-url')
    volatile.attachments = [{ kind: 'inline', local_id: 1, name: 'fixture.png', mime: 'image/png', dataUrl: 'blob:unpersisted' }]
    park(target, volatile)
    for (let i = 0; i < 20; i++) park(target, item(`other-${i}`))
    expect([...target.keys()]).toEqual(['active', 'changed', 'volatile-url'])
    expect(target.usage().protectedSessions).toBe(3)
  })

  it('rebuilds persisted object URLs and releases unused hydration handles and owned handles on disposal', async () => {
    const create = vi.spyOn(URL, 'createObjectURL').mockReturnValueOnce('blob:restored').mockReturnValueOnce('blob:unused')
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const retained = new Set<string>()
    const target = cache(() => false, url => retained.has(url))
    try {
      const attachment = { kind: 'staged' as const, local_id: 1, name: 'fixture.txt', mime: 'text/plain',
        file: new File(['synthetic contents'], 'fixture.txt'), dataUrl: 'blob:previous-renderer' }
      const restored = target.restoreAttachment({ ...attachment })
      expect(restored.file).toBe(attachment.file)
      expect(restored.dataUrl).toBe('blob:restored')
      retained.add(restored.dataUrl!)
      target.restoreAttachment({ ...attachment }) // A reconciliation row that was not installed.
      await Promise.resolve()
      expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:unused')
      target.dispose()
      expect(revoke).toHaveBeenCalledTimes(2)
      expect(revoke).toHaveBeenLastCalledWith('blob:restored')
    } finally { target.dispose(); create.mockRestore(); revoke.mockRestore() }
  })
})
