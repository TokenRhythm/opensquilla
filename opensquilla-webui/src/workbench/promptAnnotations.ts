export const ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT =
  'opensquilla:artifact-prompt-annotation-focus'
export const ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT =
  'opensquilla:artifact-prompt-annotation-reuse'
export const ARTIFACT_PROMPT_ANNOTATIONS_ACCEPTED_EVENT =
  'opensquilla:artifact-prompt-annotations-accepted'

interface ArtifactPromptAnnotationActivationCallbacks {
  /** Synchronously proves that the trusted Control UI accepted the request. */
  acknowledge?: () => void
  /** Resolves only after the matching native surface is active and ready. */
  complete?: (ready: boolean) => void
}

export interface ArtifactPromptAnnotationFocusDetail
  extends ArtifactPromptAnnotationActivationCallbacks {
  annotationId: string
  documentId: string
  sessionKey: string
}

export interface ArtifactPromptAnnotationReuseDetail
  extends ArtifactPromptAnnotationActivationCallbacks {
  body: string
  documentId: string
  sessionKey: string
}

export interface ArtifactPromptAnnotationsAcceptedDetail {
  acceptedIds: string[]
  sessionKey: string
  /** The key used by the send request before a draft/session was materialized. */
  requestSessionKey?: string
}

function requestArtifactPromptAnnotationActivation(
  eventName: string,
  detail: Omit<ArtifactPromptAnnotationFocusDetail, keyof ArtifactPromptAnnotationActivationCallbacks>
    | Omit<ArtifactPromptAnnotationReuseDetail, keyof ArtifactPromptAnnotationActivationCallbacks>,
): Promise<boolean> {
  if (typeof window === 'undefined') return Promise.resolve(false)
  return new Promise((resolve) => {
    let handled = false
    let settled = false
    const finish = (ready: boolean) => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      resolve(ready)
    }
    const timer = window.setTimeout(() => finish(false), 3_000)
    window.dispatchEvent(new CustomEvent(eventName, {
      detail: {
        ...detail,
        acknowledge: () => { handled = true },
        complete: finish,
      },
    }))
    // Dispatch is synchronous. A missing Workbench listener must not leave the
    // composer waiting for the activation timeout.
    if (!handled) finish(false)
  })
}

export function focusArtifactPromptAnnotation(
  detail: Omit<ArtifactPromptAnnotationFocusDetail, keyof ArtifactPromptAnnotationActivationCallbacks>,
): Promise<boolean> {
  return requestArtifactPromptAnnotationActivation(
    ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT,
    detail,
  )
}

export function reuseArtifactPromptAnnotation(
  detail: Omit<ArtifactPromptAnnotationReuseDetail, keyof ArtifactPromptAnnotationActivationCallbacks>,
): Promise<boolean> {
  return requestArtifactPromptAnnotationActivation(
    ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT,
    detail,
  )
}

export function notifyArtifactPromptAnnotationsAccepted(
  detail: ArtifactPromptAnnotationsAcceptedDetail,
): void {
  if (typeof window === 'undefined' || !detail.sessionKey || detail.acceptedIds.length === 0) return
  const requestSessionKey = detail.requestSessionKey?.trim()
  window.dispatchEvent(new CustomEvent(ARTIFACT_PROMPT_ANNOTATIONS_ACCEPTED_EVENT, {
    detail: {
      acceptedIds: [...detail.acceptedIds],
      sessionKey: detail.sessionKey,
      ...(requestSessionKey && requestSessionKey !== detail.sessionKey
        ? { requestSessionKey }
        : {}),
    },
  }))
}
