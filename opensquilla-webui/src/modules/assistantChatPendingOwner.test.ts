import { describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { assistantChatPendingOwner, retireAssistantChatPendingOwner } from './assistantChatPendingOwner'
import { useAttachmentDraftPersistence } from '@/composables/chat/useAttachmentDraftPersistence'
import type { Attachment } from '@/types/chat'

describe('side owner storage policy for the shared chat implementation', () => {
  it('keeps drafts and Stop policy in memory across reader remounts, isolated by owner', () => {
    const first = assistantChatPendingOwner('first')
    const other = assistantChatPendingOwner('other')
    const scope = { deliveryIdentity: 'authority', sessionKey: 'child' }
    first.draftStorage.setItem('draft', 'temporary text')
    first.policy.pause(scope)
    expect(assistantChatPendingOwner('first')).toBe(first)
    expect(assistantChatPendingOwner('first').draftStorage.getItem('draft')).toBe('temporary text')
    expect(first.policy.read(scope)).toMatchObject({ persisted: true, paused: true })
    expect(other.policy.read(scope).paused).toBe(false)
    expect(other.draftStorage.getItem('draft')).toBeNull()
    retireAssistantChatPendingOwner('first'); retireAssistantChatPendingOwner('other')
  })

  it('discards retired drafts but still rejects delivery writes from retired shared chat readers', async () => {
    const owner = assistantChatPendingOwner('closed')
    const scope = { identity: 'authority', sessionKey: 'child' }
    await owner.attachmentDraftStore.save(scope, [])
    retireAssistantChatPendingOwner('closed')
    expect(owner.isClosed()).toBe(true)
    expect(() => owner.draftStorage.setItem('draft', 'late')).not.toThrow()
    expect(owner.draftStorage.getItem('draft')).toBeNull()
    await expect(owner.attachmentDraftStore.save(scope, [{ kind: 'staged', local_id: 1,
      name: 'late.txt', mime: 'text/plain', size: 4, file_uuid: 'late-file' }])).resolves.toBeUndefined()
    expect(await owner.attachmentDraftStore.load(scope)).toEqual([])
    expect(await owner.attachmentDraftStore.consume!(scope, 'r1', [0])).toBe(false)
    await expect(owner.wal.put({ schemaVersion: 1, pendingInputId: 'late-input', sessionKey: 'child',
      clientRequestId: 'late-request', clientMessageId: 'late-message', text: 'late send',
      attachments: [], intent: null, state: 'staged', createdAt: 1, updatedAt: 1,
    })).rejects.toThrow('closed')
  })

  it('discards the shared reader final draft save after close acknowledgement without reporting a failure', async () => {
    const binding = 'close-before-reader-disposal'
    const owner = assistantChatPendingOwner(binding)
    const scope = { identity: 'authority', sessionKey: 'child' }
    const inputText = ref('')
    const attachments = ref<Attachment[]>([])
    const onError = vi.fn()
    const reader = effectScope()
    const persistence = reader.run(() => useAttachmentDraftPersistence({
      attachments, scope: () => scope, ownerState: () => inputText.value,
      store: owner.attachmentDraftStore,
      beforeScopeChange: () => { attachments.value = [] },
      restore: async restored => { attachments.value = restored },
      onError,
    }))!
    await persistence.flush()
    inputText.value = 'Unsent draft belongs only to this closed side'
    await persistence.flush()
    // The lifecycle owner retires data after the server acknowledges close;
    // Vue disposes the shared conversation and its final autosave afterwards.
    retireAssistantChatPendingOwner(binding)
    reader.stop()
    await persistence.flush()
    expect(onError).not.toHaveBeenCalled()
    expect(await owner.attachmentDraftStore.load(scope)).toEqual([])
    const replacement = assistantChatPendingOwner(binding)
    expect(replacement).not.toBe(owner)
    expect(await replacement.attachmentDraftStore.load(scope)).toEqual([])
    retireAssistantChatPendingOwner(binding)
  })
})
