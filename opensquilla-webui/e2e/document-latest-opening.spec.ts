import { expect, test, type Page } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { inflateRawSync } from 'node:zlib'
import { MAIN, installGateway, logs } from './support/real-conversation-gateway'
import { docxFixture, officeHash } from './support/office-fixtures'

// Set OPENSQUILLA_DOCUMENT_FIXTURE_WORKSPACE to reports/annotate-live/workspace
// to exercise the actual original and edited DOCX files from the live test
// conversation. The document renderer and application are always real; only
// Gateway/model responses are isolated, so these tests never send to a provider.
const workspace = process.env.OPENSQUILLA_DOCUMENT_FIXTURE_WORKSPACE
const name = '26-report.docx'
const originalPath = `preview-gallery/${name}`
const editedPath = `unregistered/${name}`
const documentId = 'doc_latest_opening'
const independentId = 'doc_independent_copy'
const mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const titles = new WeakMap<Page, { original: string; edited: string }>()

function firstParagraph(bytes: Buffer): string {
  // Read the frozen file independently of the production Office parser. The
  // live fixture may be edited while a developer runs these acceptance tests.
  const end = bytes.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]))
  let offset = bytes.readUInt32LE(end + 16)
  const count = bytes.readUInt16LE(end + 10)
  for (let index = 0; index < count; index++) {
    const nameLength = bytes.readUInt16LE(offset + 28)
    const extraLength = bytes.readUInt16LE(offset + 30), commentLength = bytes.readUInt16LE(offset + 32)
    if (bytes.subarray(offset + 46, offset + 46 + nameLength).toString() === 'word/document.xml') {
      const local = bytes.readUInt32LE(offset + 42)
      const start = local + 30 + bytes.readUInt16LE(local + 26) + bytes.readUInt16LE(local + 28)
      const payload = bytes.subarray(start, start + bytes.readUInt32LE(offset + 20))
      const xml = (bytes.readUInt16LE(offset + 10) === 8 ? inflateRawSync(payload) : payload).toString()
      for (const match of xml.matchAll(/<w:p(?:\s[^>]*|)>([\s\S]*?)<\/w:p>/g)) {
        const text = [...match[1]!.matchAll(/<w:t(?:\s[^>]*|)>([\s\S]*?)<\/w:t>/g)].map(item => item[1]).join('')
        if (text) return text.replaceAll('&amp;', '&').replaceAll('&lt;', '<').replaceAll('&gt;', '>')
          .replaceAll('&quot;', '"').replaceAll('&apos;', "'")
      }
    }
    offset += 46 + nameLength + extraLength + commentLength
  }
  throw new Error('DOCX fixture must contain a non-empty first paragraph.')
}

test.afterEach(async ({ page }, testInfo) => {
  const events = logs.get(page)
  if (!events) return
  const path = testInfo.outputPath('gateway-and-render-events.json')
  await writeFile(path, JSON.stringify(events, null, 2))
  await testInfo.attach('gateway-and-render-events', { path, contentType: 'application/json' })
})

async function installDocuments(page: Page, options: {
  workspaceTree?: boolean; reference?: boolean; initialVersion?: 'original'
} = {}) {
  await page.setViewportSize({ width: 1700, height: 1080 })
  await page.addInitScript(() => {
    window.OPENSQUILLA_FEATURES = { ...(window.OPENSQUILLA_FEATURES || {}),
      artifactWorkbench: true, officePromptAnnotations: true }
  })
  const gateway = await installGateway(page, false, false, false, false, { workspace: options.workspaceTree })
  gateway.enableResourceCatalogue()
  const original = workspace ? await readFile(join(workspace, originalPath))
    : docxFixture('Synthetic preview report / 合成报告')
  const edited = workspace ? await readFile(join(workspace, editedPath))
    : docxFixture('File Preview & Annotation Test Report')
  titles.set(page, { original: firstParagraph(original), edited: firstParagraph(edited) })
  expect(titles.get(page)!.edited).not.toBe(titles.get(page)!.original)
  const register = (id: string, path: string, bytes: Buffer, revisionId: string, artifactId: string) =>
    gateway.registerDocument(MAIN, id, name, {
      format: 'docx', mime, workspacePath: path, sha256: officeHash(bytes),
      size: bytes.byteLength, annotation: true, revisionId, artifactId,
    })
  const first = register(documentId, editedPath, original, 'rev-before-edit', 'art-before-edit')
  let headIsEdited = options.initialVersion !== 'original'
  const advance = () => {
    headIsEdited = true
    return register(documentId, editedPath, edited, 'rev-after-edit', 'art-after-edit')
  }
  const current = headIsEdited ? advance() : null
  const independent = register(independentId, originalPath, original, 'rev-independent', 'art-independent')
  gateway.appendHistory(MAIN, { role: 'assistant', id: 'first-delivery',
    text: `Original delivery. [Open edited file](${editedPath})`, artifacts: [first] })
  if (current) gateway.appendHistory(MAIN, { role: 'assistant', id: 'latest-delivery',
    text: 'Edited the title of the same document.', artifacts: [current] })
  gateway.appendHistory(MAIN, { role: 'assistant', id: 'independent-delivery',
    text: `An independent same-name file. [Open independent file](${originalPath})`, artifacts: [independent] })
  if (options.reference) {
    const reference = { version: 1, kind: 'workspace_file', id: editedPath, label: editedPath,
      scope: { sessionKey: MAIN, workspaceId: 'fixture-workspace' },
      locator: { relativePath: editedPath, startLine: 1, endLine: 1 },
      state: { available: true, revision: `file_${officeHash(edited).slice(0, 16)}` }, capabilities: { open: true, copy: true } }
    // The real source-reference endpoint cannot decode Office ZIP bytes as
    // UTF-8. This must open through authorized workspace/document discovery.
    gateway.markBinaryReference(MAIN, editedPath)
    gateway.appendHistory(MAIN, { role: 'assistant', id: 'document-reference',
      text: 'Reference to the edited document.', tool_calls: [
        { type: 'tool_use', tool_use_id: 'inspect-document', name: 'read_source', input: { path: editedPath } },
        { type: 'tool_result', tool_use_id: 'inspect-document', name: 'read_source',
          result: JSON.stringify({ status: 'success', reference }), execution_status: { status: 'success' } },
      ] })
  }

  const bytesByArtifact = new Map([
    ['art-before-edit', original], ['art-after-edit', edited], ['art-independent', original],
  ])
  const fileResolveRequests: Array<{ paths: string[]; reference?: unknown }> = []
  await page.route('**/api/v1/artifacts/*', route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!
    return route.fulfill({ contentType: mime, body: bytesByArtifact.get(id)! })
  })
  await page.route('**/api/v1/artifact-documents/**', route => {
    const url = new URL(route.request().url())
    const id = url.pathname.split('/').at(-1)
    const bytes = id === independentId || url.searchParams.get('revisionId') === 'rev-before-edit'
      || !url.searchParams.has('revisionId') && !headIsEdited
      ? original : edited
    return route.fulfill({ contentType: mime, body: bytes })
  })
  await page.route('**/api/v1/workspace-files/resolve', route => {
    const request = route.request().postDataJSON() as { paths: string[]; reference?: unknown }
    fileResolveRequests.push(request)
    return route.fulfill({ json: { workspaceBinding: 'fixture-workspace',
      ...(request.reference ? { referenceScopeValidated: true } : {}),
      files: request.paths.map(path => ({
        requestedPath: path, path, name, mime,
        size: path === editedPath ? edited.byteLength : original.byteLength,
        kind: 'download', revision: `file-${path}`,
        documentTarget: { documentId: path === editedPath ? documentId : independentId },
        annotationCapability: { supported: true },
      })) } })
  })
  await page.route('**/api/v1/workspace-files/content?**', route => {
    const path = new URL(route.request().url()).searchParams.get('path')
    return route.fulfill({ contentType: mime, body: path === editedPath ? edited : original })
  })
  if (options.workspaceTree) {
    await page.route('**/api/v1/workspace-files/list?**', route => {
      const path = new URL(route.request().url()).searchParams.get('path') || ''
      return route.fulfill({ json: {
        workspaceBinding: 'fixture-workspace', relativePath: path, nextCursor: null, hasMore: false, truncated: false,
        entries: path ? [{ name, path: `${path}/${name}`, type: 'file', kind: 'download',
          mime, size: edited.byteLength, mtimeNs: '1234567890' }]
          : ['preview-gallery', 'unregistered'].map(directory => ({
              name: directory, path: directory, type: 'directory', kind: 'directory', mime: '', size: null, mtimeNs: '1234567890',
            })),
      } })
    })
  }
  await page.goto(`/control/chat?session=${encodeURIComponent(MAIN)}`)
  return { gateway, advance, fileResolveRequests, fixtures: [
    { path: originalPath, size: original.byteLength, sha256: officeHash(original) },
    { path: editedPath, size: edited.byteLength, sha256: officeHash(edited) },
  ] }
}

function documentTabs(page: Page) {
  return page.getByTestId('workbench-host').locator('.workbench-host__tab').filter({ hasText: name })
}

async function expectTitle(page: Page, current: boolean) {
  const workbench = page.getByTestId('workbench-host')
  await expect(workbench.locator('.office-preview__canvas canvas').first()).toBeVisible({ timeout: 60_000 })
  // DOCX rendering splits text into source runs; inspect the real text layer as
  // a whole rather than relying on a synthetic DOM overlay or image matching.
  await expect.poll(async () => (await workbench.locator('[data-ooxml-selection-run]').allTextContents()).join(''))
    .toContain(current ? titles.get(page)!.edited : titles.get(page)!.original)
}

async function openDelivery(page: Page, index: number) {
  await page.locator('.msg-artifact-chip').filter({ hasText: name }).nth(index)
    .getByRole('button', { name: `Open ${name}`, exact: true }).click()
}

test('latest document, historical delivery and independent same-name file keep distinct identities', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const { gateway, fixtures } = await installDocuments(page)
  await testInfo.attach('real-docx-fixtures', { contentType: 'application/json', body: JSON.stringify(fixtures) })
  const workbench = page.getByTestId('workbench-host')

  // The old delivery opens the head without changing the preserved old bytes.
  await openDelivery(page, 0)
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)
  await openDelivery(page, 1)
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)

  await page.locator('#app-main').getByRole('link', { name: `Open ${editedPath}`, exact: true }).click()
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)

  // The catalogue counts two files, not three deliveries or revisions.
  const fileAction = page.getByTestId('chat-header-actions').locator('.chat-deliverables-btn').filter({ visible: true })
  await expect(fileAction).toContainText('2')
  await fileAction.click()
  const rows = workbench.locator('.resource-collection__item')
  await expect(rows).toHaveCount(2)
  await expect(rows.filter({ hasText: editedPath })).toHaveCount(1)
  await expect(rows.filter({ hasText: originalPath })).toHaveCount(1)
  await page.screenshot({ path: testInfo.outputPath('independent-files-and-sources.png') })
  await rows.filter({ hasText: editedPath }).locator('.resource-collection__open').click()
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)

  // Historical opening is explicit and remains a separate, clearly named tab.
  await page.locator('.msg-artifact-chip').filter({ hasText: name }).first()
    .locator('[aria-haspopup="menu"]').click()
  await page.getByRole('menuitem', { name: 'View delivered version', exact: true }).click()
  await expectTitle(page, false)
  await expect(documentTabs(page)).toHaveCount(2)
  await expect(workbench.locator('.workbench-host__tab[aria-selected="true"]')).toContainText(/histor/i)
  await page.screenshot({ path: testInfo.outputPath('historical-delivery.png') })
  await openDelivery(page, 0)
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(2)

  // A separate file with the same basename must not reuse the edited tab.
  await openDelivery(page, 2)
  await expectTitle(page, false)
  await expect(documentTabs(page)).toHaveCount(3)
  await expect(documentTabs(page).filter({ hasText: 'preview-gallery' })).toHaveCount(1)
  await expect(documentTabs(page).filter({ hasText: 'unregistered' })).toHaveCount(2)
  await page.screenshot({ path: testInfo.outputPath('same-name-source-tabs.png') })

  await openDelivery(page, 0)
  await expectTitle(page, true)
  await workbench.getByRole('button', { name: 'Annotate', exact: true }).click()
  await expect(workbench.locator('.office-preview__guide')).toBeVisible()
  await workbench.locator('[data-ooxml-selection-run]').first().click()
  const card = workbench.locator('.annotation-comment')
  await expect(card).toBeVisible()
  const instruction = 'Keep this instruction pending until I send from the left.'
  await card.locator('textarea').fill(instruction)
  await card.locator('.annotation-comment__record').click()
  await expect(page.getByTestId('composer-prompt-annotations')).toContainText(instruction)
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
  await page.locator('#app-main .chat-send-btn.btn--primary').click()
  await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
  const send = gateway.requests.find(request => request.method === 'chat.send')!.params!
  expect(send.pageContext).toMatchObject({ resourceId: `document:${documentId}`, revisionId: 'rev-after-edit' })
  expect(JSON.stringify(send.pageContext)).toContain(instruction)
  expect(gateway.errors).toEqual([])
})

test('opening a workspace path first and then its old delivery reuses the current document tab', async ({ page }) => {
  test.setTimeout(90_000)
  const { gateway } = await installDocuments(page)
  await page.locator('#app-main').getByRole('link', { name: `Open ${editedPath}`, exact: true }).click()
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)
  await openDelivery(page, 0)
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
  expect(gateway.errors).toEqual([])
})

test('workspace tree and reference card reuse the associated current document', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const { gateway, fileResolveRequests } = await installDocuments(page, { workspaceTree: true, reference: true })
  const workbench = page.getByTestId('workbench-host')
  await openDelivery(page, 0)
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)
  await page.getByTestId('workbench-new-panel-trigger').click()
  await page.getByTestId('workbench-new-panel-menu-list').locator('[data-panel-kind="workspace-files"]').click()
  const tree = page.getByTestId('workspace-files-panel')
  await tree.getByRole('treeitem', { name: 'Expand folder unregistered', exact: true }).click({ timeout: 15_000 })
  await tree.getByRole('treeitem', { name, exact: true }).click({ timeout: 15_000 })
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)
  await expect(documentTabs(page)).toHaveAttribute('aria-selected', 'true')
  await workbench.locator('.workbench-host__tab').filter({ hasText: 'Workspace files' }).click()
  await expect(tree).toBeVisible()
  await page.getByTestId('workspace-reference-card').locator('.workspace-reference__open').click({ timeout: 15_000 })
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)
  await expect(documentTabs(page)).toHaveAttribute('aria-selected', 'true')
  expect(fileResolveRequests.find(request => request.reference)).toMatchObject({ paths: [editedPath], reference: {
    scope: { sessionKey: MAIN, workspaceId: 'fixture-workspace' }, locator: { relativePath: editedPath },
  } })
  expect(gateway.requests.filter(request => request.method === 'workspaces.references.read')).toHaveLength(0)
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
  expect(gateway.errors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('tree-reference-current-document.png') })
})

test('version history explicitly opens the selected pinned revision', async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  const { gateway } = await installDocuments(page)
  const workbench = page.getByTestId('workbench-host')
  await openDelivery(page, 0)
  await expectTitle(page, true)
  await expect(workbench.locator('.artifact-document__current-version')).toContainText('Current v2')
  await workbench.getByRole('tab', { name: 'Version history (2)', exact: true }).click()
  const revisions = workbench.locator('.artifact-document__versions > li')
  await expect(revisions).toHaveCount(2)
  await revisions.last().locator('[data-artifact-action="view-revision"]').click()
  await expectTitle(page, false)
  await expect(documentTabs(page)).toHaveCount(2)
  await expect(workbench.locator('.workbench-host__tab[aria-selected="true"]')).toContainText('History')
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
  expect(gateway.errors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('explicit-version-history.png') })
})

test('reopening the old delivery refreshes a newer document head without creating another tab', async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  const { gateway, advance } = await installDocuments(page, { initialVersion: 'original' })
  await openDelivery(page, 0)
  await expectTitle(page, false)
  await expect(documentTabs(page)).toHaveCount(1)
  // No push notification or remount: the normal open action must discover
  // the new head even when this document was already previewed and cached.
  advance()
  await openDelivery(page, 0)
  await expectTitle(page, true)
  await expect(documentTabs(page)).toHaveCount(1)
  await expect(page.getByTestId('workbench-host').locator('.artifact-document__current-version')).toContainText('Current v2')
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
  expect(gateway.errors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('reopened-current-head.png') })
})
