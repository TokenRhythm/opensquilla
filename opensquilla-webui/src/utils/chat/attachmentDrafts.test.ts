// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'

import { attachmentDraftPayloadBytes, ATTACHMENT_DRAFT_TTL_MS, createMemoryAttachmentDraftStore } from './attachmentDrafts'

const MiB = 1024 * 1024
const workspaceFile = {
  workspaceId: 'project-A', relativePath: 'data/large.bin', name: 'large.bin',
  mime: 'application/octet-stream', size: 80 * MiB,
}

describe('attachment draft payload accounting', () => {
  it('charges no payload bytes for a valid live workspace reference', () => {
    expect(attachmentDraftPayloadBytes([{
      name: 'large.bin', mime: 'application/octet-stream', size: 80 * MiB, workspaceFile,
    }])).toBe(0)
  })

  it('charges a Blob by its actual size even when workspace metadata is also present', () => {
    const blob = new Blob([new Uint8Array(7)])
    expect(attachmentDraftPayloadBytes([{
      name: 'mixed.bin', mime: 'application/octet-stream', size: 80 * MiB, workspaceFile, blob,
    }])).toBe(7)
  })

  it('conservatively charges unknown v1 attachments without workspace metadata by declared size', () => {
    expect(attachmentDraftPayloadBytes([
      { name: 'legacy.bin', mime: 'application/octet-stream', size: 9 },
    ])).toBe(9)
  })

  it('rejects invalid workspace metadata instead of granting zero-byte accounting', () => {
    const invalidReferences = [
      { ...workspaceFile, workspaceId: 'x'.repeat(257) },
      { ...workspaceFile, relativePath: '../outside.bin' },
      { ...workspaceFile, relativePath: 'drive:C/file.bin' },
      { ...workspaceFile, relativePath: 'bad\nfile.bin' },
      { ...workspaceFile, relativePath: 'x'.repeat(4097) },
      { ...workspaceFile, name: 'x'.repeat(1025) },
      { ...workspaceFile, mime: 'x'.repeat(257) },
      { ...workspaceFile, size: -1 },
      { ...workspaceFile, size: 1.5 },
      { ...workspaceFile, size: 0, unexpected: new Blob([new Uint8Array(11)]) },
    ]
    for (const [index, ref] of invalidReferences.entries()) {
      expect(() => attachmentDraftPayloadBytes([{ name: `invalid-${index}.bin`,
        mime: 'application/octet-stream', size: 0, workspaceFile: ref }])).toThrow(/reference is invalid/)
    }
  })
})

describe('ephemeral attachment drafts use the shared storage contract', () => {
  const scope = { identity: 'side-owner', sessionKey: 'child' }
  const attachment = { kind: 'workspace' as const, local_id: 7, name: workspaceFile.name, mime: workspaceFile.mime, size: workspaceFile.size, workspaceFile }

  it('isolates scope and returns copies through the same bounded restoration codec', async () => {
    const store = createMemoryAttachmentDraftStore()
    await store.save(scope, [attachment], 'r1')
    const loaded = await store.loadSnapshot!(scope)
    expect(loaded.revision).toBe('r1')
    expect(loaded.attachments[0].workspaceFile).toEqual(workspaceFile)
    loaded.attachments[0].workspaceFile!.name = 'modified.bin'
    expect((await store.load(scope))[0].workspaceFile?.name).toBe(workspaceFile.name)
    expect(await store.load({ ...scope, identity: 'other-owner' })).toEqual([])
  })

  it('retains file bytes without preserving a browser object URL', async () => {
    const store = createMemoryAttachmentDraftStore()
    await store.save(scope, [{ kind: 'inline', local_id: 1, name: 'note.txt', mime: 'text/plain', data: btoa('draft') }], 'r1')
    const [restored] = await store.load(scope)
    expect(restored.kind).toBe('failed') // goes through the shared upload preparation on restoration
    expect(await restored.file?.text()).toBe('draft')
    expect(restored.dataUrl).toBeUndefined()
  })

  it('consumes only the acknowledged revision and leaves a newer draft untouched', async () => {
    const store = createMemoryAttachmentDraftStore()
    await store.save(scope, [attachment], 'old')
    await store.save(scope, [attachment, { ...attachment, local_id: 8, name: 'second.bin' }], 'new')
    expect(await store.consume!(scope, 'old', [0])).toBe(false)
    expect((await store.load(scope))).toHaveLength(2)
    expect(await store.consume!(scope, 'new', [0])).toBe(true)
    expect((await store.load(scope))).toHaveLength(1)
    expect((await store.loadSnapshot!(scope)).revision).not.toBe('new')
  })

  it('expires retained drafts and drops all bytes when their owner closes', async () => {
    let now = 1000
    const store = createMemoryAttachmentDraftStore(() => now)
    await store.save(scope, [attachment], 'r1')
    now += ATTACHMENT_DRAFT_TTL_MS
    expect(await store.load(scope)).toEqual([])
    await store.save(scope, [attachment], 'r2')
    store.clear()
    expect(await store.load(scope)).toEqual([])
  })

  it('applies the same validation to unsafe workspace references', async () => {
    const store = createMemoryAttachmentDraftStore()
    await expect(store.save(scope, [{ ...attachment, workspaceFile: { ...workspaceFile, relativePath: '../private.txt' } }])).rejects.toThrow('reference is invalid')
    expect(await store.load(scope)).toEqual([])
  })
})
