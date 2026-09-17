import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
// vue-tsc still consumes the TypeScript 6 compiler API. Keep that dependency
// explicit while the native TypeScript 7 CLI checks the build configuration.
const compilerPath = require.resolve('@typescript/typescript6/lib/tsc')
const compiler = require('@typescript/typescript6')
console.log(`Vue SFC typecheck: TypeScript ${compiler.version} (${compilerPath})`)
require('vue-tsc').run(compilerPath)
