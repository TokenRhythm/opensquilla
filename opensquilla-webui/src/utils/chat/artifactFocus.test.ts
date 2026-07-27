// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ArtifactPayload } from '@/types/rpc'
import {
  artifactFocusKey,
  findArtifactCard,
  focusArtifactInTranscript,
} from './artifactFocus'

function artifact(id: string): ArtifactPayload {
  return {
    id,
    name: `${id}.bin`,
    mime: 'application/octet-stream',
    download_url: `/api/v1/artifacts/${id}`,
  }
}

describe('artifact transcript focus', () => {
  beforeEach(() => {
    document.body.replaceChildren()
  })

  it('finds the newest matching card by stable artifact identity', () => {
    const older = document.createElement('article')
    older.className = 'msg-video-card'
    older.dataset.artifactKey = 'clip'
    const newer = older.cloneNode() as HTMLElement
    document.body.append(older, newer)

    expect(artifactFocusKey(artifact('clip'))).toBe('clip')
    expect(findArtifactCard(document, artifact('clip'))).toBe(newer)
  })

  it('prefers a loaded media element over an earlier download button', () => {
    const card = document.createElement('article')
    card.className = 'msg-video-card'
    card.dataset.artifactKey = 'clip'
    card.innerHTML = `
      <button class="msg-video-card__download">Download</button>
      <video controls tabindex="0"></video>
    `
    const scrollIntoView = vi.fn()
    card.scrollIntoView = scrollIntoView
    document.body.append(card)

    expect(focusArtifactInTranscript(document, artifact('clip'), 'auto')).toBe(true)
    expect(document.activeElement).toBe(card.querySelector('video'))
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'center' })
  })

  it('focuses Play before Download while inline media is not loaded', () => {
    const card = document.createElement('article')
    card.className = 'msg-audio-card'
    card.dataset.artifactKey = 'sample'
    card.innerHTML = `
      <button class="msg-audio-card__download">Download</button>
      <button class="msg-audio-card__action">Play</button>
    `
    card.scrollIntoView = vi.fn()
    document.body.append(card)

    expect(focusArtifactInTranscript(document, artifact('sample'))).toBe(true)
    expect(document.activeElement).toBe(card.querySelector('.msg-audio-card__action'))
  })

  it('focuses a download-only card identity without activating its download', () => {
    const card = document.createElement('article')
    card.className = 'msg-artifact-chip'
    card.dataset.artifactKey = 'data'
    card.innerHTML = `
      <button class="msg-artifact-body">data.json</button>
      <button class="msg-artifact-action">Download</button>
    `
    card.scrollIntoView = vi.fn()
    document.body.append(card)

    expect(focusArtifactInTranscript(document, artifact('data'))).toBe(true)
    expect(document.activeElement).toBe(card.querySelector('.msg-artifact-body'))
  })

  it('focuses the media card when an unsupported format only exposes Download', () => {
    const card = document.createElement('article')
    card.className = 'msg-video-card'
    card.dataset.artifactKey = 'legacy-video'
    card.innerHTML = `
      <button class="msg-video-card__download">Download</button>
    `
    card.scrollIntoView = vi.fn()
    document.body.append(card)

    expect(focusArtifactInTranscript(document, artifact('legacy-video'))).toBe(true)
    expect(document.activeElement).toBe(card)
    expect(card.tabIndex).toBe(-1)
  })
})
