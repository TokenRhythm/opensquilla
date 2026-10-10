import assert from 'node:assert/strict'
import test from 'node:test'
import { preserveKeychainPreferences, withSigningKeychainSearch } from './mac-keychain-preferences.mjs'

for (const fails of [false, true]) {
  test(`keychain initialization restores account preferences on ${fails ? 'failure' : 'success'}`, () => {
    const original = ['/synthetic/login.keychain-db', '/synthetic/System.keychain']
    let search = [...original]
    let defaultPath = original[0]
    const run = args => {
      const isSearch = args[0] === 'list-keychains'
      if (args[3] === '-s') {
        if (isSearch) search = args.slice(4)
        else defaultPath = args[4]
        return ''
      }
      return (isSearch ? search : [defaultPath]).map(path => JSON.stringify(path)).join('\n')
    }
    const initialize = () => preserveKeychainPreferences(() => {
      search = ['/synthetic/temporary.keychain-db']
      defaultPath = search[0]
      if (fails) throw new Error('synthetic initialization error')
      return 'created'
    }, run)
    if (fails) assert.throws(initialize, /synthetic initialization error/)
    else assert.equal(initialize(), 'created')
    assert.deepEqual(search, original)
    assert.equal(defaultPath, original[0])
  })
}

test('missing account preferences reject initialization before changes', () => {
  assert.throws(() => preserveKeychainPreferences(
    () => assert.fail('initialization must not run'), () => '',
  ), /missing user Keychain preferences/)
})

const signingKeychain = '/synthetic/build.keychain-db'
const originalSearch = ['/synthetic/login.keychain-db', '/synthetic/System.keychain']

function signingPreferences({ search = originalSearch, defaults = [originalSearch[0]] } = {}) {
  const state = { search: [...search], defaults: [...defaults], writes: [] }
  const run = args => {
    const isSearch = args[0] === 'list-keychains'
    if (args[3] === '-s') {
      state.writes.push([...args])
      if (isSearch) state.search = args.slice(4)
      else state.defaults = args.slice(4)
      return ''
    }
    return (isSearch ? state.search : state.defaults).map(path => JSON.stringify(path)).join('\n')
  }
  return { state, run }
}

test('signing membership stays present until asynchronous success and preserves original order', async () => {
  const { state, run } = signingPreferences()
  let finish
  const pending = withSigningKeychainSearch(signingKeychain, () => new Promise(resolve => {
    finish = resolve
  }), run)
  assert.deepEqual(state.search, [...originalSearch, signingKeychain])
  assert.deepEqual(state.defaults, [originalSearch[0]])
  await Promise.resolve()
  assert.deepEqual(state.search, [...originalSearch, signingKeychain])
  finish('signed')
  assert.equal(await pending, 'signed')
  assert.deepEqual(state.search, originalSearch)
  assert.deepEqual(state.defaults, [originalSearch[0]])
  assert.equal(state.writes.every(args => args[0] === 'list-keychains'), true,
    'ordinary signing must never write the default keychain')
})

for (const mode of ['throw', 'reject']) {
  test(`signing membership is removed after ${mode} without swallowing its error`, async () => {
    const { state, run } = signingPreferences()
    const failure = new Error('synthetic signing failure')
    await assert.rejects(withSigningKeychainSearch(signingKeychain, () => {
      if (mode === 'throw') throw failure
      return Promise.reject(failure)
    }, run), error => error === failure)
    assert.deepEqual(state.search, originalSearch)
    assert.deepEqual(state.defaults, [originalSearch[0]])
  })
}

test('cleanup retains concurrent search-list additions in their current order', async () => {
  const { state, run } = signingPreferences()
  await withSigningKeychainSearch(signingKeychain, async () => {
    state.search = ['/synthetic/concurrent-first', ...state.search, '/synthetic/concurrent-last']
    await Promise.resolve()
  }, run)
  assert.deepEqual(state.search, ['/synthetic/concurrent-first', ...originalSearch, '/synthetic/concurrent-last'])
})

test('pre-existing signing membership is not removed or rewritten', async () => {
  const search = [originalSearch[0], signingKeychain, originalSearch[1]]
  const { state, run } = signingPreferences({ search })
  await withSigningKeychainSearch(signingKeychain, async () => {
    state.search.push('/synthetic/concurrent')
  }, run)
  assert.deepEqual(state.search, [...search, '/synthetic/concurrent'])
  assert.deepEqual(state.writes, [])
})

test('unexpected default changes are restored without replacing unrelated search entries', async () => {
  const { state, run } = signingPreferences()
  await withSigningKeychainSearch(signingKeychain, async () => {
    state.defaults = ['/synthetic/unexpected-default']
    state.search.push('/synthetic/concurrent')
  }, run)
  assert.deepEqual(state.defaults, [originalSearch[0]])
  assert.deepEqual(state.search, [...originalSearch, '/synthetic/concurrent'])
  assert.deepEqual(state.writes.filter(args => args[0] === 'default-keychain'), [
    ['default-keychain', '-d', 'user', '-s', originalSearch[0]],
  ])
})

for (const missing of [{ search: [] }, { defaults: [] }, { defaults: ['one', 'two'] }]) {
  test(`invalid preferences stop signing before writes: ${JSON.stringify(missing)}`, async () => {
    const { state, run } = signingPreferences(missing)
    await assert.rejects(withSigningKeychainSearch(signingKeychain,
      () => assert.fail('signing must not start'), run), /missing user Keychain preferences/)
    assert.deepEqual(state.writes, [])
  })
}

test('partial membership write failure still restores the original search list', async () => {
  const { state, run } = signingPreferences()
  const failure = new Error('synthetic append failure')
  await assert.rejects(withSigningKeychainSearch(signingKeychain,
    () => assert.fail('signing must not start'), args => {
      const result = run(args)
      if (args[0] === 'list-keychains' && args[3] === '-s' && args.includes(signingKeychain)) throw failure
      return result
    }), error => error === failure)
  assert.deepEqual(state.search, originalSearch)
})

test('cleanup attempts search restoration even when default restoration fails and keeps both errors', async () => {
  const { state, run } = signingPreferences()
  const failure = new Error('synthetic signing failure')
  const restoreFailure = new Error('synthetic restoration failure')
  await assert.rejects(withSigningKeychainSearch(signingKeychain, async () => {
    state.defaults = ['/synthetic/unexpected-default']
    throw failure
  }, args => {
    if (args[0] === 'default-keychain' && args[3] === '-s') throw restoreFailure
    return run(args)
  }), error => error instanceof AggregateError
    && error.errors[0] === failure && error.errors[1] === restoreFailure)
  assert.deepEqual(state.search, originalSearch)
})
