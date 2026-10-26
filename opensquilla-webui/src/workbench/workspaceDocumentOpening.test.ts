import { describe, expect, it, vi } from 'vitest'
import type { WorkspaceFile, WorkspaceFiles } from '@/modules/workspaceFiles'
import { normalizeWorkspaceFileReferenceV1 } from '@/types/references'
import { createResolvedWorkspaceFileItem, createWorkspaceFileItem } from './workspaceFileItems'
import { resolveWorkspaceDocumentFile } from './workspaceDocumentOpening'

const reference = normalizeWorkspaceFileReferenceV1({
  version: 1, kind: 'workspace_file', id: 'report.docx', label: 'report.docx',
  scope: { sessionKey: 'child', workspaceId: 'original-workspace' },
  locator: { relativePath: 'report.docx' },
  state: { available: true, revision: 'file_1234567890abcdef' }, capabilities: { open: true },
})!
const file: WorkspaceFile = {
  path: 'report.docx', requestedPath: 'report.docx', name: 'report.docx',
  mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  size: 20, kind: 'download', workspaceBinding: 'original-binding',
}
function services() {
  return {
    resolve: vi.fn<WorkspaceFiles['resolve']>().mockResolvedValue([file]),
    resolveReference: vi.fn<NonNullable<WorkspaceFiles['resolveReference']>>().mockResolvedValue([file]),
    read: vi.fn<WorkspaceFiles['read']>(),
  }
}

describe('workspace Document opening authority', () => {
  it('revalidates a retained reference with its child session and original workspace scope', async () => {
    const files = services()
    const item = createWorkspaceFileItem('child', reference, 'parent')
    expect(await resolveWorkspaceDocumentFile(files, item, () => true)).toEqual(file)
    expect(files.resolveReference).toHaveBeenCalledExactlyOnceWith('child', reference)
    expect(files.resolve).not.toHaveBeenCalled()
  })

  it('never falls back to a same-path file in another workspace after scope rejection', async () => {
    const files = services()
    files.resolve.mockResolvedValue([{ ...file, workspaceBinding: 'replacement-binding',
      documentTarget: { documentId: 'unrelated-document' } }])
    files.resolveReference.mockRejectedValue(new Error('workspace scope mismatch'))
    await expect(resolveWorkspaceDocumentFile(files, createWorkspaceFileItem('child', reference), () => true))
      .rejects.toThrow('workspace scope mismatch')
    expect(files.resolve).not.toHaveBeenCalled()
  })

  it('requires scope-aware discovery for reference-only items on an older Gateway', async () => {
    const { resolveReference: _unsupported, ...files } = services()
    expect(await resolveWorkspaceDocumentFile(files, createWorkspaceFileItem('child', reference), () => true)).toBeNull()
    expect(files.resolve).not.toHaveBeenCalled()
  })

  it('retains resolved-file binding checks for ordinary path items', async () => {
    const files = services()
    files.resolve.mockResolvedValue([{ ...file, workspaceBinding: 'replacement-binding' }])
    expect(await resolveWorkspaceDocumentFile(files, createResolvedWorkspaceFileItem('child', file), () => true)).toBeNull()
    expect(files.resolve).toHaveBeenCalledExactlyOnceWith('child', [file.path])
    expect(files.resolveReference).not.toHaveBeenCalled()
  })

  it('discards a delayed resolution after the user changes the active item', async () => {
    const files = services()
    let finish!: (value: WorkspaceFile[]) => void
    files.resolveReference.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    let active = true
    const opening = resolveWorkspaceDocumentFile(files, createWorkspaceFileItem('child', reference), () => active)
    active = false
    finish([file])
    expect(await opening).toBeNull()
  })

  it('returns an authorized unregistered Office file without creating a document', async () => {
    const files = services()
    expect(await resolveWorkspaceDocumentFile(files, createWorkspaceFileItem('child', reference), () => true))
      .toMatchObject({ path: file.path, workspaceBinding: file.workspaceBinding })
    expect(files.read).not.toHaveBeenCalled()
  })
})
