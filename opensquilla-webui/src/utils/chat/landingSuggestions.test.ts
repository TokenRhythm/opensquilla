import { describe, expect, it } from 'vitest'
import viewSource from '../../views/ChatView.vue?raw'
import { shouldDisableLandingSuggestions } from './landingSuggestions'

describe('landing suggestion protection', () => {
  it.each([
    {
      name: 'a Sessions Hub prefill',
      state: { landingPrefilled: true, composerText: '', attachmentCount: 0 },
    },
    {
      name: 'a typed composer draft',
      state: { landingPrefilled: false, composerText: 'Keep this draft', attachmentCount: 0 },
    },
    {
      name: 'an attachment in any state',
      state: { landingPrefilled: false, composerText: '', attachmentCount: 1 },
    },
  ])('disables suggestions for $name', ({ state }) => {
    expect(shouldDisableLandingSuggestions(state)).toBe(true)
  })

  it('keeps suggestions available for an otherwise empty landing composer', () => {
    expect(shouldDisableLandingSuggestions({
      landingPrefilled: false,
      composerText: '   ',
      attachmentCount: 0,
    })).toBe(false)
  })

  it('uses the same state for rendering and stale-click protection', () => {
    expect(viewSource).toContain(':suppressed="landingSuggestionsHidden"')
    expect(viewSource).toContain(':disabled="landingSuggestionsDisabled"')
    expect(viewSource).toMatch(
      /function applyLandingSuggestion\(text: string\) \{\s+if \(landingSuggestionsDisabled\.value\) return\s+sendComposerText\(text\)\s+\}/,
    )
  })
})
