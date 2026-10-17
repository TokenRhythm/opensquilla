import { parse } from 'smol-toml'

// Use the same source precedence as GatewayConfig / ControlUiConfig: explicit
// profile fields, nested Gateway environment fields, Control UI environment,
// then the Gateway default. Capture this once for each owned Gateway launch.
export function desktopControlUiBasePath(
  rawConfig: string | null,
  environment: NodeJS.ProcessEnv = {},
): string {
  const config = rawConfig === null ? {} : parse(rawConfig)
  const configured = config.control_ui as { base_path?: unknown } | undefined
  const readEnvironment = (name: string): string | undefined => {
    // Real process.env already resolves casing on Windows. Plain maps and
    // POSIX environments need Pydantic's case-insensitive name lookup too.
    if (environment[name] !== undefined) return environment[name]
    let value: string | undefined
    for (const [key, candidate] of Object.entries(environment)) {
      if (key.toUpperCase() === name) value = candidate
    }
    return value
  }
  const gatewayEnvironmentRaw = readEnvironment('OPENSQUILLA_GATEWAY_CONTROL_UI')
  const gatewayEnvironment = gatewayEnvironmentRaw
    ? JSON.parse(gatewayEnvironmentRaw) as { base_path?: unknown }
    : undefined
  const basePath = configured?.base_path
    ?? readEnvironment('OPENSQUILLA_GATEWAY_CONTROL_UI__BASE_PATH')
    ?? gatewayEnvironment?.base_path
    ?? readEnvironment('OPENSQUILLA_CONTROL_UI_BASE_PATH')
    ?? '/control'
  if (typeof basePath !== 'string') throw new Error('Control UI base_path must be a string.')
  return basePath.replace(/\/+$/, '')
}
