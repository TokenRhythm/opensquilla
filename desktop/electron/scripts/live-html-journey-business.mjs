import { setTimeout as delay } from 'node:timers/promises'

export const DASHBOARD_ROWS = Object.freeze([
  { customer: '辰星工作室', region: '华东', status: '已完成', amount: 1200 },
  { customer: '北岸设计', region: '华北', status: '待跟进', amount: 800 },
  { customer: '青禾商店', region: '华东', status: '已完成', amount: 1600 },
  { customer: '南桥团队', region: '华南', status: '已完成', amount: 900 },
  { customer: '远山实验室', region: '华北', status: '待跟进', amount: 500 },
  { customer: '汐岸书屋', region: '华南', status: '已完成', amount: 1000 },
])

export const JOURNEY_CASES = {
  landing: {
    brief: '为虚构的团队计划产品「澄光协作」制作精致的中文产品发布页，包含有设计感的首屏、任务视图/团队协作/进度洞察三个功能介绍、免费版0元/月和团队版49元/人/月的价格区，以及清晰的主CTA「开始体验」。点击CTA要在本页打开标题为「体验申请」的面板，可通过「关闭」回到页面；仅做本地演示，不向外部发送申请。请自主设计视觉风格、排版与装饰，使用CSS图形或内嵌SVG，避免外部图片依赖。',
    selection: { role: 'button', name: '开始体验', allowLink: true },
    annotation: '请重做这个首屏的信息层级：突出「让团队计划真正落地」这一核心价值，主标题、副说明和按钮之间有清晰的字号与留白层次。把这个主CTA改为「立即体验」，提高识别度，并保持点击打开「体验申请」、点击「关闭」返回页面的行为。保留功能与价格内容。',
    followup: '请让同一页面的功能区和价格区与改好的首屏在字体、间距、色彩和按钮风格上统一，完善手机端布局。保留三个功能、两个价格方案和「立即体验」的可用行为。',
  },
  dashboard: {
    brief: '制作中文客户运营仪表盘，使用下方给定的六条合成数据，不得增删或修改数据。页面应有「客户数」「金额合计」两项统计、客户表格、带清楚可访问名称的「地区」「状态」筛选和「搜索客户」搜索框。地区支持全部地区/华东/华北/华南；状态支持全部状态/已完成/待跟进。统计和表格立即随组合筛选更新，搜索按客户名包含匹配，无结果显示「没有匹配客户」。设计要有清晰层级、可读表格和手机布局。数据：' + JSON.stringify(DASHBOARD_ROWS),
    selection: { name: '地区', control: true },
    annotation: '请整理这块筛选区：桌面端对齐并明确区分地区、状态与客户搜索，手机端自然纵向排列。增加「重置筛选」按钮，一次清空所有筛选和搜索，恢复完整数据及统计。筛选条件变化时，表格、客户数和金额合计必须同步更新。',
    followup: '请补齐这个仪表盘的交互边界：组合地区、状态和客户搜索时结果准确；无匹配客户时显示清楚的空状态，客户数和金额合计都为0；重置后恢复全部六条数据；连续切换条件不能显示旧统计。保持已改好的筛选布局和原始合成数据。',
  },
  registration: {
    brief: '为虚构的「城市创意日」制作完整的活动报名页，有活动日程、嘉宾介绍以及带清楚可访问名称的「姓名」「邮箱」「参加场次」表单字段。场次初始为「请选择场次」，可选择「上午场」或「下午场」，按钮文案「提交报名」。三个字段都必填，邮箱格式要校验，错误时给出可理解的提示，成功时在本页显示「报名成功」；这是本地演示，不向外部发送个人信息。必须把HTML、CSS、JavaScript分为独立文件，并用相对路径加载。桌面端有清晰分栏，手机端顺序合理且无横向滚动。请自主设计有吸引力的视觉风格。',
    selection: { name: '邮箱', control: true },
    annotation: '请改善这块报名表单的提示与手机排列：缺少姓名时提示「请填写姓名」，邮箱格式错误时提示「请输入有效邮箱」，没选场次时提示「请选择场次」。提示要靠近对应字段并易读，提交成功仍显示「报名成功」。手机端按姓名、邮箱、参加场次、提交报名的顺序单列排列，保留桌面端布局。',
    followup: '请继续通过这个项目的独立CSS文件统一表单焦点态、错误态、重点提示和主按钮样式，使用清晰的深蓝强调色，并让手机端间距更舒适。保持HTML、CSS、JavaScript分离，保留刚才的错误提示、必填与邮箱校验、成功确认和移动排列。',
  },
}

export function journeyPrompts(caseId, filename) {
  const item = JOURNEY_CASES[caseId]
  return {
    generation: `${item.brief} 请直接实现为可打开的完整页面项目，以名为 ${filename} 的HTML入口产物交付。页面应有清晰主标题和响应式布局，不要只给代码示例。`,
    selection: item.annotation,
    annotation: '请根据我刚提交的页面批注修改这个页面，并交付更新后的结果。',
    followup: item.followup,
  }
}

// This runs in the generated page. It resolves public labels and roles without
// depending on generated classes, IDs, component names, or a prescribed DOM tree.
async function semanticControl(spec) {
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim()
  const visible = element => {
    const rect = element.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return false
    for (let node = element; node instanceof Element; node = node.parentElement) {
      const style = getComputedStyle(node)
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || Number(style.opacity) <= 0.01) return false
    }
    return true
  }
  const role = element => element.getAttribute('role') || ({ BUTTON: 'button', A: element.hasAttribute('href') ? 'link' : '', SELECT: 'combobox', TEXTAREA: 'textbox', OPTION: 'option' })[element.tagName] || (element.tagName === 'INPUT' ? (['button', 'submit'].includes(element.type) ? 'button' : 'textbox') : '')
  const name = element => normalize(element.getAttribute('aria-label') || (element.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ') || [...(element.labels || [])].map(label => label.innerText).join(' ') || element.getAttribute('placeholder') || (['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName) ? '' : element.innerText) || element.value)
  const pool = [...document.querySelectorAll('button,a[href],input,textarea,select,[role="button"],[role="combobox"],[role="textbox"],[role="option"]')].filter(visible).filter(element => {
    if (spec.control && !['combobox', 'textbox'].includes(role(element))) return false
    return !spec.role || role(element) === spec.role || (spec.allowLink && role(element) === 'link')
  })
  const wanted = normalize(spec.name)
  const candidates = pool.filter(element => name(element) === wanted)
  const element = candidates[0] || pool.find(item => name(item).includes(wanted))
  if (!element) throw new Error('SEMANTIC_CONTROL_MISSING')
  element.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' })
  let rect = element.getBoundingClientRect()
  let stable = false
  for (let attempt = 0; attempt < 8; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 16))
    const next = element.getBoundingClientRect()
    stable = ['x', 'y', 'width', 'height'].every(key => Math.abs(next[key] - rect[key]) <= 0.5)
    rect = next
    if (stable) break
  }
  if (!element.isConnected || !visible(element) || !stable || element.matches(':disabled,[aria-disabled="true"]')) throw new Error('SEMANTIC_CONTROL_UNAVAILABLE')
  const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right)
  const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom)
  const x = (left + right) / 2, y = (top + bottom) / 2
  const hit = document.elementFromPoint(x, y)
  if (right <= left || bottom <= top || !hit || (hit !== element && !element.contains(hit))) throw new Error('SEMANTIC_CONTROL_COVERED')
  if (spec.kind === 'fill') { element.focus(); if (typeof element.select === 'function') element.select() }
  if (spec.kind === 'choose' && element.tagName === 'SELECT') {
    const index = [...element.options].findIndex(option => normalize(option.text) === normalize(spec.value))
    if (index < 0) throw new Error('SEMANTIC_OPTION_MISSING')
    element.selectedIndex = index
    element.dispatchEvent(new Event('input', { bubbles: true }))
    element.dispatchEvent(new Event('change', { bubbles: true }))
    return { nativeSelection: true }
  }
  return { x, y, role: role(element), name: name(element), tag: element.tagName }
}

export function createBusinessDriver(app, getPreviewId, capture) {
  const evaluate = expression => app.evaluate(async ({ webContents }, request) => {
    const contents = webContents.fromId(request.id)
    if (!contents || contents.isDestroyed()) throw new Error('SELECTED_PREVIEW_DESTROYED')
    return contents.executeJavaScript(request.expression, true)
  }, { id: getPreviewId(), expression })
  const action = async (kind, spec) => {
    const result = await app.evaluate(async ({ webContents }, request) => {
      const contents = webContents.fromId(request.id)
      if (!contents || contents.isDestroyed()) throw new Error('SELECTED_PREVIEW_DESTROYED')
      let point
      const until = Date.now() + 4000
      while (!point) {
        try { point = await contents.executeJavaScript(`(${request.resolver})(${JSON.stringify({ ...request.spec, kind: request.kind })})`, true) }
        catch (error) {
          if (!/SEMANTIC_CONTROL_(?:MISSING|UNAVAILABLE|COVERED)\b/.test(String(error)) || Date.now() >= until) throw error
          await new Promise(resolve => setTimeout(resolve, 100))
        }
      }
      if (request.kind === 'point' || point.nativeSelection) return point
      contents.focus()
      const attachedHere = !contents.debugger.isAttached()
      if (attachedHere) contents.debugger.attach('1.3')
      try {
        if (request.kind === 'fill') await contents.debugger.sendCommand('Input.insertText', { text: request.spec.value })
        else for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await contents.debugger.sendCommand('Input.dispatchMouseEvent', { type, x: point.x, y: point.y, button: type === 'mouseMoved' ? 'none' : 'left', clickCount: 1 })
      } finally { if (attachedHere) contents.debugger.detach() }
      return point
    }, { id: getPreviewId(), resolver: semanticControl.toString(), kind, spec })
    if (kind === 'choose' && !result.nativeSelection) await action('click', { role: 'option', name: spec.value })
    return result
  }
  const read = () => evaluate(`(() => {
    const text=document.body.innerText;
    const visible=e=>{const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;for(let n=e;n instanceof Element;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)<=0.01)return false}return true};
    const rendered=node=>{
      if(node.nodeType===Node.TEXT_NODE)return visible(node.parentElement)?node.textContent||'':'';
      if(!(node instanceof Element)||['SCRIPT','STYLE','NOSCRIPT'].includes(node.tagName))return '';
      const style=getComputedStyle(node);
      if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse'||Number(style.opacity)<=0.01)return '';
      if(node.tagName==='BR')return visible(node.parentElement)?' ':'';
      const value=[...node.childNodes].map(rendered).join('');
      return style.display.startsWith('inline')||style.display==='contents'?value:' '+value+' ';
    };const renderedText=rendered(document.body).replace(/\\s+/g,' ').trim();
    const interactive='a[href],button,input,select,textarea,[role="button"],[role="link"]';const normalize=value=>(value||'').replace(/\\s+/g,' ').trim();
    const primaryHeadings=[...document.querySelectorAll('h1,[role="heading"][aria-level="1"]')]
      .filter(e=>visible(e)&&!e.querySelector('p,h1,h2,h3,h4,h5,h6,[role="heading"]'))
      .map(e=>normalize(rendered(e)));
    const visibleLabels=[...document.querySelectorAll('body *')].filter(e=>visible(e)&&!e.closest(interactive)&&!e.querySelector(interactive)&&!['SCRIPT','STYLE','NOSCRIPT'].includes(e.tagName)).filter(e=>{const label=normalize(e.textContent);return label&&label.length<=100&&![...e.children].some(child=>normalize(child.textContent)===label)}).map(e=>normalize(e.textContent));
    const metrics={};
    const metricValue=area=>{const digits=(area.innerText||'').replaceAll(',','').match(/(?:^|[^0-9])[0-9]+(?:\\.[0-9]+)?/g);return digits?.length===1?Number(digits[0].replace(/[^0-9.]/g,'')):undefined};
    for(const label of ['客户数','金额合计']) {
      const nodes=[...document.querySelectorAll('body *')].filter(e=>visible(e)&&e.textContent.trim()===label);
      for(const node of nodes) {
        const term=node.closest('dt');
        if(term&&term.textContent.trim()===label){
          const definitions=[];for(let next=term.nextElementSibling;next&&next.tagName!=='DT';next=next.nextElementSibling)if(next.tagName==='DD'&&visible(next))definitions.push(next);
          const value=definitions.length===1?metricValue(definitions[0]):undefined;
          if(value!==undefined){metrics[label]=value;break}
          continue;
        }
        let area=node;for(let i=0;i<4&&area;i++,area=area.parentElement){const value=metricValue(area);if(value!==undefined){metrics[label]=value;break}}
        if(label in metrics)break;
      }
    }
    const name=e=>normalize(e.getAttribute('aria-label')||(e.getAttribute('aria-labelledby')||'').split(/\\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' ')||[...(e.labels||[])].map(l=>l.innerText).join(' ')||e.getAttribute('placeholder')||(['INPUT','TEXTAREA','SELECT'].includes(e.tagName)?'':e.innerText)||e.value);
    return {text,renderedText,primaryHeadings,visibleLabels,metrics,controls:[...document.querySelectorAll('input,select,textarea')].filter(visible).map(e=>({name:name(e),type:e.type,value:e.value,invalid:e.validity?!e.validity.valid:e.getAttribute('aria-invalid')==='true'}))};
  })()`)
  return {
    point: spec => action('point', spec),
    click: (name, extra = {}) => action('click', { role: 'button', name, ...extra }),
    fill: (name, value) => action('fill', { name, control: true, value }),
    choose: (name, value) => action('choose', { name, control: true, value }),
    read,
    capture,
    async settle(check) {
      let result
      const until = Date.now() + 4000
      do { result = await read(); if (check(result)) return result; await delay(100) } while (Date.now() < until)
      return result
    },
    async reload() {
      await app.evaluate(async ({ webContents }, id) => {
        const contents = webContents.fromId(id)
        if (!contents || contents.isDestroyed()) throw new Error('SELECTED_PREVIEW_DESTROYED')
        await new Promise((resolve, reject) => {
          const timer=setTimeout(()=>reject(new Error('PREVIEW_RELOAD_EXPIRED')),15000)
          contents.once('did-finish-load',()=>{clearTimeout(timer);resolve()})
          contents.reload()
        })
      }, getPreviewId())
    },
  }
}

export function expectedDashboard({ region = '', status = '', search = '' } = {}) {
  const rows = DASHBOARD_ROWS.filter(row => (!region || row.region === region) && (!status || row.status === status) && row.customer.includes(search))
  return { names: rows.map(row => row.customer), count: rows.length, amount: rows.reduce((sum, row) => sum + row.amount, 0) }
}

export async function verifyBusinessCase({ caseId, stage, driver, record }) {
  const check = (name, passed, detail = {}) => {
    record({ name, passed: Boolean(passed), ...detail })
    if (!passed) {
      const error = new Error('BUSINESS_CHECK_FAILED')
      error.diagnostic = { businessStage: stage, check: name, ...(detail.code ? { cause: detail.code } : {}) }
      throw error
    }
  }
  try {
    if (caseId === 'landing') {
      const initial = await driver.read()
      for (const value of ['澄光协作', '任务视图', '团队协作', '进度洞察', '免费版', '团队版']) check(`content:${value}`, initial.text.includes(value))
      check('pricing-values', /(?:0\s*元|[¥￥]\s*0)/.test(initial.text) && /(?:49\s*元|[¥￥]\s*49)/.test(initial.text))
      if (stage !== 'generation') check('hero-value-proposition', initial.primaryHeadings.some(heading => heading.replace(/\s+/g, '').includes('让团队计划真正落地')))
      const applicationTitles = state => state.visibleLabels.filter(label => label.replace(/\s+/g, '') === '体验申请').length
      const initialTitles = applicationTitles(initial)
      await driver.click(stage === 'generation' ? '开始体验' : '立即体验', { allowLink: true })
      const applicationVisible = state => applicationTitles(state) > initialTitles
      const opened = await driver.settle(applicationVisible)
      check('cta-opens-application', applicationVisible(opened))
      await driver.capture('application-open')
      await driver.click('关闭')
      const closed = await driver.settle(state => !applicationVisible(state))
      check('application-closes', !applicationVisible(closed))
      return
    }
    if (caseId === 'dashboard') {
      const verify = async (label, filter) => {
        const expected = expectedDashboard(filter)
        const matches = state => DASHBOARD_ROWS.every(row => state.text.includes(row.customer) === expected.names.includes(row.customer)) && state.metrics['客户数'] === expected.count && state.metrics['金额合计'] === expected.amount
        const state = await driver.settle(matches)
        const actualNames = DASHBOARD_ROWS.filter(row => state.text.includes(row.customer)).map(row => row.customer)
        check(label, matches(state), { expected, actual: { names: actualNames, count: state.metrics['客户数'] ?? null, amount: state.metrics['金额合计'] ?? null } })
        if (expected.count === 0) check(`${label}:empty-state`, state.text.includes('没有匹配客户'))
      }
      await verify('all-data', {})
      await driver.choose('地区', '华东')
      await verify('east', { region: '华东' })
      await driver.choose('状态', '待跟进')
      await verify('east-pending-empty', { region: '华东', status: '待跟进' })
      await driver.capture('empty-state')
      await driver.choose('地区', '华北')
      await driver.fill('搜索客户', '远山')
      await verify('combined-filter', { region: '华北', status: '待跟进', search: '远山' })
      await driver.fill('搜索客户', '不存在客户XYZ')
      await verify('search-empty', { region: '华北', status: '待跟进', search: '不存在客户XYZ' })
      if (stage === 'generation') {
        await driver.choose('地区', '全部地区'); await driver.choose('状态', '全部状态'); await driver.fill('搜索客户', '')
      } else await driver.click('重置筛选')
      await verify('reset-restores-all', {})
      await driver.choose('地区', '华南'); await driver.choose('地区', '华东'); await driver.choose('地区', '华北')
      await verify('consecutive-filter-latest-state', { region: '华北' })
      if (stage === 'generation') await driver.choose('地区', '全部地区')
      else await driver.click('重置筛选')
      await verify('final-reset', {})
      return
    }
    await driver.click('提交报名')
    const emptyBlocked = state => !state.text.includes('报名成功') && (state.controls.some(item => item.invalid) || /请填写|必填|请选择/.test(state.text))
    const empty = await driver.settle(state => emptyBlocked(state) && (stage === 'generation' || state.renderedText.includes('请填写姓名')))
    check('empty-submit-blocked', emptyBlocked(empty))
    if (stage !== 'generation') check('name-inline-hint', empty.renderedText.includes('请填写姓名'))
    await driver.fill('姓名', '合成访客')
    await driver.fill('邮箱', 'not-an-email')
    await driver.choose('参加场次', '上午场')
    await driver.click('提交报名')
    const emailBlocked = state => !state.text.includes('报名成功') && (state.controls.some(item => item.name.includes('邮箱') && item.invalid) || /邮箱.{0,12}(?:正确|有效|格式)|(?:正确|有效|格式).{0,12}邮箱/.test(state.text))
    const invalid = await driver.settle(state => emailBlocked(state) && (stage === 'generation' || state.renderedText.includes('请输入有效邮箱')))
    check('invalid-email-blocked', emailBlocked(invalid))
    if (stage !== 'generation') check('email-inline-hint', invalid.renderedText.includes('请输入有效邮箱'))
    await driver.capture('invalid-email')
    await driver.fill('邮箱', 'visitor@example.test')
    await driver.choose('参加场次', '请选择场次')
    await driver.click('提交报名')
    const sessionBlocked = state => !state.text.includes('报名成功') && (state.controls.some(item => item.name.includes('参加场次') && item.invalid) || state.text.includes('请选择场次'))
    const missingSession = await driver.settle(state => sessionBlocked(state) && (stage === 'generation' || state.renderedText.includes('请选择场次')))
    check('missing-session-blocked', sessionBlocked(missingSession))
    if (stage !== 'generation') check('session-inline-hint', missingSession.renderedText.includes('请选择场次'))
    await driver.choose('参加场次', '上午场')
    await driver.click('提交报名')
    const success = await driver.settle(state => state.text.includes('报名成功'))
    check('valid-registration-confirmed', success.text.includes('报名成功'))
    await driver.capture('registration-success')
    await driver.reload()
  } catch (error) {
    if (error?.message === 'BUSINESS_CHECK_FAILED') throw error
    check('business-interaction-error', false, { code: String(error?.message || '').match(/\b[A-Z][A-Z0-9_]{3,}\b/)?.[0] || 'BUSINESS_INTERACTION_ERROR', errorClass: String(error?.name || 'Error') })
  }
}
