// Compatibility exports. Lease transport and Desktop brokering live in the Workbench Adapter.
export {
  ArtifactPreviewLeaseError,
  artifactPreviewId,
  createArtifactPreviewLease,
  parseArtifactPreviewLease,
  parseArtifactPreviewLeaseRenewal,
  renewArtifactPreviewLease,
  revokeArtifactPreviewLease,
} from '@/adapters/gateway/artifactPreviewLeaseV4'
export type {
  ArtifactPreviewCollectionStatus,
  ArtifactPreviewLease,
  ArtifactPreviewLeaseContext,
  ArtifactPreviewLeaseRenewal,
  ArtifactPreviewLeaseSource,
  ArtifactPreviewMode,
  ArtifactPreviewNativeBroker,
} from '@/adapters/gateway/artifactPreviewLeaseV4'
