import assert from 'node:assert/strict'
import { NativeWorkbenchPdfResourcePolicy } from '../dist/native-workbench-pdf-policy.js'

const pdfUrl = 'https://example.test/document.pdf'
const viewer = 'chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html'
const embedderCss = 'chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/pdf_embedder.css'
const resources = 'chrome://resources/js/assert.js'
const pageFrame = { frameTreeNodeId: 1, url: 'https://example.test/page', parent: null }
const pdfFrame = { frameTreeNodeId: 2, url: pdfUrl, parent: pageFrame }
const viewerFrame = { frameTreeNodeId: 3, url: viewer, parent: pdfFrame }
const policy = new NativeWorkbenchPdfResourcePolicy()
const request = (url, frame, resourceType, overrides = {}) => policy.requestAllowed({
  url, method: 'GET', resourceType, webContentsId: 10, frame, ...overrides,
})
const response = (overrides = {}) => ({
  url: pdfUrl, statusCode: 200, resourceType: 'subFrame', webContentsId: 10,
  frame: pdfFrame, responseHeaders: { 'Content-Type': ['application/pdf'] }, ...overrides,
})

assert.equal(request(embedderCss, pdfFrame, 'stylesheet'), false)
assert.equal(request(viewer, { ...viewerFrame, url: 'about:blank' }, 'subFrame'), false)
assert.equal(request(resources, viewerFrame, 'script'), false)
policy.observeResponse(response())
assert.equal(request(embedderCss, pdfFrame, 'stylesheet'), true)
assert.equal(request(viewer, { ...viewerFrame, url: 'about:blank' }, 'subFrame'), true)
assert.equal(request(viewer, viewerFrame, 'subFrame'), false)
assert.equal(request(resources, viewerFrame, 'script'), true)
assert.equal(request('chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/main.js',
  viewerFrame, 'script'), true)

for (const [url, frame, resourceType, overrides] of [
  [embedderCss, pageFrame, 'stylesheet'],
  [embedderCss, pdfFrame, 'script'],
  [embedderCss, pdfFrame, 'stylesheet', { method: 'POST' }],
  [embedderCss, pdfFrame, 'stylesheet', { webContentsId: 11 }],
  [viewer, { ...viewerFrame, url: 'about:blank', parent: pageFrame }, 'subFrame'],
  [viewer, { ...viewerFrame, url: 'about:blank' }, 'mainFrame'],
  [resources, { ...viewerFrame, parent: pageFrame }, 'script'],
  [resources, { ...viewerFrame, url: 'https://example.test/page' }, 'script'],
  [resources, viewerFrame, 'mainFrame'],
  ['chrome://settings/passwords', viewerFrame, 'script'],
  ['chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/main.js', viewerFrame, 'script'],
  ['chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai.evil.test/main.js',
    viewerFrame, 'script'],
  ['chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai:123/main.js', viewerFrame, 'script'],
]) assert.equal(request(url, frame, resourceType, overrides), false, url)

policy.forgetWebContents(10)
assert.equal(request(embedderCss, pdfFrame, 'stylesheet'), false)

for (const invalid of [
  response({ statusCode: 404 }),
  response({ responseHeaders: { 'Content-Type': ['text/html'] } }),
  response({ responseHeaders: { 'Content-Type': ['application/pdf'],
    'Content-Disposition': ['attachment; filename="document.pdf"'] } }),
  response({ url: 'file:///private/document.pdf' }),
  response({ resourceType: 'xhr' }),
]) {
  policy.observeResponse(invalid)
  assert.equal(request(embedderCss, pdfFrame, 'stylesheet'), false)
}

policy.observeResponse(response())
assert.equal(request(embedderCss, pdfFrame, 'stylesheet'), true)
assert.equal(policy.isViewerResourceRequest({ url: resources, method: 'GET',
  resourceType: 'script', webContentsId: 10, frame: viewerFrame }), true)
assert.equal(policy.isViewerResourceRequest({ url: 'chrome://settings/passwords', method: 'GET',
  resourceType: 'script', webContentsId: 10, frame: viewerFrame }), false)
request('https://example.test/next.html', pdfFrame, 'subFrame')
assert.equal(request(embedderCss, pdfFrame, 'stylesheet'), false)

policy.observeResponse(response())
assert.equal(request(embedderCss, pdfFrame, 'stylesheet'), true)
request('https://example.test/another.html', pageFrame, 'mainFrame')
assert.equal(request(embedderCss, pdfFrame, 'stylesheet'), false)

for (let id = 100; id < 165; id++) {
  policy.observeResponse(response({ frame: { ...pdfFrame, frameTreeNodeId: id } }))
}
assert.equal(request(embedderCss, { ...pdfFrame, frameTreeNodeId: 100 }, 'stylesheet'), false)
assert.equal(request(embedderCss, { ...pdfFrame, frameTreeNodeId: 164 }, 'stylesheet'), true)

policy.clear()
policy.observeResponse(response({ frame: { ...pdfFrame, url: 'about:blank' } }))
assert.equal(request(embedderCss, pdfFrame, 'stylesheet'), true,
  'HTTP response evidence precedes the PDF document commit')

for (const dynamicUrl of ['blob:https://example.test/synthetic-pdf',
  'data:application/pdf;base64,JVBERi0xLjQK']) {
  policy.clear()
  const dynamicFrame = { ...pdfFrame, url: dynamicUrl }
  const dynamicViewer = { ...viewerFrame, parent: dynamicFrame, url: 'about:blank' }
  const details = { url: viewer, method: 'GET', resourceType: 'subFrame',
    webContentsId: 10, frame: dynamicViewer }
  const tree = (mimeType, children = []) => ({ frameTree: { frame: { url: pageFrame.url,
    mimeType: 'text/html' }, childFrames: [{ frame: { url: dynamicUrl, mimeType } }, ...children] } })
  assert.equal(policy.needsFrameTree(details), true)
  policy.observeFrameTree(details, tree('text/html'))
  assert.equal(request(viewer, dynamicViewer, 'subFrame'), false)
  policy.observeFrameTree(details, tree('application/pdf', [{ frame: { url: dynamicUrl,
    mimeType: 'text/html' } }]))
  assert.equal(request(viewer, dynamicViewer, 'subFrame'), false,
    'A duplicated URL must not select a PDF ahead of an ambiguous HTML frame')
  policy.observeFrameTree(details, { frameTree: { frame: { url: 'https://other.example.test/',
    mimeType: 'text/html' }, childFrames: [{ frame: { url: dynamicUrl, mimeType: 'application/pdf' } }] } })
  assert.equal(request(viewer, dynamicViewer, 'subFrame'), false, 'The complete ancestry must match')
  policy.observeFrameTree(details, tree('application/pdf', Array.from({ length: 1024 }, (_, index) =>
    ({ frame: { url: `https://example.test/extra-${index}`, mimeType: 'text/html' } }))))
  assert.equal(request(viewer, dynamicViewer, 'subFrame'), false,
    'Exceeding the canonical frame-tree budget must fail closed')
  policy.observeFrameTree(details, tree('application/pdf', [{ frame: { url: dynamicUrl,
    mimeType: 'application/pdf' } }]))
  assert.equal(request(viewer, dynamicViewer, 'subFrame'), true)
  assert.equal(request(embedderCss, dynamicFrame, 'stylesheet'), true)
  assert.equal(request(resources, { ...dynamicViewer, url: viewer }, 'script'), true)
  assert.equal(request(resources, { ...dynamicViewer, url: viewer }, 'script',
    { webContentsId: 11 }), false, 'The canonical tree cannot grant another target')
  assert.equal(request('chrome://settings/', { ...dynamicViewer, url: viewer }, 'script'), false)
  request('data:text/html,synthetic', dynamicFrame, 'subFrame')
  assert.equal(request(embedderCss, dynamicFrame, 'stylesheet'), false,
    'A new dynamic document clears its predecessor grant')
}

console.log('Browser PDF resource policy tests passed')
