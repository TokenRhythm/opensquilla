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
// SVG image documents do not inherit browser page zoom. Layout at the source
// pixel ratio, with a compensating viewBox, keeps measured auto-sized text from
// wrapping differently while retaining the same PNG dimensions.
const layoutBefore = 'width:${a}px;height:${o}px;overflow:hidden;'
const layoutAfter = 'zoom:${r.devicePixelRatio||1};width:${a}px;height:${o}px;overflow:hidden;'
const viewBoxBefore = '0 0 ${a} ${o}'
const viewBoxAfter = '0 0 ${a*(r.devicePixelRatio||1)} ${o*(r.devicePixelRatio||1)}'
const sha256 = text => createHash('sha256').update(text).digest('hex')

export function patchBootstrap(source, version) {
  if (version !== UPSTREAM_VERSION) throw new Error(`Unsupported generative-frame version: ${version}`)
  const reverted = source.replace(after, before)
    .replace(layoutAfter, layoutBefore).replace(viewBoxAfter, viewBoxBefore)
  if (sha256(reverted) !== ORIGINAL_SHA256 || [before, layoutBefore, viewBoxBefore].some(fragment => reverted.split(fragment).length !== 2)) {
    throw new Error('Unknown generative-frame bootstrap bytes; review the screenshot patch before updating')
  }
  return reverted.replace(before, after)
    .replace(layoutBefore, layoutAfter).replace(viewBoxBefore, viewBoxAfter)
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
