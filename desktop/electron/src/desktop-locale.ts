// Pure OS-language → bundled-locale resolution, split out of main.ts so it can
// be unit-tested without pulling in Electron (same pattern as
// update-feed-resolver.ts). main.ts feeds it
// [...app.getPreferredSystemLanguages(), app.getLocale()] in preference order.

export type DesktopLocale = 'en' | 'zh-Hans' | 'ja' | 'fr' | 'de' | 'es'

export const DESKTOP_LOCALES: DesktopLocale[] = ['en', 'zh-Hans', 'ja', 'fr', 'de', 'es']

/**
 * Normalize the Gateway's persisted control-ui locale. This deliberately
 * mirrors GatewayConfig's compatibility rule rather than the OS-locale
 * resolver below: Gateway accepts every zh* spelling as Simplified Chinese
 * and reduces unsupported values to English.
 */
export function normalizeGatewayLocale(value: string): DesktopLocale {
  const normalized = value.trim().toLowerCase()
  if (normalized.startsWith('zh')) return 'zh-Hans'
  for (const code of ['ja', 'fr', 'de', 'es'] as const) {
    if (normalized.startsWith(code)) return code
  }
  return 'en'
}

/**
 * Map the user's ordered BCP-47 language tags to the first bundled locale.
 * First match wins, so a top-preference tag can never lose to a
 * lower-preference one (e.g. en-HK above fr-HK must yield 'en', not 'fr').
 */
export function resolveLocaleFromTags(tags: readonly unknown[]): DesktopLocale {
  for (const raw of tags) {
    if (typeof raw !== 'string') continue
    let locale: Intl.Locale
    try {
      // Electron returns canonical BCP-47 tags. Accept underscores too for
      // compatibility with legacy locale strings, but let Intl.Locale reject
      // malformed tags instead of matching them by substring.
      locale = new Intl.Locale(raw.trim().replaceAll('_', '-'))
    } catch {
      continue
    }
    const language = locale.language.toLowerCase()
    // English is a bundled locale and must match here: without this branch a
    // top-preference en-* tag falls through and a LOWER-preference language
    // (e.g. fr-HK behind en-HK on a Hong Kong system) wins the loop.
    if (language === 'en') return 'en'
    if (language === 'zh') {
      // Only Simplified Chinese is bundled. An explicit script subtag wins
      // over region: zh-Hans-HK/TW/MO is Simplified wherever the reader
      // lives. Only then route Traditional variants — explicit zh-Hant, or
      // bare region tags that default to Traditional (zh-TW / zh-HK /
      // zh-MO) — to the English fallback rather than forcing Simplified
      // text a Traditional reader may not want.
      const script = locale.script?.toLowerCase()
      const region = locale.region?.toLowerCase()
      if (script === 'hans') return 'zh-Hans'
      if (script === 'hant' || region === 'tw' || region === 'hk' || region === 'mo') continue
      return 'zh-Hans'
    }
    for (const code of ['ja', 'fr', 'de', 'es'] as const) {
      if (language === code) return code
    }
  }
  return 'en'
}
