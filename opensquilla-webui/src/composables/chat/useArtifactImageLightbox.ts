import {
  inject,
  provide,
  shallowReadonly,
  shallowRef,
  type InjectionKey,
  type ShallowRef,
} from 'vue'
import type { ArtifactPayload } from '@/types/artifacts'
import type { DisplayAttachment } from '@/types/chat'
import { isImageDisplayAttachment } from '@/utils/chat/attachments'

export type ImageLightboxItem =
  | { kind: 'artifact', artifact: ArtifactPayload }
  | { kind: 'attachment', attachment: DisplayAttachment }

export interface ArtifactImageLightboxRequest {
  image: ImageLightboxItem
  navigationImages: readonly ImageLightboxItem[]
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
  openAttachments(request: {
    attachment: DisplayAttachment
    navigationAttachments: readonly DisplayAttachment[]
    sessionKey: string
  }): void
  show(image: ImageLightboxItem): void
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
        image: { kind: 'artifact', artifact: nextRequest.artifact },
        navigationImages: nextRequest.navigationArtifacts.map(artifact => ({ kind: 'artifact', artifact })),
        sessionKey: nextRequest.sessionKey,
        invoker: document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null,
      }
    },
    openAttachments(nextRequest) {
      if (!isImageDisplayAttachment(nextRequest.attachment)) return
      request.value = {
        image: { kind: 'attachment', attachment: nextRequest.attachment },
        navigationImages: nextRequest.navigationAttachments
          .filter(isImageDisplayAttachment)
          .map(attachment => ({ kind: 'attachment', attachment })),
        sessionKey: nextRequest.sessionKey,
        invoker: document.activeElement instanceof HTMLElement ? document.activeElement : null,
      }
    },
    show(image) {
      if (!request.value) return
      request.value = {
        ...request.value,
        image,
      }
    },
    updateNavigation(navigationArtifacts, sessionKey) {
      if (!request.value || request.value.sessionKey !== sessionKey
        || request.value.image.kind !== 'artifact') return
      request.value = {
        ...request.value,
        navigationImages: navigationArtifacts.map(artifact => ({ kind: 'artifact', artifact })),
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
