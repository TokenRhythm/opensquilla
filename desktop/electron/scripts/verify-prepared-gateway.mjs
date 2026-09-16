import { fileURLToPath } from 'node:url'
import { verifyGatewayIntegrity } from './gateway-integrity.mjs'

const repoRoot = fileURLToPath(new URL('../../../', import.meta.url))
const runtimeRoot = fileURLToPath(new URL('../runtime/gateway/', import.meta.url))
verifyGatewayIntegrity(repoRoot, runtimeRoot, { prepared: true })
console.log('Prepared Gateway inputs and outputs verified.')
