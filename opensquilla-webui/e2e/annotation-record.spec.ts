import { expect, test } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { MAIN, installGateway, logs } from './support/real-conversation-gateway'
import { docxFixture, officeHash, pptxFixture, xlsxFixture } from './support/office-fixtures'

test.afterEach(async ({ page }, testInfo) => {
  const path = testInfo.outputPath('gateway-and-render-events.json')
  await writeFile(path, JSON.stringify(logs.get(page), null, 2))
  await testInfo.attach('gateway-and-render-events', { contentType: 'application/json', path })
})

// The routed application owns the workbench, annotation store and composer.
// Only Gateway responses and this synthetic Office file are isolated.
const formats = {
  docx: { bytes: () => docxFixture('Annotation fixture text'), mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
  pptx: { bytes: () => pptxFixture('Annotation fixture title'), mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' },
  xlsx: { bytes: xlsxFixture, mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
}
const realWorkspace = process.env.OPENSQUILLA_ANNOTATION_FIXTURE_WORKSPACE
const realNames = { docx: '26-report.docx', pptx: '27-deck.pptx', xlsx: '28-workbook.xlsx' }
for (const format of ['docx', 'pptx', 'xlsx'] as const) {
test(`${format} annotation records on the right and sends only from the left composer`, async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1560, height: 1000 })
  await page.addInitScript(() => {
    window.OPENSQUILLA_FEATURES = { ...(window.OPENSQUILLA_FEATURES || {}),
      artifactWorkbench: true, officePromptAnnotations: true }
  })
  const gateway = await installGateway(page)
  const bytes = realWorkspace
    ? await readFile(join(realWorkspace, 'preview-gallery', realNames[format]))
    : formats[format].bytes()
  await testInfo.attach('office-file-identity', { contentType: 'application/json',
    body: JSON.stringify({ format, source: realWorkspace ? realNames[format] : 'generated fixture',
      size: bytes.byteLength, sha256: officeHash(bytes) }) })
  const documentId = 'doc_annotation_record'
  const name = `annotation.${format}`, workspacePath = `outputs/${name}`
  const artifact = gateway.registerDocument(MAIN, documentId, name, {
    format, mime: formats[format].mime, workspacePath,
    sha256: officeHash(bytes), size: bytes.byteLength, annotation: true,
  })
  gateway.appendHistory(MAIN, { role: 'assistant', id: 'annotation-delivery',
    text: `Review this file. [Open workspace file](${workspacePath})`, artifacts: [artifact] })
  await page.route('**/api/v1/artifact-documents/doc_annotation_record**', route =>
    route.fulfill({ contentType: artifact.mime, body: bytes, headers: { 'content-length': String(bytes.byteLength) } }))
  await page.route('**/api/v1/artifacts/art-doc_annotation_record**', route =>
    route.fulfill({ contentType: artifact.mime, body: bytes, headers: { 'content-length': String(bytes.byteLength) } }))
  await page.route('**/api/v1/workspace-files/resolve', route =>
    route.fulfill({ json: { workspaceBinding: 'fixture-workspace', files: [{
      requestedPath: workspacePath, path: workspacePath, name, mime: artifact.mime, size: bytes.byteLength,
      kind: 'download', revision: 'fixture-file-revision', documentTarget: { documentId },
      annotationCapability: { supported: true },
    }] } }))
  await page.route('**/api/v1/workspace-files/content?**', route =>
    route.fulfill({ contentType: artifact.mime, body: bytes, headers: { 'content-length': String(bytes.byteLength) } }))
  await page.goto(`/control/chat?session=${encodeURIComponent(MAIN)}`)
  await page.locator('.msg-artifact-chip', { hasText: name }).getByRole('button', { name: `Open ${name}` }).click()
  const workbench = page.getByTestId('workbench-host')
  await expect(workbench).toBeVisible()
  await expect(workbench.locator('.office-preview__canvas canvas').first()).toBeVisible({ timeout: 60_000 })
  await workbench.getByRole('button', { name: 'Annotate', exact: true }).click()
  await expect.poll(() => gateway.requests.filter(request => request.method === 'workbench.resources.open'
    && request.params?.intent === 'annotate').length).toBe(1)
  await expect(workbench.locator('.office-preview__guide')).toBeVisible()
  if (format === 'pptx') {
    await workbench.locator('[data-ooxml-selection-run]').first().click()
  } else if (format === 'docx') {
    const text = workbench.locator('[data-ooxml-selection-run]').first()
    const rect = (await text.boundingBox())!
    await page.mouse.move(rect.x + 2, rect.y + rect.height / 2)
    await page.mouse.down()
    await page.mouse.move(rect.x + rect.width * .8, rect.y + rect.height / 2, { steps: 12 })
    await page.mouse.up()
  } else {
    const rect = (await workbench.locator('.office-preview__canvas canvas').first().boundingBox())!
    await page.mouse.move(rect.x + 70, rect.y + 70)
    await page.mouse.down()
    await page.mouse.move(rect.x + 425, rect.y + 155, { steps: 12 })
    await page.mouse.up()
  }
  const card = workbench.locator('.annotation-comment')
  await expect(card).toBeVisible()
  const highlight = workbench.locator('[data-office-selected]')
  await expect(highlight).toBeVisible()
  await expect.poll(async () => {
    const target = await highlight.boundingBox(), editor = await card.boundingBox()
    if (!target || !editor) return false
    const gap = Math.hypot(Math.max(0, target.x - editor.x - editor.width, editor.x - target.x - target.width),
      Math.max(0, target.y - editor.y - editor.height, editor.y - target.y - target.height))
    return gap >= 5 && gap <= 12
  }, { message: 'Opening the editor preserves the selected target and places the card beside it.' }).toBe(true)
  const viewportBounds = (await workbench.locator('.office-preview__viewport').boundingBox())!
  const editorBounds = (await card.boundingBox())!
  expect(editorBounds.x).toBeGreaterThanOrEqual(viewportBounds.x - 1)
  expect(editorBounds.y).toBeGreaterThanOrEqual(viewportBounds.y - 1)
  expect(editorBounds.x + editorBounds.width).toBeLessThanOrEqual(viewportBounds.x + viewportBounds.width + 1)
  expect(editorBounds.y + editorBounds.height).toBeLessThanOrEqual(viewportBounds.y + viewportBounds.height + 1)
  const instruction = `Update the selected ${format} content.`
  let reselectedText = ''
  await card.locator('textarea').fill(instruction)
  await card.locator('.annotation-comment__record').click()
  const recorded = page.getByTestId('composer-prompt-annotations')
  await expect(recorded).toContainText(instruction)
  await expect(card).not.toBeVisible()
  await expect(workbench.locator('.annotation-summary')).toHaveCount(0)
  await expect(workbench.locator('.office-preview__hint')).toHaveCount(0)
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
  if (format === 'pptx') {
    await page.locator('#app-main').getByRole('link', { name: `Open ${workspacePath}`, exact: true }).click()
    // An associated workspace path resolves to the same current Document tab.
    await expect(workbench.locator('.artifact-document')).toBeVisible()
    await expect(workbench.locator('.workbench-host__tab').filter({ hasText: name })).toHaveCount(1)
    await workbench.getByRole('button', { name: 'Annotate', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'workbench.resources.open'
      && request.params?.intent === 'annotate').length).toBe(2)
    const prepared = gateway.requests.filter(request => request.method === 'workbench.resources.open'
      && request.params?.intent === 'annotate').at(-1)!
    expect(prepared.params).toMatchObject({ resourceRef: { type: 'document', documentId } })
    expect(prepared.params).not.toHaveProperty('workspaceFile')
    await expect(workbench.locator('.office-preview__guide')).toBeVisible()
    await expect(recorded.locator('.chat-prompt-annotation-chip')).toHaveCount(1)
    await expect(recorded).toContainText(instruction)
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
    const originalDraftId = await recorded.locator('.chat-prompt-annotation-chip').getAttribute('data-annotation-id')
    expect(originalDraftId).toBeTruthy()
    await recorded.locator('.chat-prompt-annotation-chip__main').click()
    await expect(card).toBeVisible()
    await expect(card.locator('textarea')).toHaveValue(instruction)
    await card.getByRole('button', { name: 'Select again', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'workbench.resources.open'
      && request.params?.intent === 'annotate').length).toBe(3)
    expect(gateway.requests.filter(request => request.method === 'workbench.resources.open'
      && request.params?.intent === 'annotate').at(-1)!.params).toMatchObject({
        resourceRef: { type: 'document', documentId },
      })
    await expect(workbench.locator('.office-preview__guide')).toBeVisible()
    const nextTarget = workbench.locator('[data-ooxml-selection-run]').nth(1)
    reselectedText = (await nextTarget.textContent())!.trim()
    await nextTarget.click()
    await expect(card).toBeVisible()
    await expect(card.locator('textarea')).toHaveValue(instruction)
    await card.locator('.annotation-comment__record').click()
    await expect(card).not.toBeVisible()
    await expect(recorded.locator('.chat-prompt-annotation-chip')).toHaveCount(1)
    await expect(recorded.locator('.chat-prompt-annotation-chip')).not.toHaveAttribute('data-annotation-id', originalDraftId!)
    await expect(recorded).toContainText(instruction)
    await expect(recorded.locator('.chat-prompt-annotation-chip__target')).toContainText(reselectedText)
    await expect(recorded.locator('.chat-prompt-annotation-chip__target')).not.toContainText('Annotation fixture title')
    await expect(workbench.locator('.annotation-summary')).toHaveCount(0)
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
  }
  await page.screenshot({ path: testInfo.outputPath('recorded-before-send.png') })
  const composer = page.locator('#app-main .chat-inline-editor')
  const sendButton = page.locator('#app-main .chat-send-btn.btn--primary')
  await expect(composer).toHaveText('')
  await expect(sendButton).toBeEnabled()
  if (format === 'docx') {
    // A rejected admission must preserve the annotation-only input and allow
    // an explicit retry from the same left composer, without re-recording.
    gateway.rejectNextSend(MAIN)
    await sendButton.click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    await expect(recorded).toContainText(instruction)
    await expect(sendButton).toBeEnabled()
    await expect(composer).toHaveText('')
    await page.screenshot({ path: testInfo.outputPath('rejected-send-draft-preserved.png') })
  }
  await sendButton.click()
  await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length)
    .toBe(format === 'docx' ? 2 : 1)
  const send = gateway.requests.filter(request => request.method === 'chat.send').at(-1)!.params!
  expect(send.pageContext).toMatchObject({ resourceId: `document:${documentId}`, revisionId: `rev-${documentId}` })
  expect(JSON.stringify(send.pageContext)).toContain(instruction)
  expect(JSON.stringify(send.pageContext)).toContain(`"format":"${format}"`)
  if (format === 'pptx') {
    expect(send.pageContext).toMatchObject({ annotations: [{
      text: instruction, selectionText: reselectedText,
      selection: { format: 'pptx', kind: 'element', text: reselectedText },
    }] })
    if (!realWorkspace) expect(send.pageContext).toMatchObject({
      annotations: [{ selection: { shapeId: '5' } }],
    })
  }
  await expect(recorded.locator('.chat-prompt-annotation-chip')).toHaveCount(0)
  expect(gateway.errors).toEqual([])
})
}
