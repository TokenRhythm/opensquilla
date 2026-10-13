import { describe, expect, it } from 'vitest'
import { useToolInputPreviews } from './useToolInputPreviews'
import type { ToolInputPreview } from '@/types/toolInputPreview'

const draft = (patch: Partial<ToolInputPreview> = {}): ToolInputPreview => ({
  previewId: 'draft-1', attemptId: 'attempt-1', toolUseId: 'tool-1', generationEpoch: 0,
  revision: 1, state: 'generating', toolName: 'write_file', path: 'page.html', content: '<html>', truncated: false,
  ...patch,
})
describe('live tool input previews', () => {
  it('replaces snapshots without duplicating rows or changing their chronological position', () => {
    const store = useToolInputPreviews()
    expect(store.accept(draft(), 4)).toBe(true)
    expect(store.accept(draft({ revision: 2, content: '<html>new' }), 8)).toBe(true)
    expect(store.previews.value).toHaveLength(1)
    expect(store.previews.value[0]).toMatchObject({ content: '<html>new', activityOrder: 4 })
    expect(store.accept(draft({ revision: 1, content: 'old' }), 9)).toBe(false)
    expect(store.accept(draft({ revision: 3, attemptId: 'other' }), 10)).toBe(false)
    expect(store.accept(draft({ revision: 3, generationEpoch: 1 }), 11)).toBe(false)
  })
  it.each(['discarded', 'committed'] as const)('never resurrects a %s preview', state => {
    const store = useToolInputPreviews()
    store.accept(draft(), 1)
    store.accept(draft({ revision: 2, state, content: '' }), 2)
    expect(store.previews.value).toHaveLength(state === 'committed' ? 1 : 0)
    if (state === 'committed') {
      expect(store.previews.value[0]?.content).toBe('<html>')
      store.handoff('tool-1')
    }
    expect(store.accept(draft({ revision: 3 }), 3)).toBe(false)
    expect(store.previews.value).toEqual([])
  })
  it('ignores terminal-only replay and bounds retained content to the latest real text', () => {
    const store = useToolInputPreviews()
    store.accept(draft({ state: 'committed' }), 1)
    expect(store.previews.value).toEqual([])
    store.clear()
    store.accept(draft({ content: 'old'.repeat(30_000) + 'latest' }), 2)
    expect(store.previews.value[0]?.content).toHaveLength(65_536)
    expect(store.previews.value[0]?.content.endsWith('latest')).toBe(true)
    expect(store.previews.value[0]?.truncated).toBe(true)
    store.clear()
    expect(store.previews.value).toEqual([])
  })
  it('does not leave an orphan surrogate when bounding a Unicode draft tail', () => {
    const store = useToolInputPreviews()
    store.accept(draft({ content: 'prefix🚀' + 'x'.repeat(65_535) }), 1)
    expect(store.previews.value[0]?.content).toBe('x'.repeat(65_535))
    expect(store.previews.value[0]?.truncated).toBe(true)
  })
  it('applies replayed terminal facts only to an existing matching preview', () => {
    const store = useToolInputPreviews()
    expect(store.accept(draft({ state: 'discarded', revision: 2 }), 2, true)).toBe(false)
    expect(store.accept(draft(), 1)).toBe(true)
    expect(store.accept(draft({ state: 'discarded', revision: 2, attemptId: 'other' }), 2, true)).toBe(false)
    expect(store.accept(draft({ state: 'discarded', revision: 2, toolUseId: 'other' }), 2, true)).toBe(false)
    expect(store.previews.value).toHaveLength(1)
    expect(store.accept(draft({ state: 'discarded', revision: 2 }), 2, true)).toBe(true)
    expect(store.previews.value).toEqual([])
  })
})
