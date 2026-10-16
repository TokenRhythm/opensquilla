import { describe, expect, it } from 'vitest'
import { assistantChatPendingOwner, retireAssistantChatPendingOwner } from './assistantChatPendingOwner'

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

  it('closes attachment/draft stores and rejects delayed writes from retired shared chat readers', async () => {
    const owner = assistantChatPendingOwner('closed')
    const scope = { identity: 'authority', sessionKey: 'child' }
    await owner.attachmentDraftStore.save(scope, [])
    retireAssistantChatPendingOwner('closed')
    expect(owner.isClosed()).toBe(true)
    expect(() => owner.draftStorage.setItem('draft', 'late')).toThrow('closed')
    await expect(owner.attachmentDraftStore.save(scope, [])).rejects.toThrow('closed')
    expect(await owner.attachmentDraftStore.load(scope)).toEqual([])
    expect(await owner.attachmentDraftStore.consume!(scope, 'r1', [0])).toBe(false)
  })
})
