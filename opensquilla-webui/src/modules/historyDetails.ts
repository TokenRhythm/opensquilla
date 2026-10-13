import type { InjectionKey } from 'vue'

/** The owning history row shares its pending read with existing full-detail actions. */
export const HISTORY_DETAILS_READY: InjectionKey<() => boolean | Promise<boolean>> = Symbol('history-details-ready')
