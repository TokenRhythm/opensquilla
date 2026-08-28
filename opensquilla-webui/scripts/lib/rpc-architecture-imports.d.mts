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

export function isDirectCallMemberReference(ts: any, node: any): boolean

export function destructuredCallSourceText(
  ts: any,
  node: any,
  source: any,
): string | null

export function isRpcCapabilityReceiverText(receiver: string): boolean

export function isKnownNonRpcCallReceiver(receiver: string): boolean
