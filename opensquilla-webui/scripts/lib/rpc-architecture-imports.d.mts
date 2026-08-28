export interface GeneratedContractImport {
  root: string
  importer: string
  specifier: string
}

export function resolveSourceImport(
  root: string,
  importer: string,
  specifier: string,
): string | null

export function generatedContractImportViolation(
  input: GeneratedContractImport,
): string | null

export function privateGatewayTransportImportViolation(
  input: GeneratedContractImport,
): string | null

export function gatewayAdapterRpcStoreImportViolation(
  input: GeneratedContractImport,
): string | null

export function boundaryModuleKind(
  input: GeneratedContractImport,
): 'generated Contract' | 'private Gateway transport' | null

export function boundaryReexportViolation(
  input: GeneratedContractImport,
): string | null

export function collectBoundaryArchitectureViolations(input: {
  ts: typeof import('typescript')
  root: string
  sources: Array<{
    rel: string
    source: import('typescript').SourceFile
  }>
  analysis?: import('./rpc-typescript-program.mjs').RpcAnalysisProgram
}): string[]

export function moduleReferenceSpecifier(ts: any, node: any): string | null

export function callMemberReceiverText(
  ts: any,
  node: any,
  source: any,
): string | null

export function callMemberReferenceReceiverText(
  ts: any,
  node: any,
  source: any,
): string | null

export function namedMemberCallReceiverText(
  ts: any,
  node: any,
  source: any,
  memberName: string,
): string | null

export function namedMemberReferenceReceiverText(
  ts: any,
  node: any,
  source: any,
  memberName: string,
): string | null

export function isDirectCallMemberReference(ts: any, node: any): boolean

export function destructuredCallSourceText(
  ts: any,
  node: any,
  source: any,
): string | null

export function destructuredMemberSourceText(
  ts: any,
  node: any,
  source: any,
  memberName: string,
): string | null

export function isRpcCapabilityReceiverText(receiver: string): boolean

export function isKnownNonRpcCallReceiver(receiver: string): boolean
