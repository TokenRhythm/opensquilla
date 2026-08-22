import {
  DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
  DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION_V3,
  DESKTOP_ARTIFACT_BRIDGE_UNSUPPORTED_CAPABILITIES,
  parseDesktopArtifactBrowserActRequest,
  parseDesktopArtifactBrowserInspectRequest,
  parseDesktopArtifactBindCandidatePreviewRequest,
  parseDesktopArtifactCaptureSelectionRequest,
  parseDesktopArtifactFocusAnnotationRequest,
  parseDesktopArtifactOfficeFlushRequest,
  parseDesktopArtifactReloadSurfaceRequest,
  parseDesktopArtifactRestoreCanonicalPreviewRequest,
  parseDesktopArtifactResolveAnnotationSelectionRequest,
  parseDesktopArtifactScreenshotRequest,
  type DesktopArtifactBridgeCapabilities,
  type DesktopArtifactBridgeProtocolVersion,
  type DesktopArtifactBridgeMethod,
  type DesktopArtifactBridgeRequestByMethod,
  type DesktopArtifactBridgeValueByMethod,
} from './desktop-artifact-bridge-contract.js'

export type DesktopArtifactBridgeErrorCode =
  | 'invalid-request'
  | 'unavailable'
  | 'unsupported'
  | 'timed-out'
  | 'operation-failed'

export interface DesktopArtifactBridgeSuccess<M extends DesktopArtifactBridgeMethod> {
  ok: true
  method: M
  value: DesktopArtifactBridgeValueByMethod[M]
}

export interface DesktopArtifactBridgeFailure<M extends DesktopArtifactBridgeMethod> {
  ok: false
  method: M
  code: DesktopArtifactBridgeErrorCode
  message: string
}

export type DesktopArtifactBridgeResult<M extends DesktopArtifactBridgeMethod> =
  | DesktopArtifactBridgeSuccess<M>
  | DesktopArtifactBridgeFailure<M>

type DesktopArtifactBridgeHandler<M extends DesktopArtifactBridgeMethod> = (
  request: DesktopArtifactBridgeRequestByMethod[M],
  signal: AbortSignal,
) => DesktopArtifactBridgeValueByMethod[M] | Promise<DesktopArtifactBridgeValueByMethod[M]>

/**
 * An opaque, main-process-only binding to the active artifact surface.
 * Deliberately has no surface identifier, URL, JavaScript or CDP escape hatch.
 */
export interface DesktopArtifactBridgeTarget {
  /** Protocol negotiated by the active native surface, when known. */
  protocolVersion?: DesktopArtifactBridgeProtocolVersion
  capabilities: Partial<Record<DesktopArtifactBridgeMethod, boolean>>
  isCurrent(): boolean
  captureSelection?: DesktopArtifactBridgeHandler<'captureSelection'>
  resolveAnnotationSelection?: DesktopArtifactBridgeHandler<'resolveAnnotationSelection'>
  focusAnnotation?: DesktopArtifactBridgeHandler<'focusAnnotation'>
  browserInspect?: DesktopArtifactBridgeHandler<'browserInspect'>
  browserAct?: DesktopArtifactBridgeHandler<'browserAct'>
  /** Optional v4 lifecycle hooks. Implementations must accept only opaque handles. */
  bindCandidatePreview?: DesktopArtifactBridgeHandler<'bindCandidatePreview'>
  restoreCanonicalPreview?: DesktopArtifactBridgeHandler<'restoreCanonicalPreview'>
  screenshot?: DesktopArtifactBridgeHandler<'screenshot'>
  officeFlush?: DesktopArtifactBridgeHandler<'officeFlush'>
  reloadSurface?: DesktopArtifactBridgeHandler<'reloadSurface'>
}

export interface DesktopArtifactBridgeOptions {
  getActiveTarget(): DesktopArtifactBridgeTarget | null
  operationTimeoutMs?: number
}

const REQUEST_PARSERS = {
  captureSelection: parseDesktopArtifactCaptureSelectionRequest,
  resolveAnnotationSelection: parseDesktopArtifactResolveAnnotationSelectionRequest,
  focusAnnotation: parseDesktopArtifactFocusAnnotationRequest,
  browserInspect: parseDesktopArtifactBrowserInspectRequest,
  browserAct: parseDesktopArtifactBrowserActRequest,
  bindCandidatePreview: parseDesktopArtifactBindCandidatePreviewRequest,
  screenshot: parseDesktopArtifactScreenshotRequest,
  officeFlush: parseDesktopArtifactOfficeFlushRequest,
  reloadSurface: parseDesktopArtifactReloadSurfaceRequest,
  restoreCanonicalPreview: parseDesktopArtifactRestoreCanonicalPreviewRequest,
} satisfies {
  [M in DesktopArtifactBridgeMethod]: (
    value: unknown,
  ) => DesktopArtifactBridgeRequestByMethod[M]
}

function handlerFor<M extends DesktopArtifactBridgeMethod>(
  target: DesktopArtifactBridgeTarget,
  method: M,
): DesktopArtifactBridgeHandler<M> | undefined {
  return target[method] as DesktopArtifactBridgeHandler<M> | undefined
}

/**
 * Serializes the small, typed artifact operation set against the UI-selected
 * active surface. Every request is parsed before it reaches a target and every
 * capability must be affirmatively enabled alongside a concrete handler.
 */
export class DesktopArtifactBridge {
  private operationQueue: Promise<void> = Promise.resolve()
  private readonly operationTimeoutMs: number

  constructor(private readonly options: DesktopArtifactBridgeOptions) {
    const configuredTimeout = options.operationTimeoutMs ?? 15_000
    this.operationTimeoutMs = Number.isFinite(configuredTimeout)
      ? Math.max(100, Math.min(60_000, Math.floor(configuredTimeout)))
      : 15_000
  }

  getCapabilities(): DesktopArtifactBridgeCapabilities {
    const target = this.activeTarget()
    if (!target || !this.targetIsCurrent(target)) {
      return { ...DESKTOP_ARTIFACT_BRIDGE_UNSUPPORTED_CAPABILITIES }
    }
    return {
      // Keep the transport contract at v4 while reporting the active
      // surface's negotiated version.  This prevents a v3 legacy preview
      // from being mistaken for an autonomous v4 browser surface by the
      // Gateway, without removing its old screenshot/reload operations.
      version: target.protocolVersion === DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION_V3
        ? DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION_V3
        : DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
      available: true,
      captureSelection: this.methodAvailable(target, 'captureSelection'),
      resolveAnnotationSelection: this.methodAvailable(target, 'resolveAnnotationSelection'),
      focusAnnotation: this.methodAvailable(target, 'focusAnnotation'),
      browserInspect: this.methodAvailable(target, 'browserInspect'),
      browserAct: this.methodAvailable(target, 'browserAct'),
      bindCandidatePreview: this.methodAvailable(target, 'bindCandidatePreview'),
      restoreCanonicalPreview: this.methodAvailable(target, 'restoreCanonicalPreview'),
      screenshot: this.methodAvailable(target, 'screenshot'),
      officeFlush: this.methodAvailable(target, 'officeFlush'),
      reloadSurface: this.methodAvailable(target, 'reloadSurface'),
    }
  }

  captureSelection(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'captureSelection'>> {
    return this.invoke('captureSelection', value, signal)
  }

  resolveAnnotationSelection(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'resolveAnnotationSelection'>> {
    return this.invoke('resolveAnnotationSelection', value, signal)
  }

  focusAnnotation(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'focusAnnotation'>> {
    return this.invoke('focusAnnotation', value, signal)
  }

  browserInspect(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'browserInspect'>> {
    return this.invoke('browserInspect', value, signal)
  }

  browserAct(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'browserAct'>> {
    return this.invoke('browserAct', value, signal)
  }

  bindCandidatePreview(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'bindCandidatePreview'>> {
    return this.invoke('bindCandidatePreview', value, signal)
  }

  restoreCanonicalPreview(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'restoreCanonicalPreview'>> {
    return this.invoke('restoreCanonicalPreview', value, signal)
  }

  screenshot(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'screenshot'>> {
    return this.invoke('screenshot', value, signal)
  }

  officeFlush(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'officeFlush'>> {
    return this.invoke('officeFlush', value, signal)
  }

  reloadSurface(
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<'reloadSurface'>> {
    return this.invoke('reloadSurface', value, signal)
  }

  private invoke<M extends DesktopArtifactBridgeMethod>(
    method: M,
    value: unknown,
    signal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<M>> {
    let request: DesktopArtifactBridgeRequestByMethod[M]
    try {
      request = REQUEST_PARSERS[method](value) as DesktopArtifactBridgeRequestByMethod[M]
    } catch (error) {
      return Promise.resolve({
        ok: false,
        method,
        code: 'invalid-request',
        message: error instanceof Error ? error.message : 'The Desktop artifact request is invalid.',
      })
    }

    // Bind before entering the queue. If the user switches items while an
    // earlier operation is running, this request must fail against its stale
    // binding instead of silently targeting the newly-active surface.
    const target = this.activeTarget()
    if (!target) {
      return Promise.resolve({
        ok: false,
        method,
        code: 'unavailable',
        message: 'No active protocol-v4 Desktop artifact surface is available.',
      })
    }

    const operation = this.operationQueue.then(
      () => this.perform(method, request, target, signal),
      () => this.perform(method, request, target, signal),
    )
    this.operationQueue = operation.then(() => undefined, () => undefined)
    return operation
  }

  private async perform<M extends DesktopArtifactBridgeMethod>(
    method: M,
    request: DesktopArtifactBridgeRequestByMethod[M],
    target: DesktopArtifactBridgeTarget,
    externalSignal?: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<M>> {
    if (externalSignal?.aborted) {
      return {
        ok: false,
        method,
        code: 'timed-out',
        message: 'The Desktop artifact operation timed out.',
      }
    }
    if (!this.targetIsCurrent(target)) {
      return {
        ok: false,
        method,
        code: 'unavailable',
        message: 'The active Desktop artifact surface changed before the operation ran.',
      }
    }
    const handler = handlerFor(target, method)
    if (!this.methodAvailable(target, method) || !handler) {
      return {
        ok: false,
        method,
        code: 'unsupported',
        message: `The active Desktop artifact surface does not support ${method}.`,
      }
    }

    const controller = new AbortController()
    let timeout: NodeJS.Timeout | undefined
    let abortListener: (() => void) | undefined
    let externalAbortListener: (() => void) | undefined
    try {
      const aborted = new Promise<never>((_resolve, reject) => {
        abortListener = () => reject(new Error('Desktop artifact operation timed out.'))
        controller.signal.addEventListener('abort', abortListener, { once: true })
      })
      if (externalSignal) {
        externalAbortListener = () => controller.abort()
        externalSignal.addEventListener('abort', externalAbortListener, { once: true })
        if (externalSignal.aborted) controller.abort()
      }
      timeout = setTimeout(() => controller.abort(), this.operationTimeoutMs)
      timeout.unref()
      const value = await Promise.race([
        handler(request, controller.signal),
        aborted,
      ])
      if (!this.targetIsCurrent(target)) {
        return {
          ok: false,
          method,
          code: 'unavailable',
          message: 'The active Desktop artifact surface changed during the operation.',
        }
      }
      return { ok: true, method, value }
    } catch (error) {
      const timedOut = controller.signal.aborted
      return {
        ok: false,
        method,
        code: timedOut ? 'timed-out' : 'operation-failed',
        message: timedOut
          ? 'The Desktop artifact operation timed out.'
          : 'The Desktop artifact operation failed.',
      }
    } finally {
      if (timeout) clearTimeout(timeout)
      if (abortListener) controller.signal.removeEventListener('abort', abortListener)
      if (externalSignal && externalAbortListener) {
        externalSignal.removeEventListener('abort', externalAbortListener)
      }
    }
  }

  private activeTarget(): DesktopArtifactBridgeTarget | null {
    try {
      return this.options.getActiveTarget()
    } catch {
      return null
    }
  }

  private methodAvailable(
    target: DesktopArtifactBridgeTarget,
    method: DesktopArtifactBridgeMethod,
  ): boolean {
    return target.capabilities[method] === true
      && typeof handlerFor(target, method) === 'function'
  }

  private targetIsCurrent(target: DesktopArtifactBridgeTarget): boolean {
    try {
      return target.isCurrent() === true
    } catch {
      return false
    }
  }
}
