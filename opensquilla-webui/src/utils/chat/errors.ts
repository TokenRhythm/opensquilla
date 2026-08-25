import i18n from '@/i18n'
import { isUsageAccountingBarrier } from '@/utils/chat/usageAccountingFailure'

export const ENSEMBLE_MULTIMODAL_UNSUPPORTED = 'ensemble_multimodal_unsupported'
export const IMAGE_INPUT_UNSUPPORTED = 'image_input_unsupported'

export function isImageInputUnsupported(code: unknown): boolean {
  return code === IMAGE_INPUT_UNSUPPORTED || code === ENSEMBLE_MULTIMODAL_UNSUPPORTED
}

/** Preserve server-authored text for unknown failures, while localizing stable errors. */
export function localizedChatErrorMessage(
  code: unknown,
  fallback: string,
  replaySafe = false,
): string {
  if (isUsageAccountingBarrier(code)) {
    return i18n.global.t(
      replaySafe
        ? 'chat.usageAccountingBlockedMessage'
        : 'chat.usageAccountingBlockedUnsafeMessage',
    )
  }
  if (code === ENSEMBLE_MULTIMODAL_UNSUPPORTED) {
    return i18n.global.t('chat.composer.ensembleImageUnsupported')
  }
  return code === IMAGE_INPUT_UNSUPPORTED
    ? i18n.global.t('chat.composer.imageInputUnsupported')
    : fallback
}
