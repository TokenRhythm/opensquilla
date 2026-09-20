import i18n from '@/i18n'
import en from '@/locales/en.json'
import { chatErrorPresentation, type ChatErrorPresentationInput } from '@/utils/chat/chatErrorPresentation'
import {
  artifactProductClientError,
  classifyArtifactProductError,
  isKnownArtifactProductErrorCode,
  type ArtifactProductErrorCode,
} from '@/utils/artifactProductErrors'

export const ENSEMBLE_MULTIMODAL_UNSUPPORTED = 'ensemble_multimodal_unsupported'
export const IMAGE_INPUT_UNSUPPORTED = 'image_input_unsupported'

export function isImageInputUnsupported(code: unknown): boolean {
  return code === IMAGE_INPUT_UNSUPPORTED || code === ENSEMBLE_MULTIMODAL_UNSUPPORTED
}

/** Fixed local copy only; the legacy fallback argument is deliberately never shown. */
export function localizedChatErrorMessage(
  code: unknown,
  _fallback: string,
  replaySafe = false,
  failureKind?: string,
  terminalStatus?: string,
  context: Pick<ChatErrorPresentationInput, 'reason' | 'cancellationSource' | 'outcomeKind'> = {},
): string {
  // Annotation/preview admission failures share the Chat error row, but retain
  // their existing product-owned cause. Never recover it from upstream prose.
  if ((!terminalStatus || terminalStatus === 'failed') && isKnownArtifactProductErrorCode(code)) {
    const artifactCode = String(code).trim().toUpperCase() as ArtifactProductErrorCode
    const classified = classifyArtifactProductError(artifactProductClientError(artifactCode))
    const translated = i18n.global.t(classified.messageKey)
    return typeof translated === 'string' && translated.trim() && translated !== classified.messageKey
      ? translated
      : classified.fallbackMessage
  }
  const { messageKey } = chatErrorPresentation({ code, replaySafe, failureKind, terminalStatus, ...context })
  const translated = i18n.global.t(messageKey)
  if (typeof translated === 'string' && translated.trim() && translated !== messageKey) return translated
  // Even an unavailable locale/key must not expose upstream text or an i18n key.
  if (messageKey === 'chat.usageAccountingBlockedMessage') return en.chat.usageAccountingBlockedMessage
  if (messageKey === 'chat.usageAccountingBlockedUnsafeMessage') return en.chat.usageAccountingBlockedUnsafeMessage
  if (messageKey.startsWith('chat.errorMessage.')) {
    const key = messageKey.slice('chat.errorMessage.'.length) as keyof typeof en.chat.errorMessage
    return en.chat.errorMessage[key] ?? en.chat.errorMessage.unknown
  }
  return en.chat.errorMessage.unknown
}
