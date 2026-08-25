import type { ArtifactPayload } from '@/types/rpc'

const ARTIFACT_CARD_SELECTOR = [
  '.msg-media-card',
  '.msg-audio-card',
  '.msg-video-card',
  '.msg-artifact-chip',
].join(',')

const PRIMARY_FOCUS_SELECTOR = [
  'audio',
  'video',
  '.msg-audio-card__action',
  '.msg-video-card__action',
  '.msg-artifact-body',
  '.msg-media-card__img',
  '[tabindex]:not([tabindex="-1"])',
  'button:not(.msg-audio-card__download):not(.msg-video-card__download)',
  'a[href]',
].join(',')

export function artifactFocusKey(artifact: ArtifactPayload): string {
  return String(artifact.id || artifact.download_url || artifact.name || '')
}

export function findArtifactCard(
  root: ParentNode | null | undefined,
  artifact: ArtifactPayload,
): HTMLElement | null {
  const key = artifactFocusKey(artifact)
  if (!root || !key) return null
  const candidates = root.querySelectorAll<HTMLElement>(ARTIFACT_CARD_SELECTOR)
  for (let index = candidates.length - 1; index >= 0; index -= 1) {
    const candidate = candidates[index]
    if (candidate?.dataset.artifactKey === key) return candidate
  }
  return null
}

/**
 * Reveal an artifact where it already lives in the transcript.
 *
 * Media players are preferred over download controls. Before media bytes have
 * loaded, the Play/Retry control is focused instead. Download-only file cards
 * focus their identity row so the top Deliverables entry never triggers a
 * download merely by navigating to an artifact.
 */
export function focusArtifactInTranscript(
  root: ParentNode | null | undefined,
  artifact: ArtifactPayload,
  behavior: ScrollBehavior = 'smooth',
): boolean {
  const card = findArtifactCard(root, artifact)
  if (!card) return false
  const reduceMotion = typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  card.scrollIntoView?.({
    behavior: reduceMotion && behavior === 'smooth' ? 'auto' : behavior,
    block: 'center',
  })
  const focusable = card.matches(PRIMARY_FOCUS_SELECTOR)
    ? card
    : card.querySelector<HTMLElement>(PRIMARY_FOCUS_SELECTOR)
  if (focusable) {
    focusable.focus({ preventScroll: true })
  } else {
    // An unsupported media card may expose download as its only action. Keep
    // navigation non-destructive by focusing the card itself instead.
    card.tabIndex = -1
    card.focus({ preventScroll: true })
  }
  return true
}
