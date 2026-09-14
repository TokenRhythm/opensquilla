"""Run only synthetic NSIS executables/files; never install or launch OpenSquilla."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def nsis(value):
    return str(value).replace('$', '$$').replace('"', '$\\"')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nsis-root', required=True, type=Path)
    parser.add_argument('--evidence-root', required=True, type=Path)
    parser.add_argument('--fixture-parent', type=Path, default=Path(r'C:\Temp'))
    parser.add_argument('--upstream-uninstaller', type=Path, default=Path(__file__).resolve().parents[2]
                        / 'node_modules/app-builder-lib/templates/nsis/uninstaller.nsh')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This acceptance test requires native Windows.')
    args.evidence_root.mkdir(parents=True, exist_ok=True)
    args.fixture_parent.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix='oq1441-', dir=args.fixture_parent))
    boundary = Path(__file__).with_name('legacy-uninstaller-temp.nsh').resolve()
    compiler = args.nsis_root / 'Bin' / 'makensis.exe'
    environment = dict(os.environ, NSISDIR=str(args.nsis_root))
    result = {'scope': __doc__, 'fixtureRoot': str(fixture), 'cases': []}
    capture = fixture / 'capture.py'
    capture.write_text('''import json, os, sys
from pathlib import Path
with Path(sys.argv[1]).open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({'phase': sys.argv[2], 'TEMP': os.environ.get('TEMP'), 'TMP': os.environ.get('TMP'), 'pluginsDir': sys.argv[3] if len(sys.argv) > 3 else None}, ensure_ascii=False) + '\\n')
''', encoding='utf-8')

    def compile_script(path, source):
        path.write_text(source, encoding='utf-8-sig')
        process = subprocess.run([str(compiler), '-V2', str(path)], env=environment,
                                 capture_output=True, text=True, timeout=30)
        path.with_suffix('.compile.log').write_text(process.stdout + process.stderr, encoding='utf-8')
        if process.returncode or re.search(r'(?im)^warning\s+\d+:', process.stdout + process.stderr):
            raise AssertionError(process.stdout + process.stderr)

    def capture_command(log, phase, plugin=False):
        extra = ' "$PLUGINSDIR"' if plugin else ''
        return f'''ExecWait '"{nsis(sys.executable)}" "{nsis(capture)}" "{nsis(log)}" "{phase}"{extra}' $9'''

    # Compile the complete patched upstream include, in its actual declaration
    # order. Tiny wrapper-only fixtures otherwise hide forward-variable mistakes.
    patched_include = fixture / 'patched-installUtil.nsh'
    upstream_include = args.upstream_uninstaller.parent / 'include/installUtil.nsh'
    original_include = Path(str(upstream_include) + '.opensquilla-1441-original')
    if not original_include.is_file():
        original_include = upstream_include
    subprocess.run(['node', '-e', "const f=require('node:fs'),p=require(process.argv[1]);f.writeFileSync(process.argv[4],p.patchedTemplate(f.readFileSync(process.argv[2]),f.readFileSync(process.argv[3],'utf8')))",
                    str(boundary.with_name('prepare-upgrade-template.cjs')), str(original_include),
                    str(boundary), str(patched_include)], check=True, capture_output=True, text=True)
    compile_script(fixture / 'complete-template.nsi', f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
Name "Compile-only upstream declaration-order check"
OutFile "{nsis(fixture / 'compile-only-template.exe')}"
!include "LogicLib.nsh"
Var installMode
Var appExe
!define INSTALL_REGISTRY_KEY "Software\\Synthetic-NSIS-1441"
!define UNINSTALL_REGISTRY_KEY "Software\\Synthetic-NSIS-1441\\Uninstall"
!define isUpdated '0 = 1'
!define isDeleteAppData '0 = 1'
!macro SyntheticParent RESULT VALUE
  Push "${{VALUE}}"
  Call GetFileParent
  Pop ${{RESULT}}
!macroend
!define StdUtils.GetParentPath '!insertmacro SyntheticParent'
LangString uninstallFailed 1033 "Synthetic uninstall failed"
LangString appCannotBeClosed 1033 "Synthetic app cannot be closed"
!include "{nsis(patched_include)}"
Section
  StrCpy $installMode "CurrentUser"
  StrCpy $appExe "$EXEDIR\\synthetic.exe"
  !insertmacro uninstallOldVersion SHELL_CONTEXT
  !insertmacro handleUninstallResult SHELL_CONTEXT
SectionEnd
''')
    result['completeTemplateCompile'] = {'passed': True, 'executed': False,
                                         'script': str(fixture / 'complete-template.nsi')}
    print('PASS complete-template-compile (not executed)', flush=True)

    def case(name, iteration=2, prior=2, child_exit=2, modifications='',
             temp_value=None, missing=False, fallback=False, deny_directory=False,
             query_fail=False, wait_fail_once=False):
        root = fixture / name
        root.mkdir()
        install = root / 'app'
        install.mkdir()
        long_temp = root / ('original-temporary-path-' + 't' * 45)
        long_temp.mkdir()
        other_temp = root / ('separate-original-temp-' + 'm' * 30)
        other_temp.mkdir()
        log = root / 'environment.jsonl'
        child = root / 'synthetic-child.exe'
        parent = root / 'synthetic-parent.exe'
        case_boundary = boundary
        if query_fail or wait_fail_once:
            boundary_source = boundary.read_text(encoding='utf-8')
            if query_fail:
                needle = "    System::Call 'kernel32::GetExitCodeProcess(p r4, p r2) i.r6'"
                assert boundary_source.count(needle) == 1
                boundary_source = boundary_source.replace(needle, '''    System::Call '*$2(i 0)'
    StrCpy $6 0 ; Fixture: failed query also wrote an untrustworthy zero''')
            if wait_fail_once:
                needle = "    System::Call 'kernel32::WaitForSingleObject(p r4, i 100) i.r7'"
                assert boundary_source.count(needle) == 1
                boundary_source = 'Var /GLOBAL fixtureWaitFailedOnce\n' + boundary_source.replace(needle, needle + '''
    ${If} $fixtureWaitFailedOnce != 1
      StrCpy $fixtureWaitFailedOnce 1
      StrCpy $7 -1 ; Fixture: one WAIT_FAILED while child is still running
    ${EndIf}''')
            case_boundary = root / 'injected-boundary.nsh'
            case_boundary.write_text(boundary_source, encoding='utf-8-sig')
        compile_script(root / 'child.nsi', f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
Name "Synthetic NSIS environment child"
OutFile "{nsis(child)}"
Section
  InitPluginsDir
  {'Sleep 500' if wait_fail_once else ''}
  {capture_command(log, 'child', True)}
  SetErrorLevel {child_exit}
SectionEnd
''')
        executable = root / 'does-not-exist.exe' if missing or fallback else child
        call = f'''!insertmacro OpenSquillaExecLegacyUninstaller "{nsis(executable)}"
  StrCpy $8 0
  IfErrors 0 +2
    StrCpy $8 1
  FileOpen $7 "{nsis(root / 'first-result.txt')}" w
  FileWrite $7 "exit=$R0$\\r$\\nerrors=$8$\\r$\\n"
  FileClose $7
'''
        if fallback:
            call += f'''!insertmacro OpenSquillaExecLegacyUninstaller "{nsis(child)}"
'''
        compile_script(root / 'parent.nsi', f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
Name "Synthetic NSIS environment parent"
OutFile "{nsis(parent)}"
Var installationDir
!include "{nsis(case_boundary)}"
Section
  InitPluginsDir
  {modifications}
  {capture_command(log, 'before')}
  StrCpy $installationDir "{nsis(install)}"
  StrCpy $0 "/currentuser --updated"
  StrCpy $R5 {iteration}
  StrCpy $R0 {prior}
  ClearErrors
  !insertmacro OpenSquillaLegacyRetry
  {call}
  {capture_command(log, 'after')}
  SetErrorLevel 0
SectionEnd
''')
        child_environment = dict(environment, TEMP=temp_value if temp_value is not None else str(other_temp), TMP=str(long_temp))
        denied_sid = None
        if deny_directory:
            who = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                                 text=True, check=True).stdout.strip()
            import csv
            denied_sid = next(csv.reader([who]))[1]
            subprocess.run(['icacls', str(root), '/deny', f'*{denied_sid}:(AD)'], check=True,
                           capture_output=True, text=True)
        try:
            ran = subprocess.run([str(parent), '/S'], env=child_environment, timeout=40,
                                 capture_output=True)
        finally:
            if denied_sid:
                subprocess.run(['icacls', str(root), '/remove:d', f'*{denied_sid}'], check=True,
                               capture_output=True, text=True)
        records = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        before = records[0]
        after = records[-1]
        assert ran.returncode == 0, (name, ran.returncode, records)
        assert before['phase'] == 'before' and after['phase'] == 'after'
        assert (before['TEMP'], before['TMP']) == (after['TEMP'], after['TMP']), (name, records)
        children = [item for item in records if item['phase'] == 'child']
        first = dict(line.split('=', 1) for line in (root / 'first-result.txt').read_text().splitlines())
        if missing or fallback:
            assert first['errors'] == '1', (name, first)
        else:
            assert first == {'exit': str(2 if query_fail else child_exit), 'errors': '0'}, (name, first)
        if query_fail or wait_fail_once:
            assert len(children) == 1, (name, records)
        shortened = iteration > 1 and prior == 2 and not deny_directory
        for item in children:
            if shortened:
                assert len(item['TMP']) < len(str(long_temp)), (name, item)
                assert item['TMP'] == item['TEMP'], (name, item)
                assert Path(item['TMP']).samefile(root), (name, item)
            else:
                assert (item['TEMP'], item['TMP']) == (before['TEMP'], before['TMP']), (name, records)
        result['cases'].append({'name': name, 'passed': True, 'exitCode': ran.returncode,
                                'firstExecResult': first, 'environmentRecords': records,
                                'expectedShortened': shortened, 'script': str(root / 'parent.nsi')})
        (args.evidence_root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'PASS {name}', flush=True)

    case('first-attempt', iteration=1, prior=0, child_exit=0)
    case('retry-other-error', prior=1)
    case('retry-success', child_exit=0)
    case('retry-exit2', child_exit=2)
    case('exec-error', missing=True)
    case('in-place-fallback', fallback=True, child_exit=0)
    case('long-unicode', temp_value='C:\\' + '临时环境值' * 650)
    case('absent-temp', modifications='System::Call \'kernel32::SetEnvironmentVariableW(w "TEMP", p 0)\'')
    case('empty-temp', modifications='System::Call \'kernel32::SetEnvironmentVariableW(w "TEMP", w "")\'')
    case('both-absent', modifications='''System::Call 'kernel32::SetEnvironmentVariableW(w "TEMP", p 0)'
  System::Call 'kernel32::SetEnvironmentVariableW(w "TMP", p 0)' ''')
    case('directory-denied', deny_directory=True)
    case('exit-query-failure-no-fallback', child_exit=0, query_fail=True)
    case('transient-wait-failure-one-child', child_exit=0, wait_fail_once=True)

    upstream = args.upstream_uninstaller.read_bytes()
    upstream_sha = hashlib.sha256(upstream).hexdigest()
    assert upstream_sha == '9ee2dac4593478083e8aa6f8487287ce9401006ccd50ecc538871d133ea4a42c', upstream_sha
    upstream_source = upstream.decode('utf-8')
    atomic = upstream_source[upstream_source.index('Function un.atomicRMDir'):
                             upstream_source.index('!ifndef UNINSTALL_SECTION_NAME')]
    result['upstreamUninstallerSha256'] = upstream_sha

    def atomic_case(name, locked):
        root = fixture / name
        root.mkdir()
        install = root / 'app'
        install.mkdir()
        original_temp = root / ('original-temporary-directory-' + 't' * 40)
        original_temp.mkdir()
        relative_length = 222 - len(str(install)) - 1
        parts = []
        while relative_length > 60:
            parts.append('d' * 35)
            relative_length -= 36
        parts.append('f' * (relative_length - 4) + '.txt')
        relative = Path('readable.txt') if locked else Path(*parts)
        target = install / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'Only synthetic legacy-upgrade data.\n')
        expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        log = root / 'environment.jsonl'
        maker = root / 'synthetic-maker.exe'
        legacy = root / 'synthetic-old-uninstaller.exe'
        parent = root / 'synthetic-upgrade.exe'
        compile_script(root / 'legacy.nsi', f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
Name "Synthetic old atomicRMDir"
OutFile "{nsis(maker)}"
!include "LogicLib.nsh"
!define UNINSTALL_FILENAME "synthetic-old-uninstaller.exe"
Function .onInit
  WriteUninstaller "{nsis(legacy)}"
  SetErrorLevel 0
  Quit
FunctionEnd
Section
SectionEnd
Function un.onInit
  InitPluginsDir
  StrCpy $INSTDIR "{nsis(install)}"
FunctionEnd
{atomic}
Section "Uninstall"
  {capture_command(log, 'atomic-child', True)}
  CreateDirectory "$PLUGINSDIR\\old-install"
  Push ""
  Call un.atomicRMDir
  Pop $R0
  ${{If}} $R0 != 0
    Push ""
    Call un.restoreFiles
    Pop $R0
    Abort "Synthetic atomicRMDir failure"
  ${{EndIf}}
SectionEnd
''')
        subprocess.run([str(maker), '/S'], env=environment, timeout=20, check=True,
                       capture_output=True)
        compile_script(root / 'upgrade.nsi', f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
Name "Synthetic legacy upgrade"
OutFile "{nsis(parent)}"
Var installationDir
!include "{nsis(boundary)}"
Section
  InitPluginsDir
  {capture_command(log, 'before')}
  StrCpy $installationDir "{nsis(install)}"
  StrCpy $0 "/currentuser --updated"
  StrCpy $R5 1
  StrCpy $R0 0
  ClearErrors
  !insertmacro OpenSquillaLegacyRetry
  !insertmacro OpenSquillaExecLegacyUninstaller "{nsis(legacy)}"
  StrCpy $8 $R0
  ${{If}} $R0 == 2
    IntOp $R5 $R5 + 1
    !insertmacro OpenSquillaLegacyRetry
    !insertmacro OpenSquillaExecLegacyUninstaller "{nsis(legacy)}"
  ${{EndIf}}
  FileOpen $7 "{nsis(root / 'exit.txt')}" w
  FileWrite $7 "first=$8$\\r$\\nfinal=$R0$\\r$\\n"
  FileClose $7
  {capture_command(log, 'after')}
  SetErrorLevel $R0
SectionEnd
''')
        run_environment = dict(environment, TEMP=str(original_temp), TMP=str(original_temp))
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                                      ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = None
        if locked:
            handle = kernel.CreateFileW(str(target), 0x80000000, 3, None, 3, 0x80, None)
            assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
            assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_hash
        try:
            ran = subprocess.run([str(parent), '/S'], env=run_environment, timeout=40,
                                 capture_output=True)
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
        exits = dict(line.split('=', 1) for line in (root / 'exit.txt').read_text().splitlines())
        records = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        children = [item for item in records if item['phase'] == 'atomic-child']
        assert len(children) == 2, records
        assert exits == {'first': '2', 'final': '2' if locked else '0'}, exits
        assert ran.returncode == (2 if locked else 0), ran.returncode
        assert records[0]['TEMP'] == records[-1]['TEMP'] == str(original_temp)
        assert records[0]['TMP'] == records[-1]['TMP'] == str(original_temp)
        destinations = [str(Path(item['pluginsDir']) / 'old-install' / relative) for item in children]
        if locked:
            assert target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected_hash
            recovery = subprocess.run([str(parent), '/S'], env=run_environment, timeout=40,
                                      capture_output=True)
            assert recovery.returncode == 0 and not target.exists()
        else:
            assert len(str(target)) == 222 and len(destinations[0]) > 260
            assert len(destinations[1]) < 260 and not target.exists(), destinations
        result['cases'].append({'name': name, 'passed': True, 'exitCode': ran.returncode,
                                'execResults': exits, 'sourcePath': str(target),
                                'sourceLength': len(str(target)), 'destinations': destinations,
                                'destinationLengths': [len(value) for value in destinations],
                                'lockRecoveryExit': 0 if locked else None,
                                'environmentRecords': records, 'script': str(root / 'upgrade.nsi')})
        (args.evidence_root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'PASS {name}', flush=True)

    atomic_case('atomic-longpath', locked=False)
    atomic_case('atomic-readlock', locked=True)
    subprocess.run([sys.executable, str(boundary.with_name('test-legacy-environment-faults-native.py')),
                    '--nsis-root', str(args.nsis_root), '--evidence-root', str(args.evidence_root / 'environment-faults'),
                    '--fixture-parent', str(args.fixture_parent)], check=True, timeout=180)
    fault_result = json.loads((args.evidence_root / 'environment-faults/result.json').read_text(encoding='utf-8'))
    assert fault_result['completed'] and not fault_result['productRiskConfirmed']
    result['environmentFaultCases'] = len(fault_result['cases'])
    result['environmentFaultEvidence'] = str(args.evidence_root / 'environment-faults/result.json')
    result['passed'] = True
    (args.evidence_root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Evidence: {args.evidence_root / "result.json"}', flush=True)


if __name__ == '__main__':
    main()
