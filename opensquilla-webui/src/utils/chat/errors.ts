import i18n from '@/i18n'
import { isUsageAccountingBarrier } from '@/utils/chat/usageAccountingFailure'
import { localizedProviderFailureKind } from '@/utils/chat/providerFailure'

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
  failureKind?: string,
  terminalStatus?: string,
): string {
  if (terminalStatus === 'timeout' || terminalStatus === 'abandoned' || terminalStatus === 'cancelled') return fallback
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
  if (code === IMAGE_INPUT_UNSUPPORTED) return i18n.global.t('chat.composer.imageInputUnsupported')
  const kind = localizedProviderFailureKind(failureKind, code, fallback)
  return kind ? i18n.global.t(`chat.providerFailure.${kind}`) : fallback
}
