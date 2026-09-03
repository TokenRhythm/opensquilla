import assert from 'node:assert/strict'
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  applyDesktopTelemetryConsentPayload,
  desktopPrivacyTomlLines,
  parseDesktopTelemetryConsent,
  parseLegacyNetworkObservabilityDisabled,
  replaceDesktopTelemetryConsentInPrivacy,
  requireExplicitOnboardingConsent,
} from '../dist/telemetry/onboarding-consent.js'
import {
  clearGrowthAnalyticsIdentity,
  CONSENT_MIRROR_SCHEMA_VERSION,
  readConsentMirror,
  writeConsentMirror,
} from '../dist/telemetry/consent-mirror.js'

const NOW = '2026-09-02T01:02:03.004Z'
const crlfConfig = [
  'state_dir = "C:\\\\OpenSquilla\\\\state"',
  '',
  '[privacy]',
  'disable_network_observability = false # legacy switch remains authoritative',
  'reliability_diagnostics_enabled = true',
  'reliability_notice_version = "reliability-v1"',
  `reliability_consented_at_utc = "${NOW}"`,
  'product_analytics_enabled = false',
  '',
  '[control_ui]',
  'enabled = true',
  '',
].join('\r\n')

const parsed = parseDesktopTelemetryConsent(crlfConfig)
assert.deepEqual(parsed, {
  reliability: {
    enabled: true,
    noticeVersion: 'reliability-v1',
    consentedAtUtc: NOW,
  },
  growth: { enabled: false, noticeVersion: null, consentedAtUtc: null },
})
assert.equal(parseLegacyNetworkObservabilityDisabled(crlfConfig), false)
assert.deepEqual(desktopPrivacyTomlLines(false, parsed, true), [
  '',
  '[privacy]',
  'disable_network_observability = false',
  'reliability_diagnostics_enabled = true',
  'reliability_notice_version = "reliability-v1"',
  `reliability_consented_at_utc = "${NOW}"`,
  'product_analytics_enabled = false',
])

assert.throws(
  () => requireExplicitOnboardingConsent({ reliabilityDiagnosticsEnabled: true }),
  /both telemetry categories/,
)
requireExplicitOnboardingConsent({
  reliabilityDiagnosticsEnabled: false,
  productAnalyticsEnabled: false,
})
const changed = applyDesktopTelemetryConsentPayload(parsed, {
  reliabilityDiagnosticsEnabled: false,
  productAnalyticsEnabled: true,
}, NOW)
assert.deepEqual(changed, {
  reliability: { enabled: false, noticeVersion: null, consentedAtUtc: null },
  growth: {
    enabled: true,
    noticeVersion: 'growth-v1',
    consentedAtUtc: NOW,
  },
})

const patched = replaceDesktopTelemetryConsentInPrivacy(crlfConfig, changed)
assert.ok(patched.includes('\r\n'), 'an imported CRLF config must keep its newline convention')
assert.match(patched, /disable_network_observability = false # legacy switch remains authoritative/)
assert.match(patched, /reliability_diagnostics_enabled = false/)
assert.doesNotMatch(patched, /reliability_notice_version/)
assert.doesNotMatch(patched, /reliability_consented_at_utc/)
assert.match(patched, /product_analytics_enabled = true/)
assert.match(patched, /product_analytics_notice_version = "growth-v1"/)
assert.match(patched, new RegExp(`product_analytics_consented_at_utc = "${NOW.replaceAll('.', '\\.')}`))
assert.equal((patched.match(/\[privacy\]/g) || []).length, 1)
assert.match(patched, /\[control_ui\]\r\nenabled = true/)

const root = mkdtempSync(join(tmpdir(), 'opensquilla-consent-mirror-'))
try {
  const mirrorPath = join(root, 'nested', 'desktop-consent-mirror.json')
  const mirror = {
    schema_version: CONSENT_MIRROR_SCHEMA_VERSION,
    reliability: {
      enabled: false,
      notice_version: null,
      consented_at_utc: null,
      forced_off: false,
    },
    growth: {
      enabled: true,
      notice_version: 'growth-v1',
      consented_at_utc: NOW,
      forced_off: false,
    },
  }
  await writeConsentMirror(mirrorPath, mirror)
  assert.deepEqual(readConsentMirror(mirrorPath), mirror)
  assert.equal(readdirSync(join(root, 'nested')).some((name) => name.endsWith('.tmp')), false)
  assert.equal(readFileSync(mirrorPath, 'utf8').endsWith('\n'), true)
  if (process.platform !== 'win32') {
    assert.equal(statSync(mirrorPath).mode & 0o777, 0o600)
  }

  const replacement = {
    ...mirror,
    growth: {
      enabled: false,
      notice_version: null,
      consented_at_utc: null,
      forced_off: true,
    },
  }
  await writeConsentMirror(mirrorPath, replacement)
  assert.deepEqual(readConsentMirror(mirrorPath), replacement)
  assert.equal(existsSync(mirrorPath), true)

  const identityPath = join(root, 'nested', 'growth_identity.json')
  writeFileSync(identityPath, '{"synthetic":"identity"}\n', { mode: 0o600 })
  assert.equal(await clearGrowthAnalyticsIdentity(identityPath), true)
  assert.equal(existsSync(identityPath), false)
  assert.equal(await clearGrowthAnalyticsIdentity(identityPath), false)

  if (process.platform !== 'win32') {
    const identityTarget = join(root, 'identity-target.json')
    const identityLink = join(root, 'nested', 'growth-identity-link.json')
    writeFileSync(identityTarget, '{}')
    symlinkSync(identityTarget, identityLink)
    await assert.rejects(
      clearGrowthAnalyticsIdentity(identityLink),
      /not a regular file/,
    )
    assert.equal(existsSync(identityTarget), true)
  }
} finally {
  rmSync(root, { recursive: true, force: true })
}

console.log('telemetry consent round-trip tests passed')
