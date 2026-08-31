import type { InjectionKey } from 'vue'

export type SettingsValue = string | number | boolean | null | SettingsObject | SettingsValue[]
export interface SettingsObject { [key: string]: SettingsValue }

export interface AppSettingsSnapshot {
  values: SettingsObject
  revision?: number
  restartRequired?: boolean
}

export interface SettingsPatch { path: string; value: SettingsValue }

export type AppSettingsErrorCode =
  | 'not-found' | 'unsupported' | 'forbidden' | 'conflict' | 'unavailable' | 'invalid'

export class AppSettingsError extends Error {
  readonly code: AppSettingsErrorCode
  constructor(code: AppSettingsErrorCode, message: string) {
    super(message)
    this.name = 'AppSettingsError'
    this.code = code
  }
}

export interface AppSettings {
  get(path?: string, options?: { signal?: AbortSignal }): Promise<AppSettingsSnapshot>
  effective(options?: { signal?: AbortSignal }): Promise<AppSettingsSnapshot>
  patch(patches: SettingsPatch[], options?: { signal?: AbortSignal }): Promise<AppSettingsSnapshot>
  patchSafe(patches: SettingsPatch[], options?: { signal?: AbortSignal }): Promise<AppSettingsSnapshot>
}

export const APP_SETTINGS_KEY: InjectionKey<AppSettings> = Symbol('AppSettings')
