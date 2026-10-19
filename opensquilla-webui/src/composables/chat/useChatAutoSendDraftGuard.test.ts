import { effectScope, reactive } from 'vue'
import { describe, expect, it } from 'vitest'
import { useChatAutoSendDraftGuard, type AutoSendDraftState } from './useChatAutoSendDraftGuard'

function harness(overrides: Partial<AutoSendDraftState> = {}, isAuthorityCurrent?: () => boolean) {
  // Avoid type-level unwrapping of the recursive visualization payload; the
  // runtime proxy remains deep so in-place attachment/skill edits are tested.
  const state = reactive<object>({
    text: 'draft', composerText: 'draft', localPaths: [], attachments: [], skills: [],
    visualization: null, sessionKey: 'session-a', authority: 'authority-a', projectKey: '',
    ...overrides,
  }) as AutoSendDraftState
  const scope = effectScope()
  const guard = scope.run(() => useChatAutoSendDraftGuard(() => state, { isAuthorityCurrent }))!
  return { state, guard, stop: () => scope.stop() }
}

describe('useChatAutoSendDraftGuard', () => {
  it('accepts the first authority binding only when the coordinator preserves this owner', () => {
    const h = harness({ authority: null }, () => true)
    try {
      h.state.authority = 'authority-a'
      h.state.localPaths = []
      expect(h.guard.isUnchanged()).toBe(true)
      h.state.authority = 'authority-b'
      h.state.authority = 'authority-a'
      expect(h.guard.isUnchanged()).toBe(false)
    } finally { h.stop() }
  })

  it('rejects unapproved first identity binding and authority revocation', () => {
    const unapproved = harness({ authority: null })
    let current = true
    const revoked = harness({ authority: null }, () => current)
    try {
      unapproved.state.authority = 'authority-a'
      expect(unapproved.guard.isUnchanged()).toBe(false)
      current = false
      revoked.state.authority = 'authority-a'
      current = true
      expect(revoked.guard.isUnchanged()).toBe(false)
    } finally { unapproved.stop(); revoked.stop() }
  })

  it('never clears an edit-then-undo cancellation when the first identity binds', () => {
    const h = harness({ authority: null }, () => true)
    try {
      h.state.text = 'edited'
      h.state.text = 'draft'
      h.state.authority = 'authority-a'
      expect(h.guard.isUnchanged()).toBe(false)
    } finally { h.stop() }
  })

  it('accepts equivalent empty collection restoration without relaxing the handoff', () => {
    const h = harness()
    try {
      h.state.localPaths = []
      h.state.skills = []
      h.state.attachments = []
      expect(h.guard.isUnchanged()).toBe(true)
    } finally { h.stop() }
  })

  it('permanently cancels an edit then undo even within one synchronous turn', () => {
    const h = harness()
    try {
      h.state.text = 'edited'
      h.state.text = 'draft'
      expect(h.guard.isUnchanged()).toBe(false)
    } finally { h.stop() }
  })

  it('accepts a copied non-empty draft but rejects a changed workspace attachment target', () => {
    const attachment = { kind: 'workspace' as const, local_id: 1, name: 'file.txt', mime: 'text/plain',
      workspaceFile: { workspaceId: 'workspace', relativePath: 'file.txt', name: 'file.txt', mime: 'text/plain' } }
    const h = harness({ localPaths: ['C:/chosen.txt'], attachments: [attachment] })
    try {
      h.state.localPaths = ['C:/chosen.txt']
      h.state.attachments = [{ ...attachment, workspaceFile: { ...attachment.workspaceFile } }]
      expect(h.guard.isUnchanged()).toBe(true)
      h.state.attachments[0]!.workspaceFile!.relativePath = 'other.txt'
      h.state.attachments[0]!.workspaceFile!.relativePath = 'file.txt'
      expect(h.guard.isUnchanged()).toBe(false)
    } finally { h.stop() }
  })

  it.each(['sessionKey', 'authority', 'projectKey'] as const)('fences %s change then return', key => {
    const h = harness()
    try {
      const original = h.state[key]
      h.state[key] = 'replacement'
      h.state[key] = original as string
      expect(h.guard.isUnchanged()).toBe(false)
    } finally { h.stop() }
  })

  it('retains path and skill edits even when their final collections are empty', () => {
    for (const field of ['localPaths', 'skills'] as const) {
      const h = harness()
      try {
        if (field === 'localPaths') h.state.localPaths = ['C:/chosen.txt']
        else h.state.skills = [{ name: 'skill', instanceId: 'instance', digest: 'digest' }]
        h.state[field] = []
        expect(h.guard.isUnchanged()).toBe(false)
      } finally { h.stop() }
    }
  })

  it('accepts equivalent skill copies but cancels in-place skill changes', () => {
    const skill = { name: 'skill', instanceId: 'instance', digest: 'digest' }
    const h = harness({ skills: [skill] })
    try {
      h.state.skills = [{ ...skill }]
      expect(h.guard.isUnchanged()).toBe(true)
      h.state.skills[0]!.digest = 'changed'
      h.state.skills[0]!.digest = 'digest'
      expect(h.guard.isUnchanged()).toBe(false)
    } finally { h.stop() }
  })

  it('allows upload completion and TTL refresh for the same immutable selected File', () => {
    const file = new File(['bytes'], 'file.txt', { type: 'text/plain' })
    const h = harness({ attachments: [{ kind: 'uploading', local_id: 1,
      name: file.name, mime: file.type, size: file.size, file }] })
    try {
      h.state.attachments = [{ ...h.state.attachments[0]!, kind: 'staged',
        file_uuid: 'uploaded', expires_at: 100, ttl_seconds: 100 }]
      expect(h.guard.isUnchanged()).toBe(true)
      h.state.attachments = [{ ...h.state.attachments[0]!, file_uuid: 'refreshed', expires_at: 200 }]
      expect(h.guard.isUnchanged()).toBe(true)
      h.state.attachments = [{ ...h.state.attachments[0]!, file: new File(['other'], 'file.txt') }]
      expect(h.guard.isUnchanged()).toBe(false)
    } finally { h.stop() }
  })

  it('fences attachment additions/removal and in-place inline content changes', () => {
    const attachment = { kind: 'inline' as const, local_id: 1, name: 'text', mime: 'text/plain', data: 'one' }
    const added = harness()
    const edited = harness({ attachments: [attachment] })
    try {
      added.state.attachments = [attachment]
      added.state.attachments = []
      expect(added.guard.isUnchanged()).toBe(false)
      edited.state.attachments[0]!.data = 'two'
      edited.state.attachments[0]!.data = 'one'
      expect(edited.guard.isUnchanged()).toBe(false)
    } finally { added.stop(); edited.stop() }
  })

  it('fences canvas attachment then removal and retires with its owner scope', () => {
    const h = harness()
    h.state.visualization = Object.freeze({ id: 1, sessionKey: 'session-a',
      documentId: 'document', revisionId: 'revision', name: 'canvas', summary: 'context' })
    h.state.visualization = null
    expect(h.guard.isUnchanged()).toBe(false)
    h.stop()
    const fresh = harness()
    expect(fresh.guard.isUnchanged()).toBe(true)
    fresh.stop()
    expect(fresh.guard.isUnchanged()).toBe(false)
  })
})
