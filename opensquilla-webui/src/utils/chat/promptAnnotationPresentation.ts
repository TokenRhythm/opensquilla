import type { PromptAnnotation, PromptAnnotationSnapshot } from '@/types/promptAnnotations'

type PromptAnnotationTarget = Pick<
  PromptAnnotation | PromptAnnotationSnapshot,
  'targetKind' | 'targetText'
>

const PRODUCT_TARGET_KINDS = new Set([
  'heading',
  'button',
  'link',
  'image',
  'input',
  'form',
  'section',
  'list',
  'table',
  'text',
  'region',
])

export function promptAnnotationTargetLabel(
  annotation: PromptAnnotationTarget,
  translate: (key: string, params?: Record<string, unknown>) => string,
): string {
  const kind = PRODUCT_TARGET_KINDS.has(annotation.targetKind || '')
    ? annotation.targetKind
    : 'element'
  const kindLabel = translate(`chat.promptAnnotations.targetKinds.${kind}`)
  const candidate = String(annotation.targetText || '').replace(/\s+/g, ' ').trim().slice(0, 80)
  const text = /^<\/?[a-z][\w:-]*(?:\s[^<>]*)?>$/i.test(candidate) ? '' : candidate
  return text
    ? translate('chat.promptAnnotations.targetLabel', { kind: kindLabel, text })
    : kindLabel
}
