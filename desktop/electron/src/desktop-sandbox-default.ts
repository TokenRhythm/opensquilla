/**
 * Seed a sandbox mode only while creating a brand-new Desktop profile.
 * Existing profiles are authoritative so an application update never changes
 * the user's selected mode merely because the Desktop rewrites owned config.
 */
export function freshDesktopSandboxConfigLines(
  existingConfig: string | null,
  platform: NodeJS.Platform,
): string[] {
  if (existingConfig !== null) return []
  const runMode = platform === 'darwin' ? 'safe' : 'full'
  return ['[sandbox]', `run_mode = "${runMode}"`, '']
}
