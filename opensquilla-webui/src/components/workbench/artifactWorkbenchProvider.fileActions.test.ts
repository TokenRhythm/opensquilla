// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Platform } from '@/platform/types'
import type { ArtifactPayload } from '@/types/artifacts'
import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'
import { createV4ArtifactContentAccess } from '@/adapters/gateway/artifactAccessV4'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import { httpTransportTestDouble } from '@/testing/httpTransport.test-helper'
import { createLegacyArtifactWorkspace } from '@/workbench/artifactDocumentProvider'
import { createArtifactPreviewWorkbenchItem } from '@/workbench/artifactItems'
import { downloadBlob } from '@/utils/browser'
import { createArtifactWorkbenchDefinitions } from './artifactWorkbenchProvider'

vi.mock('@/utils/browser', async importOriginal => ({
  ...await importOriginal<typeof import('@/utils/browser')>(), downloadBlob: vi.fn(),
}))

function revision(version: number): ArtifactPayload {
  return {
    id: `artifact-${version}`, documentId: 'document-1', revisionId: `revision-${version}`,
    name: `deck-v${version}.pptx`, mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    size: version * 10, sha256: String(version).repeat(64),
    download_url: '/api/v1/artifact-documents/document-1',
    revision_download_url: `/api/v1/artifacts/artifact-${version}`,
  }
}

async function harness(options: { http?: ReturnType<typeof httpTransportTestDouble>; initialHead?: ArtifactPayload } = {}) {
  let current = revision(2)
  if (options.initialHead) current = options.initialHead
  const initial = revision(1)
  const http = options.http || httpTransportTestDouble()
  const blob = new Blob(['revision two'], { type: String(current.mime) })
  const fetchArtifact = vi.fn<ArtifactContentAccess['fetchArtifact']>().mockResolvedValue({ ok: true, status: 200, url: '', blob })
  const openArtifact = vi.fn<NonNullable<Platform['files']['openArtifact']>>().mockResolvedValue({ ok: true })
  const headArtifact = vi.fn(() => current)
  const item = createArtifactPreviewWorkbenchItem({ artifact: initial, nativeHtml: false, sessionKey: 'session-a' })
  const definition = createArtifactWorkbenchDefinitions({
    artifactContent: { ...createV4ArtifactContentAccess(http), ...(options.http ? {} : { fetchArtifact }),
      fetchAttachment: vi.fn(), uploadAttachment: vi.fn() },
    artifactPreviews: createV4ArtifactPreviews(http),
    artifactDocuments: {
      load: vi.fn(async () => undefined), headArtifact,
      snapshot: () => ({ key: 'document-1', loaded: true, loading: false, stale: false, error: null,
        workspace: createLegacyArtifactWorkspace(current, 'session-a') }),
    },
    currentSessionId: () => 'session-a', baseOrigin: 'http://localhost',
    confirmRemoteResources: vi.fn(async () => true), pushToast: vi.fn(), t: key => key,
    platform: { id: 'desktop', capabilities: { canOpenArtifactsNatively: true }, files: { openArtifact } } as unknown as Platform,
  }).find(value => value.kind === 'artifact-preview')!
  const runtime = await definition.createRuntime!(item, {
    getRenderState: () => ({}), updateRenderState: vi.fn(), isItemOpen: () => true,
    setExpanded: vi.fn(), reportError: vi.fn(),
  })
  headArtifact.mockClear()
  return { initial, item, runtime, blob, fetchArtifact, openArtifact, headArtifact,
    advance: () => { Object.assign(current, revision(3)); current = revision(3) } }
}

afterEach(() => vi.clearAllMocks())

describe('Workbench revision file actions', () => {
  it.each(['download', 'open-external'])('uses the whole current preview revision for %s after a v1 to v2 edit', async action => {
    const f = await harness()
    await f.runtime.performAction?.(action, f.item)
    expect(f.fetchArtifact).toHaveBeenCalledWith({ ...revision(2), download_url: '/api/v1/artifacts/artifact-2' }, { sessionKey: 'session-a', requireSameOrigin: true })
    if (action === 'download') expect(downloadBlob).toHaveBeenCalledWith(f.blob, 'deck-v2.pptx')
    else {
      expect(f.openArtifact).toHaveBeenCalledWith(expect.objectContaining({ name: 'deck-v2.pptx', mime: revision(2).mime }))
      const payload = f.openArtifact.mock.calls[0]?.[0] as unknown as { data: ArrayBuffer }
      expect(new TextDecoder().decode(payload.data)).toBe('revision two')
    }
  })

  it('keeps download bytes and filename pinned when the head advances during the request', async () => {
    const f = await harness()
    f.fetchArtifact.mockImplementationOnce(async () => {
      f.advance()
      return { ok: true as const, status: 200, url: '', blob: f.blob }
    })
    await f.runtime.performAction?.('download', f.item)
    expect(f.fetchArtifact.mock.calls[0]?.[0]).toMatchObject({ id: 'artifact-2', revisionId: 'revision-2',
      download_url: '/api/v1/artifacts/artifact-2', name: 'deck-v2.pptx', sha256: '2'.repeat(64) })
    expect(downloadBlob).toHaveBeenCalledWith(f.blob, 'deck-v2.pptx')
  })

  it('preserves explicit history revision and download-latest component intents', async () => {
    const f = await harness()
    const history = { ...f.initial, download_url: f.initial.revision_download_url }
    await f.runtime.handleComponentEvent?.({ type: 'artifact-download', payload: history }, f.item)
    expect(f.fetchArtifact).toHaveBeenLastCalledWith(history, { sessionKey: 'session-a', requireSameOrigin: true })
    expect(downloadBlob).toHaveBeenLastCalledWith(f.blob, 'deck-v1.pptx')
    const latest = revision(2)
    await f.runtime.handleComponentEvent?.({ type: 'artifact-download', payload: latest }, f.item)
    expect(f.fetchArtifact).toHaveBeenLastCalledWith(latest, { sessionKey: 'session-a' })
    expect(f.headArtifact).not.toHaveBeenCalled()
  })

  it.each(['download', 'open-external'])('rejects an untrusted pinned URL for %s without falling back to the mutable head', async action => {
    const http = httpTransportTestDouble()
    vi.spyOn(http, 'requestBinary')
    vi.spyOn(http, 'fetchExternalArtifact')
    const f = await harness({ http, initialHead: { ...revision(2), revision_download_url: 'https://untrusted.invalid/other.pptx' } })
    await f.runtime.performAction?.(action, f.item)
    expect(http.requestBinary).not.toHaveBeenCalled()
    expect(http.fetchExternalArtifact).not.toHaveBeenCalled()
    expect(f.openArtifact).not.toHaveBeenCalled()
    expect(downloadBlob).not.toHaveBeenCalled()
  })
})
