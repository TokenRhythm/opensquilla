// Keep the locked packaging APIs while using the patched, nonrecursive matcher.
// Remove these adapters when upstream asar, dir-compare and glob support v11.
const { createHash } = require('node:crypto')
const { readFile, writeFile } = require('node:fs/promises')
const { join } = require('node:path')

const sha256 = (value) => createHash('sha256').update(value).digest('hex')
const importReplacement = [
  'const minimatch_1 = __importDefault(require("minimatch"));',
  'const minimatch_1 = __importDefault(require("minimatch").minimatch);',
]
const matcherReplacements = [
  ['var rawGlob = pn._glob', 'var rawGlob = pn.pattern'],
  ['m = !e.match(pn)', 'm = !pn.test(e)'],
  ['m = e.match(pn)', 'm = pn.test(e)'],
]
const PATCHES = [
  {
    packageName: '@electron/asar', version: '3.4.1', file: 'lib/asar.js',
    originalSha256: '91e89e96324626b140f3e897dffaca6e3cbb1135c4192ce6ceaa0e84af4ba152',
    patchedSha256: '0d7135c88b9a9adcd19e57c3beb64d138ee5d20fabf09821ba32fe0765127cc0', replacements: [importReplacement],
  },
  {
    packageName: 'dir-compare', version: '4.2.0',
    file: 'build/src/FilterHandler/defaultFilterHandler.js',
    originalSha256: '6b753f018576fc270dc93e2be35d98a50431bd7f1e373b98acbec5e7acf381d2',
    patchedSha256: '6e3a5f2b730da7a8131ac5726aa1c2efb56febba41d77d04ca6de654b2a378f2', replacements: [importReplacement],
  },
  {
    packageName: 'glob', version: '7.2.3', file: 'glob.js',
    originalSha256: 'b8f35657ca927593d0f9e1aae3a8cfe9c33c697bf3c5733c2f6727f25ae25be1',
    patchedSha256: '4caeca5af34b662a1598d6975bf6541a478443e6e79765095cf24479c3dbcaa4', replacements: matcherReplacements,
  },
  {
    packageName: 'glob', version: '7.2.3', file: 'sync.js',
    originalSha256: '0f61227f4b55297f1ad16798c53e6a6dd55d633856f153133716413b7c5f61ad',
    patchedSha256: 'fea0ef6613ef0bcb7d103a37383cecb80f12797539e4c76604d57d5e15bce7a5', replacements: matcherReplacements,
  },
  {
    packageName: 'glob', version: '7.2.3', file: 'common.js',
    originalSha256: 'b867e089ab5d4ab19a83e5b34da3dd7f4018fdf255fcacc681aab87d41dc77e8',
    patchedSha256: '40db395771c6efb10f4d43385a6564dd9ec2e11fafcd6b5c64cb97db5f45d56f', replacements: [
      // v3 ignored allowWindowsEscape. v11 must explicitly keep escapes.
      ['options.allowWindowsEscape = false', 'options.allowWindowsEscape = true\n  options.optimizationLevel = 0'],
      // glob7 indexes matches and globSet in parallel, including nonull and
      // nounique. Keep one set row per brace expansion instead of v11's merge.
      ['self.minimatch = new Minimatch(pattern, options)',
        `self.minimatch = new Minimatch(pattern, options)
  self.minimatch.globSet = minimatch.braceExpand(pattern, options)
  self.minimatch.set = self.minimatch.globSet.map(function (part) {
    return new Minimatch(part, Object.assign({}, options, { nobrace: true })).set[0]
  })`],
      ['new Minimatch(gpattern, { dot: true })',
        'new Minimatch(gpattern, { dot: true, optimizationLevel: 0 })'],
      ['new Minimatch(pattern, { dot: true })',
        'new Minimatch(pattern, { dot: true, optimizationLevel: 0 })'],
    ],
  },
]

function patchedSource(original, patch) {
  if (sha256(original) !== patch.originalSha256) {
    throw new Error(`Unexpected upstream ${patch.packageName}/${patch.file}; review the glob compatibility adapter.`)
  }
  let source = original.toString('utf8')
  for (const [before, after] of patch.replacements) {
    if (source.split(before).length !== 2) {
      throw new Error(`Expected one glob compatibility patch point: ${before}`)
    }
    source = source.replace(before, after)
  }
  return Buffer.from(source)
}

async function prepareInstalledGlobs(root = join(__dirname, '..')) {
  const writes = []
  // Validate every input before changing any file. Never silently patch a new
  // upstream release or overwrite local edits, including stale adapters.
  for (const patch of PATCHES) {
    const packageRoot = join(root, 'node_modules', patch.packageName)
    const manifest = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'))
    if (manifest.version !== patch.version) {
      throw new Error(`Glob compatibility adapter requires ${patch.packageName} ${patch.version}, got ${manifest.version}.`)
    }
    const target = join(packageRoot, patch.file)
    const current = await readFile(target)
    if (sha256(current) === patch.patchedSha256) continue
    const patched = patchedSource(current, patch)
    if (sha256(patched) !== patch.patchedSha256) {
      throw new Error(`Glob compatibility adapter digest mismatch for ${patch.packageName}/${patch.file}.`)
    }
    writes.push({ target, patched })
  }
  for (const { target, patched } of writes) await writeFile(target, patched)
  return { changed: writes.length }
}

module.exports = { PATCHES, patchedSource, prepareInstalledGlobs }
if (require.main === module) {
  prepareInstalledGlobs().catch((error) => { console.error(error); process.exitCode = 1 })
}
