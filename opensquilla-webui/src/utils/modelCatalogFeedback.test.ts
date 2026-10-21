import { describe, expect, it } from 'vitest'
import { modelCatalogFeedbackKey } from './modelCatalogFeedback'

describe('model catalog feedback', () => {
  const models = [{ id: 'example/chat' }]
  it('distinguishes loading from refreshing without provider error text', () => {
    expect(modelCatalogFeedbackKey({ models: [], discovering: true })).toBe('modelCatalogLoading')
    expect(modelCatalogFeedbackKey({ models, discovering: true })).toBe('modelCatalogRefreshing')
    expect(modelCatalogFeedbackKey({ models, discoverError: 'private upstream detail' }))
      .toBe('modelCatalogRefreshFailed')
    expect(modelCatalogFeedbackKey({ models: [], discoverError: 'private upstream detail' }))
      .toBe('modelCatalogLoadFailed')
  })
  it('treats explicit access rejection separately from transient failure', () => {
    expect(modelCatalogFeedbackKey({ models: [], discoverFailureKind: 'auth_invalid' }))
      .toBe('modelCatalogAccessRejected')
    expect(modelCatalogFeedbackKey({ models: [], catalog: { stale: true, accessRejected: true } }))
      .toBe('modelCatalogAccessRejected')
  })
  it('does not confuse a cache miss with an authoritative empty list', () => {
    expect(modelCatalogFeedbackKey({ models: [], catalog: { stale: true, cacheHit: false } })).toBe('')
    expect(modelCatalogFeedbackKey({ models: [], catalog: { stale: false, cacheHit: true } }))
      .toBe('modelCatalogEmpty')
  })
  it('only describes absence after a successful current inventory, never unavailability', () => {
    expect(modelCatalogFeedbackKey({ models, catalog: { cacheHit: true, stale: false } }, 'manual/id'))
      .toBe('modelCatalogSelectionMissing')
    expect(modelCatalogFeedbackKey({ models, catalog: { cacheHit: true, stale: true } }, 'manual/id'))
      .toBe('')
  })
})
