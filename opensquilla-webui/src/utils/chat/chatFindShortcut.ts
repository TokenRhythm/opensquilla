import { isMacPlatform } from '@/utils/browser'
import { bindingMatches } from '@/utils/keychord'

/** Preserve platform text-editing chords, including macOS Control+F. */
export function isChatFindShortcut(event: KeyboardEvent, mac = isMacPlatform()): boolean {
  return bindingMatches(event, { primary: true, key: 'f' }, mac)
}
