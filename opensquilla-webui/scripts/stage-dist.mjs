/**
 * Copy one verified WebUI artifact into the backend package staging area.
 *
 * Vite owns `opensquilla-webui/dist`; the Python package never becomes a Vite
 * build output.  This small, dependency-free seam is deliberately usable by
 * local builds and every CI consumer (Linux, macOS, and Windows).
 */

import {
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
} from 'node:fs'
import { dirname, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

import { verifyDist } from './verify-dist.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const webuiRoot = resolve(scriptDir, '..')
const defaultSource = resolve(webuiRoot, 'dist')
const defaultDestination = resolve(
  webuiRoot,
  '../src/opensquilla/gateway/static/dist',
)

function usage() {
  return [
    'usage: node stage-dist.mjs [--check] [--source <dir>] [--destination <dir>] [--webui-root <dir>]',
    '',
    'Verify the WebUI artifact in <source> and stage it for Python packaging.',
  ].join('\n')
}

function parseArgs(argv) {
  let source = defaultSource
  let destination = defaultDestination
  let sourceRoot = webuiRoot
  let check = false
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--check') {
      check = true
      continue
    }
    if (arg === '--source' || arg === '--destination') {
      const value = argv[index + 1]
      if (!value) throw new Error(`${arg} requires a directory\n\n${usage()}`)
      if (arg === '--source') source = resolve(value)
      else destination = resolve(value)
      index += 1
      continue
    }
    if (arg === '--webui-root') {
      const value = argv[index + 1]
      if (!value) throw new Error(`${arg} requires a directory\n\n${usage()}`)
      sourceRoot = resolve(value)
      index += 1
      continue
    }
    if (arg === '-h' || arg === '--help') {
      console.log(usage())
      process.exit(0)
    }
    throw new Error(`unknown argument: ${arg}\n\n${usage()}`)
  }
  return { source, destination, sourceRoot, check }
}

function assertDirectory(root, label) {
  if (!existsSync(root) || !lstatSync(root).isDirectory()) {
    throw new Error(`${label} directory is missing: ${root}`)
  }
}

function listFiles(root) {
  const files = []
  function walk(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name)
      if (entry.isSymbolicLink() || lstatSync(path).isSymbolicLink()) {
        throw new Error(`WebUI artifact must not contain symlinks: ${path}`)
      }
      if (entry.isDirectory()) walk(path)
      else if (entry.isFile()) files.push(path)
    }
  }
  walk(root)
  return files.sort((left, right) => Buffer.compare(
    Buffer.from(relative(root, left).split(sep).join('/'), 'utf8'),
    Buffer.from(relative(root, right).split(sep).join('/'), 'utf8'),
  ))
}

function bytesEqual(left, right) {
  if (!existsSync(left) || !existsSync(right)) return false
  const leftFiles = listFiles(left)
  const rightFiles = listFiles(right)
  if (leftFiles.length !== rightFiles.length) return false
  for (let index = 0; index < leftFiles.length; index += 1) {
    const leftRelative = relative(left, leftFiles[index]).split(sep).join('/')
    const rightRelative = relative(right, rightFiles[index]).split(sep).join('/')
    if (leftRelative !== rightRelative) return false
    if (!readFileSync(leftFiles[index]).equals(readFileSync(rightFiles[index]))) {
      return false
    }
  }
  return true
}

function stage(source, destination, sourceRoot) {
  // Keep the source and destination distinct.  A same-directory copy would
  // silently make the staging contract meaningless.
  if (resolve(source) === resolve(destination)) {
    throw new Error(`WebUI source and staging destination must differ: ${source}`)
  }
  assertDirectory(source, 'WebUI artifact')
  verifyDist(source, { sourceRoot })
  mkdirSync(dirname(destination), { recursive: true })
  rmSync(destination, { recursive: true, force: true })
  cpSync(source, destination, { recursive: true, force: true, errorOnExist: false })
  if (!bytesEqual(source, destination)) {
    throw new Error(`staged WebUI artifact differs from source: ${destination}`)
  }
}

function main(argv) {
  const { source, destination, sourceRoot, check } = parseArgs(argv)
  assertDirectory(source, 'WebUI artifact')
  verifyDist(source, { sourceRoot })
  if (check) {
    if (!bytesEqual(source, destination)) {
      throw new Error(`staged WebUI artifact is missing or stale: ${destination}`)
    }
    console.log(`WebUI artifact staging is current: ${destination}`)
    return
  }
  stage(source, destination, sourceRoot)
  console.log(`Staged verified WebUI artifact: ${source} -> ${destination}`)
}

try {
  main(process.argv.slice(2))
} catch (error) {
  console.error(`stage-dist: ${error instanceof Error ? error.message : error}`)
  process.exit(1)
}
