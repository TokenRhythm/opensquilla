/**
 * Application-facing artifact descriptor shared by Chat and the Workbench.
 *
 * The Gateway Adapter validates list/get responses before exposing these
 * values. Legacy aliases remain additive here because persisted transcripts
 * and mixed-version replay can still contain them.
 */
export interface ArtifactPayload {
  id?: string
  key?: string
  kind?: string
  sha256?: string
  session_id?: string
  session_key?: string
  sessionKey?: string
  epoch?: number
  generation_epoch?: number
  generationEpoch?: number
  stream_seq?: number
  name?: string
  mime?: string
  size?: number | string
  source?: string
  created_at?: string
  createdAt?: string
  store?: string
  download_url?: string
  thumbnail_url?: string
  [key: string]: unknown
}
