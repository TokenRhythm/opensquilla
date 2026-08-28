import { dirname, resolve } from 'node:path'

const normalized = path => path.replace(/\\/g, '/')
const moduleKey = path => normalized(path).replace(/\.(?:vue|[cm]?[jt]sx?)$/, '')

function resolveSourceImport(root, importer, specifier) {
  const cleanSpecifier = specifier.split(/[?#]/, 1)[0]
  if (cleanSpecifier.startsWith('@/')) {
    return resolve(root, 'src', cleanSpecifier.slice(2))
  }
  if (cleanSpecifier.startsWith('./') || cleanSpecifier.startsWith('../')) {
    return resolve(dirname(resolve(root, importer)), cleanSpecifier)
  }
  return null
}

function extensionFor(ts, rel) {
  if (/\.tsx$/i.test(rel)) return ts.Extension.Tsx
  if (/\.jsx$/i.test(rel)) return ts.Extension.Jsx
  if (/\.mjs$/i.test(rel)) return ts.Extension.Mjs ?? ts.Extension.Js
  if (/\.cjs$/i.test(rel)) return ts.Extension.Cjs ?? ts.Extension.Js
  if (/\.mts$/i.test(rel)) return ts.Extension.Mts ?? ts.Extension.Ts
  if (/\.cts$/i.test(rel)) return ts.Extension.Cts ?? ts.Extension.Ts
  if (/\.js$/i.test(rel)) return ts.Extension.Js
  return ts.Extension.Ts
}

/**
 * Bind the already extracted WebUI sources into one in-memory TypeScript
 * Program.  The Program is diagnostic-only: it deliberately has no standard
 * library and never reads source outside the supplied set.
 */
export function createRpcAnalysisProgram({ ts, root, sources }) {
  const records = new Map()
  const byModuleKey = new Map()
  for (const { rel, source } of sources) {
    const absolute = normalized(resolve(root, rel))
    const record = {
      rel,
      absolute,
      text: source.getFullText(),
      kind: source.scriptKind,
    }
    records.set(absolute, record)
    byModuleKey.set(moduleKey(absolute), record)
  }

  function resolveRecord(importerRel, specifier) {
    const target = resolveSourceImport(root, importerRel, specifier)
    if (!target) return null
    return byModuleKey.get(moduleKey(target))
      ?? byModuleKey.get(`${moduleKey(target)}/index`)
      ?? null
  }

  const options = {
    allowJs: true,
    allowNonTsExtensions: true,
    checkJs: false,
    jsx: ts.JsxEmit.Preserve,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    noLib: true,
    noResolve: false,
    skipLibCheck: true,
    target: ts.ScriptTarget.ESNext,
  }
  const defaultHost = ts.createCompilerHost(options, true)
  function resolvedModule(specifier, containingFile) {
    const importer = records.get(normalized(containingFile))
    const record = importer ? resolveRecord(importer.rel, specifier) : null
    return record ? {
      extension: extensionFor(ts, record.rel),
      isExternalLibraryImport: false,
      resolvedFileName: record.absolute,
    } : undefined
  }
  const host = {
    ...defaultHost,
    fileExists(fileName) {
      return records.has(normalized(fileName))
    },
    readFile(fileName) {
      return records.get(normalized(fileName))?.text
    },
    getSourceFile(fileName, languageVersion) {
      const record = records.get(normalized(fileName))
      if (!record) return undefined
      return ts.createSourceFile(
        record.absolute,
        record.text,
        languageVersion,
        true,
        record.kind,
      )
    },
    resolveModuleNames(moduleNames, containingFile) {
      return moduleNames.map(specifier => resolvedModule(specifier, containingFile))
    },
    resolveModuleNameLiterals(moduleLiterals, containingFile) {
      return moduleLiterals.map(literal => ({
        resolvedModule: resolvedModule(literal.text, containingFile),
      }))
    },
    getCurrentDirectory: () => normalized(resolve(root)),
    getCanonicalFileName: fileName => normalized(fileName),
    useCaseSensitiveFileNames: () => true,
    getNewLine: () => '\n',
    writeFile: () => {},
  }
  const program = ts.createProgram({
    rootNames: [...records.keys()],
    options,
    host,
  })
  const checker = program.getTypeChecker()
  const relByAbsolute = new Map([...records].map(([absolute, record]) => [absolute, record.rel]))

  function relForSource(sourceFile) {
    return relByAbsolute.get(normalized(sourceFile.fileName)) ?? null
  }

  function sourceForRel(rel) {
    const record = [...records.values()].find(candidate => candidate.rel === rel)
    return record ? program.getSourceFile(record.absolute) ?? null : null
  }

  function canonicalSymbol(symbol) {
    let current = symbol ?? null
    const seen = new Set()
    while (current && (current.flags & ts.SymbolFlags.Alias) && !seen.has(current)) {
      seen.add(current)
      const target = checker.getAliasedSymbol(current)
      if (!target || target === current) break
      current = target
    }
    return current
  }

  function symbolAt(node) {
    return node ? checker.getSymbolAtLocation(node) ?? null : null
  }

  function exportedSymbol(rel, name) {
    const source = sourceForRel(rel)
    const moduleSymbol = source ? symbolAt(source) : null
    if (!moduleSymbol) return null
    return checker.getExportsOfModule(moduleSymbol)
      .find(symbol => symbol.getName() === name) ?? null
  }

  return {
    program,
    checker,
    sources: [...records.values()].map(record => ({
      rel: record.rel,
      source: program.getSourceFile(record.absolute),
    })).filter(entry => entry.source),
    resolveRecord,
    relForSource,
    sourceForRel,
    canonicalSymbol,
    symbolAt,
    exportedSymbol,
  }
}
