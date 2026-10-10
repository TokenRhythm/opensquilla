import type { InjectionKey } from 'vue'
import type { ContentRangeCache } from '@/utils/chat/contentRangeCache'

/** Each consumer owns its finite cache and cancellation lifetime. */
export type HistoryContentReaderFactory = () => ContentRangeCache

export const HISTORY_CONTENT_READER_KEY: InjectionKey<HistoryContentReaderFactory> = Symbol('HistoryContentReader')
