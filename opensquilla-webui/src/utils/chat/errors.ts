import i18n from '@/i18n'

export const ENSEMBLE_MULTIMODAL_UNSUPPORTED = 'ensemble_multimodal_unsupported'

/** Preserve server-authored text for unknown failures, but give the stable
 * Ensemble image-input error an actionable message in the active UI locale. */
export function localizedChatErrorMessage(
  code: unknown,
  fallback: string,
): string {
  return code === ENSEMBLE_MULTIMODAL_UNSUPPORTED
    ? i18n.global.t('chat.composer.ensembleImageUnsupported')
    : fallback
}
