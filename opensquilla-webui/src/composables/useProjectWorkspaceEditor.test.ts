import { describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'
import { useProjectWorkspaceEditor } from './useProjectWorkspaceEditor'

function deferred() {
  let resolve!: () => void
  let reject!: (error: unknown) => void
  const promise = new Promise<void>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function setup() {
  const rename = vi.fn<(id: string, name: string) => Promise<void>>().mockResolvedValue()
  const onError = vi.fn()
  let allowed = true
  const scope = effectScope()
  const editor = scope.run(() => useProjectWorkspaceEditor({
    allowed: () => allowed, rename, onError,
  }))!
  return { editor, rename, onError, scope, deny: () => { allowed = false } }
}

describe('project workspace editor lifecycle', () => {
  it('saves the captured project once and closes the current opening', async () => {
    const { editor, rename, scope } = setup()
    const pending = deferred()
    rename.mockReturnValueOnce(pending.promise)
    editor.open('a')
    const save = editor.save(' New name ')
    await editor.save('Duplicate')
    expect(rename).toHaveBeenCalledExactlyOnceWith('a', 'New name')
    expect(editor.saving.value).toBe(true)
    pending.resolve()
    await save
    expect(editor.workspaceId.value).toBe('')
    expect(editor.saving.value).toBe(false)
    scope.stop()
  })

  it.each(['a', 'b'])('does not close or unlock reopened project %s when an older rename finishes', async nextId => {
    const { editor, rename, scope } = setup()
    const old = deferred()
    const current = deferred()
    rename.mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    editor.open('a')
    const oldSave = editor.save('Old name')
    editor.close()
    editor.open(nextId)
    const newSave = editor.save('Current name')
    old.resolve()
    await oldSave
    expect(editor.workspaceId.value).toBe(nextId)
    expect(editor.saving.value).toBe(true)
    current.resolve()
    await newSave
    expect(editor.workspaceId.value).toBe('')
    scope.stop()
  })

  it('keeps the current dialog open for a retry after failure', async () => {
    const { editor, rename, onError, scope } = setup()
    const error = new Error('denied')
    rename.mockRejectedValueOnce(error)
    editor.open('a')
    await editor.save('Name')
    expect(onError).toHaveBeenCalledExactlyOnceWith(error)
    expect(editor.workspaceId.value).toBe('a')
    expect(editor.saving.value).toBe(false)
    await editor.save('Retry')
    expect(editor.workspaceId.value).toBe('')
    scope.stop()
  })

  it.each(['reopen', 'dispose'])('ignores an old failure after %s', async action => {
    const { editor, rename, onError, scope } = setup()
    const old = deferred()
    rename.mockReturnValueOnce(old.promise)
    editor.open('a')
    const save = editor.save('Old name')
    if (action === 'dispose') scope.stop()
    else { editor.close(); editor.open('a') }
    old.reject(new Error('old failure'))
    await save
    expect(onError).not.toHaveBeenCalled()
    expect(editor.workspaceId.value).toBe(action === 'dispose' ? '' : 'a')
    scope.stop()
  })

  it('does not start a rename without permission or a nonempty name', async () => {
    const { editor, rename, deny, scope } = setup()
    editor.open('a')
    await editor.save(' ')
    deny()
    await editor.save('Name')
    editor.close()
    editor.open('b')
    expect(rename).not.toHaveBeenCalled()
    expect(editor.workspaceId.value).toBe('')
    scope.stop()
  })
})
