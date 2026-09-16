import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { after, before, test } from 'node:test'
import { chromium } from 'playwright'
import { createBusinessDriver, verifyBusinessCase } from './live-html-journey-business.mjs'
import { createSupplementalClient } from './live-html-supplemental-client.mjs'

let browser
before(async () => { browser = await chromium.launch() })
after(async () => { await browser?.close() })

async function fixture(html) {
  const context = await browser.newContext({ viewport: { width: 800, height: 700 } })
  const page = await context.newPage()
  await page.setContent(html)
  const cdp = await context.newCDPSession(page)
  const failures = [], inputs = []
  const contents = {
    isDestroyed: () => false,
    focus() {},
    async executeJavaScript(expression) {
      try { return await page.evaluate(expression) }
      catch (error) { failures.push(String(error.message)); throw error }
    },
    debugger: {
      isAttached: () => true,
      async sendCommand(command, args) { inputs.push(command); return cdp.send(command, args) },
    },
  }
  const app = { evaluate: (fn, args) => fn({ webContents: { fromId: () => contents } }, args) }
  return { page, failures, inputs, driver: createBusinessDriver(app, () => 1, async () => {}), close: () => context.close() }
}

test('moving modal close waits for actionability and receives a real pointer click', async () => {
  const f = await fixture(`
    <style>
      @keyframes enter { from { transform: translateX(300px); } to { transform: translateX(0); } }
      #panel { position:fixed;top:100px;left:100px;padding:60px;background:white;animation:enter .8s linear; }
    </style>
    <button onclick="panel.hidden=false">Open</button>
    <div id="panel" hidden><button onclick="window.closedByPointer=(window.closedByPointer||0)+1;panel.hidden=true">Close</button></div>`)
  try {
    await f.driver.click('Open')
    await f.page.waitForFunction(() => document.getElementById('panel').getAnimations()[0]?.currentTime > 100)
    await f.driver.click('Close')
    assert.equal(await f.page.evaluate(() => window.closedByPointer), 1)
    assert.equal(await f.page.locator('#panel').isVisible(), false)
    assert.equal(f.inputs.filter(command => command === 'Input.dispatchMouseEvent').length, 6)
  } finally { await f.close() }
})

for (const kind of ['disabled', 'covered']) {
  test(`${kind} control remains a failure without forced input`, async () => {
    const f = await fixture(`
      <button ${kind === 'disabled' ? 'disabled' : ''} onclick="window.clicked=true">Blocked</button>
      ${kind === 'covered' ? '<div style="position:fixed;inset:0;background:white;z-index:10"></div>' : ''}`)
    try {
      await assert.rejects(f.driver.click('Blocked'), new RegExp(kind === 'disabled' ? 'SEMANTIC_CONTROL_UNAVAILABLE' : 'SEMANTIC_CONTROL_COVERED'))
      assert.equal(f.inputs.length, 0)
      assert.equal(await f.page.evaluate(() => Boolean(window.clicked)), false)
    } finally { await f.close() }
  })
}

test('dialog title transitions distinguish persistent page titles and same-name footer links', async () => {
  const f = await fixture(`
    <p>澄光协作 任务视图 团队协作 进度洞察 免费版 ¥0 团队版 ¥49</p>
    <h2>体验申请</h2><footer><a href="#apply">体验申请</a></footer>
    <button onclick="panel.hidden=false">开始体验</button>
    <div id="panel" hidden><h3>体验申请</h3><button onclick="panel.hidden=true">关闭</button></div>`)
  const checks = []
  try {
    assert.deepEqual((await f.driver.read()).visibleLabels.filter(label => label === '体验申请'), ['体验申请'])
    await verifyBusinessCase({ caseId: 'landing', stage: 'generation', driver: f.driver, record: value => checks.push(value) })
    assert.ok(checks.every(value => value.passed))
    assert.ok(checks.some(value => value.name === 'application-closes'))
    assert.equal(await f.page.locator('#panel').isVisible(), false)
  } finally { await f.close() }
})

test('first business failure retains its check and blocks dependent actions', async () => {
  const checks = []
  let clicked = false
  const driver = { read: async () => ({ text: '', visibleLabels: [] }), click: async () => { clicked = true } }
  await assert.rejects(verifyBusinessCase({ caseId: 'landing', stage: 'generation', driver, record: value => checks.push(value) }), error => error.message === 'BUSINESS_CHECK_FAILED' && error.diagnostic.check === 'content:澄光协作' && error.diagnostic.businessStage === 'generation')
  assert.equal(clicked, false)
  assert.deepEqual(checks, [{ name: 'content:澄光协作', passed: false }])
})

test('wrapped browser action error preserves the first semantic cause', async () => {
  const checks = []
  const driver = {
    read: async () => ({ text: '澄光协作 任务视图 团队协作 进度洞察 免费版 ¥0 团队版 ¥49', visibleLabels: [] }),
    click: async () => { throw new Error('webContents.evaluate: Error: SEMANTIC_CONTROL_COVERED') },
  }
  await assert.rejects(verifyBusinessCase({ caseId: 'landing', stage: 'generation', driver, record: value => checks.push(value) }), error => error.message === 'BUSINESS_CHECK_FAILED' && error.diagnostic.cause === 'SEMANTIC_CONTROL_COVERED' && error.diagnostic.businessStage === 'generation')
  assert.equal(checks.filter(value => !value.passed).length, 1)
  assert.equal(checks.at(-1).code, 'SEMANTIC_CONTROL_COVERED')
})

test('rendered text preserves inline words and block boundaries while excluding hidden content', async () => {
  const f = await fixture(`
    <h1>暮色<span>研究</span></h1>
    <p>保留 <em>词间</em> 空格</p><p>另一段</p><p>上行<br>下行</p>
    <div hidden>隐藏内容</div><div style="display:none">未显示内容</div>
    <div style="visibility:hidden">不可见内容</div>
    <div style="opacity:0"><span>透明内容</span></div>`)
  try {
    assert.equal((await f.driver.read()).renderedText, '暮色研究 保留 词间 空格 另一段 上行 下行')
    await f.page.locator('h1').evaluate(element => { element.firstChild.textContent = '海岬' })
    assert.ok((await f.driver.read()).renderedText.startsWith('海岬研究 '))
    assert.ok(!(await f.driver.read()).renderedText.includes('暮色研究'))
  } finally { await f.close() }
})

for (const [label, heading] of [
  ['plain title', '<h1>让团队计划真正落地</h1>'],
  ['explicit line break', '<h1>让团队计划<br>真正落地</h1>'],
  ['inline emphasis', '<h1>让团队计划<strong>真正</strong>落地</h1>'],
  ['block span line', '<h1>让团队计划<span style="display:block">真正落地</span></h1>'],
  ['layout whitespace', '<h1> 让团队计划\n <span>真正\t落地</span> </h1>'],
  ['semantic level-one heading', '<div role="heading" aria-level="1">让团队计划<br>真正落地</div>'],
]) test(`landing hero accepts visible title layout: ${label}`, async () => {
  const f = await fixture(`
    ${heading}<p>澄光协作 任务视图 团队协作 进度洞察 免费版 ¥0 团队版 ¥49</p>
    <button onclick="panel.hidden=false">立即体验</button>
    <div id="panel" hidden><h2>体验申请</h2><button onclick="panel.hidden=true">关闭</button></div>`)
  const checks = []
  try {
    await verifyBusinessCase({ caseId: 'landing', stage: 'annotation', driver: f.driver, record: value => checks.push(value) })
    assert.ok(checks.every(value => value.passed))
    assert.ok(checks.some(value => value.name === 'hero-value-proposition' && value.passed))
    assert.ok(checks.some(value => value.name === 'application-closes' && value.passed))
  } finally { await f.close() }
})

for (const [label, content] of [
  ['missing character', '<h1>让团队计划真落地</h1>'],
  ['changed wording', '<h1>让团队计划真正上线</h1>'],
  ['hidden core text', '<h1>让团队计划<span hidden>真正落地</span></h1>'],
  ['transparent core text', '<h1>让团队计划<span style="opacity:0">真正落地</span></h1>'],
  ['invisible title', '<h1 style="visibility:hidden">让团队计划真正落地</h1>'],
  ['separate content regions', '<h1>让团队计划</h1><section><p>真正落地</p></section>'],
  ['complete phrase outside title', '<h1>欢迎体验</h1><p>让团队计划真正落地</p>'],
  ['separate level-one headings', '<h1>让团队计划</h1><h1>真正落地</h1>'],
  ['separate paragraphs inside heading container', '<div role="heading" aria-level="1"><p>让团队计划</p><p>真正落地</p></div>'],
]) test(`landing hero rejects missing visible title content: ${label}`, async () => {
  const f = await fixture(`${content}<p>澄光协作 任务视图 团队协作 进度洞察 免费版 ¥0 团队版 ¥49</p>`)
  const checks = []
  try {
    await assert.rejects(verifyBusinessCase({ caseId: 'landing', stage: 'annotation', driver: f.driver, record: value => checks.push(value) }),
      error => error.message === 'BUSINESS_CHECK_FAILED' && error.diagnostic.check === 'hero-value-proposition')
    assert.equal(f.inputs.length, 0, 'invalid title must stop before an interaction')
    assert.equal(checks.at(-1).name, 'hero-value-proposition')
    assert.equal(checks.at(-1).passed, false)
  } finally { await f.close() }
})

test('dashboard metrics read semantic definition pairs without borrowing adjacent values', async () => {
  const f = await fixture('<dl><dt>客户数</dt><dd>6</dd><dt>金额合计</dt><dd>6,000</dd></dl>')
  try {
    assert.deepEqual((await f.driver.read()).metrics, { 客户数: 6, 金额合计: 6000 })
    await f.page.locator('dd').evaluateAll(elements => elements.forEach(element => { element.textContent = '0' }))
    assert.deepEqual((await f.driver.read()).metrics, { 客户数: 0, 金额合计: 0 })
    await f.page.locator('dd').first().evaluate(element => element.insertAdjacentHTML('afterend', '<dd>99</dd>'))
    assert.equal((await f.driver.read()).metrics['客户数'], undefined, 'multiple definitions must remain ambiguous')
    assert.equal((await f.driver.read()).metrics['金额合计'], 0)
  } finally { await f.close() }
})

test('labelled-by controls retain the same names in interactions, state, and mobile geometry', async () => {
  const f = await fixture(`
    <style>input,select,button{display:block;margin-bottom:20px}</style>
    <span id="name-label">姓名</span><input aria-labelledby="name-label">
    <span id="email-label">邮箱</span><input type="email" aria-labelledby="email-label">
    <span id="session-label">参加场次</span><span id="session-suffix"></span>
    <select aria-labelledby="session-label session-suffix"><option>请选择场次</option><option>上午场</option></select>
    <button aria-labelledby="submit-label"><span id="submit-label">提交报名</span></button>`)
  try {
    await f.driver.fill('姓名', '合成访客')
    await f.driver.fill('邮箱', 'visitor@example.test')
    await f.driver.choose('参加场次', '上午场')
    assert.deepEqual((await f.driver.read()).controls.map(item => item.name), ['姓名', '邮箱', '参加场次'])
    const source = await readFile(new URL('./live-html-journey.mjs', import.meta.url), 'utf8')
    const expressions = [...source.matchAll(/const geometry = await contents\.executeJavaScript\((`[^`]+`)\)/g)]
    assert.equal(expressions.length, 1)
    // Run the actual serialized mobile observation in Chromium, without starting a journey.
    const geometry = await f.page.evaluate(Function(`return ${expressions[0][1]}`)())
    assert.deepEqual(geometry.controls.map(item => item.name), ['姓名', '邮箱', '参加场次', '提交报名'])
    assert.ok(geometry.controls.slice(1).every((item, index) => item.top >= geometry.controls[index].bottom))
  } finally { await f.close() }
})

test('registration checks wait for local validation feedback without losing required errors', async () => {
  const f = await fixture(`
    <form novalidate>
      <label>姓名<input id="name" required></label><small id="nameHint"></small>
      <label>邮箱<input id="email" type="email" required></label><small id="emailHint"></small>
      <label>参加场次<select id="session" required><option value="">请选择场次</option><option>上午场</option></select></label><small id="sessionHint"></small>
      <button>提交报名</button><p id="result"></p>
    </form>
    <script>
      document.querySelector('form').addEventListener('submit', event => {
        event.preventDefault();
        for (const id of ['nameHint', 'emailHint', 'sessionHint', 'result']) document.getElementById(id).textContent = '';
        setTimeout(() => {
          const name = document.getElementById('name'), email = document.getElementById('email'), session = document.getElementById('session');
          if (!name.value.trim()) document.getElementById('nameHint').textContent = '请填写姓名';
          else if (!email.validity.valid) document.getElementById('emailHint').textContent = '请输入有效邮箱';
          else if (!session.value) document.getElementById('sessionHint').textContent = '请选择场次';
          else document.getElementById('result').textContent = '报名成功';
        }, 80);
      });
    </script>`)
  const checks = []
  let reloads = 0
  f.driver.reload = async () => { reloads++ }
  try {
    await verifyBusinessCase({ caseId: 'registration', stage: 'annotation', driver: f.driver, record: value => checks.push(value) })
    assert.ok(checks.every(value => value.passed))
    for (const name of ['name-inline-hint', 'email-inline-hint', 'session-inline-hint', 'valid-registration-confirmed']) assert.ok(checks.some(value => value.name === name))
    assert.equal(reloads, 1)
  } finally { await f.close() }
})

test('registration still fails when required inline feedback never appears', async () => {
  const f = await fixture('<form><label>姓名<input required></label><button>提交报名</button></form>')
  try {
    await assert.rejects(verifyBusinessCase({ caseId: 'registration', stage: 'annotation', driver: f.driver, record() {} }), error => error.message === 'BUSINESS_CHECK_FAILED' && error.diagnostic.check === 'name-inline-hint')
  } finally { await f.close() }
})

async function supplementalRestoreFixture({ label = 'Version', asynchronous = false, duplicate = false } = {}) {
  const context = await browser.newContext()
  const page = await context.newPage()
  await page.setContent('<button role="tab">Versions</button><ol class="artifact-document__versions"></ol>')
  const rows = (duplicate ? [1, 1] : [12, 2, 1]).map(generation =>
    `<li><span class="artifact-document__list-main"><span class="artifact-document__version-title"><strong>Original version</strong></span><small>${label} ${generation}</small></span><span class="artifact-document__list-meta"><time>9/9/2026</time><button data-artifact-action="restore-revision" data-generation="${generation}">Restore this version</button></span></li>`).join('')
  await page.evaluate(({ rows, asynchronous }) => {
    const render = () => { document.querySelector('ol').innerHTML = rows }
    if (!asynchronous) render()
    document.querySelector('[role=tab]').addEventListener('click', () => {
      if (asynchronous) setTimeout(render, 80)
    })
    document.querySelector('ol').addEventListener('click', event => {
      const button = event.target.closest('[data-artifact-action]')
      if (!button) return
      window.restoreClicks = [...(window.restoreClicks || []), Number(button.dataset.generation)]
      // Stop after the actual DOM selection/click; RPC receipt behavior has its own tests.
      window.__htmlSupplementalObservation.reviewRequests.push({ method: 'artifacts.revisions.restore', respondedAt: Date.now(), ok: false })
    })
  }, { rows, asynchronous })
  const snapshot = { published: [{ entrypoint: 'supplemental.html', headDocumentIds: ['doc'] }],
    documents: [{ document_id: 'doc', head_revision_id: 'r1' }], revisions: [{ revision_id: 'r1', generation: 1 }] }
  const state = { artifact_documents: [{ document_id: 'doc', head_revision_id: 'r2', generation: 2 }],
    artifact_revisions: [{ document_id: 'doc', revision_id: 'r1' }, { document_id: 'doc', revision_id: 'r2' }] }
  const client = await createSupplementalClient({}, page, { report: { events: [] }, persist: async () => {},
    readState: async () => state, interrupted: () => false })
  return { page, client, snapshot, close: () => context.close() }
}

for (const label of ['Version', '版本']) test(`supplemental version selection isolates ${label} from adjacent date digits`, async () => {
  const f = await supplementalRestoreFixture({ label })
  try {
    const oldMatch = f.page.locator('.artifact-document__versions > li').filter({ hasText: /(?:Version|版本)\s*1(?:\D|$)/ })
    assert.equal(await oldMatch.count(), 0, 'the old whole-row boundary rejects the rendered Version 1 next to a date')
    await assert.rejects(f.client.restoreOriginalVersion(f.snapshot), /SUPPLEMENTAL_RESTORE_RPC_FAILED/)
    assert.deepEqual(await f.page.evaluate(() => window.restoreClicks), [1])
  } finally { await f.close() }
})

test('supplemental version selection waits for the asynchronous list after tab selection', async () => {
  const f = await supplementalRestoreFixture({ asynchronous: true })
  try {
    await assert.rejects(f.client.restoreOriginalVersion(f.snapshot), /SUPPLEMENTAL_RESTORE_RPC_FAILED/)
    assert.deepEqual(await f.page.evaluate(() => window.restoreClicks), [1])
  } finally { await f.close() }
})

test('supplemental version selection rejects genuinely ambiguous rows before clicking', async () => {
  const f = await supplementalRestoreFixture({ duplicate: true })
  try {
    await assert.rejects(f.client.restoreOriginalVersion(f.snapshot), /SUPPLEMENTAL_ORIGINAL_VERSION_ROW_AMBIGUOUS/)
    assert.equal(await f.page.evaluate(() => window.restoreClicks?.length || 0), 0)
  } finally { await f.close() }
})
