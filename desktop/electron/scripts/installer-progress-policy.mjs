import { lstat, readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'

export const INSTALLER_PROGRESS_INCLUDE = 'scripts/nsis/installer-progress.nsh'

const REQUIRED_INCLUDES = ['LogicLib.nsh', 'nsDialogs.nsh', 'WinMessages.nsh']
const REQUIRED_FRAGMENTS = [
  '!ifndef BUILD_UNINSTALLER',
  '!macro customPageAfterChangeDir',
  '!define MUI_PAGE_CUSTOMFUNCTION_SHOW OpenSquillaInstallerProgressShow',
  'Function OpenSquillaInstallerProgressShow',
  '!macro customInstall',
  'Function OpenSquillaInstallerProgressFinish',
  '${NSD_AddStyle} $1 ${PBS_MARQUEE}',
  'SendMessage $1 ${PBM_SETMARQUEE} 1 40',
  'Preparing and unpacking OpenSquilla files. This may take several minutes…',
  '正在准备并解压 OpenSquilla 文件，请稍候…',
  '正在準備並解壓縮 OpenSquilla 檔案，請稍候…',
  'Finishing installation…',
  '正在完成安装…',
  '正在完成安裝…',
]

const ALLOWED_SOURCE_LINES = [
  /^!include\s+"[^"]+"$/,
  /^!ifndef\s+[A-Z0-9_.]+$/i,
  /^!endif$/,
  /^!define(?:\s+\/ifndef)?\s+[A-Z0-9_.]+\s+.+$/i,
  /^!macro\s+[A-Z0-9_.]+$/i,
  /^!macroend$/,
  /^Function\s+[A-Z0-9_.]+$/i,
  /^FunctionEnd$/i,
  /^\$\{(?:If|IfNot|EndIf|NSD_AddStyle)\}(?:\s|$)/,
  /^(?:Push|Pop|FindWindow|GetDlgItem|SendMessage|StrCmp|Goto|Call)\b/,
  /^[A-Z0-9_.]+:$/i,
]

const FORBIDDEN_SOURCE_PATTERNS = [
  {
    label: 'file-system mutation',
    pattern: /^\s*(?:File|Delete|RMDir|CopyFiles|Rename|SetOutPath|CreateDirectory)\b/im,
  },
  {
    label: 'registry mutation',
    pattern: /^\s*(?:WriteReg\w*|DeleteReg\w*)\b/im,
  },
  {
    label: 'process execution',
    pattern: /^\s*(?:Exec|ExecWait|ExecShell|CallInstDLL)\b/im,
  },
  {
    label: 'installer state mutation',
    pattern: /^\s*(?:InstallDir|SetShellVarContext|RequestExecutionLevel|WriteUninstaller|Section|SectionEnd)\b/im,
  },
  {
    label: 'installation path access',
    pattern: /\$(?:INSTDIR|APPDATA|LOCALAPPDATA|PROGRAMFILES\w*|PROFILE|SMPROGRAMS|DESKTOP)\b/i,
  },
  {
    label: 'installer lifecycle override',
    pattern: /\b(?:customUn\w*|customRemoveFiles|customInit|customInstallMode|customCheckAppRunning|preInit)\b/i,
  },
  {
    label: 'elevation, process control, or network plugin',
    pattern: /(?:\bUAC_|\bKillProc::|\btaskkill\b|\binetc::|\bNSISdl::)/i,
  },
  {
    label: 'compile-time command execution',
    pattern: /^\s*!(?:system|execute|packhdr|appendfile|addplugindir)\b/im,
  },
]

async function pathIsFile(path) {
  const info = await lstat(path).catch(() => null)
  return info?.isFile() === true
}

async function pathExists(path) {
  return (await lstat(path).catch(() => null)) !== null
}

export function validateInstallerProgressSource(source) {
  const failures = []
  const trimmed = source.trim()

  if (!trimmed.startsWith('!ifndef BUILD_UNINSTALLER') || !trimmed.endsWith('!endif')) {
    failures.push('installer progress include must be fully guarded by BUILD_UNINSTALLER')
  }

  const includes = [...source.matchAll(/^\s*!include\s+"([^"]+)"\s*$/gim)].map((match) => match[1])
  if (JSON.stringify(includes) !== JSON.stringify(REQUIRED_INCLUDES)) {
    failures.push(`installer progress include may only load: ${REQUIRED_INCLUDES.join(', ')}`)
  }

  const macroNames = [...source.matchAll(/^\s*!macro\s+([^\s]+)\s*$/gim)].map((match) => match[1])
  if (JSON.stringify(macroNames) !== JSON.stringify(['customPageAfterChangeDir', 'customInstall'])) {
    failures.push('installer progress include may only define customPageAfterChangeDir and customInstall macros')
  }

  const functionNames = [...source.matchAll(/^\s*Function\s+([^\s]+)\s*$/gim)].map((match) => match[1])
  if (
    JSON.stringify(functionNames)
    !== JSON.stringify(['OpenSquillaInstallerProgressShow', 'OpenSquillaInstallerProgressFinish'])
  ) {
    failures.push('installer progress include may only define the two approved UI functions')
  }

  for (const fragment of REQUIRED_FRAGMENTS) {
    if (!source.includes(fragment)) {
      failures.push(`installer progress include is missing required UI contract: ${fragment}`)
    }
  }

  for (const { label, pattern } of FORBIDDEN_SOURCE_PATTERNS) {
    if (pattern.test(source)) {
      failures.push(`installer progress include contains forbidden ${label}`)
    }
  }

  for (const [index, rawLine] of source.split(/\r?\n/).entries()) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#') || line.startsWith(';')) continue
    if (!ALLOWED_SOURCE_LINES.some((pattern) => pattern.test(line))) {
      failures.push(`installer progress include line ${index + 1} is outside the display-only allowlist: ${line}`)
    }
  }

  return failures
}

export async function verifyInstallerProgressPolicy(packageRoot, packageJson) {
  const failures = []
  const nsis = packageJson.build?.nsis

  if (nsis?.include !== INSTALLER_PROGRESS_INCLUDE) {
    failures.push(`NSIS include must be exactly ${INSTALLER_PROGRESS_INCLUDE}`)
  }
  if (nsis && Object.hasOwn(nsis, 'script')) {
    failures.push('NSIS must not define a custom full installer script')
  }

  const defaultInstallerInclude = join(packageRoot, 'build', 'installer.nsh')
  if (await pathExists(defaultInstallerInclude)) {
    failures.push('NSIS default build/installer.nsh override must not be present')
  }

  const includePath = resolve(packageRoot, INSTALLER_PROGRESS_INCLUDE)
  if (!(await pathIsFile(includePath))) {
    failures.push(`installer progress include is missing at ${includePath}`)
    return failures
  }

  const source = await readFile(includePath, 'utf8')
  failures.push(...validateInstallerProgressSource(source))
  return failures
}
