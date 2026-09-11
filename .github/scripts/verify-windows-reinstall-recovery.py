"""Kill an unmodified signed NSIS installer during extraction, then reinstall.

Only runs on a disposable GitHub Windows runner. Never mutates installer bytes
or repairs the directory/registry on its behalf. Evidence remains on failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil


def run(argv, *, timeout=600):
    print('RUN', subprocess.list2cmdline([str(a) for a in argv]), flush=True)
    subprocess.run([str(a) for a in argv], check=True, timeout=timeout)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def registry(root):
    import winreg
    found = []
    for hive, hive_name in ((winreg.HKEY_CURRENT_USER, 'HKCU'), (winreg.HKEY_LOCAL_MACHINE, 'HKLM')):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            parent = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
            try:
                with winreg.OpenKey(hive, parent, 0, winreg.KEY_READ | view) as keys:
                    for i in range(winreg.QueryInfoKey(keys)[0]):
                        name = winreg.EnumKey(keys, i)
                        with winreg.OpenKey(keys, name) as key:
                            values = dict((n, v) for n, v, _ in (winreg.EnumValue(key, j) for j in range(winreg.QueryInfoKey(key)[1])))
                        command = values.get('UninstallString', '')
                        quoted = re.match(r'^"([^"]+)"', command)
                        if not quoted or Path(quoted[1]).parent.resolve() != root.resolve():
                            continue
                        # electron-builder stores InstallLocation separately
                        # under Software/<app GUID>, not in the Apps uninstall key.
                        install_key = 'Software\\' + name
                        try:
                            with winreg.OpenKey(hive, install_key, 0, winreg.KEY_READ | view) as key:
                                location = winreg.QueryValueEx(key, 'InstallLocation')[0]
                        except FileNotFoundError:
                            location = None
                        found.append({'hive': hive_name, 'view': view, 'key': parent + '\\' + name,
                                      'values': values, 'installKey': install_key, 'installLocation': location})
            except FileNotFoundError:
                pass
    return found


def profile_hashes(profile):
    return {str(p.relative_to(profile)): sha(p) for p in sorted(profile.rglob('*')) if p.is_file()}


def stop_tree(process):
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        # Suspend the installer before collecting evidence so extraction cannot
        # finish between the trigger and termination.
        parent.suspend()
        for child in children:
            try:
                child.suspend()
            except psutil.NoSuchProcess:
                pass
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
        psutil.wait_procs(children + [parent], timeout=15)
    except psutil.NoSuchProcess:
        pass


def interrupt(installer, install_args, root, evidence):
    temp = Path(os.environ['TEMP'])
    previous = {p.resolve() for p in temp.glob('ns*.tmp')}
    process = subprocess.Popen([str(installer), *install_args])
    uninstall_observed = []
    deadline = time.monotonic() + 300
    event = {'installerPid': process.pid, 'startedUtc': time.time()}
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f'Installer exited before an observed extraction interruption: {process.returncode}')
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            for child in children:
                try:
                    command = child.cmdline()
                    if any('/KEEP_APP_DATA' in arg for arg in command):
                        if not any(p['pid'] == child.pid for p in uninstall_observed):
                            uninstall_observed.append({'pid': child.pid, 'exe': child.exe(), 'command': command, 'utc': time.time()})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            extraction = []
            for directory in temp.glob('ns*.tmp'):
                if directory.resolve() in previous:
                    continue
                unpack = directory / '7z-out'
                if unpack.is_dir():
                    extraction.extend(str(p) for p in unpack.iterdir() if p.is_file())
            if uninstall_observed and extraction and not list(root.glob('Uninstall*.exe')):
                before = registry(root)
                if before:
                    time.sleep(0.05)
                    continue
                stop_tree(process)
                event.update({'oldUninstallerObserved': uninstall_observed, 'extractionFiles': extraction,
                              'interruptedUtc': time.time(), 'registryAtInterruption': before,
                              'uninstallerMissing': not bool(list(root.glob('Uninstall*.exe'))),
                              'appPresent': (root / 'OpenSquilla.exe').exists(),
                              'partialInstallFiles': [str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()]})
                (evidence / 'interruption.json').write_text(json.dumps(event, indent=2), encoding='utf-8')
                print('Observed old uninstall and active extraction; installer tree terminated.', flush=True)
                return event
            time.sleep(0.05)
        raise TimeoutError('Did not observe old uninstaller plus extracted files and missing uninstall entry')
    finally:
        if process.poll() is None:
            stop_tree(process)


def installed(root, version, installer, repo):
    app = root / 'OpenSquilla.exe'
    run(['pwsh', '-NoProfile', '-File', repo / '.github/scripts/verify-windows-signatures.ps1',
         '-InstallerPath', installer, '-InstalledRoot', root])
    raw = subprocess.check_output(['pwsh', '-NoProfile', '-Command',
        "([Diagnostics.FileVersionInfo]::GetVersionInfo($env:REINSTALL_APP)).ProductVersion"],
        env={**os.environ, 'REINSTALL_APP': str(app)}, text=True).strip()
    if raw not in (version, version + '.0'):
        raise AssertionError(f'Installed version {raw} != {version}')
    entries = registry(root)
    if not entries:
        raise AssertionError('Windows Apps uninstall registry entry missing')
    for entry in entries:
        values = entry['values']
        if not entry['installLocation'] or Path(entry['installLocation']).resolve() != root.resolve():
            raise AssertionError(f'InstallLocation registry mapping is missing or wrong: {entry}')
        if values.get('DisplayVersion') not in (version, version + '.0'):
            raise AssertionError(f'Wrong registry version: {values}')
        command = values['UninstallString']
        match = re.match(r'^"([^"]+)"', command)
        uninstaller = Path(match[1] if match else command)
        if uninstaller.parent.resolve() != root.resolve() or not uninstaller.is_file():
            raise AssertionError('UninstallString does not resolve to the restored uninstaller')
    return {'version': raw, 'registry': entries}


def main():
    parser = argparse.ArgumentParser()
    for name in ('baseline', 'candidate', 'next-installer', 'evidence'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--install-mode', choices=('default', 'custom'), required=True)
    args = parser.parse_args()
    if sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Requires a disposable GitHub Actions Windows runner')
    repo = Path(__file__).resolve().parents[2]
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    root = evidence / 'app' if args.install_mode == 'custom' else Path(os.environ['LOCALAPPDATA']) / 'Programs/OpenSquilla'
    if root.exists() or registry(root):
        raise RuntimeError(f'Requires fresh install root and registry: {root}')
    user_data = evidence / 'appdata/OpenSquilla'
    profile = user_data / 'opensquilla'
    external = evidence / 'external-sentinels'
    os.environ.update({'APPDATA': str(user_data.parent), 'LOCALAPPDATA': str(evidence / 'localappdata'),
                      'OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE': '1', 'OPENSQUILLA_RECOVERY_OFFLINE': '1'})
    install_args = ['/S'] + ([f'/D={root}'] if args.install_mode == 'custom' else [])
    report = {'ok': False, 'installMode': args.install_mode, 'scope': 'signed installer extraction interruption and manual reinstall',
              'installRoot': str(root), 'harnessSha': os.environ.get('GITHUB_SHA'), 'stages': {}}
    stages = report['stages']
    probe = repo / '.github/scripts/verify-release-profile-preservation.py'
    profile_args = ['--home', str(profile), '--label', 'reinstall-retained', '--external-root', str(external), '--baseline-version', '0.5.4']
    try:
        packages = {}
        for label, path in (('A', args.baseline), ('B', args.candidate), ('C', args.next_installer)):
            path = path.resolve()
            match = re.fullmatch(r'OpenSquilla-(\d+\.\d+\.\d+)-win-x64.exe', path.name)
            if not match:
                raise AssertionError(f'Noncanonical installer: {path}')
            run(['pwsh', '-NoProfile', '-File', repo / '.github/scripts/verify-windows-signatures.ps1', '-InstallerPath', path])
            packages[label] = {'path': str(path), 'version': match[1], 'sha256': sha(path)}
        report['packages'] = packages
        assert tuple(map(int, packages['A']['version'].split('.'))) < tuple(map(int, packages['B']['version'].split('.'))) < tuple(map(int, packages['C']['version'].split('.')))
        run([args.baseline, *install_args])
        stages['baseline'] = installed(root, packages['A']['version'], args.baseline, repo)
        run([sys.executable, probe, 'seed', *profile_args])
        before = profile_hashes(profile)
        stages['interruption'] = interrupt(args.candidate, install_args, root, evidence)
        run([sys.executable, probe, 'verify', *profile_args])
        assert before == profile_hashes(profile), 'Interrupted install changed profile bytes'
        stages['profileAfterInterruption'] = True
        # Deliberately no file deletion, registry fix, backup restoration or
        # custom recovery helper between interruption and rerunning this EXE.
        run([args.candidate, *install_args])
        stages['reinstalled'] = installed(root, packages['B']['version'], args.candidate, repo)
        run([sys.executable, probe, 'verify', *profile_args])
        assert before == profile_hashes(profile), 'Reinstallation changed profile bytes'
        stages['profileAfterReinstall'] = True
        for label, installer in (('B', args.candidate), ('C', args.next_installer)):
            if label == 'C':
                run([installer, *install_args])
                stages['subsequentUpgrade'] = installed(root, packages[label]['version'], installer, repo)
                run([sys.executable, probe, 'verify', *profile_args])
            # Retained-session rendering and isolation are checked through the
            # installed Electron executable, alongside database contents.
            run(['node', repo / 'desktop/electron/scripts/test-packaged-reinstall-retained.mjs',
                 '--executable', root / 'OpenSquilla.exe', '--user-data-dir', user_data,
                 '--output', evidence / f'{label}-launch.json'], timeout=600)
            run([sys.executable, probe, 'verify', *profile_args])
            stages[f'{label}LaunchAndRetainedData'] = True
        uninstaller = next(root.glob('Uninstall*.exe'))
        run([uninstaller, '/S'])
        deadline = time.monotonic() + 60
        while (root / 'OpenSquilla.exe').exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not (root / 'OpenSquilla.exe').exists(), 'Uninstaller did not remove executable'
        assert not registry(root), 'Uninstall registry still exists'
        run([sys.executable, probe, 'verify', *profile_args])
        stages['uninstallAndProfilePreservation'] = True
        report['ok'] = True
    except BaseException as error:
        report['error'] = str(error)
        raise
    finally:
        (evidence / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
