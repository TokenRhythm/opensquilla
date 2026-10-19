import type { Ref } from 'vue'

/** Recheck at command dispatch; an earlier successful wait is not admission. */
export type SessionLiveReadiness = () => boolean

export interface SessionRecoveryCheckpoint {
  (): boolean
  /** Stale failures must not suspend a successor authority's recovery. */
  isCurrent(): boolean
}

/** A coordinator-owned proof of a fresh read in the same authority lineage. */
export interface SessionRecoveryProof {
  /** Changes when a read or its first authority binding can satisfy a checkpoint. */
  readonly revision: Readonly<Ref<number>>
  /** Capture before requesting recovery; replacement leases may satisfy it. */
  capture(): SessionRecoveryCheckpoint
}
