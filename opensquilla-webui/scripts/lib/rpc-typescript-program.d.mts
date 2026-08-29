export interface RpcAnalysisSource {
  rel: string
  source: import('typescript').SourceFile
}

export interface RpcAnalysisProgram {
  program: import('typescript').Program
  checker: import('typescript').TypeChecker
  sources: RpcAnalysisSource[]
  resolveRecord(
    importerRel: string,
    specifier: string,
  ): { rel: string; absolute: string; text: string; kind: number } | null
  relForSource(source: import('typescript').SourceFile): string | null
  sourceForRel(rel: string): import('typescript').SourceFile | null
  canonicalSymbol(
    symbol: import('typescript').Symbol | null | undefined,
  ): import('typescript').Symbol | null
  symbolAt(node: import('typescript').Node): import('typescript').Symbol | null
  exportedSymbol(
    rel: string,
    name: string,
  ): import('typescript').Symbol | null
}

/** Shared lexical/module graph for RPC and HTTP architecture scanners. */
export function createRpcAnalysisProgram(input: {
  ts: typeof import('typescript')
  root: string
  sources: RpcAnalysisSource[]
}): RpcAnalysisProgram
