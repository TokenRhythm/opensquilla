import {
  inject,
  provide,
  shallowReadonly,
  shallowRef,
  type InjectionKey,
  type ShallowRef,
} from 'vue'
import type { ArtifactPayload } from '@/types/rpc'

export interface ArtifactImageLightboxRequest {
  artifact: ArtifactPayload
  navigationArtifacts: readonly ArtifactPayload[]
  sessionKey: string
  invoker: HTMLElement | null
}

export interface ArtifactImageLightboxOpenRequest {
  artifact: ArtifactPayload
  navigationArtifacts: readonly ArtifactPayload[]
  sessionKey: string
}

export interface ArtifactImageLightboxController {
  request: Readonly<ShallowRef<ArtifactImageLightboxRequest | null>>
  open(request: ArtifactImageLightboxOpenRequest): void
  show(artifact: ArtifactPayload): void
  updateNavigation(navigationArtifacts: readonly ArtifactPayload[], sessionKey: string): void
  close(): void
}

const artifactImageLightboxKey: InjectionKey<ArtifactImageLightboxController> =
  Symbol('artifact-image-lightbox')

export function provideArtifactImageLightbox(): ArtifactImageLightboxController {
  const request = shallowRef<ArtifactImageLightboxRequest | null>(null)

  const controller: ArtifactImageLightboxController = {
    request: shallowReadonly(request),
    open(nextRequest) {
      request.value = {
        ...nextRequest,
        navigationArtifacts: [...nextRequest.navigationArtifacts],
        invoker: document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null,
      }
    },
    show(artifact) {
      if (!request.value) return
      request.value = {
        ...request.value,
        artifact,
      }
    },
    updateNavigation(navigationArtifacts, sessionKey) {
      if (!request.value || request.value.sessionKey !== sessionKey) return
      request.value = {
        ...request.value,
        navigationArtifacts: [...navigationArtifacts],
      }
    },
    close() {
      request.value = null
    },
  }

  provide(artifactImageLightboxKey, controller)
  return controller
}

export function useArtifactImageLightbox(): ArtifactImageLightboxController {
  const controller = inject(artifactImageLightboxKey, null)
  if (!controller) {
    throw new Error('Artifact image lightbox controller is not provided')
  }
  return controller
}
