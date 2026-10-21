interface CatalogFeedbackState {
  models: readonly { id: string }[]
  discovering?: boolean
  discoverError?: string
  discoverFailureKind?: string
  catalog?: { cacheHit?: boolean; stale: boolean; accessRejected?: boolean } | null
}

/** Translate catalog state, never provider error text or model status, into UI feedback. */
export function modelCatalogFeedbackKey(state: CatalogFeedbackState, selectedModel = ''): string {
  if (state.discovering) {
    return state.models.length ? 'modelCatalogRefreshing' : 'modelCatalogLoading'
  }
  if (state.catalog?.accessRejected || state.discoverFailureKind === 'auth_invalid') {
    return 'modelCatalogAccessRejected'
  }
  if (state.discoverError) {
    return state.models.length ? 'modelCatalogRefreshFailed' : 'modelCatalogLoadFailed'
  }
  if (state.catalog?.cacheHit && !state.catalog.stale) {
    if (!state.models.length) return 'modelCatalogEmpty'
    if (selectedModel.trim() && !state.models.some(model => model.id === selectedModel.trim())) {
      return 'modelCatalogSelectionMissing'
    }
  }
  return ''
}
