import i18n from '@/i18n'
import { AppSettingsError } from '@/modules/appSettings'
import { ProviderConfigurationError } from '@/modules/providerConfiguration'
import { SetupWorkflowError } from '@/modules/setupWorkflow'

const SETUP_REASON_KEYS = {
  'provider-invalid': 'errors.onboarding.provider',
  'router-invalid': 'errors.onboarding.router',
  'search-invalid': 'errors.onboarding.search',
  'image-generation-invalid': 'errors.onboarding.image',
} as const

export function setupErrorMessage(error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error ?? '')
  if (error instanceof SetupWorkflowError && error.reason) {
    const lead = i18n.global.t(SETUP_REASON_KEYS[error.reason])
    return detail ? `${lead} (${detail})` : lead
  }
  return detail
}

export function setupSaveFailedMessage(error: unknown): string {
  return `${i18n.global.t('errors.saveFailed')}: ${setupErrorMessage(error)}`
}

export function isSetupCapabilityUnsupported(error: unknown): boolean {
  return (
    error instanceof SetupWorkflowError
    || error instanceof AppSettingsError
    || error instanceof ProviderConfigurationError
  ) && error.code === 'unsupported'
}
