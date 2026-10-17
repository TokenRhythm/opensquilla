import { expect, test, type BrowserContext, type Page } from '@playwright/test'
import { build, normalizePath } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFile } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { docxFixture, pptxFixture, xlsxFixture, pdfFixture, officeHash } from './support/office-fixtures'

// Use Chromium for the PDF/Office canvas and worker lifecycle contracts.
test.use({channel: 'chromium'})

// Real production Vue components, binary controller, Silurus parsers/renderers,
// and local fonts. Only the authenticated binary transport is replaced with a
// same-origin synthetic server. No Gateway, account, model, or Office is needed.
const ORIGIN = 'http://127.0.0.1:44179'
const BASE = '/office-fixture/'
const root = fileURLToPath(new URL('..', import.meta.url))
const assets = new Map<string, string | Uint8Array>()
const files = {
  docx: docxFixture(), pptx: pptxFixture(), xlsx: xlsxFixture(), pdf: pdfFixture(),
  updated: pptxFixture('中文更新验收'), large: docxFixture('长文档中文验收', 80), external: pptxFixture('外部图片不联网', 2, true),
  broken: Buffer.from('This is not an Office package'),
}
const mime: Record<string, string> = {
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', pdf: 'application/pdf',
}
let entryFile = ''
let styles: string[] = []

test.beforeAll(async () => {
  const source = (path: string) => JSON.stringify(normalizePath(resolve(root, path)))
  const entry = '\0office-preview-browser-fixture'
  const result = await build({
    root, configFile: false, logLevel: 'warn', base: BASE, publicDir: false,
    resolve: { alias: { '@': resolve(root, 'src'), '@office-preview-fonts': resolve(root, 'node_modules/.cache/office-preview-fonts') } },
    define: { __VUE_I18N_FULL_INSTALL__: 'true', __VUE_I18N_LEGACY_API__: 'false', __INTLIFY_PROD_DEVTOOLS__: 'false' },
    plugins: [vue(), {
      name: 'office-preview-browser-fixture',
      resolveId: id => id === entry ? entry : undefined,
      load: id => id === entry ? `
        import { createApp, h, ref, nextTick } from 'vue';
        import { createPinia } from 'pinia';
        import { createI18n } from 'vue-i18n';
        import chinese from ${source('src/locales/zh-Hans.json')};
        import OfficePreviewPanel from ${source('src/components/workbench/OfficePreviewPanel.vue')};
        import ArtifactPreviewPanel from ${source('src/components/workbench/ArtifactPreviewPanel.vue')};
        import { ARTIFACT_WORKBENCH_KEY } from ${source('src/modules/artifactWorkbench.ts')};
        import { createArtifactPreviewResource } from ${source('src/adapters/gateway/artifactPreviewResourceV4.ts')};
        import { useArtifactPromptAnnotationsStore } from ${source('src/stores/artifactPromptAnnotations.ts')};
        import { pageContextForAnnotations, pageAnnotationSnapshots } from ${source('src/types/pageContext.ts')};
        const current = ref(null), mounted = ref(true), suspended = ref(false);
        const state = { ready: 0, errors: [], selections: [], events: [], firstPaintMs: 0 };
        let started = 0;
        const http = { async requestBinary(endpoint, options) {
          const response = await fetch(endpoint, {signal: options?.signal});
          return { metadata: {status: response.status, contentType: response.headers.get('content-type'), contentLength: Number(response.headers.get('content-length'))},
            stream: () => response.body, blob: () => response.blob() };
        }};
        const app = createApp({ setup() { return () => current.value && mounted.value
          ? current.value.format === 'pdf' || current.value.panel
            ? h(ArtifactPreviewPanel, {artifact: current.value.artifact, showHeader: false, sessionKey: 'office-fixture', suspended: suspended.value,
                officeAnnotationEnabled: true, onWorkbenchEvent: event => state.events.push(event)})
            : h(OfficePreviewPanel, {key: current.value.key, data: current.value.data, format: current.value.format,
                annotationEnabled: true, suspended: suspended.value,
                onReady() {state.ready++; state.firstPaintMs = performance.now() - started;},
                onError(error) {state.errors.push(String(error));},
                onSelectionRequest(selection) {state.selections.push(selection);}})
          : h('p', 'Preview closed'); }});
        app.use(createPinia());
        app.use(createI18n({legacy: false, locale: 'zh-Hans', fallbackLocale: false, missingWarn: false, fallbackWarn: false, messages: {'zh-Hans': chinese}}));
        app.provide(ARTIFACT_WORKBENCH_KEY, {previews: {createResource: options => createArtifactPreviewResource(http, {...options, baseOrigin: () => location.origin})}});
        app.mount('#app');
        window.officeFixture = {
          state,
          async open(format, name = format, panel = false, badHash = false) {
            mounted.value = false; await nextTick();
            state.errors = []; state.selections = []; state.events = []; window.__officeDraws.length = 0; started = performance.now();
            const response = await fetch('${BASE}files/' + name);
            const data = await response.arrayBuffer();
            const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data))).map(v => v.toString(16).padStart(2, '0')).join('');
            current.value = {format, key: hash, data, panel, artifact: {id:'office-fixture', name:'sample.' + format, mime:response.headers.get('content-type'), size:data.byteLength,
              sha256:badHash ? '0'.repeat(64) : hash, revisionId:'revision-' + name, download_url:'${BASE}files/' + name}};
            suspended.value = false; mounted.value = true; await nextTick();
          },
          async close() {mounted.value = false; await nextTick();},
          async suspend(value) {suspended.value = value; await nextTick();},
          current: () => ({format: current.value?.format, sha256:current.value?.key}),
          annotations: () => useArtifactPromptAnnotationsStore(),
          async saveSelection(body) {
            const selection = state.selections.at(-1);
            if (!selection) throw new Error('Select content before creating an annotation');
            const store = useArtifactPromptAnnotationsStore();
            const annotationId = 'office-annotation-' + Date.now();
            await store.create({annotationId, sessionKey:'office-fixture', documentId:'synthetic-document',
              documentName:'中文验收文件', resourceId:'document:synthetic-document',
              revisionId:current.value.artifact.revisionId, sha256:current.value.key, body, officeSelection:selection,
              selection:{selectionId:annotationId, tagName:selection.kind, elementPath:'',
                selectionText:selection.text || selection.range || '', locatorHint:selection.shapeId || selection.range}});
            return pageContextForAnnotations(store.snapshotsForIds([annotationId]));
          },
          pageContextForAnnotations, pageAnnotationSnapshots,
        };
      ` : undefined,
    }],
    build: { write: false, minify: false, assetsInlineLimit: 0, rollupOptions: { input: entry } },
  })
  for (const output of Array.isArray(result) ? result : [result]) {
    if (!('output' in output)) throw new Error('Fixture build unexpectedly started a watcher')
    for (const asset of output.output) {
      assets.set(BASE + asset.fileName, asset.type === 'chunk' ? asset.code : asset.source)
      if (asset.type === 'chunk' && asset.isEntry) entryFile = asset.fileName
    }
  }
  styles = [...assets.keys()].filter(name => name.endsWith('.css'))
  expect(entryFile).not.toBe('')
})

async function prepare(context: BrowserContext): Promise<Page> {
  await context.addInitScript(() => {
    const original = window.Worker
    let active = 0
    Object.defineProperty(window, '__officeWorkers', {get: () => active})
    window.Worker = class extends original {
      private closed = false
      constructor(url: string | URL, options?: WorkerOptions) {super(url, options); active++}
      terminate() {if (!this.closed) {this.closed = true; active--}; super.terminate()}
    }
    const paint = CanvasRenderingContext2D.prototype.fillText
    const draws: {text: string; font: string}[] = []
    ;(window as any).__officeDraws = draws
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y, maxWidth) {
      if (draws.length < 10000 && /[\u3400-\u9fff]/u.test(text)) draws.push({text, font: this.font})
      return maxWidth === undefined ? paint.call(this, text, x, y) : paint.call(this, text, x, y, maxWidth)
    }
  })
  await context.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== ORIGIN) return route.abort('blockedbyclient')
    const pathname = url.pathname
    const known = assets.get(pathname)
    if (known !== undefined) {
      const extension = extname(pathname)
      const contentType = extension === '.wasm' ? 'application/wasm' : extension === '.css' ? 'text/css' : /\.(otf|ttf|woff2?)$/.test(extension) ? 'application/octet-stream' : 'text/javascript'
      return route.fulfill({body: typeof known === 'string' ? known : Buffer.from(known), contentType})
    }
    const name = pathname.startsWith(BASE + 'files/') ? pathname.slice((BASE + 'files/').length) : ''
    if (name in files) {
      const body = files[name as keyof typeof files]
      const format = name === 'updated' || name === 'external' ? 'pptx' : name === 'large' || name === 'broken' ? 'docx' : name
      return route.fulfill({body, contentType: mime[format], headers: {'content-length': String(body.length)}})
    }
    if (pathname === BASE || pathname === BASE + 'index.html') {
      return route.fulfill({contentType: 'text/html', headers: {
        'Content-Security-Policy': "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'; img-src 'self' blob: data:; frame-src 'self' blob: data:",
      }, body: `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">${styles.map(href => `<link rel="stylesheet" href="${href}">`).join('')}<style>html,body,#app{height:100%;margin:0}body{font-family:system-ui;background:#eee;color:#222}#app{height:900px;width:1280px}</style></head><body><div id="app"></div><script type="module" src="${BASE + entryFile}"></script></body></html>`})
    }
    const publicRoot = resolve(root, 'public-assets')
    const path = resolve(publicRoot, pathname.slice(BASE.length))
    if (pathname.startsWith(BASE) && path.startsWith(publicRoot + sep)) {
      try {return await route.fulfill({body: await readFile(path), contentType: /\.json$/.test(path) ? 'application/json' : 'application/octet-stream'})} catch {}
    }
    return route.fulfill({status: 404, body: 'Unknown synthetic fixture asset'})
  })
  const page = await context.newPage()
  await page.setViewportSize({width: 1280, height: 960})
  await page.goto(ORIGIN + BASE)
  await page.waitForFunction(() => !!(window as any).officeFixture)
  return page
}

async function open(page: Page, format: string, name = format) {
  const prior = await page.evaluate(() => (window as any).officeFixture.state.ready)
  await page.evaluate(async ({format, name}) => (window as any).officeFixture.open(format, name), {format, name})
  if (format !== 'pdf') {
    await page.waitForFunction(prior => (window as any).officeFixture.state.ready > prior || (window as any).officeFixture.state.errors.length, prior, {timeout: 60_000})
    expect(await page.evaluate(() => (window as any).officeFixture.state.errors)).toEqual([])
    await expect(page.locator('canvas').first()).toBeVisible()
  }
}

test('Chinese DOCX/PPTX/XLSX use offline fonts and destroy their workers', async ({context}, testInfo) => {
  test.setTimeout(180_000)
  const page = await prepare(context)
  const errors: string[] = []
  const external: string[] = []
  page.on('pageerror', error => errors.push(String(error)))
  page.on('request', request => {if (!request.url().startsWith(ORIGIN) && !request.url().startsWith('blob:')) external.push(request.url())})
  for (const format of ['docx', 'pptx', 'xlsx']) {
    await open(page, format)
    await page.screenshot({path: testInfo.outputPath(`chinese-${format}.png`)})
    const fonts = await page.evaluate(() => Array.from(document.fonts).map(face => ({family: face.family, status: face.status})))
    expect(fonts.some(face => /Noto/.test(face.family) && face.status === 'loaded')).toBe(true)
    expect(await page.evaluate(() => (window as any).__officeDraws.some((draw: any) => /Noto/.test(draw.font)))).toBe(true)
    await page.evaluate(() => (window as any).officeFixture.close())
    await expect.poll(() => page.evaluate(() => (window as any).__officeWorkers)).toBe(0)
    await expect(page.locator('canvas')).toHaveCount(0)
  }
  expect(errors).toEqual([])
  expect(external).toEqual([])
})

test('PPTX title selection is produced by a real click and version replacement clears focus', async ({context}, testInfo) => {
  const page = await prepare(context)
  await open(page, 'pptx')
  const title = page.getByText('中文预览验收', {exact: true})
  await expect(title).toBeVisible()
  await page.getByRole('button', {name: '请求修改', exact: true}).click()
  await title.click()
  await expect.poll(() => page.evaluate(() => (window as any).officeFixture.state.selections.length)).toBe(1)
  await expect(page.getByRole('button', {name: '请求修改', exact: true})).toHaveAttribute('aria-pressed', 'true')
  const selections = await page.evaluate(() => (window as any).officeFixture.state.selections)
  expect(selections).toHaveLength(1)
  expect(selections[0]).toMatchObject({format: 'pptx', kind: 'element', slideIndex: 0, shapeId: '4'})
  const contextPayload = await page.evaluate(() => (window as any).officeFixture.saveSelection('将标题改为十月报告'))
  expect(contextPayload).toMatchObject({resourceId:'document:synthetic-document', revisionId:'revision-pptx', sha256:officeHash(files.pptx), annotations:[{text:'将标题改为十月报告', selection:{format:'pptx', kind:'element', shapeId:'4'}}]})
  const restored = await page.evaluate(payload => (window as any).officeFixture.pageAnnotationSnapshots(payload), contextPayload)
  expect(restored[0]).toMatchObject({revisionId:'revision-pptx', sha256:officeHash(files.pptx), selection:{shapeId:'4'}})
  await page.screenshot({path: testInfo.outputPath('pptx-selected.png')})
  await open(page, 'pptx', 'updated')
  await expect(page.getByText('中文更新验收', {exact: true})).toBeVisible()
  expect(await page.evaluate(() => (window as any).officeFixture.state.selections)).toEqual([])
  expect(await page.evaluate(() => (window as any).officeFixture.current().sha256)).toBe(officeHash(files.updated))
  // A new render must never rewrite an existing draft's original source version.
  const drafts = await page.evaluate(() => (window as any).officeFixture.annotations().draftsForSession('office-fixture'))
  expect(drafts[0]).toMatchObject({revisionId:'revision-pptx', sha256:officeHash(files.pptx)})
})

test('DOCX Chinese native drag selection reaches the modification request', async ({context}) => {
  const page = await prepare(context)
  await open(page, 'docx')
  const text = page.getByText('请选择这段中文提出修改要求。', {exact: true}).first()
  await expect(text).toBeVisible()
  await page.getByRole('button', {name: '请求修改', exact: true}).click()
  const box = await text.boundingBox()
  if (!box) throw new Error('DOCX text has no selection surface')
  await page.mouse.move(box.x + 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width - 2, box.y + box.height / 2, {steps: 15})
  await page.mouse.up()
  await expect.poll(() => page.evaluate(() => (window as any).officeFixture.state.selections.length)).toBe(1)
  const selection = await page.evaluate(() => (window as any).officeFixture.state.selections.at(-1))
  expect(selection).toMatchObject({format: 'docx', kind: 'text'})
  expect(JSON.stringify(selection)).toContain('中文')
})

test('PDF uses the shared canvas renderer and releases its worker on close', async ({context}) => {
  const page = await prepare(context)
  await open(page, 'pdf')
  await expect(page.locator('.pdf-preview canvas')).toBeVisible()
  await expect(page.locator('iframe')).toHaveCount(0)
  await page.evaluate(() => (window as any).officeFixture.close())
  await expect.poll(() => page.evaluate(() => (window as any).__officeWorkers)).toBe(0)
  expect(await page.evaluate(() => Array.from(document.fonts).length)).toBe(0)
})

test('XLSX real cell selection keeps its worksheet and Chinese text in chat context', async ({context}, testInfo) => {
  const page = await prepare(context)
  await open(page, 'xlsx')
  await page.getByRole('button', {name: '请求修改', exact: true}).click()
  const canvas = page.locator('canvas').first()
  const box = await canvas.boundingBox()
  if (!box) throw new Error('No spreadsheet canvas')
  await page.mouse.click(box.x + 90, box.y + 36)
  await expect.poll(() => page.evaluate(() => (window as any).officeFixture.state.selections.length)).toBe(1)
  const selection = await page.evaluate(() => (window as any).officeFixture.state.selections.at(-1))
  expect(selection).toMatchObject({format:'xlsx', kind:'range', sheetIndex:0, sheetName:'销售概览'})
  expect(selection.range).toMatch(/^A1(?::A1)?$/)
  expect(selection.text).toContain('中文预览验收')
  const payload = await page.evaluate(() => (window as any).officeFixture.saveSelection('将标题改为季度报告'))
  expect(payload.annotations[0].selection).toMatchObject({range:selection.range, sheetName:'销售概览'})
  await page.screenshot({path: testInfo.outputPath('xlsx-selection.png')})
})

test('long Chinese documents remain cancellable and repeated opens release workers', async ({context}, testInfo) => {
  test.setTimeout(120_000)
  const page = await prepare(context)
  const timings: number[] = []
  for (let iteration = 0; iteration < 3; iteration++) {
    await open(page, 'docx', 'large')
    timings.push(await page.evaluate(() => (window as any).officeFixture.state.firstPaintMs))
    await page.evaluate(() => (window as any).officeFixture.suspend(true))
    await expect.poll(() => page.evaluate(() => (window as any).__officeWorkers)).toBe(0)
    await expect(page.locator('canvas')).toHaveCount(0)
  }
  await page.evaluate(async () => {
    await (window as any).officeFixture.open('docx', 'large')
    await (window as any).officeFixture.close()
  })
  await page.evaluate(() => (window as any).officeFixture.close())
  await expect.poll(() => page.evaluate(() => (window as any).__officeWorkers)).toBe(0)
  await testInfo.attach('first-paint-ms', {body: JSON.stringify({pages:80, timings}), contentType:'application/json'})
})

test('linked document images cannot trigger an external network fetch', async ({context}) => {
  const page = await prepare(context)
  const external: string[] = []
  page.on('request', request => {if (request.url().startsWith('https://office-fixture.invalid/')) external.push(request.url())})
  await open(page, 'pptx', 'external')
  await expect(page.getByText('外部图片不联网', {exact:true})).toBeVisible()
  expect(external).toEqual([])
})

test('artifact panel authenticates Office bytes and forwards a version-pinned selection', async ({context}) => {
  const page = await prepare(context)
  await page.evaluate(() => (window as any).officeFixture.open('pptx', 'pptx', true))
  await expect(page.locator('.office-preview__canvas canvas')).toBeVisible({timeout: 60_000})
  const title = page.getByText('中文预览验收', {exact: true}).first()
  await expect(title).toBeVisible()
  await page.getByRole('button', {name: '请求修改', exact: true}).click()
  await title.click()
  await expect.poll(() => page.evaluate(() => (window as any).officeFixture.state.events.filter((event: any) => event.type === 'office-annotation-selected').length)).toBe(1)
  const event = await page.evaluate(() => (window as any).officeFixture.state.events.find((event: any) => event.type === 'office-annotation-selected'))
  expect(event.payload).toMatchObject({revisionId: 'revision-pptx', sha256: officeHash(files.pptx), selection: {format: 'pptx', slideIndex: 0, shapeId: '4'}})
  await page.evaluate(() => (window as any).officeFixture.open('pptx', 'pptx', true, true))
  await expect(page.locator('.artifact-preview')).toHaveAttribute('data-preview-state', 'error')
  await expect(page.locator('.artifact-preview [role="alert"]')).toBeVisible()
  await expect(page.locator('.office-preview')).toHaveCount(0)
  expect(await page.evaluate(() => (window as any).__officeWorkers)).toBe(0)
})

test('malformed Office package reports a render failure without retaining a Worker', async ({context}) => {
  const page = await prepare(context)
  await page.evaluate(() => (window as any).officeFixture.open('docx', 'broken', true))
  await expect(page.locator('.artifact-preview [role="alert"]')).toBeVisible({timeout: 60_000})
  await expect.poll(() => page.evaluate(() => (window as any).__officeWorkers)).toBe(0)
})
