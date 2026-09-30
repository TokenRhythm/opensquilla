import { describe, expect, it } from 'vitest'
import { composeLocalPathText, useLocalPathDraft } from './useLocalPathDraft'

const paths = ['C:\\Users\\测试 用户\\book.pdf', '/tmp/notes #1.html']

describe('local path composer draft', () => {
  it('keeps the editor body separate while serializing full paths exactly once', () => {
    const draft = useLocalPathDraft()
    draft.composerText.value = '请总结这些文件'
    draft.appendLocalPaths(paths.join('\n'))
    draft.appendLocalPaths(paths[0]!)
    expect(draft.localPaths.value).toEqual(paths)
    expect(draft.composerText.value).toBe('请总结这些文件')
    expect(draft.inputText.value).toBe(`请总结这些文件\n${paths.join('\n')}`)
    const snapshot = draft.inputText.value
    draft.composerText.value += '，用中文'
    draft.removeLocalPath(0)
    expect(snapshot).toBe(`请总结这些文件\n${paths.join('\n')}`)
    expect(draft.inputText.value).toBe(`请总结这些文件，用中文\n${paths[1]}`)
  })

  it('supports refs-only send and clears the whole draft via the existing send contract', () => {
    const draft = useLocalPathDraft()
    draft.appendLocalPaths(paths.join('\n'))
    expect(draft.inputText.value).toBe(paths.join('\n'))
    draft.inputText.value = ''
    expect(draft.composerText.value).toBe('')
    expect(draft.localPaths.value).toEqual([])
  })

  it('does not guess chips from pasted text or recovered queue/history payloads', () => {
    const draft = useLocalPathDraft()
    draft.appendLocalPaths(paths[0]!)
    draft.inputText.value = `Recovered task\n${paths.join('\n')}`
    expect(draft.localPaths.value).toEqual([])
    expect(draft.composerText.value).toBe(draft.inputText.value)
  })

  it('keeps chips when retry recovery writes the unchanged draft back', () => {
    const draft = useLocalPathDraft()
    draft.composerText.value = 'New draft while retrying'
    draft.appendLocalPaths(paths.join('\n'))
    draft.inputText.value = draft.inputText.value
    expect(draft.localPaths.value).toEqual(paths)
    expect(draft.composerText.value).toBe('New draft while retrying')
  })

  it('restores an explicitly marked sent/queued message without duplicating paths', () => {
    const draft = useLocalPathDraft()
    const text = `Edit me\n${paths.join('\n')}`
    draft.restoreInput(text, paths)
    expect(draft.composerText.value).toBe('Edit me')
    expect(draft.localPaths.value).toEqual(paths)
    expect(draft.inputText.value).toBe(text)
    draft.restoreInput(text)
    expect(draft.composerText.value).toBe(text)
    expect(draft.localPaths.value).toEqual([])
  })

  it('preserves editor whitespace when there are no explicit refs', () => {
    expect(composeLocalPathText('draft \n', [])).toBe('draft \n')
    expect(composeLocalPathText('draft \n', paths)).toBe(`draft\n${paths.join('\n')}`)
    expect(composeLocalPathText('  ', paths)).toBe(paths.join('\n'))
  })
})
