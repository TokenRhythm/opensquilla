import type { InjectionKey } from 'vue'

export type SettingsValue = string | number | boolean | null | SettingsObject | SettingsValue[]
export interface SettingsObject { [key: string]: SettingsValue }

export interface EffectiveSetting {
  readonly value: SettingsValue
  readonly source: 'default' | 'catalog' | 'preset' | 'config' | 'session' | string
}

export interface EffectiveSettings {
  readonly fields: Record<string, EffectiveSetting>
}

export interface SettingsMutation {
  readonly values?: SettingsObject
  revision?: number
  restartRequired?: boolean
  readonly restartSections?: readonly string[]
  readonly patched?: readonly string[]
  readonly linked?: readonly string[]
  readonly modelRouting?: Record<string, unknown>
  readonly [key: string]: unknown
}

export interface SettingChange { readonly path: string; readonly value: SettingsValue }

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
  readAll(options?: { signal?: AbortSignal }): Promise<SettingsObject>
  read(path: string, options?: { signal?: AbortSignal }): Promise<SettingsValue | null>
  readEffective(options?: { signal?: AbortSignal }): Promise<EffectiveSettings>
  patch(changes: readonly SettingChange[], options?: { signal?: AbortSignal }): Promise<SettingsMutation>
  patchSafe(changes: readonly SettingChange[], options?: { signal?: AbortSignal }): Promise<SettingsMutation>
  merge(patch: SettingsObject, options?: { signal?: AbortSignal }): Promise<SettingsMutation>
}

export const APP_SETTINGS_KEY: InjectionKey<AppSettings> = Symbol('AppSettings')
