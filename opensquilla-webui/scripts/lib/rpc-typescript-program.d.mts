export interface RpcAnalysisSource {
  rel: string
  source: import('@typescript/typescript6').SourceFile
}

export interface RpcAnalysisProgram {
  program: import('@typescript/typescript6').Program
  checker: import('@typescript/typescript6').TypeChecker
  sources: RpcAnalysisSource[]
  resolveRecord(
    importerRel: string,
    specifier: string,
  ): { rel: string; absolute: string; text: string; kind: number } | null
  relForSource(source: import('@typescript/typescript6').SourceFile): string | null
  sourceForRel(rel: string): import('@typescript/typescript6').SourceFile | null
  canonicalSymbol(
    symbol: import('@typescript/typescript6').Symbol | null | undefined,
  ): import('@typescript/typescript6').Symbol | null
  symbolAt(node: import('@typescript/typescript6').Node): import('@typescript/typescript6').Symbol | null
  exportedSymbol(
    rel: string,
    name: string,
  ): import('@typescript/typescript6').Symbol | null
}

/** Shared lexical/module graph for RPC and HTTP architecture scanners. */
export function createRpcAnalysisProgram(input: {
  ts: typeof import('@typescript/typescript6')
  root: string
  sources: RpcAnalysisSource[]
}): RpcAnalysisProgram
