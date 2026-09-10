import assert from 'node:assert/strict'
import test from 'node:test'
import { requireDesktopForeground, withForegroundActions } from './live-html-foreground.mjs'

function fixture(states) {
  let clock = 0, activation = 0, disposed = 0, observations = 0
  const handle = {
    async evaluate(fn) {
      if (fn.toString().includes('owner => owner.id')) return 7
      return states[Math.min(observations++, states.length - 1)]
    },
    async dispose() { disposed += 1 },
  }
  const app = { async browserWindow(page) { assert.equal(page, 'exact-page'); return handle }, async evaluate(_fn, id) { assert.equal(id, 7); activation += 1 } }
  return { app, options: { timeoutMs: 100, now: () => clock, pause: async n => { clock += n } }, counts: () => ({ activation, disposed, observations }) }
}
const focused = { ownerId: 7, webContentsId: 8, visible: true, minimized: false, ownerFocused: true, contentsFocused: true }
test('one native activation waits for both owner and contents focus', async () => {
  const f = fixture([{ ...focused, contentsFocused: false }, focused])
  assert.deepEqual(await requireDesktopForeground(f.app, 'exact-page', f.options), focused)
  assert.deepEqual(f.counts(), { activation: 1, disposed: 1, observations: 2 })
})
for (const state of [{ ...focused, ownerFocused: false }, { ...focused, contentsFocused: false }, { ...focused, visible: false }]) {
  test(`focus gate rejects incomplete native state: ${JSON.stringify(state)}`, async () => {
    const f = fixture([state])
    await assert.rejects(requireDesktopForeground(f.app, 'exact-page', f.options), error => error.message === 'DESKTOP_FOREGROUND_REQUIRED' && error.diagnostic.observed === state)
    assert.equal(f.counts().activation, 1); assert.equal(f.counts().disposed, 1)
  })
}
test('destroyed owner fails without another activation', async () => {
  const f = fixture([{ destroyed: true }])
  await assert.rejects(requireDesktopForeground(f.app, 'exact-page', f.options), /DESKTOP_FOREGROUND_OWNER_LOST/)
  assert.equal(f.counts().activation, 1); assert.equal(f.counts().disposed, 1)
})
test('action gate cannot invoke an input method when focus was not acquired', async () => {
  let acted = false
  const client = withForegroundActions({ click() { acted = true }, read() { return 'observation' } }, ['click'], async () => { throw new Error('DESKTOP_FOREGROUND_REQUIRED') })
  assert.equal(client.read(), 'observation')
  await assert.rejects(client.click(), /DESKTOP_FOREGROUND_REQUIRED/)
  assert.equal(acted, false)
})
test('successful action gate preserves receiver and arguments', async () => {
  const steps = []
  const target = { label: 'owner', click(value) { steps.push([this.label, value]); return value } }
  const client = withForegroundActions(target, ['click'], async name => steps.push(name))
  assert.equal(await client.click('submit'), 'submit'); assert.deepEqual(steps, ['click', ['owner', 'submit']])
})
