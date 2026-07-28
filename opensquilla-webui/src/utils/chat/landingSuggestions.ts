interface LandingSuggestionState {
  landingPrefilled: boolean
  composerText: string
  attachmentCount: number
}

export function shouldDisableLandingSuggestions({
  landingPrefilled,
  composerText,
  attachmentCount,
}: LandingSuggestionState): boolean {
  return landingPrefilled || composerText.trim().length > 0 || attachmentCount > 0
}
