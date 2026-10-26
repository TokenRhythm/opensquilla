// Real WebUI + real Gateway + real Electron picker. Synthetic documents only.
// No mocked WebSocket/API/annotation response, no renderer-store injection.
// Build the WebUI/Desktop first, then run with --python <prepared-venv-python>.
// --hold keeps the isolated app open; write <output>/stop to close it gracefully.
import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { delimiter, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { _electron as electron } from 'playwright'
import { environmentWithoutProviderSecrets, writeSyntheticCredential, waitFor } from './packaged-smoke-helpers.mjs'

const exec = promisify(execFile)
const repo = resolve(fileURLToPath(new URL('../../..', import.meta.url)))
const option = (name, fallback) => process.argv.includes(name) ? process.argv[process.argv.indexOf(name) + 1] : fallback
const output = resolve(option('--output', join(repo, '.tmp', `annotation-ui-${Date.now()}`)))
const python = option('--python', process.env.PYTHON || 'python')
const hold = process.argv.includes('--hold')
await mkdir(output, { recursive: false })
const userData = join(output, 'desktop')
const profile = join(userData, 'opensquilla')
await mkdir(profile, { recursive: true })
const clean = environmentWithoutProviderSecrets(process.env)
for (const name of Object.keys(clean)) if (name.startsWith('OPENSQUILLA_')) delete clean[name]
const env = {
  ...clean, HOME: profile, USERPROFILE: profile, PYTHONPATH: join(repo, 'src'), PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1',
  PATH: dirname(python) + delimiter + (clean.PATH || clean.Path || ''),
  UV_NO_SYNC: '1',
  OPENSQUILLA_HOME: profile, OPENSQUILLA_STATE_DIR: profile, OPENSQUILLA_USER_STATE_DIR: join(output, 'user-state'),
  OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1', OPENSQUILLA_TESTING: '0',
  OPENSQUILLA_MEMORY_DREAM_DISABLED: '1', OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0',
  NO_PROXY: '127.0.0.1,localhost,::1',
  OPENSQUILLA_GATEWAY_CONFIG_PATH: join(profile, 'config.toml'),
  OPENSQUILLA_DESKTOP_REPO_ROOT: repo, OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
  OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
}
const interpreter = await exec(python, ['-B', '-c', 'import json,sys;print(json.dumps({"prefix":sys.prefix,"executable":sys.executable}))'], { env, windowsHide: true })
const runtimePython = JSON.parse(interpreter.stdout)
env.UV_PROJECT_ENVIRONMENT = option('--venv', runtimePython.prefix)
env.PATH = dirname(runtimePython.executable) + delimiter + (clean.PATH || clean.Path || '')
const reserve = createServer()
await new Promise(done => reserve.listen(0, '127.0.0.1', done))
const port = reserve.address().port
await new Promise(done => reserve.close(done))
const origin = `http://127.0.0.1:${port}`
await writeFile(env.OPENSQUILLA_GATEWAY_CONFIG_PATH, `host = "127.0.0.1"
port = ${port}
state_dir = ${JSON.stringify(join(profile, 'state'))}
[llm]
provider = "ollama"
model = "annotation-fixture-no-model-calls"
base_url = "http://127.0.0.1:11434"
[squilla_router]
enabled = false
[llm_ensemble]
enabled = false
[naming]
enabled = false
[heartbeat]
enabled = false
[mcp]
enabled = false
[privacy]
disable_network_observability = true
reliability_diagnostics_enabled = false
product_analytics_enabled = false
[model_catalog]
refresh = "off"
[memory]
auto_capture_enabled = false
retrieval_mode = "fts_only"
ttl_sweep_interval_minutes = 0
`)
const seeded = await exec(python, ['-B', join(repo, 'desktop/electron/scripts/fixtures/preview-retention-annotations/seed.py'), env.OPENSQUILLA_GATEWAY_CONFIG_PATH],
  { cwd: profile, env, maxBuffer: 8 * 1024 * 1024, timeout: 120000 })
const manifest = JSON.parse(seeded.stdout.trim())
await writeFile(join(output, 'manifest.json'), JSON.stringify(manifest, null, 2))
const report = { sourceRoot: repo, output, origin, started: new Date().toISOString(), evidence: 'Real WebUI/Gateway/Electron with synthetic HTML; no model calls', checks: [], annotations: [], errors: [], status: 'running' }
let app, page
const persist = () => writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2))
async function waitForStop() {
  let stopped=false
  const stop=()=>{stopped=true}
  process.once('SIGINT',stop)
  while(!stopped) {
    if(await readFile(join(output,'stop'),'utf8').then(()=>true,()=>false)) break
    await new Promise(done=>setTimeout(done,500))
  }
  process.off('SIGINT',stop)
}
async function nativePages() {
  return app.evaluate(async ({ webContents }) => {
    const found = []
    for (const contents of webContents.getAllWebContents()) {
      if (contents.isDestroyed()) continue
      const owner = contents.getOwnerBrowserWindow()
      const view = owner?.contentView.children.find(child => child.webContents?.id === contents.id)
      if (!view) continue
      const state = await contents.executeJavaScript(`(() => {
        const h=document.querySelector('#heading'); if(!h)return null;
        return {heading:h.textContent, counter:window.retainedCounter, draft:document.querySelector('#draft')?.value, href:location.href, hash:location.hash, delayedText:document.querySelector('#delayed-hash')?.textContent};
      })()`).catch(() => null)
      if (state) found.push({ id: contents.id, visible: view.getVisible(), ...state })
    }
    return found
  })
}
async function checkpoint(label) {
  const states = await nativePages()
  report.checks.push({ label, at: new Date().toISOString(), pages: states })
  await page.screenshot({ path: join(output, `${label}-webui.png`) })
  const native = states.find(p => p.visible)
  if (native) {
    const png = await app.evaluate(async ({ webContents }, id) => (await webContents.fromId(id).capturePage()).toPNG().toString('base64'), native.id)
    await writeFile(join(output, `${label}-native.png`), Buffer.from(png, 'base64'))
  }
  await persist()
}
async function selectChat(chat) {
  report.currentStep=`Select chat ${chat}`
  const key = manifest.find(row => row.chat === chat).key
  await page.locator(`[data-session-key=${JSON.stringify(key)}] .sidebar-history-item`).first().click()
  await waitFor(() => new URL(page.url()).searchParams.get('session') === key, `chat ${chat}`)
  await page.locator('.workspace-file-link').filter({ hasText: `preview-${chat.toLowerCase()}1/index.html` }).last().waitFor()
}
async function open(label) {
  report.currentStep=`Open preview ${label}`
  const row = manifest.find(row => row.label === label)
  const tab = page.getByTestId('workbench-host').getByRole('tab').filter({ hasText: `preview-${label.toLowerCase()}` })
  if (await tab.count()) await tab.click()
  else await page.locator('.workspace-file-link').filter({ hasText: row.path }).last().click()
  return waitFor(async () => (await nativePages()).find(p => p.visible && p.heading === `Preview ${label}`), `native preview ${label}`)
}
const toggle = () => page.getByTestId('workbench-host').locator('.app-workbench__action--annotation[aria-pressed]').first()
async function mode(enabled) {
  if (await toggle().getAttribute('aria-pressed') !== String(enabled)) await toggle().click()
  await waitFor(async () => await toggle().getAttribute('aria-pressed', {timeout:2000}) === String(enabled), `annotation mode ${enabled}`, 15000)
}
async function nativeClick(selector) {
  const current = (await nativePages()).find(p => p.visible)
  assert.ok(current, 'A native preview must be visible')
  await app.evaluate(async ({ webContents }, request) => {
    const contents = webContents.fromId(request.id)
    const point = await contents.executeJavaScript(`(() => {
      const e=document.querySelector(${JSON.stringify(request.selector)}); if(!e)throw new Error('Target missing');
      e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};
    })()`)
    contents.focus()
    for (const type of ['mouseMove', 'mouseDown', 'mouseUp']) contents.sendInputEvent({ type, x: Math.round(point.x), y: Math.round(point.y), button: 'left', clickCount: 1 })
  }, { id: current.id, selector })
  return current
}
async function visibleEditor() {
  return app.evaluate(async ({ webContents }) => {
    for (const c of webContents.getAllWebContents()) {
      if (c.isDestroyed()) continue
      const view=c.getOwnerBrowserWindow()?.contentView.children.find(child=>child.webContents?.id===c.id)
      if (!view?.getVisible()) continue
      const body=await c.executeJavaScript("document.querySelector('#annotation-body')?.value").catch(()=>undefined)
      if (body!==undefined) return {id:c.id,body}
    }
    return null
  })
}
async function retainedDraftRoundTrip() {
  const body='unsubmitted A1 draft survives both switches'
  await nativeClick('#footer')
  const editor=await waitFor(visibleEditor,'unsubmitted native editor')
  await app.evaluate(async ({webContents},request)=>{
    const c=webContents.fromId(request.id);c.focus();await c.insertText(request.body)
  }, {id:editor.id,body})
  await waitFor(async ()=>(await visibleEditor())?.body===body,'typed unsubmitted annotation draft')
  await open('A2')
  assert.notEqual((await visibleEditor())?.body,body,'A1 draft must not appear over A2')
  await selectChat('B');await open('B1')
  assert.notEqual((await visibleEditor())?.body,body,'A1 draft must not appear in chat B')
  assert.equal(await page.locator('.chat-prompt-annotation-chip').filter({hasText:body}).count(),0)
  await selectChat('A')
  await page.getByTestId('workbench-host').getByRole('tab').filter({hasText:'preview-a1'}).click()
  const recovered=await waitFor(async ()=>{
    const editor=await visibleEditor()
    if(editor?.body===body)return {kind:'native',id:editor.id}
    const fallback=page.locator('.annotation-comment textarea')
    if(await fallback.isVisible() && await fallback.inputValue()===body)return {kind:'web-fallback'}
    return null
  },'unsubmitted draft restored to original A1 target',15000)
  if(recovered.kind==='web-fallback') {
    recovered.readOnly=await page.locator('.annotation-comment textarea').evaluate(el=>el.readOnly)
    recovered.recordDisabled=await page.locator('.annotation-comment__record').isDisabled()
  }
  report.draftRecovery=recovered
  await checkpoint('draft-recovered')
  assert.notEqual(recovered.readOnly,true,'Recovered draft must be editable before Enter')
  if(recovered.kind==='native') {
    await app.evaluate(({webContents},id)=>{
      const c=webContents.fromId(id);c.focus()
      c.sendInputEvent({type:'keyDown',keyCode:'Enter'});c.sendInputEvent({type:'keyUp',keyCode:'Enter'})
    },recovered.id)
  } else await page.locator('.annotation-comment textarea').press('Enter')
  const chip=page.locator('.chat-prompt-annotation-chip').filter({hasText:body})
  await waitFor(async ()=>await chip.count()===1,'recovered draft submitted once')
  await waitFor(async ()=>!await visibleEditor() && !await page.locator('.annotation-comment textarea').isVisible(),'recovered editor dismissed')
  const target=await chip.locator('.chat-prompt-annotation-chip__target').innerText()
  assert.ok(target.includes('Independent retained page A1'),`Restored draft target: ${target}`)
  report.annotations.push({label:'A1',selector:'#footer',text:body,recovery:recovered.kind,target})
  await checkpoint('draft-recorded')
  await annotate('A1','#heading','continuous annotation after recovered draft',false)
}
async function annotate(label, selector, text, autoEnable = true) {
  if (autoEnable) await mode(true)
  await waitFor(async () => await toggle().getAttribute('aria-pressed', {timeout:2000}) === 'true', 'annotation toggle acknowledged', 15000)
  const current = await nativeClick(selector)
  const editor = await waitFor(() => app.evaluate(async ({ webContents }) => {
    for (const c of webContents.getAllWebContents()) {
      if (c.isDestroyed()) continue
      const view = c.getOwnerBrowserWindow()?.contentView.children.find(child => child.webContents?.id === c.id)
      if (!view?.getVisible()) continue
      if (await c.executeJavaScript("document.activeElement?.id==='annotation-body'").catch(() => false)) return c.id
    }
    return null
  }), `native annotation input for ${label}`, 15000)
  const png = await app.evaluate(async ({ webContents }, id) => (await webContents.fromId(id).capturePage()).toPNG().toString('base64'), editor)
  await writeFile(join(output, `annotation-${report.annotations.length + 1}-editor.png`), Buffer.from(png, 'base64'))
  await app.evaluate(async ({ webContents }, request) => {
    const c = webContents.fromId(request.id); c.focus(); await c.insertText(request.text)
    const actual = await c.executeJavaScript("document.querySelector('#annotation-body').value")
    if (actual !== request.text) throw new Error('Native annotation text mismatch')
  }, { id: editor, text })
  const filledPng = await app.evaluate(async ({ webContents }, id) => (await webContents.fromId(id).capturePage()).toPNG().toString('base64'), editor)
  await writeFile(join(output, `annotation-${report.annotations.length + 1}-editor-filled.png`), Buffer.from(filledPng, 'base64'))
  await app.evaluate(({ webContents }, id) => {
    const c = webContents.fromId(id); c.focus()
    c.sendInputEvent({type:'keyDown',keyCode:'Enter'});c.sendInputEvent({type:'keyUp',keyCode:'Enter'})
  }, editor)
  await waitFor(async () => (await page.locator('.annotation-summary, .chat-prompt-annotation-chip, .artifact-annotation-summary').allTextContents()).some(t => t.includes(text)), `recorded annotation ${text}`, 15000)
  await waitFor(() => app.evaluate(({ webContents }, id) => {
    const c = webContents.fromId(id)
    if (!c || c.isDestroyed()) return true
    const view = c.getOwnerBrowserWindow()?.contentView.children.find(child => child.webContents?.id === id)
    return !view || !view.getVisible()
  }, editor), `annotation editor dismissed for ${label}`, 15000)
  assert.equal(await toggle().getAttribute('aria-pressed'), 'true', 'Annotation stays enabled after Enter')
  report.annotations.push({ label, selector, text, nativeId: current.id, editorId: editor })
  await checkpoint(`annotated-${report.annotations.length}`)
}
try {
  await writeSyntheticCredential(userData, { model: 'annotation-fixture-no-model-calls', disableNetworkObservability: true })
  app = await electron.launch({ args: [`--user-data-dir=${userData}`, join(repo, 'desktop/electron')],
    env: { ...env, OPENSQUILLA_DESKTOP_GATEWAY_PORT: String(port) }, cwd: repo, timeout: 120000 })
  page = await app.firstWindow()
  page.on('pageerror', error => report.errors.push(error.message))
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.waitForURL('opensquilla-app://desktop/**', { timeout: 120000 })
  await page.goto(`opensquilla-app://desktop/chat?session=${encodeURIComponent(manifest[0].key)}`)
  await page.locator('.workspace-file-link').first().waitFor({ timeout: 60000 })
  await app.evaluate(({ BrowserWindow }) => { const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('opensquilla-app:')); w.setBounds({x:40,y:40,width:1600,height:1000});w.show();w.focus() })
  const desktopIdentity=await app.evaluate(({BrowserWindow})=>({
    electronPid:process.pid,
    windows:BrowserWindow.getAllWindows().map(w=>({id:w.id,title:w.getTitle(),nativeHandle:w.getNativeWindowHandle().toString('hex')})),
  }))
  await writeFile(join(output, 'ready.json'), JSON.stringify({ output, electronLauncherPid: app.process().pid, ...desktopIdentity, origin, sessionA: manifest[0].key }, null, 2))
  console.log(`READY ${join(output, 'ready.json')}`)
  if (process.argv.includes('--manual')) {
    await waitForStop()
  } else {
    for (const label of ['A1','A2','A3','A4']) await open(label)
    await checkpoint('four-previews')
    await selectChat('B')
    for (const label of ['B1','B2','B3','B4']) await open(label)
    assert.equal((await nativePages()).length, 8)
    await checkpoint('eight-previews')
    await selectChat('A'); await open('A1')
    await annotate('A1','#heading','first annotation')
    await annotate('A1','#description','second annotation',false)
    await mode(false)
    await nativeClick('#counter')
    await waitFor(async () => (await nativePages()).find(p=>p.visible)?.counter === 1, 'ordinary page counter click')
    await nativeClick('#draft')
    await app.evaluate(async ({ webContents }, id) => { await webContents.fromId(id).insertText('retained A1 input') }, (await nativePages()).find(p=>p.visible).id)
    const identity = (await nativePages()).find(p=>p.visible).id
    await checkpoint('canonical-before-reenable')
    await mode(true)
    await checkpoint('canonical-after-reenable')
    assert.equal((await nativePages()).find(p=>p.visible).id,identity, 'Re-enabling annotation retains the current native document')
    await open('A2'); await annotate('A2','#heading','other preview annotation')
    await selectChat('B'); await open('B1'); await annotate('B1','#heading','other chat annotation')
    await selectChat('A'); await open('A1')
    assert.equal((await nativePages()).find(p=>p.visible).id,identity)
    assert.equal((await nativePages()).find(p=>p.visible).counter,1)
    assert.equal((await nativePages()).find(p=>p.visible).draft,'retained A1 input')
    await annotate('A1','#footer','returned annotation',false)
    await mode(false)
    await open('A2'); await open('A1')
    assert.equal(await toggle().getAttribute('aria-pressed'),'false')
    await selectChat('B'); await open('B1'); await selectChat('A'); await open('A1')
    assert.equal(await toggle().getAttribute('aria-pressed'),'false', 'Explicit stop survives chat switches')
    await nativeClick('#counter')
    await waitFor(async () => (await nativePages()).find(p=>p.visible)?.counter === 2, 'ordinary click after stopped chat round trip')
    await checkpoint('stopped-mode-retained')
    const priorHash = (await nativePages()).find(p=>p.visible).hash
    await nativeClick('#delayed-hash')
    await waitFor(async () => (await nativePages()).find(p=>p.visible)?.delayedText==='Navigation scheduled','actual navigation timer scheduled by HTML button')
    await mode(true)
    await checkpoint('navigation-scheduled-and-enabled')
    assert.equal((await nativePages()).find(p=>p.visible).id,identity,'Re-enabling after chat round trips retains the scheduled navigation and native page')
    await waitFor(async () => (await nativePages()).find(p=>p.visible)?.hash !== priorHash, 'actual same-document navigation', 10000)
    await annotate('A1','#description','annotation after page navigation',false)
    for (let cycle=0;cycle<5;cycle++) {
      report.rapidRoundTrip=cycle+1
      await open('A2');await open('A1');await selectChat('B');await open('B1');await selectChat('A');await open('A1')
    }
    await annotate('A1','#heading','after five rapid round trips',false)
    await annotate('A1','#description','continuous annotation after rapid round trips',false)
    const sourcePreviewIdentity=(await nativePages()).find(p=>p.visible).id
    for(let cycle=0;cycle<5;cycle++){
      await page.getByTestId('workbench-host').getByRole('tab',{name:'Source',exact:true}).click()
      await page.getByTestId('workbench-host').getByRole('tab',{name:'Preview',exact:true}).click()
    }
    await waitFor(async()=>(await nativePages()).find(p=>p.visible)?.id===sourcePreviewIdentity,'same native preview restored after Source/Preview round trips')
    report.sourcePreviewCycles=5
    await annotate('A1','#footer','annotation after five Source Preview round trips',false)
    await retainedDraftRoundTrip()
    assert.equal((await nativePages()).length,8,'Exactly eight retained native pages remain without duplicate surfaces')
    assert.deepEqual(report.errors, [], 'No renderer page errors')
    report.status='passed'
    await checkpoint('complete')
    console.log(`PASS ${join(output,'report.json')}`)
    if (hold) await waitForStop()
  }
} catch (error) {
  report.status='failed';report.failure=String(error.stack||error);await persist()
  if(page) await page.screenshot({path:join(output,'failure-webui.png')}).catch(()=>{})
  console.error(error)
  if(hold) await waitForStop()
  process.exitCode=1
} finally {
  await app?.close().catch(()=>{})
  await persist()
}
