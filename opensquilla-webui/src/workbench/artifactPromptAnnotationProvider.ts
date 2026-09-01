export type { ArtifactPromptAnnotationProvider } from '@/modules/artifactWorkbench'
export {
  PROMPT_ANNOTATION_RPC_METHODS,
  createRpcArtifactPromptAnnotationProvider,
  normalizePromptAnnotation,
  normalizePromptAnnotationSnapshot,
} from '@/adapters/gateway/artifactPromptAnnotationsV4'
