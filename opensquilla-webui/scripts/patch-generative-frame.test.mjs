import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { ORIGINAL_SHA256, patchBootstrap, UPSTREAM_VERSION } from './patch-generative-frame.mjs'

const installed = await readFile(new URL('../node_modules/generative-frame/dist/bootstrap.js', import.meta.url), 'utf8')
const patched = patchBootstrap(installed, UPSTREAM_VERSION)
const original = patched
  .replace('new Set([`script`,`noscript`,`template`,`style`])', 'new Set([`script`,`noscript`,`template`])')
  .replace('let s=t.get(r),c=[],Q=r.computedStyleMap?.();', 'let s=t.get(r),c=[];')
  .replace(/let r=\/\^.*?;/, 'let r=a.getPropertyValue(t);')

test('patches the locked runtime once and is byte-for-byte idempotent', () => {
  assert.equal(patchBootstrap(patched, UPSTREAM_VERSION), patched)
  assert.equal(createHash('sha256').update(original).digest('hex'), ORIGINAL_SHA256)
  assert.equal(patchBootstrap(original, UPSTREAM_VERSION), patched)
})

test('migrates the earlier local layout-zoom patch without keeping its layout inference', () => {
  const earlier = original
    .replace('new Set([`script`,`noscript`,`template`])', 'new Set([`script`,`noscript`,`template`,`style`])')
    .replace('width:${a}px;height:${o}px;overflow:hidden;', 'zoom:${r.devicePixelRatio||1};width:${a}px;height:${o}px;overflow:hidden;')
    .replace('0 0 ${a} ${o}', '0 0 ${a*(r.devicePixelRatio||1)} ${o*(r.devicePixelRatio||1)}')
  assert.equal(patchBootstrap(earlier, UPSTREAM_VERSION), patched)
})

test('rejects a different version or any unknown upstream edit without writing', () => {
  assert.throws(() => patchBootstrap(installed, '0.0.6'), /Unsupported/)
  assert.throws(() => patchBootstrap(`${installed}\n`, UPSTREAM_VERSION), /Unknown/)
  assert.throws(() => patchBootstrap(installed.replace('buildBootstrapHtml', 'unknownBootstrap'), UPSTREAM_VERSION), /Unknown/)
})

test('keeps the public screenshot protocol and the sandbox bootstrap unchanged', () => {
  assert.match(patched, /genframe\/screenshot/)
  assert.match(patched, /Content-Security-Policy/)
  assert.match(patched, /new Set\(\[`script`,`noscript`,`template`,`style`\]\)/)
  assert.doesNotMatch(patched, /zoom:\$\{r\.devicePixelRatio\|\|1\}/)
})

// Exercise the actual patched upstream clone, rather than a reimplementation
// of the replacement expression. The package version/hash fixes these symbols.
const cloneSource = patched.slice(patched.indexOf('N=(e,t,n)=>'), patched.indexOf(',P=e=>'))
const cloneWithStyles = new Function('Node', 'A', `const ${cloneSource};return N`)(
  { TEXT_NODE: 3, ELEMENT_NODE: 1 }, new Set(['script', 'noscript', 'template', 'style']),
)

function cloneStyle(resolved, typed) {
  const names = Object.keys(resolved)
  const attributes = new Map()
  const element = {
    nodeType: 1,
    localName: 'div',
    childNodes: [],
    cloneNode: () => ({ setAttribute: (name, value) => attributes.set(name, value) }),
    ownerDocument: { defaultView: {
      getComputedStyle: () => ({
        length: names.length,
        item: index => names[index],
        getPropertyValue: name => resolved[name] ?? '',
        animationName: 'none', transitionProperty: 'none',
      }),
      HTMLCanvasElement: class {}, HTMLImageElement: class {}, HTMLInputElement: class {},
      HTMLTextAreaElement: class {}, HTMLSelectElement: class {},
    } },
    ...(typed && { computedStyleMap: () => new Map(Object.entries(typed)) }),
  }
  cloneWithStyles(element, { get: () => new Map() }, undefined)
  return attributes.get('style')
}

test('preserves responsive flex sizing inputs on both physical and logical axes', () => {
  const resolved = {
    width: '87.9883px', height: '40px', 'inline-size': '87.9883px', 'block-size': '40px',
    'min-width': '88px', 'max-inline-size': '299.023px', 'flex-basis': '87.9883px',
    color: 'rgb(0, 0, 0)',
  }
  const style = cloneStyle(resolved, {
    width: 'auto', height: 'auto', 'inline-size': 'auto', 'block-size': 'auto',
    'min-width': '88px', 'max-inline-size': '100%', 'flex-basis': 'auto', color: 'black',
  })
  assert.equal(style, 'width:auto;height:auto;inline-size:auto;block-size:auto;min-width:88px;max-inline-size:100%;flex-basis:auto;color:rgb(0, 0, 0)')
})

test('retains upstream values when Typed OM or an individual typed value is unavailable', () => {
  const resolved = { width: '87.9883px', height: '40px' }
  assert.equal(cloneStyle(resolved), 'width:87.9883px;height:40px')
  assert.equal(cloneStyle(resolved, { width: 'auto' }), 'width:auto;height:40px')
})

test('preserves automatic margins and relative padding/insets without truncating multi-layer styles', () => {
  const resolved = {
    'margin-left': '143px', 'margin-inline-start': '143px', 'padding-top': '29.9px',
    'padding-block-start': '29.9px', left: '25px', 'inset-inline-start': '25px',
    'background-image': 'linear-gradient(red, blue), linear-gradient(white, black)',
  }
  assert.equal(cloneStyle(resolved, {
    'margin-left': 'auto', 'margin-inline-start': 'auto', 'padding-top': '10%',
    'padding-block-start': '10%', left: '5%', 'inset-inline-start': '5%',
    'background-image': 'linear-gradient(red, blue)',
  }), 'margin-left:auto;margin-inline-start:auto;padding-top:10%;padding-block-start:10%;left:5%;inset-inline-start:5%;background-image:linear-gradient(red, blue), linear-gradient(white, black)')
})
