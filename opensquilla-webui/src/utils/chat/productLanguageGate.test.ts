import { describe, expect, it } from 'vitest'

import de from '@/locales/de.json'
import en from '@/locales/en.json'
import es from '@/locales/es.json'
import fr from '@/locales/fr.json'
import ja from '@/locales/ja.json'
import zhHans from '@/locales/zh-Hans.json'
import { promptAnnotationTargetLabel } from './promptAnnotationPresentation'

const FORBIDDEN_PRODUCT_LANGUAGE = /(?:\bstale\b|\breceipt\b|\breconciliation\b|EditSession|sha256|change[ -]?sets?|actorId|working copy|immutable snapshot|protocol-v3|(?:native|trusted) editor|opaque sandbox|document_apply|<\/?[a-z][^>]*>)/i

function strings(value: unknown): string[] {
  if (typeof value === 'string') return [value]
  if (!value || typeof value !== 'object') return []
  return Object.values(value).flatMap(strings)
}

function visibleFeatureCopy(locale: unknown): string {
  const root = locale as {
    chat?: Record<string, unknown>
    workbench?: Record<string, unknown>
  }
  return [
    root.workbench?.artifactAnnotation,
    root.workbench?.artifactDocument,
    root.workbench?.artifactPreview,
    root.workbench?.resources,
    root.chat?.promptAnnotations,
    root.chat?.documentMutation,
    (root.chat?.activity as Record<string, unknown> | undefined)?.document,
  ].flatMap(strings).join('\n')
}

describe('artifact product language gate', () => {
  it.each([
    ['de', de],
    ['en', en],
    ['es', es],
    ['fr', fr],
    ['ja', ja],
    ['zh-Hans', zhHans],
  ])('keeps %s feature copy free of storage and protocol terminology', (_name, locale) => {
    expect(visibleFeatureCopy(locale)).not.toMatch(FORBIDDEN_PRODUCT_LANGUAGE)
  })

  it('turns a legacy DOM-tag target into a semantic selected-area label', () => {
    const translate = (key: string, params?: Record<string, unknown>) => {
      if (key === 'chat.promptAnnotations.targetKinds.element') return 'Selected area'
      if (key === 'chat.promptAnnotations.targetLabel') {
        return `${params?.kind}: ${params?.text}`
      }
      return key
    }

    const label = promptAnnotationTargetLabel({
      targetKind: undefined,
      targetText: '<p>',
    }, translate)
    expect(label).toBe('Selected area')
    expect(label).not.toMatch(FORBIDDEN_PRODUCT_LANGUAGE)
  })
})
