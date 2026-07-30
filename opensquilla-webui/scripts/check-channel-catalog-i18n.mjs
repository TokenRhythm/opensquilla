// Channel catalog i18n coverage guard. The backend channel catalog ships
// English label/description/whatYouNeed; the Web UI overlays localized copy
// keyed by channel type (setup.channelCatalog.<type>). This guard fails CI
// when a catalog type has no localized description/needs, so adding a channel
// type without translating its gallery copy cannot silently ship English.
//
// The type list mirrors the backend _BUILDERS registry (channel_specs.py).
// When a new channel type is added there, add it here and translate its copy;
// key parity across the other locales is enforced separately by check-i18n.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const localesDir = resolve(here, '..', 'src', 'locales')

// Catalog types exposed by the backend onboarding catalog (msteams is built
// but intentionally excluded from the catalog, so it is not listed here).
const CATALOG_TYPES = ['dingtalk', 'discord', 'feishu', 'matrix', 'qq', 'slack', 'telegram', 'wecom']
const LOCALES = ['en', 'zh-Hans', 'ja', 'fr', 'de', 'es']

const failures = []

for (const locale of LOCALES) {
  let data
  try {
    data = JSON.parse(readFileSync(resolve(localesDir, `${locale}.json`), 'utf8'))
  } catch (err) {
    failures.push(`${locale}.json could not be read: ${err.message}`)
    continue
  }
  const catalog = data?.setup?.channelCatalog
  if (!catalog || typeof catalog !== 'object') {
    failures.push(`${locale}: missing setup.channelCatalog`)
    continue
  }
  for (const type of CATALOG_TYPES) {
    const entry = catalog[type]
    if (!entry || typeof entry !== 'object') {
      failures.push(`${locale}: setup.channelCatalog.${type} is missing`)
      continue
    }
    if (typeof entry.description !== 'string' || !entry.description.trim()) {
      failures.push(`${locale}: setup.channelCatalog.${type}.description is empty`)
    }
    if (!Array.isArray(entry.needs) || entry.needs.length === 0) {
      failures.push(`${locale}: setup.channelCatalog.${type}.needs is empty`)
    }
  }
}

if (failures.length > 0) {
  console.error('[check-channel-catalog-i18n] FAILED')
  for (const f of failures) console.error(`  - ${f}`)
  process.exit(1)
}
console.log(`[check-channel-catalog-i18n] OK — ${CATALOG_TYPES.length} channel types covered in ${LOCALES.length} locales`)
