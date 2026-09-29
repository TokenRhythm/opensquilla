// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'

import { attachmentDraftPayloadBytes } from './attachmentDrafts'

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
