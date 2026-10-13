import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

// Exercise the same response transform used by the custom protocol without
// loading Electron's main process or launching the user's application.
const source = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const declaration = source.match(/function secureDesktopRendererDocument\(response: Response\): Response \{[\s\S]*?\n\}/)?.[0]
assert.ok(declaration, 'desktop CSP response transform exists')
const transform = vm.runInNewContext(`(${declaration.replace('(response: Response): Response', '(response)')})`, { Headers, Response })
const response = transform(new Response('document'))
const directives = response.headers.get('content-security-policy').split(';').map(value => value.trim())
assert.ok(directives.includes("script-src 'self' opensquilla-app://desktop 'unsafe-inline' 'wasm-unsafe-eval'"))
assert.ok(directives.includes("worker-src 'self' opensquilla-app://desktop blob:"))
assert.ok(!response.headers.get('content-security-policy').includes("'unsafe-eval'"))
assert.equal(response.headers.get('x-content-type-options'), 'nosniff')
assert.equal(await response.text(), 'document')
console.log('Office preview desktop CSP checks passed.')
