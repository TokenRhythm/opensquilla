import type { InjectionKey } from 'vue'

export interface SetupCatalog { [key: string]: unknown }
export interface SetupStatus { [key: string]: unknown }
export interface SetupProfile { [key: string]: unknown }

export interface SetupWorkflow {
  catalog(options?: { signal?: AbortSignal }): Promise<SetupCatalog>
  status(options?: { signal?: AbortSignal }): Promise<SetupStatus>
  configure(profile: SetupProfile, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  reset(options?: { signal?: AbortSignal }): Promise<SetupStatus>
}

export const SETUP_WORKFLOW_KEY: InjectionKey<SetupWorkflow> = Symbol('SetupWorkflow')
