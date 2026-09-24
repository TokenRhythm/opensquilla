import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_MCP_UNDER_XVFB !== '1') {
  const child = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_MCP_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (child.error) throw child.error
  process.exit(child.status ?? 1)
}

const webRequests = []
let pendingFrameResponse
let frameRequestStarted
const frameRequest = new Promise(resolve => { frameRequestStarted = resolve })
const web = createServer(async (request, response) => {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  const body = Buffer.concat(chunks).toString('utf8')
  webRequests.push({ url: request.url, method: request.method, body })
  response.setHeader('content-type', 'text/html; charset=utf-8')
  if (request.url === '/routing-frame-pending') {
    pendingFrameResponse = response
    frameRequestStarted()
    return
  }
  if (request.url === '/routing-frame') {
    response.end('<!doctype html><title>Child route</title><p>Frame content</p>')
    return
  }
  if (request.url === '/routing') {
    response.end(`<!doctype html><title>Same-document routes</title>
      <h1 id="route">Route initial</h1><a href="#active">Active route</a>
      <button onclick="history.pushState({},'', '?view=history#completed');renderRoute()">Push history</button>
      <button onclick="window.routeTransition=new Promise(resolve=>window.addEventListener('popstate',()=>resolve(true),{once:true}));history.back()">Back route</button>
      <button onclick="window.count=(window.count||0)+1">Route increment</button>
      <iframe id="child" src="/routing-frame"></iframe>
      <script>
        function renderRoute(){document.getElementById('route').textContent='Route '+(location.hash.slice(1)||'initial')}
        addEventListener('hashchange',renderRoute);addEventListener('popstate',renderRoute);
      </script>`)
    return
  }
  if (request.url === '/basic-auth') {
    response.statusCode = 401
    response.setHeader('WWW-Authenticate', 'Basic realm="Synthetic fixture"')
    response.end('<!doctype html><title>Authentication required</title><p>Authentication required</p>')
    return
  }
  if (['/popup', '/post-popup', '/link-popup', '/background-popup'].includes(request.url)) {
    response.end(`<!doctype html><title>Child page</title><h1>Child page</h1>
      <button onclick="window.childCount=(window.childCount||0)+1">Child increment</button><script>
      window.childState = { hasOpener: !!window.opener, login: localStorage.getItem('syntheticLogin') };
      localStorage.setItem('popupShared', 'child-retained');
      if (window.opener) window.opener.postMessage({ kind: 'synthetic-child-ready', path: location.pathname }, location.origin);
      </script>`)
    return
  }
  response.end(`<!doctype html><title>MCP page</title><style>body{padding:20px;min-height:1400px}</style>
    <h1>${request.url === '/next' ? 'Second document' : 'First document'}</h1>
    <button id="increment" onclick="window.count=(window.count||0)+1">Increment</button>
    <input aria-label="Name"><select aria-label="Choice"><option value="a">A</option><option value="b">B</option></select>
    <a href="/next">Next</a>
    <button onclick="window.confirmAttempts=(window.confirmAttempts||0)+1;if(confirm('Continue synthetic step?'))window.confirmed=(window.confirmed||0)+1">Confirm step</button>
    <button onclick="window.chainAttempts=(window.chainAttempts||0)+1;if(confirm('Continue two-stage step?')){alert('Finish second stage');window.chainCompleted=(window.chainCompleted||0)+1}">Chained dialogs</button>
    <button onclick="window.popupChild=window.open('/popup','_blank')">Popup step</button>
    <a href="/link-popup" target="_blank">New tab link</a>
    <a id="background-link" href="/background-popup">Background tab link</a>
    <form action="/post-popup" method="post" target="_blank" rel="opener">
      <input type="hidden" name="ticket" value="synthetic+payload&amp;value=ok"><button>POST popup</button>
    </form>
    <button onclick="document.querySelector('dialog').showModal()">DOM dialog</button>
    <dialog><p>Close with Escape</p><button onclick="this.closest('dialog').close()">Close dialog</button></dialog>
    <script>
      window.popupMessages = [];
      addEventListener('message', event => {
        if (event.origin === location.origin && event.data?.kind === 'synthetic-child-ready') window.popupMessages.push(event.data.path);
      });
      document.querySelector('dialog').addEventListener('cancel', () => { window.escapeCount=(window.escapeCount||0)+1 });
    </script>${request.url === '/initial-alert' ? '<script>alert("Synthetic initial alert");window.initialResumed=true</script>' : ''}`)
})
await new Promise(resolve => web.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${web.address().port}`
const root = await mkdtemp(join(tmpdir(), 'opensquilla-browser-mcp-'))
let app
let fixtureStderr = ''
try {
  const executablePath = process.env.OPENSQUILLA_ELECTRON_EXECUTABLE
  const require = createRequire(import.meta.url)
  const loader = join(dirname(require.resolve('playwright-core/package.json')), 'lib/server/electron/loader.js')
  app = await electron.launch({
    ...(executablePath ? { executablePath } : {}),
    args: [...(executablePath ? ['-r', loader] : []), `--user-data-dir=${join(root, 'chromium')}`,
      fileURLToPath(new URL('./fixtures/native-workbench-smoke', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '*', no_proxy: '*' },
  })
  // The fixture's separate Playwright control connection must not auto-dismiss
  // dialogs owned by the browser driver under test.
  const keepDialogPending = page => page.on('dialog', () => {})
  app.process().stderr?.on('data', chunk => {
    fixtureStderr = (fixtureStderr + chunk.toString()).slice(-6000)
    if (process.env.OPENSQUILLA_MCP_TRACE) process.stderr.write(chunk)
  })
  app.context().on('page', keepDialogPending)
  for (const page of app.context().pages()) keepDialogPending(page)
  const setup = await app.evaluate(async ({ BrowserWindow }) => {
    const owner = new BrowserWindow({ show: true, width: 1000, height: 800,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
    await owner.loadURL('data:text/html,<title>Browser MCP host</title>')
    const events = []
    const manager = new globalThis.__opensquillaNativeWorkbenchSurfaceManager({ getWindow: () => owner, emit(event) { events.push(event) } })
    const popupCreations = []
    if (process.env.OPENSQUILLA_MCP_TRACE) {
      for (const name of ['allocateSurface', 'initializeAnnotationCdp', 'loadBrowserDocument']) {
        const method = manager[name].bind(manager)
        manager[name] = (...args) => {
          console.error('MCP lifecycle start', name, args[0]?.documentUrl ?? args[0]?.payload?.url)
          const result = method(...args)
          console.error('MCP lifecycle returned', name, args[0]?.documentUrl)
          result?.then?.(() => console.error('MCP lifecycle settled', name, args[0]?.documentUrl), () => {})
          return result
        }
      }
    }
    const configure = manager.configureWebContents.bind(manager)
    manager.configureWebContents = record => {
      if (process.env.OPENSQUILLA_MCP_TRACE) console.error('MCP configure', record.documentUrl)
      const contents = record.view.webContents
      const install = contents.setWindowOpenHandler.bind(contents)
      contents.setWindowOpenHandler = handler => install(details => {
        const result = handler(details)
        if (!result.createWindow) return result
        const create = result.createWindow
        return { ...result, createWindow: options => {
          popupCreations.push({ url: details.url, disposition: details.disposition, hasWebContents: !!options.webContents })
          if (process.env.OPENSQUILLA_MCP_TRACE) console.error('MCP popup create', JSON.stringify(popupCreations.at(-1)))
          const result = create(options)
          if (process.env.OPENSQUILLA_MCP_TRACE) console.error('MCP popup created', details.url)
          return result
        } }
      })
      return configure(record)
    }
    const server = new globalThis.__opensquillaDesktopBrowserServer(
      (request, signal) => manager.executeBrowser(request, signal), undefined,
      (request, signal) => manager.executeBrowserMcp(request, signal),
    )
    globalThis.mcpFixture = { manager, server, owner, events, popupCreations }
    return await server.start()
  })
  let serial = 0
  const call = async (name, args = {}, options = {}) => {
    const id = ++serial
    if (process.env.OPENSQUILLA_MCP_TRACE) console.log('MCP fixture start', id, name)
    const response = await fetch(setup.OPENSQUILLA_DESKTOP_BROWSER_URL + '/mcp', {
      signal: AbortSignal.timeout(40_000),
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${setup.OPENSQUILLA_DESKTOP_BROWSER_TOKEN}` },
      body: JSON.stringify({ jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: args,
        _meta: { sessionKey: options.sessionKey || 'session-a', operationId: options.operationId || `operation-${id}`,
          ...(options.mode ? { observationMode: options.mode } : {}),
          ...(options.evidence ? { nativeImageEvidence: options.evidence } : {}),
        } } }),
    })
    assert.equal(response.status, 200)
    const body = await response.json()
    if (process.env.OPENSQUILLA_MCP_TRACE) console.log('MCP fixture result', id, name, body.result?.structuredContent?.code ?? body.result?.structuredContent?.execution?.state ?? 'received')
    assert.equal(body.id, id)
    assert.equal(body.error, undefined, JSON.stringify(body))
    return body.result
  }
  const success = result => {
    assert.equal(result.isError, false, JSON.stringify(result))
    return result.structuredContent
  }
  const pageValue = (targetRef, expression) => app.evaluate(async ({}, { targetRef, expression }) => {
    const record = [...globalThis.mcpFixture.manager.surfaces.values()].find(r => r.targetRef === targetRef)
    if (!record) throw new Error('Synthetic target is no longer available')
    return await record.view.webContents.executeJavaScript(expression)
  }, { targetRef, expression })
  const waitFor = async (read, matches, label) => {
    const deadline = Date.now() + 8000
    let value
    do {
      value = await read()
      if (matches(value)) return value
      await new Promise(resolve => setTimeout(resolve, 40))
    } while (Date.now() < deadline)
    assert.fail(`${label}: ${JSON.stringify(value)}`)
  }
  const observe = async (targetRef, mode = 'dom') => success(await call('browser_observe', { targetRef }, { mode })).observation
  const actionRef = (observation, name) => {
    const found = observation.refs.find(element => element.name === name)
    assert.ok(found, `Missing synthetic control ${name}: ${JSON.stringify(observation)}`)
    return found.ref
  }
  // A page opened through the trusted renderer before the first chat turn
  // must be controllable on that draft's existing session identity.
  const manualSession = 'session-manual-draft'
  const manualSurfaceId = 'manual-browser-tab'
  const manualCreated = await app.evaluate(async ({}, { url, scopeId, surfaceId }) => {
    return await globalThis.mcpFixture.manager.createSurface({
      version: 2, surfaceId, kind: 'url-preview', payload: { url, scopeId },
    })
  }, { url: origin, scopeId: manualSession, surfaceId: manualSurfaceId })
  assert.equal(manualCreated.ok, true)
  assert.equal(manualCreated.code, undefined, JSON.stringify(manualCreated))
  const manualTabs = success(await call('browser_tabs', {}, { sessionKey: manualSession })).targets
  assert.equal(manualTabs.length, 1)
  assert.equal(manualTabs[0].surfaceId, manualSurfaceId)
  const manualTarget = manualTabs[0].targetRef
  const manualSnapshot = success(await call('browser_inspect', { targetRef: manualTarget }, { sessionKey: manualSession }))
  success(await call('browser_act', {
    targetRef: manualTarget, action: 'click',
    ref: manualSnapshot.refs.find(element => element.name === 'Increment').ref,
  }, { sessionKey: manualSession }))
  assert.equal(await app.evaluate(async ({}, surfaceId) => {
    const record = globalThis.mcpFixture.manager.surfaces.get(surfaceId)
    return await record.view.webContents.executeJavaScript('window.count')
  }, manualSurfaceId), 1)
  assert.equal(success(await call('browser_tabs')).targets.length, 0)
  assert.equal((await call('browser_inspect', { targetRef: manualTarget })).isError, true)
  await app.evaluate(async ({}, surfaceId) => {
    await globalThis.mcpFixture.manager.destroySurface(surfaceId)
  }, manualSurfaceId)
  assert.equal(success(await call('browser_tabs', {}, { sessionKey: manualSession })).targets.length, 0)

  const routingOptions = { sessionKey: 'session-document-routes', mode: 'dom' }
  const routingPage = success(await call('browser_open', { url: origin + '/routing' }, routingOptions))
  const routingObserve = async () => {
    const read = async () => success(await call('browser_observe', {
      targetRef: routingPage.targetRef,
    }, routingOptions)).observation
    let snapshot = await read()
    assert.equal(snapshot.tabs.find(tab => tab.targetRef === routingPage.targetRef).pageState, 'ready')
    // A fragment can scroll during capture; reobserve its reported change once.
    if (snapshot.consistency === 'changed') snapshot = await read()
    assert.equal(snapshot.consistency, 'consistent')
    return snapshot
  }
  const routingClick = async (snapshot, name) => success(await call('browser_act', {
    targetRef: routingPage.targetRef, action: 'click', ref: actionRef(snapshot, name),
  }, routingOptions))
  let routingSnapshot = await routingObserve()
  const oldRouteRef = actionRef(routingSnapshot, 'Route increment')
  await routingClick(routingSnapshot, 'Active route')
  routingSnapshot = await routingObserve()
  assert.match(routingSnapshot.text, /Route active/)
  const staleRouteAction = await call('browser_act', {
    targetRef: routingPage.targetRef, action: 'click', ref: oldRouteRef,
  }, routingOptions)
  assert.equal(staleRouteAction.structuredContent.code, 'STALE_ELEMENT')
  await routingClick(routingSnapshot, 'Route increment')
  assert.equal(await pageValue(routingPage.targetRef, 'window.count'), 1)
  await routingClick(await routingObserve(), 'Push history')
  routingSnapshot = await routingObserve()
  assert.match(routingSnapshot.text, /Route completed/)
  await routingClick(routingSnapshot, 'Route increment')
  await routingClick(await routingObserve(), 'Back route')
  assert.equal(await pageValue(routingPage.targetRef,
    'Promise.race([window.routeTransition,new Promise(resolve=>setTimeout(()=>resolve(location.href),3000))])'), true)
  routingSnapshot = await routingObserve()
  assert.match(routingSnapshot.text, /Route active/)
  await routingClick(routingSnapshot, 'Route increment')
  assert.equal(await pageValue(routingPage.targetRef, 'window.count'), 3)
  success(await call('browser_navigate', {
    targetRef: routingPage.targetRef, url: origin + '/routing#tool-route',
  }, routingOptions))
  routingSnapshot = await routingObserve()
  assert.match(routingSnapshot.text, /Route tool-route/)
  await routingClick(routingSnapshot, 'Route increment')
  assert.equal(await pageValue(routingPage.targetRef, 'window.count'), 4)
  await pageValue(routingPage.targetRef, `new Promise(resolve=>{
    const child=document.getElementById('child').contentWindow;
    child.addEventListener('hashchange',()=>resolve(true),{once:true});child.location.hash='child-route';
  })`)
  routingSnapshot = await routingObserve()
  assert.match(routingSnapshot.text, /Route tool-route/)
  await pageValue(routingPage.targetRef, `window.frameTransition=new Promise(resolve=>{
    const child=document.getElementById('child');child.onload=()=>resolve(true);child.src='/routing-frame-pending';
  });true`)
  await frameRequest
  await routingClick(await routingObserve(), 'Route increment')
  assert.equal(await pageValue(routingPage.targetRef, 'window.count'), 5,
    'a loading child frame must not make the main document unavailable')
  pendingFrameResponse.end('<!doctype html><title>Loaded child</title><p>Ready</p>')
  await pageValue(routingPage.targetRef, 'window.frameTransition')
  assert.equal(success(await call('browser_tabs', {}, routingOptions)).targets[0].url,
    origin + '/routing#tool-route')
  success(await call('browser_tab', { targetRef: routingPage.targetRef, tabAction: 'close' }, routingOptions))

  const initialSession = 'session-initial-dialog'
  const initialStarted = Date.now()
  const initial = success(await call('browser_open', { url: origin + '/initial-alert' }, {
    sessionKey: initialSession, mode: 'dom',
  }))
  assert.ok(Date.now() - initialStarted < 5000, 'a dialog during initial navigation must return a target promptly')
  const initialDialog = initial.observation.browserState.dialogs.pending[0]
  assert.equal(initialDialog.type, 'alert')
  assert.equal(initialDialog.message, 'Synthetic initial alert')
  success(await call('browser_handle_dialog', {
    targetRef: initial.targetRef, dialogId: initialDialog.id, accept: true,
  }, { sessionKey: initialSession, mode: 'dom' }))
  assert.equal(await pageValue(initial.targetRef, 'window.initialResumed'), true)
  success(await call('browser_tab', { targetRef: initial.targetRef, tabAction: 'close' }, { sessionKey: initialSession }))
  const authSession = 'session-authentication'
  const authStarted = Date.now()
  const authPage = success(await call('browser_open', { url: origin + '/basic-auth' }, {
    sessionKey: authSession, mode: 'dom',
  }))
  assert.ok(Date.now() - authStarted < 5000, 'a host authentication prompt must be reported promptly')
  assert.equal(authPage.observation.hostBlockers.some(blocker => blocker.kind === 'authentication' && blocker.requiresUser), true)
  const authBlocked = success(await call('browser_act', {
    targetRef: authPage.targetRef, action: 'press', key: 'Escape',
  }, { sessionKey: authSession, mode: 'dom' }))
  assert.equal(authBlocked.execution.reason, 'requires_user')
  await app.evaluate(({}, targetRef) => {
    const manager = globalThis.mcpFixture.manager
    const record = [...manager.surfaces.values()].find(r => r.targetRef === targetRef)
    manager.cancelPendingAuthentication(record)
  }, authPage.targetRef)
  success(await call('browser_tab', { targetRef: authPage.targetRef, tabAction: 'close' }, { sessionKey: authSession }))
  const first = success(await call('browser_open', { url: origin }))
  const second = success(await call('browser_open', { url: origin }))
  const other = success(await call('browser_open', { url: origin }, { sessionKey: 'session-b' }))
  assert.notEqual(first.targetRef, second.targetRef)
  assert.deepEqual(success(await call('browser_tabs')).targets.map(t => t.targetRef).sort(), [first.targetRef, second.targetRef].sort())
  assert.equal((await call('browser_inspect', { targetRef: other.targetRef })).isError, true)
  await app.evaluate(async ({}, targetRef) => {
    const record = [...globalThis.mcpFixture.manager.surfaces.values()].find(r => r.targetRef === targetRef)
    await record.view.webContents.executeJavaScript("localStorage.setItem('syntheticLogin','retained')")
  }, first.targetRef)
  let inspected = success(await call('browser_inspect', { targetRef: first.targetRef }))
  assert.match(inspected.text, /First document/)
  const ref = name => inspected.refs.find(element => element.name === name).ref
  const oldRef = ref('Increment')
  const click = { targetRef: first.targetRef, action: 'click', ref: oldRef }
  success(await call('browser_act', click, { operationId: 'one-click' }))
  success(await call('browser_act', click, { operationId: 'one-click' }))
  success(await call('browser_act', { targetRef: first.targetRef, action: 'fill', ref: ref('Name'), text: 'MCP input' }))
  success(await call('browser_act', { targetRef: first.targetRef, action: 'select', ref: ref('Choice'), text: 'b' }))
  const state = await app.evaluate(async ({}, refs) => Promise.all(refs.map(targetRef => {
    const record = [...globalThis.mcpFixture.manager.surfaces.values()].find(r => r.targetRef === targetRef)
    return record.view.webContents.executeJavaScript('({count:window.count||0,name:document.querySelector("input").value,choice:document.querySelector("select").value,login:localStorage.getItem("syntheticLogin")})')
  })), [first.targetRef, second.targetRef])
  assert.deepEqual(state, [
    { count: 1, name: 'MCP input', choice: 'b', login: 'retained' },
    { count: 0, name: '', choice: 'a', login: null },
  ])
  const capture = await call('browser_screenshot', { targetRef: first.targetRef })
  success(capture)
  const image = capture.content.find(block => block.type === 'image')
  assert.equal(image.mimeType, 'image/png')
  assert.equal(Buffer.from(image.data, 'base64').subarray(1, 4).toString(), 'PNG')
  assert.ok(capture.structuredContent.width > 0)
  assert.ok(!capture.content.find(block => block.type === 'text').text.includes(image.data))

  const hybrid = await call('browser_observe', { targetRef: first.targetRef }, { mode: 'auto' })
  const hybridState = success(hybrid).observation
  const hybridImage = hybrid.content.find(block => block.type === 'image')
  assert.ok(hybridImage, 'automatic observation includes a real viewport image')
  assert.equal(Buffer.from(hybridImage.data, 'base64').subarray(1, 4).toString(), 'PNG')
  assert.equal(hybridState.imageStatus, 'available')
  assert.deepEqual(hybridImage._meta['opensquilla/browserObservation'], {
    targetRef: first.targetRef, observationId: hybridState.observationId, imageId: hybridState.image.imageId,
  })
  const dom = await call('browser_observe', { targetRef: first.targetRef, observationMode: 'auto' }, { mode: 'dom' })
  assert.equal(success(dom).observation.imageStatus, 'omitted')
  assert.equal(dom.content.some(block => block.type === 'image'), false, 'runtime DOM mode suppresses pixels')

  const visualLayout = await app.evaluate(({}, targetRef) => {
    const record = [...globalThis.mcpFixture.manager.surfaces.values()].find(r => r.targetRef === targetRef)
    const saved = { bounds: record.view.getBounds(), visible: record.view.getVisible() }
    record.view.setBounds({ x: 0, y: 0, width: 800, height: 600 })
    record.view.setVisible(true)
    return saved
  }, first.targetRef)
  try {
    const visual = await observe(first.targetRef, 'auto')
    const point = await pageValue(first.targetRef,
      '(()=>{const r=document.querySelector("#increment").getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()')
    const coordinates = { targetRef: first.targetRef, actions: [{ action: 'click', ...point,
      observationId: visual.observationId, imageId: visual.image.imageId }] }
    const clicked = success(await call('browser_batch', coordinates))
    assert.equal(clicked.execution.state, 'completed', 'MCP coordinate input needs no model delivery receipt')
    assert.equal(await pageValue(first.targetRef, 'window.count'), 2)
    const stale = await call('browser_batch', coordinates)
    assert.equal(stale.isError, true)
    assert.equal(stale.structuredContent.code, 'STALE_OBSERVATION')
    assert.equal(stale.structuredContent.outcome, 'not_started')
    assert.equal(await pageValue(first.targetRef, 'window.count'), 2, 'a stale MCP image must not repeat input')
  } finally {
    await app.evaluate(({}, { targetRef, saved }) => {
      const record = [...globalThis.mcpFixture.manager.surfaces.values()].find(r => r.targetRef === targetRef)
      record.view.setBounds(saved.bounds)
      record.view.setVisible(saved.visible)
    }, { targetRef: first.targetRef, saved: visualLayout })
  }

  let observed = await observe(first.targetRef)
  // Restoring native bounds can resize the renderer during its first snapshot.
  // The protocol deliberately withholds refs for that observation; obtain a
  // current one before testing dialog input instead of assuming a synchronous resize.
  if (observed.consistency === 'changed') observed = await observe(first.targetRef)
  assert.equal(observed.consistency, 'consistent')
  const confirmArgs = { targetRef: first.targetRef, actions: [{ action: 'click', ref: actionRef(observed, 'Confirm step') }] }
  const confirmStarted = Date.now()
  const blocked = success(await call('browser_batch', confirmArgs, { mode: 'dom', operationId: 'confirm-once' }))
  assert.ok(Date.now() - confirmStarted < 5000, 'a dialog-blocked click must return before the ordinary action timeout')
  assert.equal(blocked.execution.state, 'blocked')
  const pending = blocked.observation.browserState.dialogs.pending
  assert.equal(pending.length, 1)
  assert.equal(pending[0].type, 'confirm')
  assert.equal(pending[0].message, 'Continue synthetic step?')
  const blockedRead = await observe(first.targetRef)
  assert.equal(blockedRead.consistency, 'blocked', JSON.stringify(blockedRead.browserState))
  assert.equal(blockedRead.browserState.dialogs.pending[0].id, pending[0].id)
  const accepted = success(await call('browser_handle_dialog', {
    targetRef: first.targetRef, dialogId: pending[0].id, accept: true,
  }, { mode: 'dom', operationId: 'accept-once' }))
  assert.equal(accepted.observation.browserState.dialogs.pending.length, 0)
  assert.deepEqual(await pageValue(first.targetRef, '({attempts:window.confirmAttempts,confirmed:window.confirmed})'),
    { attempts: 1, confirmed: 1 })
  // A retry returns the original blocked receipt, without repeating the click
  // or reopening a modal that the following tool call already answered.
  assert.equal(success(await call('browser_batch', confirmArgs, {
    mode: 'dom', operationId: 'confirm-once',
  })).execution.state, 'blocked')
  assert.deepEqual(await pageValue(first.targetRef, '({attempts:window.confirmAttempts,confirmed:window.confirmed})'),
    { attempts: 1, confirmed: 1 })
  const staleDialog = await call('browser_handle_dialog', {
    targetRef: first.targetRef, dialogId: pending[0].id, accept: true,
  }, { mode: 'dom' })
  assert.equal(staleDialog.isError, true)
  assert.equal(staleDialog.structuredContent.code, 'STALE_DIALOG')

  observed = await observe(first.targetRef)
  const chain = success(await call('browser_batch', {
    targetRef: first.targetRef, actions: [{ action: 'click', ref: actionRef(observed, 'Chained dialogs') }],
  }, { mode: 'dom' }))
  const chainFirst = chain.observation.browserState.dialogs.pending[0]
  assert.equal(chainFirst.type, 'confirm')
  const chainStarted = Date.now()
  const chainNext = success(await call('browser_handle_dialog', {
    targetRef: first.targetRef, dialogId: chainFirst.id, accept: true,
  }, { mode: 'dom' }))
  assert.ok(Date.now() - chainStarted < 5000, 'the next dialog must interrupt waiting for the first click to settle')
  const chainSecond = chainNext.observation.browserState.dialogs.pending[0]
  assert.equal(chainSecond.type, 'alert')
  assert.equal(chainSecond.message, 'Finish second stage')
  assert.notEqual(chainSecond.id, chainFirst.id)
  success(await call('browser_handle_dialog', {
    targetRef: first.targetRef, dialogId: chainSecond.id, accept: true,
  }, { mode: 'dom' }))
  assert.deepEqual(await pageValue(first.targetRef, '({attempts:window.chainAttempts,completed:window.chainCompleted})'),
    { attempts: 1, completed: 1 })

  observed = await observe(first.targetRef)
  success(await call('browser_batch', {
    targetRef: first.targetRef, actions: [{ action: 'click', ref: actionRef(observed, 'DOM dialog') }],
  }, { mode: 'dom' }))
  assert.equal(await pageValue(first.targetRef, 'document.querySelector("dialog").open'), true)
  success(await call('browser_act', { targetRef: first.targetRef, action: 'press', key: 'Escape' }, { mode: 'dom' }))
  assert.deepEqual(await pageValue(first.targetRef, '({open:document.querySelector("dialog").open,escaped:window.escapeCount})'),
    { open: false, escaped: 1 }, 'Escape must reach the webpage instead of closing the workbench')

  const parentRef = first.targetRef
  const findChild = path => waitFor(async () => success(await call('browser_tabs')).targets,
    targets => targets.some(target => target.openerTargetRef === parentRef && target.url === origin + path),
    `Popup ${path} was not registered`).then(targets => targets.find(target => target.openerTargetRef === parentRef && target.url === origin + path))
  observed = await observe(parentRef)
  success(await call('browser_batch', {
    targetRef: parentRef, actions: [{ action: 'click', ref: actionRef(observed, 'Popup step') }],
  }, { mode: 'dom' }))
  const popup = await findChild('/popup')
  assert.notEqual(popup.targetRef, parentRef)
  assert.equal(popup.openerTargetRef, parentRef)
  assert.equal((await call('browser_observe', { targetRef: popup.targetRef }, { sessionKey: 'session-b', mode: 'dom' })).isError, true)
  await waitFor(() => pageValue(parentRef, 'window.popupMessages'), messages => messages.includes('/popup'), 'Child postMessage did not reach its opener')
  assert.deepEqual(await pageValue(popup.targetRef, 'window.childState'), { hasOpener: true, login: 'retained' })
  assert.equal(await pageValue(parentRef, 'window.popupChild !== null && !window.popupChild.closed'), true,
    'window.open must return the live managed child instead of a blocked placeholder')
  assert.equal(await pageValue(parentRef, 'localStorage.getItem("popupShared")'), 'child-retained')
  const switched = success(await call('browser_tab', { targetRef: popup.targetRef, tabAction: 'switch' }, { mode: 'dom' }))
  assert.equal(switched.observation.targetRef, popup.targetRef)
  assert.equal(switched.observation.tabs.some(target => target.targetRef === parentRef), true)
  success(await call('browser_tab', { targetRef: popup.targetRef, tabAction: 'close' }))
  assert.equal((await call('browser_inspect', { targetRef: popup.targetRef })).isError, true)
  assert.equal(await pageValue(parentRef, 'window.popupChild.closed'), true)
  assert.deepEqual(await pageValue(parentRef, '({login:localStorage.getItem("syntheticLogin"),shared:localStorage.getItem("popupShared")})'),
    { login: 'retained', shared: 'child-retained' }, 'closing a shared-session child must not clear its parent storage')

  observed = await observe(parentRef)
  success(await call('browser_batch', {
    targetRef: parentRef, actions: [{ action: 'click', ref: actionRef(observed, 'POST popup') }],
  }, { mode: 'dom' }))
  const postPopup = await findChild('/post-popup')
  await waitFor(() => pageValue(parentRef, 'window.popupMessages'), messages => messages.includes('/post-popup'), 'POST child postMessage did not reach its opener')
  assert.deepEqual(webRequests.filter(request => request.url === '/post-popup'), [{
    url: '/post-popup', method: 'POST', body: 'ticket=synthetic%2Bpayload%26value%3Dok',
  }], 'the managed popup must preserve Chromium form POST semantics without a replacement GET')
  assert.deepEqual(await pageValue(postPopup.targetRef, 'window.childState'), { hasOpener: true, login: 'retained' })
  success(await call('browser_tab', { targetRef: postPopup.targetRef, tabAction: 'close' }))
  assert.equal(await pageValue(parentRef, 'localStorage.getItem("syntheticLogin")'), 'retained')
  assert.equal(success(await call('browser_tabs')).targets.length, 2, 'both child targets are removed after close')

  observed = await observe(parentRef)
  success(await call('browser_batch', {
    targetRef: parentRef, actions: [{ action: 'click', ref: actionRef(observed, 'New tab link') }],
  }, { mode: 'dom' }))
  const linkPopup = await findChild('/link-popup')
  await waitFor(() => pageValue(linkPopup.targetRef, 'window.childState'), Boolean, 'New tab link did not load its document')
  assert.deepEqual(await pageValue(linkPopup.targetRef, 'window.childState'), { hasOpener: false, login: 'retained' },
    'ordinary target=_blank links preserve their default noopener while inheriting the browser session')
  const linkObservation = await observe(linkPopup.targetRef)
  success(await call('browser_batch', {
    targetRef: linkPopup.targetRef, actions: [{ action: 'click', ref: actionRef(linkObservation, 'Child increment') }],
  }, { mode: 'dom' }))
  assert.equal(await pageValue(linkPopup.targetRef, 'window.childCount'), 1)
  assert.deepEqual(webRequests.filter(request => request.url === '/link-popup'), [{ url: '/link-popup', method: 'GET', body: '' }])
  success(await call('browser_tab', { targetRef: linkPopup.targetRef, tabAction: 'close' }))

  const survivorSession = 'session-surviving-child'
  const survivorOptions = { sessionKey: survivorSession, mode: 'dom' }
  const survivorParent = success(await call('browser_open', { url: origin }, survivorOptions))
  await pageValue(survivorParent.targetRef, "localStorage.setItem('syntheticLogin','surviving-parent')")
  success(await call('browser_batch', {
    targetRef: survivorParent.targetRef,
    actions: [{ action: 'click', ref: actionRef(survivorParent.observation, 'Popup step') }],
  }, survivorOptions))
  const survivorTargets = await waitFor(async () => success(await call('browser_tabs', {}, survivorOptions)).targets,
    targets => targets.some(target => target.openerTargetRef === survivorParent.targetRef && target.url === origin + '/popup'),
    'Surviving child was not registered')
  const survivorChild = survivorTargets.find(target => target.openerTargetRef === survivorParent.targetRef)
  await waitFor(() => pageValue(survivorChild.targetRef, 'window.childState'), Boolean, 'Surviving child did not load')
  success(await call('browser_tab', { targetRef: survivorParent.targetRef, tabAction: 'close' }, survivorOptions))
  assert.deepEqual(success(await call('browser_tabs', {}, survivorOptions)).targets.map(target => target.targetRef), [survivorChild.targetRef])
  const survivorObservation = success(await call('browser_observe', { targetRef: survivorChild.targetRef }, survivorOptions)).observation
  success(await call('browser_batch', {
    targetRef: survivorChild.targetRef,
    actions: [{ action: 'click', ref: actionRef(survivorObservation, 'Child increment') }],
  }, survivorOptions))
  assert.deepEqual(await pageValue(survivorChild.targetRef, '({count:window.childCount,login:localStorage.getItem("syntheticLogin")})'),
    { count: 1, login: 'surviving-parent' }, 'a child remains operable and retains its shared session after its opener closes')
  success(await call('browser_tab', { targetRef: survivorChild.targetRef, tabAction: 'close' }, survivorOptions))

  success(await call('browser_navigate', { targetRef: first.targetRef, url: origin + '/next' }))
  assert.equal((await call('browser_act', click)).isError, true)
  inspected = success(await call('browser_inspect', { targetRef: first.targetRef }))
  assert.match(inspected.text, /Second document/)
  success(await call('browser_reload', { targetRef: first.targetRef }))
  assert.equal((await call('browser_navigate', { targetRef: first.targetRef, url: 'file:///etc/passwd' })).isError, true)
  const setActivity = (taskId, active, sessionKey = 'session-a') => app.evaluate(
    ({}, state) => globalThis.mcpFixture.manager.setBrowserAutomationState(state),
    { sessionKey, taskId, active },
  )
  const pointerVisible = () => app.evaluate(async ({}, targetRef) => {
    const record = [...globalThis.mcpFixture.manager.surfaces.values()].find(r => r.targetRef === targetRef)
    return record.view.webContents.executeJavaScript(
      '!!document.getElementById("__opensquilla-browser-pointer")',
    )
  }, first.targetRef)
  await app.evaluate(({}, targetRef) => {
    const manager = globalThis.mcpFixture.manager
    const record = [...manager.surfaces.values()].find(r => r.targetRef === targetRef)
    manager.setSurfaceRect({ surfaceId: record.id, x: 100, y: 80, width: 700, height: 600, visible: true })
    manager.activateSurface(record.id)
  }, first.targetRef)
  assert.equal((await setActivity('synthetic-turn-one', true)).ok, true)
  success(await call('browser_inspect', { targetRef: first.targetRef }))
  await new Promise(resolve => setTimeout(resolve, 1700))
  assert.equal(await pointerVisible(), true, 'the task keeps the cursor visible between MCP calls')
  await setActivity('synthetic-turn-two', true)
  success(await call('browser_inspect', { targetRef: first.targetRef }))
  await setActivity('synthetic-turn-one', false)
  await setActivity('other-turn', false, 'session-b')
  assert.equal(await pointerVisible(), true, 'stale and other-session completions cannot clear the current cursor')
  success(await call('browser_navigate', { targetRef: first.targetRef, url: origin }))
  success(await call('browser_inspect', { targetRef: first.targetRef }))
  assert.equal(await pointerVisible(), true, 'the native page lifecycle restores the cursor after navigation')
  await setActivity('synthetic-turn-two', false)
  await new Promise(resolve => setTimeout(resolve, 220))
  assert.equal(await pointerVisible(), false, 'the completed task clears the cursor')
  success(await call('browser_inspect', { targetRef: first.targetRef }))
  assert.equal(await pointerVisible(), false, 'a late browser result cannot revive a completed task')
  await app.evaluate(async ({}, targetRef) => {
    const manager = globalThis.mcpFixture.manager
    const record = [...manager.surfaces.values()].find(r => r.targetRef === targetRef)
    await manager.destroySurface(record.id)
  }, first.targetRef)
  assert.equal((await call('browser_inspect', { targetRef: first.targetRef })).isError, true)
  assert.equal(success(await call('browser_tabs')).targets.length, 1)
  assert.equal(await app.evaluate(() => globalThis.mcpFixture.owner.isDestroyed()), false)
  const backgroundParent = second.targetRef
  // The browser protocol intentionally exposes no mouse modifiers. Real native
  // middle-button input exercises Chromium's separate background-tab branch.
  await app.evaluate(async ({}, targetRef) => {
    const manager = globalThis.mcpFixture.manager
    const record = [...manager.surfaces.values()].find(r => r.targetRef === targetRef)
    manager.setSurfaceRect({ surfaceId: record.id, x: 100, y: 80, width: 700, height: 600, visible: true })
    manager.activateSurface(record.id)
    record.view.webContents.focus()
    const position = await record.view.webContents.executeJavaScript(`(() => {
      const element = document.getElementById('background-link'); element.scrollIntoView();
      const rect = element.getBoundingClientRect(); return { x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2) };
    })()`)
    record.view.webContents.sendInputEvent({ type: 'mouseMove', ...position })
    record.view.webContents.sendInputEvent({ type: 'mouseDown', button: 'middle', clickCount: 1, ...position })
    record.view.webContents.sendInputEvent({ type: 'mouseUp', button: 'middle', clickCount: 1, ...position })
  }, backgroundParent)
  const backgroundTargets = await waitFor(async () => success(await call('browser_tabs')).targets,
    targets => targets.some(target => target.openerTargetRef === backgroundParent && target.url === origin + '/background-popup'),
    'Background popup was not registered')
  const backgroundPopup = backgroundTargets.find(target => target.openerTargetRef === backgroundParent)
  await waitFor(() => pageValue(backgroundPopup.targetRef, 'window.childState'), Boolean, 'Background tab did not load its document')
  assert.equal(await pageValue(backgroundPopup.targetRef, 'localStorage.getItem("syntheticLogin")'), null)
  const backgroundObservation = await observe(backgroundPopup.targetRef)
  success(await call('browser_batch', {
    targetRef: backgroundPopup.targetRef, actions: [{ action: 'click', ref: actionRef(backgroundObservation, 'Child increment') }],
  }, { mode: 'dom' }))
  assert.equal(await pageValue(backgroundPopup.targetRef, 'window.childCount'), 1)
  const creations = await app.evaluate(() => globalThis.mcpFixture.popupCreations)
  assert.equal(creations.some(creation => creation.url === origin + '/background-popup'
    && creation.disposition === 'background-tab' && !creation.hasWebContents), true,
  `the native background-tab test must cover OpenURLFromTab without an existing webContents: ${JSON.stringify(creations)}`)
  assert.deepEqual(webRequests.filter(request => request.url === '/background-popup'), [{ url: '/background-popup', method: 'GET', body: '' }])
  success(await call('browser_tab', { targetRef: backgroundPopup.targetRef, tabAction: 'close' }))

  console.log('Built-in browser MCP end-to-end passed: hybrid/DOM observations, same-document routes and child-frame isolation, native and DOM dialogs, popup opener/noopener/POST/background/storage/lifetime semantics, session isolation, replay, navigation, cursor lifecycle and close.')
} catch (error) {
  console.error('Browser MCP Electron fixture failed:', error)
  console.error('Electron fixture stderr:', fixtureStderr)
  console.error('Electron fixture state:', JSON.stringify(await Promise.race([app?.evaluate(() => ({
    records: [...(globalThis.mcpFixture?.manager.surfaces.values() ?? [])].map(record => ({
      targetRef: record.targetRef, openerTargetRef: record.openerTargetRef, kind: record.kind,
      mode: record.mode, disposed: record.disposed, crashed: record.crashed,
      url: record.view.webContents.isDestroyed() ? 'destroyed' : record.view.webContents.getURL(),
    })), events: globalThis.mcpFixture?.events.slice(-8), popupCreations: globalThis.mcpFixture?.popupCreations,
  })).catch(() => null), new Promise(resolve => setTimeout(() => resolve('diagnostic timed out'), 2000))])))
  throw error
} finally {
  const cleanupWatchdog = app ? setTimeout(() => app.process().kill('SIGKILL'), 8000) : undefined
  cleanupWatchdog?.unref()
  if (app) await app.evaluate(async () => {
    await globalThis.mcpFixture?.server.close()
    await globalThis.mcpFixture?.manager.destroyAll()
  }).catch(() => {})
  await app?.close().catch(() => {})
  clearTimeout(cleanupWatchdog)
  web.closeAllConnections()
  await new Promise(resolve => web.close(resolve))
  await rm(root, { recursive: true, force: true })
}
