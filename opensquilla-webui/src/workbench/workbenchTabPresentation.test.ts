import { describe, expect, it } from 'vitest'
import { createArtifactPreviewWorkbenchItem, isFixedArtifactPreview } from './artifactItems'
import { workbenchTabIsHistorical, workbenchTabSourceLabel, workbenchTabSourcePath } from './workbenchTabPresentation'

function document(id: string, sourcePath: string, historical = false) {
  return createArtifactPreviewWorkbenchItem({
    artifact: { name: 'report.docx', sourcePath },
    resourceIdentity: `document:${id}`, sessionKey: 'session-a', nativeHtml: false, historical,
  })
}

describe('Workbench file identity presentation', () => {
  it('distinguishes same-name files by their authorized path without changing identity', () => {
    const original = document('original', 'preview-gallery/report.docx')
    const copy = document('copy', 'unregistered/report.docx')
    expect(original.id).not.toBe(copy.id)
    expect(workbenchTabSourceLabel(original, [original, copy])).toBe('preview-gallery')
    expect(workbenchTabSourceLabel(copy, [original, copy])).toBe('unregistered')
    expect(workbenchTabSourcePath(copy)).toBe('unregistered/report.docx')
    expect(workbenchTabSourceLabel(original, [original])).toBe('')
  })

  it('keeps historical revisions fixed even when they retain document authority', () => {
    const current = document('one', 'reports/report.docx')
    const history = document('one:revision:old', 'reports/report.docx', true)
    expect(workbenchTabIsHistorical(history)).toBe(true)
    expect((history.payload.artifact as Record<string, unknown>).workbenchOpenIntent).toBe('history')
    expect(isFixedArtifactPreview(history)).toBe(true)
    expect(isFixedArtifactPreview(current)).toBe(false)
    expect(workbenchTabSourceLabel(history, [current, history])).toBe('')
    expect(history.id).not.toBe(current.id)
  })
})
