import { watch } from 'vue'

interface FilteredSessionPagingOptions {
  active: () => boolean
  visibleCount: () => number
  hasMore: () => boolean
  isLoading: () => boolean
  isLoadingMore: () => boolean
  hasError: () => boolean
  loadMore: () => void | Promise<void>
}

/**
 * Continue an unfiltered session traversal while the active client-side
 * filter has no match on the pages loaded so far.
 */
export function useFilteredSessionPaging(options: FilteredSessionPagingOptions) {
  watch(
    [
      options.active,
      options.visibleCount,
      options.hasMore,
      options.isLoading,
      options.isLoadingMore,
      options.hasError,
    ],
    ([active, visibleCount, hasMore, isLoading, isLoadingMore, hasError]) => {
      if (
        !active
        || visibleCount > 0
        || !hasMore
        || isLoading
        || isLoadingMore
        || hasError
      ) return
      void options.loadMore()
    },
    { flush: 'post' },
  )
}
