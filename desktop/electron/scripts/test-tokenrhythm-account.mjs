import test from 'node:test'
import assert from 'node:assert/strict'
import { chmod, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createHash } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { TokenRhythmAccount, describeAccountDevice, pauseAccountsForMaintenance } from '../dist/tokenrhythm-account.js'
import { TokenRhythmModelProxy, allowedModelRoute } from '../dist/tokenrhythm-model-proxy.js'
import { LocalAccountSecrets, LOCAL_ACCOUNT_FILE } from '../dist/local-account-secrets.js'
import { requestAccountLogin } from '../dist/account-login-recovery.js'
import { bindAccountPrimaryActivationLifecycle } from '../dist/account-primary-activation-lifecycle.js'
const scope = 'profile.read wallet.read managed_key.read managed_key.obtain session.revoke offline_access'
const until = async fn => { for(let i=0;i<200;i++){if(fn())return;await new Promise(r=>setTimeout(r,5))}throw Error('condition timeout') }
test('account device labels use the OS product version and distinguish Windows 11', () => {
 for (const [platform, version, expected] of [
  ['darwin', '26.0.1', {name:'Mac',os:'macOS 26'}],
  ['darwin', '10.15.7', {name:'Mac',os:'macOS 10.15'}],
  ['win32', '10.0.26100', {name:'PC',os:'Windows 11'}],
  ['win32', '10.0.19045', {name:'PC',os:'Windows 10'}],
  ['linux', '6.8.0-45-generic', {name:'Linux',os:'Linux 6.8.0-45-generic'}],
  ['freebsd', '14.0', undefined], ['darwin', '', undefined]
 ]) assert.deepEqual(describeAccountDevice(platform, version), expected)
})
async function browserCallback(f, extra = { code: 'browser-once' }) {
 await f.account.login()
 const authorization = new URL(f.opened)
 const callback = new URL(authorization.searchParams.get('redirect_uri'))
 callback.search = new URLSearchParams({ state: authorization.searchParams.get('state'), ...extra })
 const response = await fetch(callback)
 const html = await response.text()
 return { callback, response, html, result: response.url,
  status: async () => (await (await fetch(response.url + '/status')).json()).status }
}
async function fixture(t, options={}) {
 const directory=await mkdtemp(join(tmpdir(),'os-account-'));let opened='',id='a',now=Date.now(),refreshes=0;const calls=[]
 const env={offline:false,walletFailure:false,keyFailure:false,revoked:false,storageFailure:false,secure:true,...options}
 const deps={directory,origin:options.origin??'https://tokenrhythm.studio',assertSecureStorage(){if(!env.secure)throw Error('unavailable')},encrypt(v){if(env.storageFailure)throw Error('failed');return 'TEST-ENCRYPTED:'+Buffer.from(v).toString('base64')},decrypt:v=>Buffer.from(v.slice(15),'base64').toString(),openExternal:async v=>{opened=v},now:()=>now,loginTimeoutMs:options.loginTimeoutMs,fetch:async(url,init={})=>{
  const path=new URL(url).pathname;calls.push({path,...init});await options.beforeFetch?.(url,init);if(env.offline)throw Error('offline');const json=data=>Response.json({code:0,data})
  if(path==='/api/oauth/token'){if(new URLSearchParams(init.body).get('grant_type')==='refresh_token'){refreshes++;await new Promise(r=>setTimeout(r,10))}return Response.json({token_type:'Bearer',access_token:'access-'+id,refresh_token:'refresh-'+id+'-'+refreshes,expires_in:60,scope,account_id:id,session_id:'session-'+id})}
  if(path==='/v1/models')return Response.json({data:[{id:'model'}]})
  if(path.endsWith('/account'))return env.revoked?Response.json({code:'ACCOUNT_SESSION_REVOKED'},{status:401}):json({account_id:id,session_id:'session-'+id,display_name:'测试昵称',masked_phone:'132*****39',links:env.links??{account:'/account/account',usage:'/account',recharge:'/account/recharge'}})
  if(path.endsWith('/wallet'))return env.walletFailure?new Response('',{status:503}):json({account_id:id,currency:'CNY',balances:{available:'-0.0001',recharge:'12.30',bonus:'0.001',debt:'12.3011',frozen:'0'},as_of:'2026-10-21T01:00:00Z'})
  if(path.includes('/managed-key'))return env.keyFailure?Response.json({code:'MANAGED_KEY_DISABLED'},{status:403}):json({account_id:id,app_id:'opensquilla',status:'active',key_id:'key-'+id,generation:1,secret:'model-secret-'+id})
  if(path.endsWith('/session'))return new Response(null,{status:204});throw Error('unhandled route')
 }}
 if(options.local){const secrets=new LocalAccountSecrets(directory);Object.assign(deps,{storageFile:LOCAL_ACCOUNT_FILE,legacyStorageFile:'account.enc',assertSecureStorage:()=>secrets.assertAvailable(),encrypt:v=>secrets.encrypt(v),decrypt:v=>secrets.decrypt(v),recoverStorage:v=>secrets.recover(v)})}
 const account=new TokenRhythmAccount(deps);t.after(async()=>{await account.close();await rm(directory,{recursive:true,force:true})})
 async function login(){await account.login();const url=new URL(opened),callback=new URL(url.searchParams.get('redirect_uri'));callback.search=new URLSearchParams({state:url.searchParams.get('state'),code:'one-use-code'});await fetch(callback);await until(()=>!['authorizing','verifying'].includes(account.snapshot().status));await account.verify();return url}
 return {account,env,calls,login,deps,directory,get opened(){return opened},get refreshes(){return refreshes},setIdentity(v){id=v},advance(ms){now+=ms}}
}
test('local file login survives refresh, restart and logout without OS credential storage', async t => {
 const f=await fixture(t,{local:true});await f.login();await f.account.selectSource('account')
 assert.ok(!(await readFile(join(f.directory,LOCAL_ACCOUNT_FILE),'utf8')).includes('refresh-a'))
 f.advance(40_000);await f.account.verify(true);assert.equal(f.refreshes,1)
 await f.account.close()
 const restored=new TokenRhythmAccount(f.deps);t.after(()=>restored.close());await restored.initialize();await restored.verify(true)
 assert.equal(restored.snapshot().status,'signed_in');assert.equal(restored.snapshot().selectedAccountId,'a')
 f.env.offline=true;await restored.logout();await restored.close()
 const loggedOut=new TokenRhythmAccount(f.deps);t.after(()=>loggedOut.close());await loggedOut.initialize()
 assert.equal(loggedOut.snapshot().status,'signed_out');assert.equal(loggedOut.snapshot().pendingRevocation,true)
})
test('legacy OS-encrypted account requests login without decryption or overwriting the old record', async t => {
 const f=await fixture(t,{local:true});const legacy='synthetic-os-encrypted-record'
 await writeFile(join(f.directory,'account.enc'),legacy)
 const decrypt=f.deps.decrypt;let decrypts=0;f.deps.decrypt=v=>{decrypts++;return decrypt(v)}
 await f.account.initialize();assert.equal(decrypts,0)
 assert.equal(f.account.snapshot().status,'reauth_required');assert.equal(f.account.snapshot().source,'account')
 await f.login();assert.equal(f.account.snapshot().status,'signed_in')
 assert.equal(await readFile(join(f.directory,'account.enc'),'utf8'),legacy)
 assert.ok(await readFile(join(f.directory,LOCAL_ACCOUNT_FILE),'utf8'))
})
test('a missing local key cannot silently reset a saved login or generate a replacement', async t => {
 const f=await fixture(t,{local:true});await f.login();await f.account.close()
 await rm(join(f.directory,'account.local.key'))
 const restored=new TokenRhythmAccount(f.deps);t.after(()=>restored.close());await restored.initialize()
 assert.equal(restored.snapshot().status,'storage_unavailable');await assert.rejects(()=>restored.login())
 assert.equal(restored.snapshot().errorCode,'ACCOUNT_LOCAL_KEY_MISSING')
 await assert.rejects(()=>readFile(join(f.directory,'account.local.key')),error=>error.code==='ENOENT')
})
for (const damage of ['missing-key', 'invalid-key', 'wrong-key', 'invalid-record', 'invalid-json']) {
 test(`confirmed recovery backs up ${damage}, preserves other files, and restores account use after restart`, async t => {
  const f=await fixture(t,{local:true});const firstLogin=await f.login();await f.account.close()
  const keyPath=join(f.directory,'account.local.key'), recordPath=join(f.directory,LOCAL_ACCOUNT_FILE)
  if(damage==='missing-key')await rm(keyPath)
  if(damage==='invalid-key')await writeFile(keyPath,'damaged')
  if(damage==='wrong-key')await writeFile(keyPath,Buffer.alloc(32,9))
  if(damage==='invalid-record')await writeFile(recordPath,'damaged')
  if(damage==='invalid-json')await writeFile(recordPath,f.deps.encrypt('{invalid'))
  const original=await readFile(recordPath), oldKey=await readFile(keyPath).catch(()=>null)
  for(const [name,text] of [['config.toml','saved model settings'],['chat.db','saved conversations'],['account.enc','legacy record']])await writeFile(join(f.directory,name),text)
  const account=new TokenRhythmAccount(f.deps);t.after(()=>account.close());await account.initialize()
  assert.equal(account.snapshot().status,'storage_unavailable')
  let confirmations=0
  await requestAccountLogin(account,async()=>{confirmations++;return false})
  assert.equal(confirmations,1);assert.deepEqual(await readFile(recordPath),original)
  assert.equal((await readdir(f.directory)).filter(name=>name.startsWith('recovery-')).length,0)
  const authorizing=await requestAccountLogin(account,async()=>true)
  assert.equal(authorizing.status,'authorizing');assert.equal(authorizing.source,'account')
  const authorization=new URL(f.opened)
  assert.equal(authorization.searchParams.get('installation_id'),firstLogin.searchParams.get('installation_id'))
  const backup=join(f.directory,(await readdir(f.directory)).find(name=>name.startsWith('recovery-')))
  assert.deepEqual(await readFile(join(backup,LOCAL_ACCOUNT_FILE)),original)
  assert.deepEqual(await readFile(join(backup,'account.local.key')).catch(()=>null),oldKey)
  for(const [name,text] of [['config.toml','saved model settings'],['chat.db','saved conversations'],['account.enc','legacy record']])assert.equal(await readFile(join(f.directory,name),'utf8'),text)
  const callback=new URL(authorization.searchParams.get('redirect_uri'));callback.search=new URLSearchParams({state:authorization.searchParams.get('state'),code:'recovered-code'})
  await fetch(callback);await until(()=>account.snapshot().keyStatus==='ready');await account.close()
  const restored=new TokenRhythmAccount(f.deps);t.after(()=>restored.close());await restored.initialize();await restored.verify(true)
  assert.equal(restored.snapshot().status,'signed_in');assert.equal(restored.snapshot().walletStatus,'ready')
  assert.equal((await restored.runtimeState()).selectedAccountId,'a')
  assert.equal(await restored.modelCredential((await restored.runtimeState()).credential),'model-secret-a')
 })
}
test('concurrent recovery entrances share one confirmation and backup, and remain recoverable after backup failure', async t=>{
 const f=await fixture(t,{local:true});await f.login();await f.account.close();await rm(join(f.directory,'account.local.key'))
 const account=new TokenRhythmAccount(f.deps);t.after(()=>account.close());await account.initialize()
 const original=await readFile(join(f.directory,LOCAL_ACCOUNT_FILE));const recover=f.deps.recoverStorage
 f.deps.recoverStorage=()=>{throw new Error('disk full')}
 await assert.rejects(()=>requestAccountLogin(account,async()=>true),/ACCOUNT_RECOVERY_FAILED/)
 assert.equal(account.snapshot().errorCode,'ACCOUNT_LOCAL_KEY_MISSING');assert.deepEqual(await readFile(join(f.directory,LOCAL_ACCOUNT_FILE)),original)
 f.deps.recoverStorage=recover
 let release;let confirmations=0
 const confirm=()=>{confirmations++;return new Promise(resolve=>{release=resolve})}
 const first=requestAccountLogin(account,confirm),second=requestAccountLogin(account,confirm)
 assert.equal(first,second);await until(()=>confirmations===1);release(true);await first
 assert.equal((await readdir(f.directory)).filter(name=>name.startsWith('recovery-')).length,1)
})
test('unsafe storage permissions cannot be reset through recovery', {skip:process.platform==='win32'}, async t=>{
 const f=await fixture(t,{local:true});await f.login();await f.account.close();const path=join(f.directory,'account.local.key');await chmod(path,0o644)
 const account=new TokenRhythmAccount(f.deps);t.after(()=>account.close());await account.initialize()
 assert.equal(account.snapshot().errorCode,'ACCOUNT_STORAGE_UNAVAILABLE')
 await assert.rejects(()=>requestAccountLogin(account,async()=>{assert.fail('must not offer reset for a permission failure')}),/ACCOUNT_STORAGE_UNAVAILABLE/)
 await assert.rejects(()=>account.recoverLogin(),/ACCOUNT_RECOVERY_UNAVAILABLE/)
 assert.equal((await readdir(f.directory)).filter(name=>name.startsWith('recovery-')).length,0)
})
test('PKCE/state, display-only boundary, precise decimals, encrypted persistence',async t=>{
 const f=await fixture(t);await f.account.login();const url=new URL(f.opened);assert.equal(url.pathname,'/api/oauth/authorize');assert.equal(url.searchParams.get('code_challenge_method'),'S256');assert.match(url.searchParams.get('installation_id'),/^[a-f0-9-]{36}$/)
 const callback=new URL(url.searchParams.get('redirect_uri'));callback.search='state=wrong&code=bad';assert.equal((await fetch(callback)).status,400);assert.equal(f.account.snapshot().status,'authorizing')
 callback.search=new URLSearchParams({state:url.searchParams.get('state'),code:'valid'});await fetch(callback);await until(()=>f.account.snapshot().keyStatus==='ready')
 const verifier=new URLSearchParams(f.calls.find(c=>c.path==='/api/oauth/token').body).get('code_verifier');assert.equal(createHash('sha256').update(verifier).digest('base64url'),url.searchParams.get('code_challenge'))
 const state=JSON.stringify(f.account.snapshot());assert.ok(!/model-secret|access-a|refresh-a/.test(state));assert.equal(f.account.snapshot().wallet.balances.available,'-0.0001');assert.equal(f.account.snapshot().source,'manual');assert.ok(!(await readFile(join(f.directory,'account.enc'),'utf8')).includes('model-secret'))
})
test('login and refresh report device metadata without changing the authorization URL', async t => {
 const f=await fixture(t)
 f.deps.deviceInfo=describeAccountDevice('darwin','26.0.1')
 const authorization=await f.login()
 assert.equal(authorization.searchParams.has('device_name'),false)
 f.advance(40_000);await f.account.verify(true)
 const exchanges=f.calls.filter(call=>call.path==='/api/oauth/token')
 assert.deepEqual(exchanges.map(call=>new URLSearchParams(call.body).get('grant_type')),['authorization_code','refresh_token'])
 for(const call of exchanges) {
  assert.equal(call.headers['X-TokenRhythm-Device-Name'],'Mac')
  assert.equal(call.headers['X-TokenRhythm-Device-OS'],'macOS 26')
 }
})
test('account links resolve against the configured production or UAT origin',async t=>{
 for(const origin of ['https://tokenrhythm.studio','https://gpt.tokenrhythm.studio']) {
  const f=await fixture(t,{origin});await f.login()
  for(const [name,path] of Object.entries({account:'/account/account',usage:'/account',recharge:'/account/recharge'})) {
   await f.account.openLink(name);assert.equal(f.opened,origin+path)
  }
 }
})
test('UAT OAuth, account APIs and model requests all use the packaged test origin', async t => {
 const origin = 'https://gpt.tokenrhythm.studio'
 const f = await fixture(t, { origin, beforeFetch: async url => assert.equal(new URL(url).origin, origin) })
 await f.login()
 await f.account.selectSource('account')
 assert.equal(new URL(f.opened).origin, origin)
 assert.equal(new URL(f.opened).pathname, '/api/oauth/authorize')
 const { PassThrough } = await import('node:stream')
 const seen = []
 const request = (url, options, callback) => {
  seen.push(String(url))
  const stream = new PassThrough(); stream.setTimeout = () => stream; stream.resume()
  stream.on('finish', () => {
   const response = new PassThrough(); response.statusCode = 200; response.headers = { 'content-type': 'application/json' }
   callback(response); response.end(JSON.stringify({ id: 'uat-video-1' }))
  })
  return stream
 }
 const proxy = new TokenRhythmModelProxy(f.account, request); t.after(() => proxy.close())
 const env = await proxy.start(), lease = (await f.account.runtimeState()).credential
 for (const path of ['/v1/chat/completions', '/v1/images/generations', '/v1/videos/generations']) {
  const response = await fetch(env.OPENSQUILLA_DESKTOP_ACCOUNT_URL + path, { method: 'POST', headers: { Authorization: 'Bearer ' + lease }, body: '{}' })
  assert.equal(response.status, 200); await response.text()
 }
 assert.deepEqual(seen, ['/v1/chat/completions', '/v1/images/generations', '/v1/videos/generations'].map(path => origin + path))
})
test('old absolute same-origin links remain compatible and invalid links never open',async t=>{
 const f=await fixture(t,{links:{account:'https://tokenrhythm.studio/account/account',usage:'/account',recharge:'/account/recharge'}});await f.login()
 await f.account.openLink('account');assert.equal(f.opened,'https://tokenrhythm.studio/account/account')
 for(const href of ['https://evil.invalid/account','//evil.invalid/account','//tokenrhythm.studio/account','https://user:pass@tokenrhythm.studio/account','javascript:alert(1)','account','/\\evil.invalid/account','/\n/evil.invalid/account',undefined,42,{}]) {
  f.env.links={account:href,usage:'/account',recharge:'/account/recharge'};await f.account.verify(true)
  const before=f.opened;await assert.rejects(()=>f.account.openLink('account'),error=>error.code==='ACCOUNT_LINK_UNAVAILABLE');assert.equal(f.opened,before)
  await f.account.openLink('usage');assert.equal(f.opened,'https://tokenrhythm.studio/account')
 }
})
test('cancel and timeout never establish a session',async t=>{const f=await fixture(t,{loginTimeoutMs:20});await f.account.login();await until(()=>f.account.snapshot().status==='signed_out');assert.equal(f.account.snapshot().errorCode,'ACCOUNT_LOGIN_TIMEOUT');await f.account.login();f.account.cancel();assert.equal(f.account.snapshot().status,'signed_out');assert.equal(f.calls.length,0)})
test('wallet and key errors preserve valid login',async t=>{const f=await fixture(t,{walletFailure:true,keyFailure:true});await f.login();assert.equal(f.account.snapshot().status,'signed_in');assert.equal(f.account.snapshot().walletStatus,'error');assert.equal(f.account.snapshot().wallet,null);await assert.rejects(()=>f.account.selectSource('account'));f.env.walletFailure=false;f.env.keyFailure=false;await f.account.retryKey();assert.equal(f.account.snapshot().keyStatus,'ready');await f.account.selectSource('account')})
test('scheduled and manual wallet refreshes retain balances and only mark real failures stale', async t => {
 let holdWallet = false, release
 const f = await fixture(t, { beforeFetch: async url => {
  if (holdWallet && new URL(url).pathname.endsWith('/wallet')) await new Promise(resolve => { release = resolve })
 } })
 await f.login()
 const wallet = f.account.snapshot().wallet
 const events = []
 f.deps.onState = state => events.push(state)
 for (const force of [false, true]) {
  f.advance(60_000)
  holdWallet = true; release = undefined; events.length = 0
  const refreshing = f.account.verify(force)
  await until(() => Boolean(release))
  assert.equal(f.account.snapshot().walletStatus, 'ready')
  assert.deepEqual(f.account.snapshot().wallet, wallet)
  assert.ok(events.every(state => state.status === 'signed_in' && state.walletStatus === 'ready' && !state.walletErrorCode))
  release(); await refreshing
  assert.ok(events.every(state => state.walletStatus === 'ready' && !state.walletErrorCode))
 }
 release = undefined; events.length = 0; f.env.walletFailure = true
 const failing = f.account.verify(true)
 await until(() => Boolean(release))
 assert.equal(f.account.snapshot().walletStatus, 'ready')
 release(); await failing
 assert.equal(f.account.snapshot().walletStatus, 'stale')
 assert.ok(f.account.snapshot().walletErrorCode)
 assert.deepEqual(f.account.snapshot().wallet, wallet)
 f.env.walletFailure = false; holdWallet = false
 await f.account.verify(true)
 assert.equal(f.account.snapshot().walletStatus, 'ready')
 assert.equal(f.account.snapshot().walletErrorCode, null)
})
test('refresh is singleflight and atomically persisted',async t=>{const f=await fixture(t);await f.login();f.advance(40_000);await Promise.all([f.account.verify(true),f.account.verify(true),f.account.verify(true)]);assert.equal(f.refreshes,1);assert.ok(f.deps.decrypt(await readFile(join(f.directory,'account.enc'),'utf8')).includes('refresh-a-1'))})
test('network loss keeps the locally bound model authorization',async t=>{const f=await fixture(t);await f.login();await f.account.selectSource('account');const old=(await f.account.runtimeState()).credential;f.env.offline=true;await f.account.verify(true);assert.equal(f.account.snapshot().status,'signed_in');assert.equal(f.account.snapshot().keyStatus,'ready');assert.equal(await f.account.modelCredential(old),'model-secret-a');f.env.offline=false;await f.account.verify(true);assert.equal((await f.account.runtimeState()).credential,old)})
test('managed-key metadata failure keeps the locally bound model authorization', async t => {
 let fail = false
 const f = await fixture(t, { beforeFetch: async url => {
  if (fail && new URL(url).pathname.endsWith('/managed-key')) throw new Error('temporary managed-key outage')
 } })
 await f.login(); await f.account.selectSource('account')
 const lease = (await f.account.runtimeState()).credential
 fail = true
 await f.account.verify(true)
 assert.equal(f.account.snapshot().status, 'signed_in')
 assert.equal(f.account.snapshot().keyStatus, 'ready')
 assert.equal(await f.account.modelCredential(lease), 'model-secret-a')
})
test('structured remote 401 clears the local account model binding', async t => {
 const f = await fixture(t); await f.login(); await f.account.selectSource('account'); await f.account.close()
 const original = f.deps.fetch
 const restored = new TokenRhythmAccount({ ...f.deps, fetch: async (url, init = {}) => {
  if (new URL(url).pathname.endsWith('/account')) return Response.json({ error: 'unauthorized' }, { status: 401 })
  return original(url, init)
 } })
 t.after(() => restored.close())
 await restored.initialize()
 const lease = (await restored.runtimeState()).credential
 await restored.verify(true)
 assert.equal(restored.snapshot().status, 'reauth_required')
 assert.equal(restored.snapshot().keyStatus, 'missing')
 assert.equal(restored.saved.key, null)
 assert.equal((await restored.runtimeState()).ready, false)
 await assert.rejects(() => restored.modelCredential(lease), /ACCOUNT_MODEL_UNAVAILABLE/)
})
test('malformed remote 401 is terminal instead of an invalid-response retry', async t => {
 const f = await fixture(t); await f.login(); await f.account.selectSource('account'); await f.account.close()
 const original = f.deps.fetch
 const restored = new TokenRhythmAccount({ ...f.deps, fetch: async (url, init = {}) => {
  if (new URL(url).pathname.endsWith('/account')) return new Response('unauthorized', { status: 401 })
  return original(url, init)
 } })
 t.after(() => restored.close())
 await restored.initialize()
 await restored.verify(true)
 assert.equal(restored.snapshot().status, 'reauth_required')
 assert.equal(restored.snapshot().errorCode, 'ACCOUNT_HTTP_401')
})
test('a model 401 fences an in-flight key refresh', async t => {
 let holdKey = false, releaseKey
 const f = await fixture(t, { beforeFetch: async url => {
  if (holdKey && new URL(url).pathname.endsWith('/managed-key')) {
   await new Promise(resolve => { releaseKey = resolve })
  }
 } })
 await f.login(); await f.account.selectSource('account')
 holdKey = true
 const verifying = f.account.verify(true)
 await until(() => Boolean(releaseKey))
 await f.account.modelKeyRejected()
 releaseKey()
 await verifying
 assert.equal(f.account.saved.key, null)
 assert.equal(f.account.snapshot().keyStatus, 'unavailable')
 assert.equal(await readFile(join(f.directory, 'model-key-rejected'), 'utf8'), '1')
})
test('a stale model 401 cannot clear a newer account lease', async t => {
 const f = await fixture(t); await f.login(); await f.account.selectSource('account')
 const oldLease = (await f.account.runtimeState()).credential
 const oldSecret = f.account.saved.key.secret
 await f.account.logout(); f.setIdentity('b'); await f.login(); await f.account.selectSource('account')
 const current = await f.account.runtimeState()
 await f.account.modelKeyRejected(oldLease, oldSecret)
 assert.equal((await f.account.runtimeState()).credential, current.credential)
 assert.equal(f.account.saved.key.secret, 'model-secret-b')
 assert.equal(f.account.snapshot().keyStatus, 'ready')
})
test('account model test works before selecting the account as primary', async t => {
 const f = await fixture(t); await f.login()
 assert.equal(f.account.snapshot().source, 'manual')
 await f.account.testModel()
 assert.equal(f.account.snapshot().status, 'signed_in')
})
test('a model 401 is persisted during maintenance admission closure', async t => {
 const f = await fixture(t); await f.login(); await f.account.selectSource('account')
 const pause = f.account.pauseForMaintenance()
 await f.account.modelKeyRejected()
 assert.equal(f.account.saved.key, null)
 assert.equal(await readFile(join(f.directory, 'model-key-rejected'), 'utf8'), '1')
 await pause.drained
 pause.resume()
})
test('account recovery keeps the existing bridge lease while validation is pending', async t => {
 let holdKey = false, releaseKey
 const f = await fixture(t, { beforeFetch: async url => {
  if (holdKey && new URL(url).pathname.endsWith('/managed-key')) {
   await new Promise(resolve => { releaseKey = resolve })
  }
 } })
 await f.login()
 await f.account.selectSource('account')
 const oldLease = (await f.account.runtimeState()).credential
 assert.ok(oldLease)
 f.env.offline = true
 await f.account.verify(true)
 assert.equal(f.account.snapshot().status, 'signed_in')
 assert.equal(f.account.snapshot().keyStatus, 'ready')

 const events = []
 f.deps.onState = state => events.push(state)
 f.env.offline = false
 holdKey = true
 const recovering = f.account.verify(true)
 try {
  await until(() => Boolean(releaseKey))
  const pending = f.account.snapshot()
  assert.equal(pending.status, 'signed_in')
  assert.equal(pending.keyStatus, 'ready')
  assert.ok(events.every(state => state.status === 'signed_in' && state.keyStatus === 'ready'))
  const runtime = await f.account.runtimeState()
  assert.equal(runtime.ready, true)
  assert.equal(runtime.credential, oldLease)
 } finally {
  holdKey = false
  releaseKey?.()
  await recovering
 }
 const recovered = f.account.snapshot()
 const runtime = await f.account.runtimeState()
 assert.equal(recovered.status, 'signed_in')
 assert.equal(recovered.keyStatus, 'ready')
 assert.equal(runtime.ready, true)
 assert.ok(runtime.credential)
 assert.equal(runtime.credential, oldLease)
 assert.equal(events.at(-1).keyStatus, 'ready')
})
test('account revalidation preserves model readiness while its bridge lease remains valid', async t => {
 let holdKey = false, releaseKey
 const f = await fixture(t, { beforeFetch: async url => {
  if (holdKey && new URL(url).pathname.endsWith('/managed-key')) {
   await new Promise(resolve => { releaseKey = resolve })
  }
 } })
 await f.login()
 await f.account.selectSource('account')
 const lease = (await f.account.runtimeState()).credential
 const events = []
 f.deps.onState = state => events.push(state)
 f.advance(60_000)
 holdKey = true
 const refreshing = f.account.verify()
 try {
  await until(() => Boolean(releaseKey))
  const runtime = await f.account.runtimeState()
  assert.equal(runtime.ready, true)
  assert.equal(runtime.credential, lease)
 } finally {
  holdKey = false
  releaseKey?.()
  await refreshing
 }
 assert.ok(events.length > 0)
 assert.ok(events.every(state => state.status === 'signed_in' && state.keyStatus === 'ready'))
})
test('logout preserves old binding, revokes only old session, and isolates video jobs',async t=>{const f=await fixture(t);await f.login();await f.account.selectSource('account');const lease=(await f.account.runtimeState()).credential;await f.account.recordVideoJob('video-a',lease);assert.ok(f.account.ownsVideoJob('video-a'));f.env.offline=true;await f.account.logout();assert.equal(f.account.snapshot().status,'signed_out');assert.equal(f.account.snapshot().source,'account');assert.equal(f.account.snapshot().selectedAccountId,'a');assert.equal(f.account.snapshot().pendingRevocation,true);await assert.rejects(()=>f.account.modelCredential(lease));f.env.offline=false;f.setIdentity('b');await f.login();assert.equal(f.account.snapshot().selectedAccountId,'a');assert.equal(f.account.ownsVideoJob('video-a'),false);const revoked=f.calls.filter(c=>c.path.endsWith('/session'));assert.ok(revoked.some(c=>c.headers.Authorization==='Bearer access-a'));assert.ok(!revoked.some(c=>c.headers.Authorization==='Bearer access-b'))})
test('credential save failure cannot enable models',async t=>{const f=await fixture(t,{storageFailure:true});try {await f.login();assert.equal(f.account.snapshot().profile,null);await assert.rejects(()=>f.account.selectSource('account'))} finally {f.env.storageFailure=false}})
test('storage failure on restore cannot silently select manual credentials',async t=>{const f=await fixture(t);await f.login();await f.account.selectSource('account');f.env.secure=false;const restored=new TokenRhythmAccount(f.deps);t.after(()=>restored.close());await restored.initialize();assert.equal(restored.snapshot().status,'storage_unavailable');assert.equal((await restored.runtimeState()).source,'account');await assert.rejects(()=>restored.login())})
test('revoked sessions hide account without changing billing source',async t=>{const f=await fixture(t);await f.login();await f.account.selectSource('account');f.env.revoked=true;await f.account.verify(true);assert.equal(f.account.snapshot().status,'reauth_required');assert.equal(f.account.snapshot().profile,null);assert.equal(f.account.snapshot().source,'account')})
test('bridge requires private auth and rejects targets, queries, health, wallet and browser origins',async t=>{const f=await fixture(t);await f.login();const proxy=new TokenRhythmModelProxy(f.account);t.after(()=>proxy.close());const env=await proxy.start(),url=env.OPENSQUILLA_DESKTOP_ACCOUNT_URL,Authorization='Bearer '+env.OPENSQUILLA_DESKTOP_ACCOUNT_TOKEN;assert.equal((await fetch(url+'/state')).status,401);assert.equal((await fetch(url+'/state',{headers:{Authorization}})).status,200);assert.equal((await fetch(url+'/state',{headers:{Authorization,Origin:'https://evil.invalid'}})).status,403);for(const path of ['/health','/api/v1/integrations/opensquilla/wallet','/v1/models?url=https://evil.invalid','https://evil.invalid/v1/models'])assert.equal(allowedModelRoute('GET',path),false);assert.ok(allowedModelRoute('POST','/v1/chat/completions'));assert.ok(allowedModelRoute('GET','/v1/videos/task-123'));assert.equal((await fetch(url+'/v1/models',{headers:{Authorization:'Bearer expired'}})).status,503)})

test('model proxy streams, fixes upstream identity and authorization, retains billing receipts',async t=>{
 const {PassThrough}=await import('node:stream');const f=await fixture(t);await f.login();await f.account.selectSource('account');const seen=[]
 const request=(url,options,callback)=>{const stream=new PassThrough();stream.setTimeout=()=>stream;let body='';stream.on('data',chunk=>{body+=chunk});stream.on('finish',()=>{seen.push({url:String(url),options,body});const response=new PassThrough();response.statusCode=200;response.headers={'content-type':'text/event-stream'};callback(response);response.write('data: {"choices":[{"delta":{"tool_calls":[]}}]}\n\n');response.end('data: {"usage":{"billing_receipt":"receipt-1"}}\n\ndata: [DONE]\n\n')});return stream}
 const proxy=new TokenRhythmModelProxy(f.account,request);t.after(()=>proxy.close());const env=await proxy.start();const lease=(await f.account.runtimeState()).credential
 const response=await fetch(env.OPENSQUILLA_DESKTOP_ACCOUNT_URL+'/v1/chat/completions',{method:'POST',headers:{Authorization:'Bearer '+lease,'Content-Type':'application/json','X-Api-Key':'override','X-OpenSquilla-Execution-Id':'execution-1','X-OpenSquilla-Install-Id':'install-1'},body:JSON.stringify({stream:true,tools:[{type:'function'}]})})
 assert.equal(response.status,200);assert.match(await response.text(),/receipt-1/);assert.equal(seen.length,1);assert.equal(seen[0].url,'https://tokenrhythm.studio/v1/chat/completions');assert.equal(seen[0].options.headers.Authorization,'Bearer model-secret-a');assert.equal(seen[0].options.headers['x-api-key'],undefined);assert.equal(seen[0].options.headers['x-opensquilla-execution-id'],'execution-1');assert.equal(seen[0].options.headers['x-opensquilla-install-id'],'install-1')
})
test('model proxy upstream 401 clears the rejected local model key', async t => {
 const { PassThrough } = await import('node:stream')
 const f = await fixture(t); await f.login(); await f.account.selectSource('account')
 const request = (url, options, callback) => {
  const stream = new PassThrough(); stream.setTimeout = () => stream; stream.resume()
  stream.on('finish', () => {
   const response = new PassThrough(); response.statusCode = 401; response.headers = { 'content-type': 'application/json' }
   callback(response); response.end(JSON.stringify({ error: 'unauthorized' }))
  })
  return stream
 }
 const proxy = new TokenRhythmModelProxy(f.account, request); t.after(() => proxy.close())
 const env = await proxy.start(); const lease = (await f.account.runtimeState()).credential
 const response = await fetch(env.OPENSQUILLA_DESKTOP_ACCOUNT_URL + '/v1/chat/completions', { method: 'POST', headers: { Authorization: 'Bearer ' + lease }, body: '{}' })
 assert.equal(response.status, 401); await response.text(); await until(() => f.account.saved.key === null)
 assert.equal(f.account.snapshot().keyStatus, 'unavailable')
 assert.equal((await f.account.runtimeState()).ready, false)
})
test('model proxy refuses upstream redirects and records video ownership before returning a job',async t=>{
 const {PassThrough}=await import('node:stream');const f=await fixture(t);await f.login();await f.account.selectSource('account');let redirect=true
 const request=(url,options,callback)=>{const stream=new PassThrough();stream.setTimeout=()=>stream;stream.resume();stream.on('finish',()=>{const response=new PassThrough();response.statusCode=redirect?302:200;response.headers={'content-type':'application/json',location:'https://evil.invalid'};callback(response);if(!redirect)response.end(JSON.stringify({id:'video-1'}))});return stream}
 const proxy=new TokenRhythmModelProxy(f.account,request);t.after(()=>proxy.close());const env=await proxy.start(),url=env.OPENSQUILLA_DESKTOP_ACCOUNT_URL,lease=(await f.account.runtimeState()).credential,headers={Authorization:'Bearer '+lease}
 assert.equal((await fetch(url+'/v1/models',{headers})).status,502);redirect=false
 const response=await fetch(url+'/v1/videos/generations',{method:'POST',headers,body:'{}'});assert.equal(response.status,200);assert.equal((await response.json()).id,'video-1');assert.ok(f.account.ownsVideoJob('video-1'));assert.equal((await fetch(url+'/v1/videos/unknown',{headers})).status,403)
})
test('logout immediately interrupts an open model stream',async t=>{
 const {PassThrough}=await import('node:stream');const f=await fixture(t);await f.login();await f.account.selectSource('account');let upstreamResponse
 const request=(url,options,callback)=>{const stream=new PassThrough();stream.setTimeout=()=>stream;stream.resume();stream.on('finish',()=>{upstreamResponse=new PassThrough();upstreamResponse.statusCode=200;upstreamResponse.headers={'content-type':'text/event-stream'};callback(upstreamResponse);upstreamResponse.write('data: first\n\n')});return stream}
 const proxy=new TokenRhythmModelProxy(f.account,request);f.deps.onInvalidate=()=>proxy.invalidate();t.after(()=>{upstreamResponse?.destroy();return proxy.close()});const env=await proxy.start(),lease=(await f.account.runtimeState()).credential
 const response=await fetch(env.OPENSQUILLA_DESKTOP_ACCOUNT_URL+'/v1/chat/completions',{method:'POST',headers:{Authorization:'Bearer '+lease},body:'{}'}),reader=response.body.getReader();await reader.read();await f.account.logout();await assert.rejects(()=>reader.read())
})

test('explicit verified login offers one identity-bound default without selecting a source', async t => {
 const f=await fixture(t);await f.login()
 const request=f.account.snapshot().primaryActivation
 assert.match(request.id,/^[0-9a-f-]{36}$/)
 assert.deepEqual({accountId:request.accountId,sessionId:request.sessionId},{accountId:'a',sessionId:'session-a'})
 assert.equal(f.account.snapshot().source,'manual')
 await f.account.verify(true);await f.account.retryKey();await f.account.modelKeyRejected()
 assert.deepEqual(f.account.snapshot().primaryActivation,request)
 const saved=JSON.parse(f.deps.decrypt(await readFile(join(f.directory,'account.enc'),'utf8')))
 assert.equal('primaryActivation' in saved,false)
 await f.account.selectSource('account',request)
 assert.equal(f.account.snapshot().selectedAccountId,'a')
 assert.equal(f.account.snapshot().primaryActivation,null)
 await assert.rejects(()=>f.account.selectSource('account',request),/ACCOUNT_PRIMARY_ACTIVATION_STALE/)
})

test('an explicit login with an initially unavailable key keeps the same default through retry', async t => {
 const f=await fixture(t,{keyFailure:true});await f.login()
 const request=f.account.snapshot().primaryActivation
 assert.equal(f.account.snapshot().status,'signed_in')
 assert.equal(f.account.snapshot().keyStatus,'disabled')
 assert.ok(request)
 await assert.rejects(()=>f.account.selectSource('account',request),/ACCOUNT_MODEL_UNAVAILABLE/)
 f.env.keyFailure=false;await f.account.retryKey()
 assert.deepEqual(f.account.snapshot().primaryActivation,request)
 await f.account.selectSource('account',request)
 assert.equal(f.account.snapshot().primaryActivation,null)
})

test('cancelling the default during OAuth preserves login and suppresses its later offer', async t => {
 let release,entered
 const gate=new Promise(resolve=>{release=resolve})
 const f=await fixture(t,{beforeFetch:async(url,init)=>{
  if(new URL(url).pathname==='/api/oauth/token' && new URLSearchParams(init.body).get('grant_type')==='authorization_code'){entered=true;await gate}
 }})
 await browserCallback(f);await until(()=>entered)
 await f.account.cancelPrimaryActivation()
 release();await f.account.loginCompletionPromise
 assert.equal(f.account.snapshot().status,'signed_in')
 assert.equal(f.account.snapshot().primaryActivation,null)
 await f.account.retryKey();assert.equal(f.account.snapshot().primaryActivation,null)
})

test('manual source intent suppresses an OAuth default without cancelling authentication', async t => {
 const f=await fixture(t);await f.account.login()
 await f.account.selectSource('manual')
 await browserCallback(f);await f.account.loginCompletionPromise
 assert.equal(f.account.snapshot().status,'signed_in')
 assert.equal(f.account.snapshot().source,'manual')
 assert.equal(f.account.snapshot().primaryActivation,null)
})

test('restoration, refresh and maintenance never recreate a cancelled login default', async t => {
 const f=await fixture(t);await f.login()
 const request=f.account.snapshot().primaryActivation
 await f.account.cancelPrimaryActivation(request.id)
 await f.account.verify(true);await f.account.retryKey();await f.account.modelKeyRejected()
 const pause=f.account.pauseForMaintenance();await pause.drained;pause.resume()
 await until(()=>!f.account.admissionClosed);await f.account.verify(true)
 assert.equal(f.account.snapshot().primaryActivation,null)
 await f.account.close()
 const restored=new TokenRhythmAccount(f.deps);t.after(()=>restored.close())
 await restored.initialize();await restored.verify(true);await restored.retryKey()
 assert.equal(restored.snapshot().primaryActivation,null)
})

test('an old login default cannot bind a new account or cancel its newer offer', async t => {
 const f=await fixture(t);await f.login()
 const old=f.account.snapshot().primaryActivation
 const verify=f.account.verify.bind(f.account)
 let release,entered=false
 const gate=new Promise(resolve=>{release=resolve})
 f.account.verify=async()=>{entered=true;await gate;return verify()}
 const selecting=f.account.selectSource('account',old)
 await until(()=>entered)
 f.account.verify=verify
 await f.account.logout();f.setIdentity('b');await f.login()
 const current=f.account.snapshot().primaryActivation
 assert.notEqual(current.id,old.id)
 await f.account.cancelPrimaryActivation(old.id)
 assert.deepEqual(f.account.snapshot().primaryActivation,current)
 release();await assert.rejects(()=>selecting,/ACCOUNT_PRIMARY_ACTIVATION_STALE/)
 assert.equal(f.account.snapshot().selectedAccountId,null)
 assert.deepEqual(f.account.snapshot().primaryActivation,current)
 await f.account.selectSource('account',current)
 assert.equal(f.account.snapshot().selectedAccountId,'b')
})

test('failed guarded persistence restores its previous choice and retains the matching offer', async t => {
 const f=await fixture(t);await f.login()
 const request=f.account.snapshot().primaryActivation
 f.env.storageFailure=true
 await assert.rejects(()=>f.account.selectSource('account',request),/failed/)
 assert.equal(f.account.snapshot().source,'manual')
 assert.equal(f.account.snapshot().selectedAccountId,null)
 assert.deepEqual(f.account.snapshot().primaryActivation,request)
 f.env.storageFailure=false
 await f.account.selectSource('account',request)
 assert.equal(f.account.snapshot().primaryActivation,null)
})

test('an older failed bind cannot roll back a newer manual choice', async t => {
 const f=await fixture(t);await f.login()
 const request=f.account.snapshot().primaryActivation
 const persist=f.account.persist.bind(f.account)
 let release,entered=false
 const gate=new Promise(resolve=>{release=resolve})
 f.account.persist=async()=>{entered=true;await gate;throw new Error('older write failed')}
 const selecting=f.account.selectSource('account',request)
 await until(()=>entered)
 f.account.persist=persist
 await f.account.selectSource('manual')
 release();await assert.rejects(()=>selecting,/older write failed/)
 assert.equal(f.account.snapshot().source,'manual')
 assert.equal(f.account.snapshot().primaryActivation,null)
 const saved=JSON.parse(f.deps.decrypt(await readFile(join(f.directory,'account.enc'),'utf8')))
 assert.equal(saved.source,'manual')
})

test('expected activation payload rejects extra fields and mismatched identities', async t => {
 const f=await fixture(t);await f.login()
 const request=f.account.snapshot().primaryActivation
 for(const value of [null,[],{}, {...request,extra:true},{...request,id:'bad'},{...request,accountId:'a\n'}]){
  await assert.rejects(()=>f.account.selectSource('account',value),/INVALID_ACCOUNT_ACTIVATION/)
 }
 for(const value of [{...request,accountId:'b'},{...request,sessionId:'session-b'}]){
  await assert.rejects(()=>f.account.selectSource('account',value),/ACCOUNT_PRIMARY_ACTIVATION_STALE/)
 }
 await assert.rejects(()=>f.account.cancelPrimaryActivation({id:request.id}),/INVALID_ACCOUNT_ACTIVATION/)
 assert.deepEqual(f.account.snapshot().primaryActivation,request)
 assert.equal(f.account.snapshot().source,'manual')
})

test('cancel after verified login publishes the cleared default without signing out', async t => {
 const f=await fixture(t);await f.login()
 const states=[];f.deps.onState=state=>states.push(state)
 const state=f.account.cancel()
 assert.equal(state.status,'signed_in')
 assert.equal(state.primaryActivation,null)
 assert.equal(states.at(-1).primaryActivation,null)
 await f.account.retryKey()
 assert.equal(f.account.snapshot().primaryActivation,null)
})

test('closed admission rejects source choices without clearing the pending default', async t => {
 const f=await fixture(t);await f.login()
 const request=f.account.snapshot().primaryActivation
 const pause=f.account.pauseForMaintenance();await pause.drained
 const before=f.account.snapshot()
 await assert.rejects(()=>f.account.selectSource('manual'),/ACCOUNT_CLOSING/)
 assert.deepEqual(f.account.snapshot(),before)
 assert.deepEqual(f.account.snapshot().primaryActivation,request)
 pause.resume();await until(()=>!f.account.admissionClosed)
 await f.account.close()
 const closed=f.account.snapshot()
 await assert.rejects(()=>f.account.selectSource('manual'),/ACCOUNT_CLOSING/)
 assert.deepEqual(f.account.snapshot(),closed)
})

test('only committed main-document navigation retires a login default and preserves account choices', async t => {
 const f=await fixture(t);await f.login();await f.account.selectSource('account')
 await f.account.logout();f.setIdentity('b');await f.login()
 const request=f.account.snapshot().primaryActivation
 const saved=await readFile(join(f.directory,'account.enc'),'utf8')
 const contents=new EventEmitter()
 bindAccountPrimaryActivationLifecycle(contents,()=>f.account.invalidatePrimaryActivation())
 contents.emit('did-navigate-in-page',{},'opensquilla://desktop/control/settings',true)
 contents.emit('did-frame-navigate',{},'about:blank',200,'OK',false,1,2)
 contents.emit('did-start-navigation',{},'opensquilla://desktop/control',false,true)
 const cancelled={defaultPrevented:false,preventDefault(){this.defaultPrevented=true}}
 cancelled.preventDefault();contents.emit('will-navigate',cancelled,'opensquilla://desktop/control')
 assert.deepEqual(f.account.snapshot().primaryActivation,request)
 contents.emit('did-navigate',{},'opensquilla://desktop/control',200,'OK')
 // A newly mounted renderer reads this host snapshot with no replayable offer.
 const remounted=f.account.snapshot()
 assert.equal(remounted.primaryActivation,null)
 assert.equal(remounted.status,'signed_in')
 assert.equal(remounted.profile.accountId,'b')
 assert.equal(remounted.source,'account')
 assert.equal(remounted.selectedAccountId,'a')
 assert.equal(await readFile(join(f.directory,'account.enc'),'utf8'),saved)
 await assert.rejects(()=>f.account.selectSource('account',request),/ACCOUNT_PRIMARY_ACTIVATION_STALE/)
 await f.account.verify(true);assert.equal(f.account.snapshot().primaryActivation,null)
})

for(const lifecycle of ['render-process-gone','destroyed']){
 test(`${lifecycle} retires defaults during maintenance even when renderer delivery throws`,async t=>{
  const f=await fixture(t);await f.login()
  const request=f.account.snapshot().primaryActivation
  const contents=new EventEmitter()
  bindAccountPrimaryActivationLifecycle(contents,()=>f.account.invalidatePrimaryActivation())
  const pause=f.account.pauseForMaintenance();await pause.drained
  const onState=f.deps.onState
  f.deps.onState=()=>{throw new Error('renderer frame is gone')}
  assert.doesNotThrow(()=>contents.emit(lifecycle,{}, {reason:'crashed'}))
  assert.equal(f.account.snapshot().primaryActivation,null)
  assert.equal(f.account.snapshot().status,'signed_in')
  f.deps.onState=onState
  pause.resume();await until(()=>!f.account.admissionClosed);await f.account.verify(true)
  assert.equal(f.account.snapshot().primaryActivation,null)
  await assert.rejects(()=>f.account.selectSource('account',request),/ACCOUNT_PRIMARY_ACTIVATION_STALE/)
 })
}

test('renderer lifecycle suppresses a pending OAuth default without cancelling its login',async t=>{
 let release,entered=false
 const gate=new Promise(resolve=>{release=resolve})
 const f=await fixture(t,{beforeFetch:async(url,init)=>{
  if(new URL(url).pathname==='/api/oauth/token' && new URLSearchParams(init.body).get('grant_type')==='authorization_code'){entered=true;await gate}
 }})
 const contents=new EventEmitter()
 bindAccountPrimaryActivationLifecycle(contents,()=>f.account.invalidatePrimaryActivation())
 await browserCallback(f);await until(()=>entered)
 contents.emit('did-navigate',{},'opensquilla://desktop/control',200,'OK')
 release();await f.account.loginCompletionPromise
 assert.equal(f.account.snapshot().status,'signed_in')
 assert.equal(f.account.snapshot().primaryActivation,null)
 assert.equal(f.account.snapshot().source,'manual')
})

test('renderer destruction invalidates every account while normal admission is closing',async t=>{
 const first=await fixture(t),second=await fixture(t)
 await first.login();await second.login()
 const contents=new EventEmitter()
 bindAccountPrimaryActivationLifecycle(contents,()=>{
  for(const account of [first.account,second.account]) account.invalidatePrimaryActivation()
 })
 first.deps.onState=()=>{throw new Error('destroyed frame')}
 const closing=first.account.close()
 assert.doesNotThrow(()=>contents.emit('destroyed'))
 assert.equal(first.account.snapshot().primaryActivation,null)
 assert.equal(second.account.snapshot().primaryActivation,null)
 assert.equal(second.account.snapshot().status,'signed_in')
 first.deps.onState=undefined
 await closing
})
test('restart restores encrypted login and exposes the local model lease before remote validation',async t=>{
 const f=await fixture(t);await f.login();await f.account.selectSource('account');const old=(await f.account.runtimeState()).credential;f.account.close()
 const restored=new TokenRhythmAccount(f.deps);t.after(()=>restored.close());await restored.initialize()
 assert.equal(restored.snapshot().status,'signed_in');assert.equal(restored.snapshot().keyStatus,'ready');assert.equal(restored.snapshot().selectedAccountId,'a');const restoredRuntime=await restored.runtimeState();assert.equal(restoredRuntime.ready,true);assert.ok(restoredRuntime.credential);assert.notEqual(restoredRuntime.credential,old);await restored.verify(true)
 assert.ok(!JSON.stringify(restored.snapshot()).includes('model-secret'))
})

test('failed media-source persistence rolls back its account binding',async t=>{
 const f=await fixture(t);await f.login();f.env.storageFailure=true
 await assert.rejects(()=>f.account.selectMediaSource('image','account'))
 assert.equal(f.account.snapshot().mediaSources.image,'inherit')
 assert.equal((await f.account.runtimeState()).mediaAccountIds.image,null)
})
test('independent media account binding keeps the local model lease with a manual text source', async t => {
 const f = await fixture(t); await f.login(); await f.account.selectMediaSource('image', 'account')
 assert.equal(f.account.snapshot().source, 'manual')
 const runtime = await f.account.runtimeState()
 assert.equal(runtime.ready, true)
 assert.ok(runtime.credential)
 assert.equal(runtime.mediaAccountIds.image, 'a')
 assert.equal(await f.account.modelCredential(runtime.credential), 'model-secret-a')
})
test('model 401 invalidation survives failed encrypted persistence', async t => {
 const f = await fixture(t); await f.login(); await f.account.selectSource('account')
 f.env.storageFailure = true
 await assert.rejects(() => f.account.modelKeyRejected(), /ACCOUNT_STORAGE_UNAVAILABLE/)
 assert.equal(f.account.snapshot().status, 'storage_unavailable')
 assert.equal(f.account.saved.key, null)
 assert.equal(await readFile(join(f.directory, 'model-key-rejected'), 'utf8'), '1')
 f.env.storageFailure = false
 await f.account.close()
 const restored = new TokenRhythmAccount(f.deps); t.after(() => restored.close())
 await restored.initialize()
 assert.equal(restored.saved.key, null)
 assert.equal((await restored.runtimeState()).ready, false)
 await restored.verify(true)
 assert.equal(restored.snapshot().status, 'signed_in')
 assert.equal(restored.snapshot().keyStatus, 'ready')
 assert.equal(restored.saved.key?.secret, 'model-secret-a')
 await assert.rejects(() => readFile(join(f.directory, 'model-key-rejected')), error => error.code === 'ENOENT')
})
test('cancel after the token exchange clears local authorization immediately',async t=>{
 const f=await fixture(t);const original=f.deps.fetch;let release
 f.deps.fetch=async (url,init)=>{if(new URL(url).pathname.endsWith('/account'))await new Promise(resolve=>{release=resolve});return original(url,init)}
 const account=new TokenRhythmAccount(f.deps);t.after(()=>account.close());await account.login()
 const url=new URL(f.opened),callback=new URL(url.searchParams.get('redirect_uri'));callback.search=new URLSearchParams({state:url.searchParams.get('state'),code:'valid'});await fetch(callback);await until(()=>Boolean(release))
 account.cancel();assert.equal(account.snapshot().status,'signed_out');release();await account.verify(true);assert.equal(account.snapshot().profile,null);assert.equal((await account.runtimeState()).credential,'');await until(()=>!account.snapshot().pendingRevocation);await account.close()
})

test('cancel during the first encrypted write retains the logout marker when saving the cleared state fails', async t => {
 const f = await fixture(t)
 await f.account.login()
 let releaseWrite
 f.account.writes = new Promise(resolve => { releaseWrite = resolve })
 const url = new URL(f.opened), callback = new URL(url.searchParams.get('redirect_uri'))
 callback.search = new URLSearchParams({ state: url.searchParams.get('state'), code: 'valid' })
 await fetch(callback)
 await until(() => Boolean(f.account.saved.tokens))
 const completion = f.account.loginCompletionPromise
 f.env.storageFailure = true
 f.account.cancel()
 const logout = assert.rejects(f.account.logoutPromise, /failed/)
 releaseWrite()
 await Promise.all([completion, logout])
 await f.account.close()
 // The failed replacement leaves an old encrypted login on disk. Its durable
 // marker must survive the older login's completion and win on restart.
 const stale = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
 assert.ok(stale.tokens)
 assert.equal(await readFile(join(f.directory, 'logged-out'), 'utf8'), '1')
 f.env.storageFailure = false
 const restored = new TokenRhythmAccount(f.deps)
 t.after(() => restored.close())
 await restored.initialize()
 assert.equal(restored.snapshot().status, 'signed_out')
 assert.equal(restored.saved.tokens, null)
 await restored.close()
})

test('immediate re-login waits for a cancelled exchange and its revocation for the same installation', async t => {
 const f = await fixture(t)
 let releaseExchange, releaseRevoke, currentSession = null
 const events = []
 const original = f.deps.fetch
 const account = new TokenRhythmAccount({ ...f.deps, fetch: async (url, init = {}) => {
  const path = new URL(url).pathname
  if (path === '/api/oauth/token') {
   const authCode = new URLSearchParams(init.body).get('code')
   events.push(`exchange:${authCode}`)
   if (authCode === 'old') await new Promise(resolve => { releaseExchange = resolve })
   // Match exchangeCode's upsert: each authorization replaces the session ID
   // belonging to this user/client/installation, invalidating the previous ID.
   currentSession = authCode
   return Response.json({ token_type: 'Bearer', access_token: `access:${authCode}`, refresh_token: `refresh:${authCode}`, expires_in: 3600, scope, account_id: 'a', session_id: authCode })
  }
  const sid = String(init.headers?.Authorization).replace('Bearer access:', '')
  if (sid !== currentSession || !currentSession) return Response.json({ code: 'ACCOUNT_SESSION_REVOKED' }, { status: 401 })
  if (path.endsWith('/session')) {
   await new Promise(resolve => { releaseRevoke = resolve })
   assert.equal(sid, currentSession)
   events.push(`revoke:${sid}`)
   currentSession = null
   return new Response(null, { status: 204 })
  }
  const response = await original(url, init)
  if (!path.endsWith('/account')) return response
  const body = await response.json()
  body.data.session_id = sid
  return Response.json(body)
 } })
 t.after(async () => { releaseExchange?.(); releaseRevoke?.(); await account.close() })
 const callback = async authCode => {
  const url = new URL(f.opened), target = new URL(url.searchParams.get('redirect_uri'))
  target.search = new URLSearchParams({ state: url.searchParams.get('state'), code: authCode })
  const response = await fetch(target)
  await response.text()
 }
 await account.login()
 const firstUrl = f.opened
 await callback('old')
 await until(() => Boolean(releaseExchange))
 account.cancel()
 let newLoginStarted = false
 const relogin = account.login().then(() => { newLoginStarted = true })
 await new Promise(setImmediate)
 assert.equal(newLoginStarted, false)
 assert.equal(f.opened, firstUrl)
 releaseExchange()
 await until(() => Boolean(releaseRevoke))
 assert.equal(newLoginStarted, false, 'the old revocation also belongs to the cancelled authorization')
 releaseRevoke()
 await relogin
 assert.equal(new URL(f.opened).searchParams.get('installation_id'), new URL(firstUrl).searchParams.get('installation_id'))
 await callback('new')
 await account.loginCompletionPromise
 await account.verify(true)
 assert.equal(account.snapshot().status, 'signed_in')
 assert.equal(account.snapshot().profile.sessionId, 'new')
 assert.equal(currentSession, 'new')
 assert.deepEqual(events, ['exchange:old', 'revoke:old', 'exchange:new'])
})

for (const recovery of ['reconnect', 'restart', 'disk-recovery']) {
 test(`cancelled token exchange retains failed revocation through ${recovery}`, async t => {
  let releaseExchange, online = false, revocations = 0
  const f = await fixture(t, { beforeFetch: async (url, init) => {
   const path = new URL(url).pathname
   if (path === '/api/oauth/token') await new Promise(resolve => { releaseExchange = resolve })
   if (path.endsWith('/session')) {
    assert.equal(init.headers.Authorization, 'Bearer access-a')
    if (!online) throw Error('synthetic revocation network failure')
    revocations++
   }
  } })
  await f.account.login()
  const url = new URL(f.opened), callback = new URL(url.searchParams.get('redirect_uri'))
  callback.search = new URLSearchParams({ state: url.searchParams.get('state'), code: 'cancelled' })
  await fetch(callback)
  await until(() => Boolean(releaseExchange))
  const completion = f.account.loginCompletionPromise
  f.account.cancel()
  f.env.storageFailure = recovery === 'disk-recovery'
  releaseExchange()
  await completion
  assert.equal(f.account.snapshot().status, 'signed_out')
  assert.equal(f.account.snapshot().pendingRevocation, true)
  assert.equal(f.account.saved.tokens, null)
  let account = f.account
  if (recovery !== 'reconnect') {
   if (recovery === 'disk-recovery') {
    await assert.rejects(() => account.close(), /failed/)
    f.env.storageFailure = false
   }
   await account.close()
   account = new TokenRhythmAccount(f.deps)
   t.after(() => account.close())
   await account.initialize()
   await account.revokePromise
  }
  const saved = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
  assert.equal(saved.tokens, null)
  assert.equal(saved.pending[0].sessionId, 'session-a')
  assert.equal(account.snapshot().pendingRevocation, true)
  online = true
  await account.verify(true)
  await account.revokePromise
  assert.equal(revocations, 1)
  assert.equal(account.snapshot().pendingRevocation, false)
  assert.equal(account.snapshot().status, 'signed_out')
  assert.equal(JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8'))).pending.length, 0)
 })
}

test('shutdown waits for an in-flight refresh and preserves its session for restart', async t => {
 let releaseRefresh
 const f = await fixture(t, { beforeFetch: async (url, init) => {
  if (new URL(url).pathname === '/api/oauth/token' && new URLSearchParams(init.body).get('grant_type') === 'refresh_token') {
   await new Promise(resolve => { releaseRefresh = resolve })
  }
 } })
 await f.login()
 await f.account.selectSource('account')
 f.advance(40_000)
 const refreshing = f.account.verify(true)
 await until(() => Boolean(releaseRefresh))
 let closed = false
 const closing = f.account.close()
 closing.then(() => { closed = true })
 try {
  assert.equal(f.account.close(), closing, 'repeated shutdown joins the existing drain')
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(closed, false, 'an exchange awaiting its response has not safely drained')
  await assert.rejects(() => f.account.selectSource('manual'), /ACCOUNT_CLOSING/)
 } finally { releaseRefresh() }
 await Promise.all([refreshing, closing])
 const saved = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
 assert.equal(saved.tokens.refreshToken, 'refresh-a-1')
 assert.equal(f.calls.filter(call => call.path.endsWith('/session')).length, 0, 'shutdown does not revoke the rotated session')
 const restored = new TokenRhythmAccount(f.deps)
 t.after(() => restored.close())
 await restored.initialize()
 await restored.verify(true)
 assert.equal(restored.snapshot().status, 'signed_in')
 assert.equal(restored.snapshot().selectedAccountId, 'a')
})

test('shutdown waits until a rotated token reaches the encrypted record', async t => {
 const f = await fixture(t)
 await f.login()
 let finishDiskWrite
 // Hold the serial disk queue to model slow I/O after the server has rotated.
 f.account.writes = new Promise(resolve => { finishDiskWrite = resolve })
 f.advance(40_000)
 const refreshing = f.account.verify(true)
 await until(() => f.account.saved.tokens.refreshToken === 'refresh-a-1')
 let closed = false
 const closing = f.account.close().then(() => { closed = true })
 try {
  await new Promise(resolve => setImmediate(resolve))
  const before = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
  assert.equal(before.tokens.refreshToken, 'refresh-a-0')
  assert.equal(closed, false)
 } finally { finishDiskWrite() }
 await Promise.all([refreshing, closing])
 const after = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
 assert.equal(after.tokens.refreshToken, 'refresh-a-1')
 assert.equal(f.calls.filter(call => call.path.endsWith('/session')).length, 0)
})

test('shutdown saves an authorization exchange already accepted by the callback', async t => {
 let releaseExchange
 const f = await fixture(t, { beforeFetch: async (url, init) => {
  if (new URL(url).pathname === '/api/oauth/token') await new Promise(resolve => { releaseExchange = resolve })
 } })
 await f.account.login()
 const url = new URL(f.opened)
 const callback = new URL(url.searchParams.get('redirect_uri'))
 callback.search = new URLSearchParams({ state: url.searchParams.get('state'), code: 'valid' })
 await fetch(callback)
 await until(() => Boolean(releaseExchange))
 let closed = false
 const closing = f.account.close().then(() => { closed = true })
 try {
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(closed, false)
 } finally { releaseExchange() }
 await closing
 const saved = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
 assert.equal(saved.tokens.refreshToken, 'refresh-a-0')
 assert.equal(f.calls.filter(call => call.path.endsWith('/session')).length, 0)
})

test('shutdown rejects when both rotated-token persistence and logout marker fail, then retries after recovery', async t => {
 let releaseRefresh
 const f = await fixture(t, { beforeFetch: async (url, init) => {
  if (new URL(url).pathname === '/api/oauth/token' && new URLSearchParams(init.body).get('grant_type') === 'refresh_token') {
   await new Promise(resolve => { releaseRefresh = resolve })
  }
 } })
 await f.login()
 const marker = join(f.directory, 'logged-out')
 f.advance(40_000)
 const refreshing = f.account.verify(true)
 await until(() => Boolean(releaseRefresh))
 try {
  f.env.storageFailure = true
  // Exercise the real marker write failing independently of encryption.
  await mkdir(marker)
  const closing = f.account.close()
  const rejectedClose = assert.rejects(closing, /failed/)
  releaseRefresh()
  await Promise.all([refreshing, rejectedClose])
  const stale = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
  assert.equal(stale.tokens.refreshToken, 'refresh-a-0')
  assert.equal(f.account.snapshot().status, 'reauth_required')
  await assert.rejects(() => f.account.selectSource('manual'), /ACCOUNT_CLOSING/)
 } finally {
  releaseRefresh()
  f.env.storageFailure = false
  await rm(marker, { recursive: true, force: true })
 }
 await f.account.close()
 const saved = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
 assert.equal(saved.tokens, null, 'retry must replace the old rotated token before reporting successful shutdown')
 const restored = new TokenRhythmAccount(f.deps)
 t.after(() => restored.close())
 await restored.initialize()
 assert.equal(restored.snapshot().status, 'signed_out')
})

for (const stage of ['exchange', 'disk']) {
 test(`update pause waits for ${stage} and recovers without losing the account`, async t => {
  let release
  const f = await fixture(t, { beforeFetch: async (url, init) => {
   if (stage === 'exchange' && new URL(url).pathname === '/api/oauth/token'
    && new URLSearchParams(init.body).get('grant_type') === 'refresh_token') {
    await new Promise(resolve => { release = resolve })
   }
  } })
  await f.login()
  await f.account.selectSource('account')
  const oldLease = (await f.account.runtimeState()).credential
  if (stage === 'disk') f.account.writes = new Promise(resolve => { release = resolve })
  f.advance(40_000)
  const refreshing = f.account.verify(true)
  await until(() => stage === 'exchange' ? Boolean(release) : f.account.saved.tokens.refreshToken === 'refresh-a-1')
  const pause = pauseAccountsForMaintenance([f.account])
  let drained = false
  const waiting = pause.wait(AbortSignal.timeout(1000)).then(() => { drained = true })
  try {
   await new Promise(setImmediate)
   assert.equal(drained, false)
   await assert.rejects(() => f.account.selectSource('manual'), /ACCOUNT_CLOSING/)
   await assert.rejects(() => f.account.modelCredential(oldLease), /ACCOUNT_CLOSING/)
  } finally { release() }
  await Promise.all([refreshing, waiting])
  const saved = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
  assert.equal(saved.tokens.refreshToken, 'refresh-a-1')
  assert.equal(saved.selectedAccountId, 'a')
  assert.equal(f.calls.filter(call => call.path.endsWith('/session')).length, 0)
  pause.resume()
  await until(() => f.account.lease && f.account.lease !== oldLease)
  assert.equal(f.account.snapshot().status, 'signed_in')
  assert.equal(f.refreshes, 1)
  await f.account.selectSource('manual')
 })
}

test('timed-out update resumes only after token persistence and cannot reopen a later update', async t => {
 let release
 const f = await fixture(t, { beforeFetch: async (url, init) => {
  if (new URL(url).pathname === '/api/oauth/token' && new URLSearchParams(init.body).get('grant_type') === 'refresh_token') {
   await new Promise(resolve => { release = resolve })
  }
 } })
 await f.login()
 f.advance(40_000)
 const refreshing = f.account.verify(true)
 await until(() => Boolean(release))
 const first = pauseAccountsForMaintenance([f.account])
 const controller = new AbortController()
 const waiting = first.wait(controller.signal)
 controller.abort(new Error('synthetic update deadline'))
 await assert.rejects(waiting, /synthetic update deadline/)
 first.resume()
 await assert.rejects(() => f.account.selectSource('manual'), /ACCOUNT_CLOSING/)
 const second = pauseAccountsForMaintenance([f.account])
 release()
 await Promise.all([refreshing, second.wait(AbortSignal.timeout(1000))])
 await new Promise(setImmediate)
 await assert.rejects(() => f.account.selectSource('manual'), /ACCOUNT_CLOSING/)
 second.resume()
 await until(() => f.account.lease)
 await f.account.selectSource('manual')
})

test('update pause saves an accepted login exchange and can resume after update failure', async t => {
 let release
 const f = await fixture(t, { beforeFetch: async url => {
  if (new URL(url).pathname === '/api/oauth/token') await new Promise(resolve => { release = resolve })
 } })
 await f.account.login()
 const url = new URL(f.opened), callback = new URL(url.searchParams.get('redirect_uri'))
 callback.search = new URLSearchParams({ state: url.searchParams.get('state'), code: 'valid' })
 await fetch(callback)
 await until(() => Boolean(release))
 const pause = pauseAccountsForMaintenance([f.account])
 release()
 await pause.wait(AbortSignal.timeout(1000))
 const saved = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
 assert.equal(saved.tokens.refreshToken, 'refresh-a-0')
 pause.resume()
 await until(() => f.account.snapshot().keyStatus === 'ready')
 assert.equal(f.account.snapshot().status, 'signed_in')
})

test('update cancellation resets browser authorization and cannot reopen a real shutdown', async t => {
 const f = await fixture(t)
 await f.account.login()
 const pause = pauseAccountsForMaintenance([f.account])
 await pause.wait(AbortSignal.timeout(1000))
 assert.equal(f.account.snapshot().status, 'signed_out')
 await f.account.close()
 pause.resume()
 await new Promise(setImmediate)
 await assert.rejects(() => f.account.login(), /ACCOUNT_CLOSING/)
})

test('update drain rejects an undurable logout and permits recovery after storage returns', async t => {
 const f = await fixture(t)
 await f.login()
 const marker = join(f.directory, 'logged-out')
 await mkdir(marker)
 f.env.storageFailure = true
 await assert.rejects(() => f.account.logout())
 const pause = pauseAccountsForMaintenance([f.account])
 await assert.rejects(pause.wait(AbortSignal.timeout(1000)), /failed/)
 assert.equal(f.account.snapshot().status, 'signed_out')
 pause.resume()
 f.env.storageFailure = false
 await rm(marker, { recursive: true, force: true })
 await new Promise(setImmediate)
 await f.account.close()
 const saved = JSON.parse(f.deps.decrypt(await readFile(join(f.directory, 'account.enc'), 'utf8')))
 assert.equal(saved.tokens, null, 'a later Quit cannot restore the older encrypted login')
})

test('browser receipt waits for verified credentials, removes OAuth parameters, and opens only the current dev app', async t => {
 let release
 const f = await fixture(t, { beforeFetch: async url => {
  if (new URL(url).pathname === '/api/oauth/token') await new Promise(resolve => { release = resolve })
 } })
 t.after(() => release?.())
 let opens = 0
 f.deps.onOpenDesktop = async () => { opens++ }
 const receipt = await browserCallback(f)
 assert.equal(receipt.response.status, 200)
 assert.match(new URL(receipt.result).pathname, /^\/oauth\/result\/[A-Za-z0-9_-]{43}$/)
 assert.equal(new URL(receipt.result).search, '')
 assert.match(receipt.html, /<h1 id="result-title">正在完成登录<\/h1>/)
 assert.doesNotMatch(receipt.html, /browser-once|model-secret-a|access-a|refresh-a|code_verifier/)
 assert.equal(await receipt.status(), 'pending')
 const open = (headers = {}) => fetch(receipt.result + '/open', { method: 'POST', headers })
 const trusted = { origin: new URL(receipt.result).origin, 'x-opensquilla-login-result': '1' }
 assert.equal((await open(trusted)).status, 403)
 assert.equal((await fetch(receipt.callback)).status, 400)
 assert.equal(f.calls.filter(call => call.path === '/api/oauth/token').length, 1)
 release()
 await f.account.loginCompletionPromise
 assert.equal(await receipt.status(), 'success')
 // A website's top-level redirect can retain cross-site Fetch Metadata.
 assert.equal((await fetch(receipt.result, { headers: { 'sec-fetch-site': 'cross-site' } })).status, 200)
 assert.equal((await fetch(receipt.result + '/status', { headers: { 'sec-fetch-site': 'cross-site' } })).status, 403)
 assert.equal((await open()).status, 403)
 assert.equal((await open({ ...trusted, origin: 'https://evil.invalid' })).status, 403)
 assert.equal((await open({ ...trusted, 'sec-fetch-site': 'cross-site' })).status, 403)
 assert.equal(opens, 0)
 assert.equal((await open(trusted)).status, 204)
 assert.equal(opens, 1)
 assert.match(receipt.response.headers.get('content-security-policy'), /script-src 'nonce-/)
 assert.equal(receipt.response.headers.get('referrer-policy'), 'no-referrer')
 await f.account.close()
 await assert.rejects(() => fetch(receipt.result))
})

test('packaged browser receipt uses the fixed credential-free application protocol', async t => {
 const f = await fixture(t)
 f.deps.openAppViaProtocol = true
 const receipt = await browserCallback(f)
 await f.account.loginCompletionPromise
 assert.equal(await receipt.status(), 'success')
 assert.match(receipt.html, /href="opensquilla:\/\/open"/)
 assert.match(receipt.html, /data-protocol="true"/)
 assert.equal((await fetch(receipt.result + '/open', { method: 'POST', headers: {
  origin: new URL(receipt.result).origin, 'x-opensquilla-login-result': '1'
 } })).status, 403)
})

test('denied browser consent shows cancellation without exchanging a code', async t => {
 const f = await fixture(t)
 const receipt = await browserCallback(f, { error: 'access_denied' })
 assert.equal(await receipt.status(), 'cancelled')
 assert.equal(f.calls.length, 0)
 assert.equal(f.account.snapshot().status, 'signed_out')
})

test('failed storage, account verification and model credential acquisition never show login success', async t => {
 for (const failure of ['storageFailure', 'offline', 'keyFailure', 'revoked']) {
  const f = await fixture(t, { [failure]: true })
  const receipt = await browserCallback(f)
  await f.account.loginCompletionPromise
  assert.equal(await receipt.status(), 'error', failure)
  f.env[failure] = false
 }
})

test('cancelling a pending exchange cannot turn its browser receipt into success', async t => {
 let release
 const f = await fixture(t, { beforeFetch: async url => {
  if (new URL(url).pathname === '/api/oauth/token') await new Promise(resolve => { release = resolve })
 } })
 t.after(() => release?.())
 const receipt = await browserCallback(f)
 f.account.cancel()
 assert.equal(await receipt.status(), 'cancelled')
 release()
 await f.account.loginCompletionPromise
 assert.equal(await receipt.status(), 'cancelled')
 assert.equal(f.account.snapshot().status, 'signed_out')
})

test('browser receipt expires and closes its loopback listener', async () => {
 const { createServer } = await import('node:http')
 const { once } = await import('node:events')
 const { TokenRhythmLoginResult } = await import('../dist/tokenrhythm-login-result.js')
 const server = createServer()
 server.listen(0, '127.0.0.1')
 await once(server, 'listening')
 const origin = `http://127.0.0.1:${server.address().port}`
 const receipt = new TokenRhythmLoginResult(server, origin, false, async () => {}, 20)
 try {
  await new Promise(resolve => setTimeout(resolve, 35))
  assert.equal(server.listening, false)
  await assert.rejects(() => fetch(origin + receipt.path))
 } finally { receipt.close() }
})

test('local WebUI receipt returns to the browser without exposing the authorization code', async () => {
 const { createServer } = await import('node:http')
 const { once } = await import('node:events')
 const { TokenRhythmLoginResult } = await import('../dist/tokenrhythm-login-result.js')
 let receipt
 const server = createServer((req, res) => { if (!receipt.handle(req, res)) res.writeHead(404).end() })
 server.listen(0, '127.0.0.1')
 await once(server, 'listening')
 const origin = `http://127.0.0.1:${server.address().port}`
 const returnUrl = 'http://127.0.0.1:5174/control/settings/account'
 receipt = new TokenRhythmLoginResult(server, origin, false, async () => {}, 1000, returnUrl)
 try {
  const response = await fetch(origin + receipt.path)
  const html = await response.text()
  assert.equal(response.status, 200)
  assert.match(html, /data-web-return="http:\/\/127\.0\.0\.1:5174\/control\/settings\/account"/)
  assert.doesNotMatch(html, /one-use-code|code_verifier|refresh_token/)
  assert.throws(() => new TokenRhythmLoginResult(server, origin, false, async () => {}, 1000, 'https://example.com/control/settings/account'))
 } finally { receipt.close() }
})
