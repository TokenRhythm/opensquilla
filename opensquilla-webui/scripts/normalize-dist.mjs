import { readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const distDir = resolve(import.meta.dirname, '../../src/opensquilla/gateway/static/dist')
const textFilePattern = /\.(css|html|js|map)$/

function normalizeNewlines(value) {
  return value.replace(/\r\n/g, '\n').replace(/\r/g, '')
}

function normalizeSourceMap(content) {
  try {
    const parsed = JSON.parse(normalizeNewlines(content))
    return JSON.stringify(replaceWindowsNewlines(parsed))
  } catch {
    return normalizeNewlines(content)
  }
}

function replaceWindowsNewlines(value) {
  if (Array.isArray(value)) {
    return value.map(replaceWindowsNewlines)
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, entry]) => [key, replaceWindowsNewlines(entry)]),
    )
  }
  if (typeof value === 'string') {
    return normalizeNewlines(value)
  }
  return value
}

function normalizeFile(path) {
  const before = readFileSync(path, 'utf8')
  let after = path.endsWith('.map') ? normalizeSourceMap(before) : normalizeNewlines(before)

  if (path.endsWith('.css')) {
    after = after.replace(/\n+$/g, '')
  }

  if (after !== before) {
    writeFileSync(path, after, 'utf8')
  }
}

function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const path = resolve(dir, entry)
    if (entry === '.DS_Store') {
      rmSync(path, { force: true, recursive: true })
      continue
    }
    const stat = statSync(path)
    if (stat.isDirectory()) {
      walk(path)
      continue
    }
    if (textFilePattern.test(entry)) {
      normalizeFile(path)
    }
  }
}

walk(distDir)
