import { effectScope, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useFilteredSessionPaging } from './useFilteredSessionPaging'

describe('useFilteredSessionPaging', () => {
  it('continues across unmatched pages after a filter changes', async () => {
    const active = ref(false)
    const visibleCount = ref(12)
    const hasMore = ref(true)
    const isLoading = ref(false)
    const isLoadingMore = ref(false)
    const hasError = ref(false)
    const loadMore = vi.fn()

    useFilteredSessionPaging({
      active: () => active.value,
      visibleCount: () => visibleCount.value,
      hasMore: () => hasMore.value,
      isLoading: () => isLoading.value,
      isLoadingMore: () => isLoadingMore.value,
      hasError: () => hasError.value,
      loadMore,
    })

    active.value = true
    visibleCount.value = 0
    await nextTick()
    expect(loadMore).toHaveBeenCalledTimes(1)

    isLoadingMore.value = true
    await nextTick()
    isLoadingMore.value = false
    await nextTick()
    expect(loadMore).toHaveBeenCalledTimes(2)

    isLoadingMore.value = true
    await nextTick()
    visibleCount.value = 1
    isLoadingMore.value = false
    await nextTick()
    expect(loadMore).toHaveBeenCalledTimes(2)
  })

  it('stops on an error or terminal page and resumes only with usable state', async () => {
    const active = ref(true)
    const visibleCount = ref(0)
    const hasMore = ref(true)
    const isLoading = ref(true)
    const isLoadingMore = ref(false)
    const hasError = ref(false)
    const loadMore = vi.fn()

    useFilteredSessionPaging({
      active: () => active.value,
      visibleCount: () => visibleCount.value,
      hasMore: () => hasMore.value,
      isLoading: () => isLoading.value,
      isLoadingMore: () => isLoadingMore.value,
      hasError: () => hasError.value,
      loadMore,
    })

    isLoading.value = false
    hasError.value = true
    await nextTick()
    expect(loadMore).not.toHaveBeenCalled()

    hasError.value = false
    await nextTick()
    expect(loadMore).toHaveBeenCalledTimes(1)

    hasMore.value = false
    isLoadingMore.value = true
    await nextTick()
    isLoadingMore.value = false
    await nextTick()
    expect(loadMore).toHaveBeenCalledTimes(1)
  })

  it('stops watching after its owning scope is disposed', async () => {
    const active = ref(true)
    const visibleCount = ref(1)
    const hasMore = ref(true)
    const isLoading = ref(false)
    const isLoadingMore = ref(false)
    const hasError = ref(false)
    const loadMore = vi.fn()
    const scope = effectScope()

    scope.run(() => useFilteredSessionPaging({
      active: () => active.value,
      visibleCount: () => visibleCount.value,
      hasMore: () => hasMore.value,
      isLoading: () => isLoading.value,
      isLoadingMore: () => isLoadingMore.value,
      hasError: () => hasError.value,
      loadMore,
    }))
    scope.stop()

    visibleCount.value = 0
    await nextTick()
    expect(loadMore).not.toHaveBeenCalled()
  })
})
