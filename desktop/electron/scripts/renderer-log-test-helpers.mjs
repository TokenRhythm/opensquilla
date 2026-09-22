import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from '@typescript/typescript6'

import {
  buildRendererConsoleLogEntry,
  isLiveMainFrameConsoleMessage,
  RendererConsoleLogLimiter,
} from '../dist/desktop-renderer-log.js'

// Exercise the actual compiled listener without booting the application's
// Gateway, update service, or user profile in a lifecycle regression test.
const source = ts.createSourceFile(
  'main.js',
  readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.JS,
)
const listeners = []
function findListener(node) {
  if (ts.isCallExpression(node)
    && node.expression.getText(source) === 'window.webContents.on'
    && ts.isStringLiteral(node.arguments[0])
    && node.arguments[0].text === 'console-message') {
    listeners.push(node.arguments[1].getText(source))
  }
  ts.forEachChild(node, findListener)
}
findListener(source)
assert.equal(listeners.length, 1, 'Expected one production main-window console listener.')

export function createMainWindowConsoleListener(window, records) {
  return new Function(
    'window', 'app', 'buildRendererConsoleLogEntry', 'rendererConsoleLogLimiter', 'desktopLog', 'isLiveMainFrameConsoleMessage',
    `return (${listeners[0]})`,
  )(
    window,
    { getPath: () => '/synthetic-profile' },
    buildRendererConsoleLogEntry,
    new RendererConsoleLogLimiter(),
    (event, detail) => records.push({ event, detail }),
    isLiveMainFrameConsoleMessage,
  )
}
