import { stringify } from 'smol-toml'

import { freshDesktopSandboxConfigLines } from './desktop-sandbox-default.js'

/** Seed the local application without selecting a model or storing credentials. */
export function renderUnconfiguredDesktopConfig(
  defaultLocale: string,
  platform: NodeJS.Platform,
): string {
  return [
    stringify({
      // Explicit empty fields prevent built-in/provider-environment defaults
      // from turning a deferred setup into a selected model service.
      llm: { provider: '', model: '', api_key: '', api_key_env: '', base_url: '' },
      squilla_router: { enabled: false },
      llm_ensemble: { enabled: false },
      control_ui: { enabled: true, base_path: '/control', default_locale: defaultLocale },
    }).trimEnd(),
    '',
    ...freshDesktopSandboxConfigLines(null, platform),
  ].join('\n')
}
