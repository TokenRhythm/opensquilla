import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from 'smol-toml'
import { mayNeedPythonTomlParser } from '../dist/desktop-startup-toml.js'

// Shared with the local Python 3.12 tomllib differential check. CI needs only
// Node: these cover the syntax boundary, while smol-toml owns validation.
const fixtures = JSON.parse(readFileSync(new URL('./fixtures/desktop-startup-toml.json', import.meta.url), 'utf8'))
for (const { name, toml, defer } of fixtures) {
  assert.equal(mayNeedPythonTomlParser(toml), defer, name)
  if (!defer) assert.doesNotThrow(() => parse(toml), name)
}

// Forbidden controls must still defer even when hidden inside a comment or
// literal string, where ordinary punctuation no longer forces a fallback.
const controls = [...Array(32).keys(), 127].filter(code => ![9, 10, 13].includes(code))
for (const code of controls) {
  for (const toml of [`# comment ${String.fromCharCode(code)}\n`, `value = '${String.fromCharCode(code)}'\n`]) {
    assert.equal(mayNeedPythonTomlParser(toml), true, `control ${code}`)
    assert.throws(() => parse(toml), `control ${code}`)
  }
}

// Invalid syntax is still the parser's responsibility, not an alternate grammar
// in the compatibility filter. A malformed document must not become settings.
for (const toml of ['value = { a = 1 a = 2 }', 'value = { a = 1, a = 2 }', 'value = "bad\\q"']) {
  assert.throws(() => parse(toml))
}
console.log(`Desktop startup TOML compatibility: ${fixtures.length} syntax cases and ${controls.length * 2} control cases passed.`)
