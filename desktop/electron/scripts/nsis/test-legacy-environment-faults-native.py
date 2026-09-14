"""Synthetic failure injection for NSIS environment error branches.

No OpenSquilla installer, client, profile or registration is executed or changed.
API failures are explicit replacements in a copied source, not real OS failures.
"""
import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--nsis-root', required=True, type=Path)
parser.add_argument('--evidence-root', required=True, type=Path)
parser.add_argument('--fixture-parent', type=Path, default=Path(r'C:\Temp'))
args = parser.parse_args()
if os.name != 'nt':
    parser.error('Native Windows is required.')
EVIDENCE = args.evidence_root.resolve()
EVIDENCE.mkdir(parents=True, exist_ok=True)
args.fixture_parent.mkdir(parents=True, exist_ok=True)
NSIS = args.nsis_root.resolve()
FIXTURE = Path(tempfile.mkdtemp(prefix='qef-', dir=args.fixture_parent))
SOURCE_PATH = Path(__file__).with_name('legacy-uninstaller-temp.nsh').resolve()
BASE = SOURCE_PATH.read_bytes()
(EVIDENCE / 'original-boundary.nsh').write_bytes(BASE)
BASE_TEXT = BASE.decode('utf-8').replace('\r\n', '\n')
ENV = dict(os.environ, NSISDIR=str(NSIS))
REPORT = {'scope': __doc__, 'sourcePath': str(SOURCE_PATH),
          'sourceSha256': hashlib.sha256(BASE).hexdigest(), 'fixtureRoot': str(FIXTURE), 'cases': []}


def quoted(value):
    return str(value).replace('$', '$$').replace('"', '$\\"')


def patch_once(source, before, after):
    assert source.count(before) == 1, before
    return source.replace(before, after)


def compile_script(path, source):
    path.write_text(source, encoding='utf-8-sig')
    ran = subprocess.run([str(NSIS / 'Bin/makensis.exe'), '-V2', str(path)], env=ENV,
                         capture_output=True, text=True, timeout=30)
    output = ran.stdout + ran.stderr
    path.with_suffix('.compile.log').write_text(output, encoding='utf-8')
    assert ran.returncode == 0 and not re.search(r'(?im)^warning\s+\d+:', output), output


capture = FIXTURE / 'capture.py'
capture.write_text('''import json, os, sys
from pathlib import Path
with Path(sys.argv[1]).open('a', encoding='utf-8') as stream:
    stream.write(json.dumps({'phase': sys.argv[2], 'TEMP': os.environ.get('TEMP'), 'TMP': os.environ.get('TMP')}, ensure_ascii=False) + '\\n')
''', encoding='utf-8')


def observe(log, phase):
    return f'''ExecWait '"{quoted(sys.executable)}" "{quoted(capture)}" "{quoted(log)}" "{phase}"' $9'''


old_child = FIXTURE / 'synthetic-old-uninstaller.exe'
finish = FIXTURE / 'synthetic-finish.exe'
old_child_observe = observe('$0\\environment.jsonl', 'old-child').replace('$$0', '$0')
compile_script(FIXTURE / 'old-child.nsi', f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
Name "Synthetic old-file removal for environment fault audit"
OutFile "{quoted(old_child)}"
Section
  InitPluginsDir
  ReadEnvStr $0 "OSQ1441_FIXTURE_CASE"
  FileOpen $1 "$0\\old-child.marker" w
  FileWrite $1 "Synthetic old uninstaller ran.$\\r$\\n"
  FileClose $1
  {old_child_observe}
  Delete "$0\\app\\old-files.sentinel"
  FileOpen $1 "$0\\old-removal-success.marker" w
  FileWrite $1 "Synthetic previous files removed; exiting 0.$\\r$\\n"
  FileClose $1
  SetErrorLevel 0
SectionEnd
''')
compile_script(FIXTURE / 'finish.nsi', f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
Name "Synthetic Finish marker for environment fault audit"
OutFile "{quoted(finish)}"
Section
  ReadEnvStr $0 "OSQ1441_FIXTURE_CASE"
  FileOpen $1 "$0\\finish.marker" w
  FileWrite $1 "Synthetic Finish launched.$\\r$\\n"
  FileClose $1
  SetErrorLevel 0
SectionEnd
''')

SET_TEMP = '''  System::Call 'kernel32::SetEnvironmentVariableW(w "TEMP", w r0) i.r1' '''.rstrip()
SET_TMP = '''  System::Call 'kernel32::SetEnvironmentVariableW(w "TMP", w r0) i.r1' '''.rstrip()
RESTORE_TEMP = '''    System::Call 'kernel32::SetEnvironmentVariableW(w "TEMP", p $osqLegacySavedTemp) i.r1' '''.rstrip()
RESTORE_TMP = '''    System::Call 'kernel32::SetEnvironmentVariableW(w "TMP", p $osqLegacySavedTmp) i.r1' '''.rstrip()
ALLOCATE = '    System::Alloc $3\n    Pop ${POINTER}'
SNAPSHOT = '''  System::Call 'kernel32::GetEnvironmentStringsW() p.r1' '''.rstrip()
PROCESS_ALLOCATE = "  System::Call 'kernel32::GlobalAlloc(i 0x40, p ${OSQ_LEGACY_STARTUP_SIZE}) p.r1'"
CREATE = "  System::Call 'kernel32::CreateProcessW(p 0, w r0, p 0, p 0, i 0, i 0x04000400, p $osqLegacyChildEnvironment, p 0, p r1, p r2) i.r5'"


def run_case(name, faults):
    root = FIXTURE / name
    root.mkdir()
    install = root / 'app'
    install.mkdir()
    old_file = install / 'old-files.sentinel'
    old_file.write_bytes(b'Synthetic old installed bytes only.')
    original_temp = root / ('original-temp-' + 't' * 40)
    original_tmp = root / ('different-original-tmp-' + 'm' * 40)
    original_temp.mkdir()
    original_tmp.mkdir()
    log = root / 'environment.jsonl'
    source = BASE_TEXT
    patches = []
    for fault in faults:
        if fault in ('save-temp', 'save-tmp'):
            env_name = 'TEMP' if fault == 'save-temp' else 'TMP'
            after = f'''    !if "${{NAME}}" == "{env_name}"
      StrCpy ${{POINTER}} 0 ; FIXTURE: pretend native allocation failed
    !else
{ALLOCATE}
    !endif'''
            source = patch_once(source, ALLOCATE, after)
            patches.append({'fault': fault, 'before': ALLOCATE, 'after': after})
        else:
            before = {'set-temp': SET_TEMP, 'set-tmp': SET_TMP,
                      'restore-temp': RESTORE_TEMP, 'restore-tmp': RESTORE_TMP,
                      'snapshot': SNAPSHOT, 'process-allocate': PROCESS_ALLOCATE}[fault]
            after = '    StrCpy $1 0 ; FIXTURE: skip API and return failure without changing its variable'
            source = patch_once(source, before, after)
            patches.append({'fault': fault, 'before': before, 'after': after})
    # These observers are fixture instrumentation immediately before the two
    # terminal exits. They do not change product error-branch control flow.
    source = patch_once(source,
                        '    DetailPrint "Unable to safely preserve the uninstaller environment."',
                        f'''    {observe(log, 'prepare-fatal')}
    DetailPrint "Unable to safely preserve the uninstaller environment."''')
    source = patch_once(source,
                        '    DetailPrint "Unable to restore the installer environment. Installation stopped."',
                        f'''    {observe(log, 'restore-fatal')}
    DetailPrint "Unable to restore the installer environment. Installation stopped."''')
    source = patch_once(source, CREATE, f'''  {observe(log, 'parent-before-old-launch')}
{CREATE}''')
    source = patch_once(source,
                        '    DetailPrint "Unable to prepare the previous uninstaller process."',
                        f'''    {observe(log, 'process-fatal')}
    DetailPrint "Unable to prepare the previous uninstaller process."''')
    copied_boundary = root / 'injected-boundary.nsh'
    copied_boundary.write_text(source, encoding='utf-8-sig')
    parent = root / 'synthetic-parent.exe'
    compile_script(root / 'parent.nsi', f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
Name "Synthetic injected NSIS environment failure"
OutFile "{quoted(parent)}"
Var installationDir
!include "{quoted(copied_boundary)}"
Section
  InitPluginsDir
  {observe(log, 'before')}
  StrCpy $installationDir "{quoted(install)}"
  StrCpy $0 "/currentuser --updated"
  StrCpy $R5 2
  StrCpy $R0 2
  ClearErrors
  !insertmacro OpenSquillaLegacyRetry
  !insertmacro OpenSquillaExecLegacyUninstaller "{quoted(old_child)}"
  FileOpen $7 "{quoted(root / 'new-files.marker')}" w
  FileWrite $7 "Synthetic new install reached.$\\r$\\n"
  FileClose $7
  {observe(log, 'after')}
  ExecWait '"{quoted(finish)}" /S' $9
  SetErrorLevel 0
SectionEnd
''')
    child_environment = dict(ENV, TEMP=str(original_temp), TMP=str(original_tmp),
                             OSQ1441_FIXTURE_CASE=str(root))
    ran = subprocess.run([str(parent), '/S'], env=child_environment,
                         capture_output=True, timeout=30)
    records = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    observed = {'oldChildStarted': (root / 'old-child.marker').exists(),
                'oldRemovalSucceeded': (root / 'old-removal-success.marker').exists(),
                'oldFilesRemain': old_file.exists(),
                'newInstallReached': (root / 'new-files.marker').exists(),
                'finishStarted': (root / 'finish.marker').exists()}
    restored = (records[-1]['TEMP'], records[-1]['TMP']) == (records[0]['TEMP'], records[0]['TMP'])
    expected_prior_child = not faults
    assert ran.returncode == (2 if faults else 0), (name, ran.returncode, records)
    assert observed['oldChildStarted'] == expected_prior_child, (name, observed)
    assert observed['oldRemovalSucceeded'] == expected_prior_child, (name, observed)
    assert observed['oldFilesRemain'] != expected_prior_child, (name, observed)
    assert observed['newInstallReached'] == (not faults), (name, observed)
    assert observed['finishStarted'] == (not faults), (name, observed)
    if not any(f.startswith('restore-') for f in faults):
        assert restored, (name, records)
    dangerous_gap = observed['oldRemovalSucceeded'] and not observed['newInstallReached']
    assert not dangerous_gap, (name, observed)
    if not faults:
        before_launch = next(item for item in records if item['phase'] == 'parent-before-old-launch')
        assert (before_launch['TEMP'], before_launch['TMP']) == (records[0]['TEMP'], records[0]['TMP'])
        child_record = next(item for item in records if item['phase'] == 'old-child')
        assert child_record['TEMP'] == child_record['TMP']
        assert Path(child_record['TEMP']).samefile(root)
    row = {'name': name, 'injectedFaults': faults, 'injectionPatches': patches,
           'instrumentation': 'Copied fixture source observes TEMP/TMP before terminal Quit and immediately before child creation; synthetic child also observes its environment.',
           'exitCode': ran.returncode, **observed, 'environmentRestoredAtLastObservation': restored,
           'oldRemovedButNewNotInstalled': dangerous_gap, 'environmentRecords': records,
           'script': str(root / 'parent.nsi'), 'injectedBoundary': str(copied_boundary),
           'injectedBoundarySha256': hashlib.sha256(copied_boundary.read_bytes()).hexdigest()}
    REPORT['cases'].append(row)
    (EVIDENCE / 'result.json').write_text(json.dumps(REPORT, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: row[k] for k in ('name', 'exitCode', 'oldChildStarted', 'oldFilesRemain',
                                         'newInstallReached', 'finishStarted', 'oldRemovedButNewNotInstalled')}), flush=True)


run_case('control', [])
run_case('save-temp-fail', ['save-temp'])
run_case('save-tmp-fail', ['save-tmp'])
run_case('set-temp-fail', ['set-temp'])
run_case('set-tmp-fail', ['set-tmp'])
run_case('restore-temp-fail', ['restore-temp'])
run_case('restore-tmp-fail', ['restore-tmp'])
run_case('half-set-restore-fail', ['set-tmp', 'restore-temp'])
run_case('snapshot-fail', ['snapshot'])
run_case('process-allocate-fail', ['process-allocate'])
REPORT['completed'] = True
REPORT['productRiskConfirmed'] = any(row['oldRemovedButNewNotInstalled'] for row in REPORT['cases'])
(EVIDENCE / 'result.json').write_text(json.dumps(REPORT, indent=2, ensure_ascii=False), encoding='utf-8')
print(f'Evidence: {EVIDENCE / "result.json"}', flush=True)
