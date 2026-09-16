import type { InjectionKey } from 'vue'
import type { UsageReportingRequestOptions } from '@/modules/usageReporting'

export interface CommandCatalogItem {
  readonly name?: string
  readonly cmd?: string
  readonly label?: string
  readonly description?: string
  readonly desc?: string
  readonly aliases?: unknown
  readonly execution?: Readonly<{ action?: string }>
  readonly [key: string]: unknown
}

export interface CommandCatalogResult {
  readonly commands?: readonly CommandCatalogItem[]
  readonly surface?: string
}

export interface CommandCatalog {
  list(
    surface: string,
    options?: UsageReportingRequestOptions,
  ): Promise<CommandCatalogResult>
}

export const COMMAND_CATALOG_KEY: InjectionKey<CommandCatalog> = Symbol('CommandCatalog')
