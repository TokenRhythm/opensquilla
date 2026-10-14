import { createHash } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

// generative-frame 0.0.5, MIT, Copyright (c) 2026 AgentbaseAI Inc.
// Its public screenshot RPC bundles src/runtime/screenshot.ts in bootstrap.js.
// The styled clone must not reapply author stylesheets in the new SVG document:
// e.g. an unscoped `svg` rule can also match the screenshot's outer SVG.
export const UPSTREAM_VERSION = '0.0.5'
export const ORIGINAL_SHA256 = '099be85a7d5581ada49860453ba2944465edc0690cc98cca5894cdfacc99fad8'
const before = 'new Set([`script`,`noscript`,`template`])'
const after = 'new Set([`script`,`noscript`,`template`,`style`])'
// Exact earlier local candidates are accepted only after reversing them and
// verifying the complete original hash. These changes are not emitted now.
const styleBefore = '(r!==s.get(t)||n&&r!==n.getPropertyValue(t))&&c.push'
const styleAfter = '(t===`box-sizing`||t===`line-height`||t.startsWith(`font-`)||r!==s.get(t)||n&&r!==n.getPropertyValue(t))&&c.push'
const layoutBefore = 'width:${a}px;height:${o}px;overflow:hidden;'
const layoutAfter = 'zoom:${r.devicePixelRatio||1};width:${a}px;height:${o}px;overflow:hidden;'
const viewBoxBefore = '0 0 ${a} ${o}'
const viewBoxAfter = '0 0 ${a*(r.devicePixelRatio||1)} ${o*(r.devicePixelRatio||1)}'
// CSSOM resolves dimensions to already-laid-out pixels. Reusing those pixels as
// flex inputs or frozen auto margins can wrap a formerly single-line row.
// Typed OM preserves auto/percentages, including logical axes. Older engines
// retain the upstream path; equivalent PNG fidelity there is not established.
const mapBefore = 'let s=t.get(r),c=[];'
const mapAfter = 'let s=t.get(r),c=[],Q=r.computedStyleMap?.();'
const valueBefore = 'let r=a.getPropertyValue(t);'
const dimensionsOnlyAfter = 'let r=/^(?:(?:min|max)-)?(?:width|height|inline-size|block-size)$/.test(t)||t===`flex-basis`?Q?.get(t)?.toString()??a.getPropertyValue(t):a.getPropertyValue(t);'
// Only single-value layout longhands: get() on a multi-value Typed OM property
// can return just the first layer, so a blanket substitution would lose data.
const layoutProperties = '^(?:(?:min|max)-)?(?:width|height|inline-size|block-size)$|^(?:margin|padding)-(?:top|right|bottom|left|(?:inline|block)-(?:start|end))$|^(?:top|right|bottom|left|inset-(?:inline|block)-(?:start|end)|flex-basis)$'
const valueAfter = `let r=/${layoutProperties}/.test(t)?Q?.get(t)?.toString()??a.getPropertyValue(t):a.getPropertyValue(t);`
const sha256 = text => createHash('sha256').update(text).digest('hex')

export function patchBootstrap(source, version) {
  if (version !== UPSTREAM_VERSION) throw new Error(`Unsupported generative-frame version: ${version}`)
  const reverted = source.replace(after, before)
    .replace(styleAfter, styleBefore)
    .replace(layoutAfter, layoutBefore).replace(viewBoxAfter, viewBoxBefore)
    .replace(mapAfter, mapBefore).replace(valueAfter, valueBefore).replace(dimensionsOnlyAfter, valueBefore)
  if (sha256(reverted) !== ORIGINAL_SHA256 || [before, mapBefore, valueBefore].some(fragment => reverted.split(fragment).length !== 2)) {
    throw new Error('Unknown generative-frame bootstrap bytes; review the screenshot patch before updating')
  }
  return reverted.replace(before, after)
    .replace(mapBefore, mapAfter).replace(valueBefore, valueAfter)
}

export async function prepareGenerativeFrame(packageDir = new URL('../node_modules/generative-frame/', import.meta.url)) {
  const manifest = JSON.parse(await readFile(new URL('package.json', packageDir), 'utf8'))
  const path = new URL('dist/bootstrap.js', packageDir)
  const source = await readFile(path, 'utf8')
  const patched = patchBootstrap(source, manifest.version)
  if (source !== patched) await writeFile(path, patched, 'utf8')
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await prepareGenerativeFrame()
}
