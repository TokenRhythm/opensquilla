// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import SettingsAccountPanel from './SettingsAccountPanel.vue'
import AccountProviderCard from '@/components/setup/AccountProviderCard.vue'
import SidebarAccountMenu from '@/components/SidebarAccountMenu.vue'
import type { TokenRhythmAccountState } from '../../../../contracts/desktop/tokenrhythm-account'
const mock = vi.hoisted(() => ({ store: null as any }))
vi.mock('@/stores/account', () => ({ useAccountStore: () => mock.store }))
vi.mock('@/composables/usage/useTodayUsage', () => ({
  useTodayUsage: () => ({ tokens: ref(0), loading: ref(false), error: ref(false), approximate: ref(false) }),
}))
const apps: ReturnType<typeof createApp>[] = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = '' })
function state(patch: Partial<TokenRhythmAccountState> = {}): TokenRhythmAccountState {
  return {revision:1,status:'signed_out',profile:null,wallet:null,walletStatus:'idle',keyStatus:'missing',errorCode:null,walletErrorCode:null,source:'manual',selectedAccountId:null,mediaSources:{image:'inherit',video:'inherit'},pendingRevocation:false,loginExpiresAt:null,...patch}
}
async function mount(component: any, value: TokenRhythmAccountState, props: object = {}) {
  mock.store = reactive({state:value,busy:false,error:'',supported:true,loggedIn:['signed_in','unverified'].includes(value.status),ready:value.keyStatus==='ready',name:'小明',initial:'小',init:vi.fn(async()=>{}),action:vi.fn(async()=>value),openLink:vi.fn(async()=>{}),selectMediaSource:vi.fn(async(kind: 'image' | 'video', source: string)=>{ mock.store.state.mediaSources[kind] = source })})
  i18n.global.setLocaleMessage('zh-Hans', zhHans)
  i18n.global.locale.value='zh-Hans'
  const el=document.createElement('div');document.body.append(el)
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  const app=createApp({render:()=>h(component,props)});app.use(i18n);app.use(router);app.mount(el);apps.push(app);await nextTick();return el
}
const profile={accountId:'a',sessionId:'s',displayName:'小明',maskedPhone:'132*****39'}
describe('account settings and source visibility',()=>{
 it('renders login independent of Gateway and shows cancellation during authorization',async()=>{
  const el=await mount(SettingsAccountPanel,state({status:'authorizing'}))
  expect(el.textContent).toContain('等待浏览器授权');expect(el.textContent).toContain('重新打开浏览器')
  const cancel=[...el.querySelectorAll('button')].find(b=>b.textContent==='取消')!;cancel.click();await nextTick();expect(mock.store.action).toHaveBeenCalledWith('cancel')
 })
 it('never invents a zero balance on wallet failure and formats stale balances to two decimals',async()=>{
  const el=await mount(SettingsAccountPanel,state({status:'signed_in',profile,keyStatus:'ready',walletStatus:'error'}))
  expect(el.textContent).toContain('暂时无法获取余额');expect(el.textContent).not.toContain('¥0.00')
  const asOf=new Date(2026,9,21,9,0,0,393).toISOString()
  mock.store.state.wallet={currency:'CNY',balances:{available:'27.98408236',recharge:'0.00',bonus:'27.98408236',debt:'10.00010001',frozen:'0'},asOf}
  mock.store.state.walletStatus='stale';await nextTick()
  expect([...el.querySelectorAll('.account-balance')].map(row=>row.lastElementChild?.textContent)).toEqual(['¥27.99','¥0.00','¥27.99'])
  expect(el.querySelector('details')).toBeNull()
  expect(mock.store.state.wallet.balances.available).toBe('27.98408236')
  expect(el.textContent).toContain('更新时间 2026-10-21 09:00:00');expect(el.textContent).not.toContain(asOf)
 })
 it.each(['ACCOUNT_LOCAL_KEY_MISSING','ACCOUNT_LOCAL_KEY_INVALID','ACCOUNT_LOCAL_DATA_INVALID'])('offers explicit recovery for %s without starting it automatically',async(errorCode)=>{
  const el=await mount(SettingsAccountPanel,state({status:'storage_unavailable',errorCode}))
  expect(el.textContent).toContain('本地登录信息无法恢复')
  expect(el.textContent).not.toContain('系统安全存储不可用')
  expect(mock.store.action).not.toHaveBeenCalled()
  const recover=[...el.querySelectorAll<HTMLButtonElement>('button')].find(button=>button.textContent==='重置本机登录并重新授权')!
  expect(recover).toBeTruthy();recover.click();await nextTick()
  expect(mock.store.action).toHaveBeenCalledExactlyOnceWith('login')
 })
 it('describes storage permission failures without offering credential reset',async()=>{
  const el=await mount(SettingsAccountPanel,state({status:'storage_unavailable',errorCode:'ACCOUNT_STORAGE_UNAVAILABLE'}))
  expect(el.textContent).toContain('请检查目录权限和磁盘空间')
  expect(el.textContent).not.toContain('重置本机登录并重新授权')
 })
 it('allows logout when an encrypted session is restored offline before profile data',async()=>{
  const el=await mount(SettingsAccountPanel,state({status:'unverified',errorCode:'ACCOUNT_NETWORK_UNAVAILABLE'}))
  expect(el.textContent).toContain('暂时无法验证')
  const logout=[...el.querySelectorAll('button')].find(b=>b.textContent==='退出登录')!
  logout.click();await nextTick();expect(mock.store.action).not.toHaveBeenCalled()
  document.querySelector<HTMLButtonElement>('.logout-dialog__confirm')!.click();await nextTick()
  expect(mock.store.action).toHaveBeenCalledWith('logout')
  expect(el.textContent).not.toContain('¥0.00')
 })
 it('groups account details and confirmed logout inside the identity card',async()=>{
  const el=await mount(SettingsAccountPanel,state({status:'signed_in',profile,keyStatus:'ready'}))
  const identity=el.querySelector('.account-identity')!
  const buttons=[...identity.querySelectorAll('button')]
  expect(buttons.map(button=>button.textContent?.trim())).toEqual(['更多账号信息','退出登录'])
  buttons[0]!.click();expect(mock.store.openLink).toHaveBeenCalledWith('account')
  buttons[1]!.click();await nextTick()
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  expect(mock.store.action).not.toHaveBeenCalled()
 })
 it('keeps the account card and logout available during refresh without a wallet warning',async()=>{
  const wallet={currency:'CNY',balances:{available:'27.97',recharge:'0',bonus:'27.97',debt:'0',frozen:'0'},asOf:new Date().toISOString()}
  const el=await mount(SettingsAccountPanel,state({status:'signed_in',profile,keyStatus:'ready',walletStatus:'ready',wallet}))
  const identity=el.querySelector('.account-identity')
  const balance=el.querySelector('.account-wallet')
  const logout=[...el.querySelectorAll('button')].find(button=>button.textContent==='退出登录')!
  const refresh=el.querySelector<HTMLButtonElement>('.account-refresh')!
  refresh.click();await nextTick()
  expect(mock.store.action).toHaveBeenCalledWith('refresh')
  mock.store.refreshing=true;await nextTick()
  expect(refresh.disabled).toBe(true)
  expect(refresh.getAttribute('aria-busy')).toBe('true')
  expect(logout.disabled).toBe(false)
  expect(el.querySelector('.account-identity')).toBe(identity)
  expect(el.querySelector('.account-wallet')).toBe(balance)
  expect(el.querySelector('.account-error')).toBeNull()
  expect(el.textContent).toContain('¥27.97')
  mock.store.refreshing=false;await nextTick()
  expect(refresh.disabled).toBe(false)
  expect(logout.disabled).toBe(false)
  mock.store.state.walletStatus='stale';mock.store.state.walletErrorCode='ACCOUNT_NETWORK_UNAVAILABLE';await nextTick()
  expect(el.textContent).toContain('暂时无法获取余额')
  expect(el.textContent).toContain('¥27.97')
 })
 it('hides account provider while signed out, disables activation before the key is ready',async()=>{
  const el=await mount(AccountProviderCard,state());expect(el.querySelector('[data-testid="account-provider"]')).toBeNull()
  mock.store.loggedIn=true;mock.store.state.profile=profile;mock.store.state.status='signed_in';await nextTick()
  const activate=[...el.querySelectorAll('button')].find(b=>b.textContent==='设为主用')!;expect(activate.disabled).toBe(true);expect(el.textContent).not.toContain('复制');expect(el.textContent).not.toContain('编辑')
 })
 it.each(['signed_out', 'reauth_required'] as const)('hides the provider card and media controls when %s',async(status)=>{
  const el=await mount(AccountProviderCard,state({status,mediaSources:{image:'account',video:'account'}}))
  expect(el.querySelector('[data-testid="account-provider"]')).toBeNull()
  expect(el.querySelector('[data-testid="account-media-recovery"]')).toBeNull()
  expect(el.querySelector('select')).toBeNull()
  expect(el.querySelector('button')).toBeNull()
 })
 it('does not leave a floating account entry when the sidebar is collapsed',async()=>{
  const el=await mount(SidebarAccountMenu,state({status:'signed_in',profile,keyStatus:'ready'}),{collapsed:true,class:'collapsed-account-menu'})
  expect(el.querySelector('[data-testid="sidebar-account-trigger"]')).toBeNull()
  expect(el.querySelector('[role="menu"]')).toBeNull()
 })
})

describe('account provider editing', () => {
 it('emits edit from the primary card without owning a separate editor or media controls', async () => {
  const onEdit=vi.fn()
  const el=await mount(AccountProviderCard,state({status:'signed_in',profile,keyStatus:'ready'}),{active:true,onEdit})
  const edit=[...el.querySelectorAll('button')].find(button=>button.textContent?.trim()==='编辑')!
  edit.click();await nextTick()
  expect(onEdit).toHaveBeenCalledOnce()
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(el.querySelector('select')).toBeNull()
  expect(el.textContent).not.toContain('图片与视频调用来源')
 })
 it('emits edit from a secondary menu and retains connection testing', async () => {
  const onEdit=vi.fn()
  const el=await mount(AccountProviderCard,state({status:'signed_in',profile,keyStatus:'ready'}),{onEdit})
  const trigger=el.querySelector<HTMLButtonElement>('[aria-haspopup="menu"]')!
  trigger.click();await nextTick();await nextTick()
  let items=[...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
  expect(items.map(item=>item.textContent?.trim())).toEqual(['编辑','测试连接','查看账号'])
  items[0]!.click();await nextTick()
  expect(onEdit).toHaveBeenCalledOnce()
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  trigger.click();await nextTick();await nextTick()
  items=[...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
  items.find(item=>item.textContent?.trim()==='测试连接')!.click()
  await vi.waitFor(()=>expect(mock.store.action).toHaveBeenCalledWith('test'))
  await vi.waitFor(()=>expect(el.textContent).toContain('连接验证成功'))
 })
})

describe('logout confirmation', () => {
 async function openLogout() {
  const el = await mount(SidebarAccountMenu, state({ status: 'signed_in', profile, keyStatus: 'ready' }))
  const trigger = el.querySelector<HTMLButtonElement>('[aria-haspopup="menu"]')!
  trigger.click()
  await nextTick()
  const logout = [...el.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(button => button.textContent === '退出登录')!
  logout.click()
  await nextTick()
  await nextTick()
  return { el, trigger, dialog: document.querySelector<HTMLElement>('[role="dialog"]')! }
 }

 it.each(['signed_out', 'signed_in', 'unverified'] as const)('hands settings focus back to the menu trigger while %s', async (status) => {
  let focusAtSettings: Element | null = null
  const onSettings = vi.fn(() => { focusAtSettings = document.activeElement })
  const el = await mount(SidebarAccountMenu, state({ status, profile }), { onSettings })
  mock.store.busy = true
  const trigger = el.querySelector<HTMLButtonElement>('[aria-haspopup="menu"]')!
  trigger.click()
  await nextTick()
  const settings = [...el.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(button => button.textContent === '设置')!
  expect(settings.disabled).toBe(false)
  settings.focus()
  settings.click()
  await nextTick()
  expect(onSettings).toHaveBeenCalledOnce()
  expect(focusAtSettings).toBe(trigger)
  expect(document.activeElement).toBe(trigger)
  expect(el.querySelector('[role="menu"]')).toBeNull()
  expect(mock.store.action).not.toHaveBeenCalled()
 })

 it.each(['cancel', 'close', 'escape', 'backdrop'])('dismisses with %s without signing out and restores focus', async (method) => {
  const { el, trigger, dialog } = await openLogout()
  expect(el.querySelector('[role="menu"]')).toBeNull()
  expect(dialog.textContent).toContain('退出登录不会删除你的任务和设置')
  expect(document.activeElement?.textContent).toBe('取消')
  expect(mock.store.action).not.toHaveBeenCalled()
  if (method === 'cancel') [...dialog.querySelectorAll('button')].find(button => button.textContent === '取消')!.click()
  if (method === 'close') dialog.querySelector<HTMLButtonElement>('[aria-label="关闭"]')!.click()
  if (method === 'escape') document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  if (method === 'backdrop') document.querySelector<HTMLElement>('.logout-overlay')!.click()
  await nextTick()
  await nextTick()
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(mock.store.action).not.toHaveBeenCalled()
  expect(document.activeElement).toBe(trigger)
 })

 it('signs out only once after confirmation and waits for completion', async () => {
  const { dialog } = await openLogout()
  let resolve!: () => void
  mock.store.action.mockImplementation(() => new Promise<void>(done => { resolve = done }))
  const confirm = dialog.querySelector<HTMLButtonElement>('.logout-dialog__confirm')!
  confirm.click()
  confirm.click()
  await nextTick()
  expect(mock.store.action).toHaveBeenCalledExactlyOnceWith('logout')
  expect(dialog.getAttribute('aria-busy')).toBe('true')
  expect(confirm.textContent).toBe('正在退出…')
  expect([...dialog.querySelectorAll('button')].every(button => button.disabled)).toBe(true)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  document.querySelector<HTMLElement>('.logout-overlay')!.click()
  await nextTick()
  expect(document.querySelector('[role="dialog"]')).toBe(dialog)
  resolve()
  await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull())
 })

 it('keeps a failed logout open with an error and allows retry', async () => {
  const { dialog } = await openLogout()
  mock.store.action.mockRejectedValueOnce(new Error('logout failed'))
  const confirm = dialog.querySelector<HTMLButtonElement>('.logout-dialog__confirm')!
  confirm.click()
  await vi.waitFor(() => expect(dialog.querySelector('[role="alert"]')?.textContent).toBe('退出登录失败，请重试。'))
  expect(confirm.disabled).toBe(false)
  confirm.click()
  await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull())
  expect(mock.store.action).toHaveBeenCalledTimes(2)
 })
})
