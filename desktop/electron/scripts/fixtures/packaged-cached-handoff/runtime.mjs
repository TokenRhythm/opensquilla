// Loaded only by the explicit native cached mode. Existing download/manual
// contracts keep their original independent bridge and download boundaries.
export { _electron as electron } from 'playwright'
export { environmentWithoutProviderSecrets } from '../../packaged-smoke-helpers.mjs'
export { captureElectronProcessIdentity, closeElectronAndObserveExit } from '../../packaged-first-send-cleanup.mjs'
export { desktopShutdownEvidenceSince, gatewayProcessSnapshot } from '../../e2e-shutdown-helpers.mjs'
export { desktopProfileFingerprint, loadDesktopGatewayOwnershipRecord, verifyDesktopGatewayOwnership } from '../../../dist/desktop-gateway-ownership.js'
export { DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS } from '../../../dist/gateway-lifecycle.js'
export { assertCachedRestartEvidence, fileSha256, requestCachedQuitOnce, stageVerifiedCachedHandoff, waitForRestoredCache } from './contract.mjs'
