export const lane = 'independent'

export const debt = {
  'src/components/workbench/AppWorkbench.vue': {
    on: 2,
    httpRequest: 1,
    httpApiEndpoint: 1,
  },
  'src/components/workbench/artifactWorkbenchProvider.ts': {
    httpRequest: 1,
    httpApiEndpoint: 1,
  },
  'src/composables/chat/useSessionArtifacts.ts': {
    call: 1,
    markMethodUnavailable: 1,
    supportsMethod: 1,
  },
  'src/composables/workbench/useArtifactPreviewResource.ts': {
    httpRequest: 1,
    httpApiEndpoint: 1,
    httpAuthToken: 1,
    httpAuthorizationHeader: 1,
    httpSessionKeyHeader: 1,
  },
  'src/utils/workbench/artifactPreviewLease.ts': {
    httpRequest: 3,
    httpApiEndpoint: 3,
    httpAuthToken: 3,
    httpAuthorizationHeader: 3,
    httpSessionKeyHeader: 3,
  },
  'src/workbench/artifactDocumentProvider.ts': {
    call: 1,
    markMethodUnavailable: 1,
    supportsMethod: 1,
  },
  'src/workbench/artifactPromptAnnotationProvider.ts': {
    call: 1,
    markMethodUnavailable: 1,
    supportsMethod: 1,
  },
  'src/workbench/workbenchResourceProvider.ts': {
    call: 1,
    markMethodUnavailable: 1,
    supportsMethod: 1,
  },
}
