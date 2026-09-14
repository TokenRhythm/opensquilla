#!/usr/bin/env python3
"""Accept a fixed NSIS candidate on an otherwise empty GitHub Windows runner.

This script deliberately installs/uninstalls real packages and may hold a file
open while upgrading. It refuses every non-GitHub-runner invocation. Each matrix
cell must get a fresh runner. Baseline and long-TEMP replacement must succeed;
a genuine read lock must fail without data loss and recover with the same B.
The installed B then runs the real retained-profile interaction/quit/restart
probe. Only synthetic profiles are used; no process is killed by name. This is
an unsigned-package regression gate, not a signing, UAC, or real-provider gate.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid

OFFICIAL_BASELINE_SHA256 = {
    '0.5.3': '0a5869c7cee68317b98ee05cc3b0decbddb321c0d6ef57418ccb0b15a164e562',
    '0.5.4': '15205b147b274f2260c86d2b7bf2091d5dfaa6e057d4e9217f1fa3580f714b19',
}

REPOSITORY = Path(__file__).resolve().parents[2]
PROFILE_HELPER = REPOSITORY / '.github/scripts/verify-release-profile-preservation.py'
INTERACTION_PROBE = REPOSITORY / 'desktop/electron/scripts/test-packaged-retained-interaction.mjs'


def environment_registry() -> dict:
    """Read only TEMP/TMP, never emit the runner's other environment values."""
    import winreg
    result = {}
    for label, hive, key_path in [
        ('user', winreg.HKEY_CURRENT_USER, 'Environment'),
        ('machine', winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
    ]:
        values = {}
        try:
            with winreg.OpenKey(hive, key_path) as key:
                for name in ('TEMP', 'TMP'):
                    try:
                        value, kind = winreg.QueryValueEx(key, name)
                        values[name] = {'value': value, 'kind': kind}
                    except FileNotFoundError:
                        values[name] = None
        except FileNotFoundError:
            pass
        result[label] = values
    return result


def profile_helper():
    spec = importlib.util.spec_from_file_location('nsis_regression_preservation', PROFILE_HELPER)
    require(spec is not None and spec.loader is not None, 'Missing preservation helper')
    module = importlib.util.module_from_spec(spec)
    # The helper's pinned 0.5.4 baseline loader is in the same scripts directory.
    sys.path.insert(0, str(PROFILE_HELPER.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class FILETIME(ctypes.Structure):
    _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]

    def ticks(self) -> int:
        return (self.high << 32) | self.low


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
        ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
        ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t), ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]


class Windows:
    def __init__(self) -> None:
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateToolhelp32Snapshot': ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            'Process32FirstW': ([wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
            'Process32NextW': ([wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'GetProcessTimes': ([wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)], wintypes.BOOL),
            'GetExitCodeProcess': ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'TerminateProcess': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'CreateFileW': ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            'CreateEventW': ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'ResetEvent': ([wintypes.HANDLE], wintypes.BOOL),
            'ReadDirectoryChangesW': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED), ctypes.c_void_p], wintypes.BOOL),
            'GetOverlappedResult': ([wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL], wintypes.BOOL),
            'CancelIoEx': ([wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)], wintypes.BOOL),
            'ReadProcessMemory': ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            method = getattr(self.k, name)
            method.argtypes = arguments
            method.restype = result
        self.u = ctypes.WinDLL('user32', use_last_error=True)
        self.window_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        ui_signatures = {
            'EnumWindows': ([self.window_callback, wintypes.LPARAM], wintypes.BOOL),
            'EnumChildWindows': ([wintypes.HWND, self.window_callback, wintypes.LPARAM], wintypes.BOOL),
            'GetWindowTextW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
            'GetDlgCtrlID': ([wintypes.HWND], ctypes.c_int),
            'PostMessageW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        }
        for name, (arguments, result) in ui_signatures.items():
            method = getattr(self.u, name)
            method.argtypes = arguments
            method.restype = result

    def temp_environment(self, process: dict) -> dict:
        """Observe only TEMP/TMP in an identity-matched child; never write memory.

        NtQueryInformationProcess selects the native or WOW64 PEB. The stable
        x86/x64 ProcessParameters offsets are intentionally audit-only: any
        unreadable/changed layout records a gap and cannot satisfy this gate.
        See Microsoft NtQueryInformationProcess and Crashpad's win/process_info
        implementation for native/WOW64 selection. No whole environment is
        persisted, including on errors.
        """
        require(ctypes.sizeof(ctypes.c_void_p) == 8, 'Environment observation requires x64 Python')
        handle = self.k.OpenProcess(0x0400 | 0x0010, False, process['pid'])
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            require(self.birth(handle) == process['birth'], 'Process identity changed before environment read')
            query = ctypes.WinDLL('ntdll').NtQueryInformationProcess
            query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p]
            query.restype = wintypes.LONG
            wow64_peb = ctypes.c_size_t()
            require(query(handle, 26, ctypes.byref(wow64_peb), ctypes.sizeof(wow64_peb), None) == 0, 'Cannot query child WOW64 identity')
            width = 4 if wow64_peb.value else 8
            if wow64_peb.value:
                peb = wow64_peb.value
            else:
                basic = (ctypes.c_size_t * 6)()
                require(query(handle, 0, basic, ctypes.sizeof(basic), None) == 0, 'Cannot query child PEB')
                peb = basic[1]

            def read(address: int, count: int) -> bytes:
                data = ctypes.create_string_buffer(count)
                received = ctypes.c_size_t()
                ok = self.k.ReadProcessMemory(handle, address, data, count, ctypes.byref(received))
                require(ok and received.value == count, 'Child environment memory unavailable')
                return data.raw

            def pointer(address: int) -> int:
                return int.from_bytes(read(address, width), 'little')

            parameters = pointer(peb + (0x10 if width == 4 else 0x20))
            address = pointer(parameters + (0x48 if width == 4 else 0x80))
            require(address != 0, 'Child environment is not initialized')
            payload = bytearray()
            # Read page-bounded pieces rather than assuming the allocation has
            # a large readable suffix. Only a complete double-NUL block passes.
            for _ in range(1024):
                count = min(4096 - (address % 4096), 4096)
                payload.extend(read(address, count))
                text = payload.decode('utf-16-le', errors='surrogatepass')
                end = text.find('\0\0')
                if end >= 0:
                    result = {'TEMP': None, 'TMP': None}
                    for entry in text[:end].split('\0'):
                        name, separator, value = entry.partition('=')
                        if separator and name.upper() in result:
                            result[name.upper()] = value
                    return result
                address += count
            raise RuntimeError('Child environment exceeded the bounded observation size')
        finally:
            self.k.CloseHandle(handle)

    def window_text(self, hwnd: int, class_name: bool = False) -> str:
        buffer = ctypes.create_unicode_buffer(4096)
        method = self.u.GetClassNameW if class_name else self.u.GetWindowTextW
        method(hwnd, buffer, len(buffer))
        return buffer.value

    def dialogs(self, active: list[dict]) -> list[dict]:
        owners = {item['pid']: item for item in active}
        result = []

        @self.window_callback
        def visit(hwnd: int, _: int) -> bool:
            pid = wintypes.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in owners or not self.u.IsWindowVisible(hwnd) or self.window_text(hwnd, True) != '#32770':
                return True
            controls = []

            @self.window_callback
            def child(control: int, _unused: int) -> bool:
                controls.append({'hwnd': int(control), 'id': self.u.GetDlgCtrlID(control), 'class': self.window_text(control, True), 'text': self.window_text(control)})
                return True

            self.u.EnumChildWindows(hwnd, child, 0)
            result.append({'hwnd': int(hwnd), 'pid': pid.value, 'birth': owners[pid.value]['birth'], 'title': self.window_text(hwnd), 'controls': controls})
            return True

        self.u.EnumWindows(visit, 0)
        return result

    def birth(self, handle: int) -> int | None:
        values = [FILETIME() for _ in range(4)]
        if not self.k.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            return None
        return values[0].ticks()

    def is_running_exact(self, process: dict) -> bool:
        """A read-only end-of-sample fence, including process creation time."""
        handle = self.k.OpenProcess(0x1000, False, process['pid'])
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return (self.birth(handle) == process['birth']
                    and bool(self.k.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259)
        finally:
            self.k.CloseHandle(handle)

    def processes(self) -> dict[int, dict]:
        snapshot = self.k.CreateToolhelp32Snapshot(2, 0)
        require(snapshot != ctypes.c_void_p(-1).value, 'Process snapshot failed')
        result: dict[int, dict] = {}
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            found = self.k.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                pid = int(entry.th32ProcessID)
                item = {'pid': pid, 'parentPid': int(entry.th32ParentProcessID), 'name': entry.szExeFile, 'image': None, 'birth': None}
                handle = self.k.OpenProcess(0x1000, False, pid)
                if handle:
                    try:
                        item['birth'] = self.birth(handle)
                        buffer = ctypes.create_unicode_buffer(32768)
                        length = wintypes.DWORD(len(buffer))
                        if self.k.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                            item['image'] = buffer.value
                    finally:
                        self.k.CloseHandle(handle)
                result[pid] = item
                found = self.k.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.k.CloseHandle(snapshot)
        return result

    def stop_exact(self, process: dict) -> dict:
        """Use an open process handle, checking birth before terminating it."""
        handle = self.k.OpenProcess(0x1001, False, process['pid'])
        if not handle:
            return {'pid': process['pid'], 'action': 'already-exited-or-inaccessible'}
        try:
            actual = self.birth(handle)
            if actual is None or actual != process['birth']:
                return {'pid': process['pid'], 'action': 'identity-mismatch-not-terminated'}
            ok = bool(self.k.TerminateProcess(handle, 125))
            return {'pid': process['pid'], 'birth': actual, 'action': 'terminated' if ok else 'terminate-failed', 'error': 0 if ok else ctypes.get_last_error()}
        finally:
            self.k.CloseHandle(handle)

    def lock_readable(self, path: Path) -> int:
        # Python.exe is outside the installation root. READ and WRITE sharing
        # allow the report's read probe; withholding DELETE prevents Rename.
        handle = self.k.CreateFileW(str(path), 0x80000000, 1 | 2, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle


class DirectoryEvents:
    """Arm the real TEMP-directory event subscription before spawning NSIS.

    A dedicated consumer immediately re-arms overlapped ReadDirectoryChangesW,
    retaining old-install creation even if it disappears between process polls.
    Only directory names are requested, avoiding the enormous file-copy stream.
    """

    def __init__(self, win: Windows, root: Path) -> None:
        self.win = win
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.io_lock = threading.Lock()
        self.events: list[dict] = []
        self.error: str | None = None
        self.started = time.monotonic()
        self.handle = win.k.CreateFileW(str(root), 1, 1 | 2 | 4, None, 3, 0x02000000 | 0x40000000, None)
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.event = win.k.CreateEventW(None, True, False, None)
        if not self.event:
            win.k.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())
        self.overlapped = OVERLAPPED()
        self.overlapped.hEvent = self.event
        self.buffer = ctypes.create_string_buffer(65536)
        try:
            self.arm()
        except Exception:
            win.k.CloseHandle(self.event)
            win.k.CloseHandle(self.handle)
            raise
        self.thread = threading.Thread(target=self.consume, name='nsis-temp-directory-events', daemon=True)
        self.thread.start()

    def arm(self) -> None:
        self.win.k.ResetEvent(self.event)
        self.overlapped.Internal = 0
        self.overlapped.InternalHigh = 0
        self.overlapped.Offset = 0
        self.overlapped.OffsetHigh = 0
        # FILE_NOTIFY_CHANGE_DIR_NAME only, recursively.
        ok = self.win.k.ReadDirectoryChangesW(self.handle, self.buffer, len(self.buffer), True, 2, None, ctypes.byref(self.overlapped), None)
        if not ok and ctypes.get_last_error() != 997:
            raise ctypes.WinError(ctypes.get_last_error())

    def consume(self) -> None:
        try:
            while True:
                size = wintypes.DWORD()
                ok = self.win.k.GetOverlappedResult(self.handle, ctypes.byref(self.overlapped), ctypes.byref(size), True)
                if not ok:
                    error = ctypes.get_last_error()
                    if self.closed.is_set() and error == 995:
                        return
                    raise ctypes.WinError(error)
                if size.value == 0:
                    raise RuntimeError('TEMP directory-change buffer overflow; actual-path evidence is incomplete')
                payload = self.buffer.raw[:size.value]
                offset = 0
                captured = []
                while True:
                    next_offset, action, name_length = struct.unpack_from('<III', payload, offset)
                    name = payload[offset + 12:offset + 12 + name_length].decode('utf-16-le')
                    parts = Path(name).parts
                    if len(parts) == 2 and parts[0].casefold().startswith('ns') and parts[0].casefold().endswith('.tmp') and parts[1].casefold() == 'old-install':
                        captured.append({'relativePath': name, 'action': action, 'observedSeconds': round(time.monotonic() - self.started, 3)})
                    if next_offset == 0:
                        break
                    offset += next_offset
                with self.lock:
                    self.events.extend(captured)
                with self.io_lock:
                    if self.closed.is_set():
                        return
                    self.arm()
        except Exception as error:
            if not self.closed.is_set():
                self.error = str(error)

    def drain(self) -> list[dict]:
        with self.lock:
            events, self.events = self.events, []
        return events

    def close(self) -> None:
        with self.io_lock:
            self.closed.set()
            self.win.k.CancelIoEx(self.handle, ctypes.byref(self.overlapped))
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            # Do not close handles still used by a running native read. The
            # process will release them, and the audit must report failure.
            self.error = 'TEMP directory observer did not stop after CancelIoEx'
            return
        self.win.k.CloseHandle(self.event)
        self.win.k.CloseHandle(self.handle)


def installed_registry() -> list[dict]:
    import winreg
    found: list[dict] = []
    uninstall = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    for hive_name, hive in [('HKCU', winreg.HKEY_CURRENT_USER), ('HKLM', winreg.HKEY_LOCAL_MACHINE)]:
        for view in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
            try:
                parent = winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view)
            except FileNotFoundError:
                continue
            with parent:
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    name = winreg.EnumKey(parent, index)
                    with winreg.OpenKey(parent, name) as key:
                        values = {}
                        for field in ['DisplayName', 'DisplayVersion', 'InstallLocation', 'UninstallString', 'QuietUninstallString', 'Publisher']:
                            try:
                                values[field] = winreg.QueryValueEx(key, field)[0]
                            except FileNotFoundError:
                                pass
                        if 'opensquilla' in str(values).casefold():
                            found.append({'hive': hive_name, 'view': view, 'key': uninstall + '\\' + name, 'values': values})
    return found


def version_info(path: Path) -> dict:
    """Read language-independent PE versions without starting PowerShell."""
    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            'dwSignature', 'dwStrucVersion', 'dwFileVersionMS', 'dwFileVersionLS',
            'dwProductVersionMS', 'dwProductVersionLS', 'dwFileFlagsMask',
            'dwFileFlags', 'dwFileOS', 'dwFileType', 'dwFileSubtype',
            'dwFileDateMS', 'dwFileDateLS',
        )]

    version = ctypes.WinDLL('version', use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL

    ignored = wintypes.DWORD()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        raise ctypes.WinError(ctypes.get_last_error())
    value = ctypes.c_void_p()
    length = wintypes.UINT()
    found = version.VerQueryValueW(data, '\\', ctypes.byref(value), ctypes.byref(length))
    require(found and value.value and length.value >= ctypes.sizeof(VS_FIXEDFILEINFO), f'Missing fixed PE version information: {path}')
    fixed = ctypes.cast(value, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    require(fixed.dwSignature == 0xFEEF04BD, f'Invalid fixed PE version signature: {path}')

    def dotted(ms: int, ls: int) -> str:
        return '.'.join(str(part) for part in (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF))

    return {
        'fileVersion': dotted(fixed.dwFileVersionMS, fixed.dwFileVersionLS),
        'productVersion': dotted(fixed.dwProductVersionMS, fixed.dwProductVersionLS),
    }


def version_matches(actual: str, expected: str) -> bool:
    return actual in {expected, expected + '.0'}


def manifest(root: Path) -> dict[str, dict]:
    values = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            values[path.relative_to(root).as_posix()] = {'size': path.stat().st_size, 'sha256': digest(path)}
    return values


def manifest_changes(before: dict, after: dict) -> dict:
    return {
        'removed': sorted(before.keys() - after.keys()),
        'added': sorted(after.keys() - before.keys()),
        'changed': sorted(key for key in before.keys() & after.keys() if before[key] != after[key]),
    }


class Audit:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.win = Windows()
        self.evidence = Path(args.evidence_root).resolve()
        self.evidence.mkdir(parents=True, exist_ok=True)
        require(not (self.evidence / 'result.json').exists(), 'Evidence result already exists; use a new evidence-root')
        self.root = Path('C:/') / ('o1441-' + uuid.uuid4().hex[:8])
        require(not self.root.exists(), 'Unique synthetic installation root collision')
        self.root.mkdir()
        self.install = (Path(os.environ['LOCALAPPDATA']) / 'Programs' / 'OpenSquilla'
                        if args.install_path == 'default' else self.root / 'Custom Apps' / 'OpenSquilla')
        require(not self.install.exists(), 'Installation root already exists; fresh runner required')
        self.install.parent.mkdir(parents=True, exist_ok=True)
        self.normal_temp = self.root / 'normal-temp-with-a-long-path-for-nsis'
        while len(str(self.normal_temp)) < len(str(self.install.parent)) + 40:
            self.normal_temp /= 'long-temp-component'
        self.short_temp = self.root / 't'
        self.normal_temp.mkdir(parents=True)
        self.short_temp.mkdir()
        self.user_data = self.root / 'retained-user-data'
        self.profile = self.user_data / 'opensquilla'
        self.external = self.root / 'external-sentinels'
        self.seed_label = self.root.name
        self.profile_before: dict = {}
        self.runtime_started = False
        self.environment_before = environment_registry()
        self.lock: int | None = None
        self.fault_relative: Path | None = None
        self.sentinels: dict[str, str] = {}
        self.report = {
            'schemaVersion': 1, 'ok': False, 'stage': 'created', 'case': args.case,
            'baselineVersion': args.baseline_version, 'candidateVersion': args.candidate_version,
            'installPathMode': args.install_path, 'candidateSourceSha': args.candidate_source_sha,
            'taskRoot': str(self.root), 'installRoot': str(self.install),
            'normalChildTemp': str(self.normal_temp), 'shortChildTemp': str(self.short_temp),
            'runnerOs': os.environ.get('RUNNER_OS'), 'runnerTemp': os.environ['RUNNER_TEMP'],
            'githubRunId': os.environ.get('GITHUB_RUN_ID'), 'githubSha': os.environ.get('GITHUB_SHA'),
            'environmentRegistryBefore': self.environment_before,
            'proofs': {'fixedUpgrade': False, 'readLockFailedWithoutDataLoss': None,
                       'sameCandidateRecovery': None, 'longPathFirstAttempt': None,
                       'legacyUninstallerTempIsolated': None,
                       'realClientStarted': False, 'firstSend': False, 'toolRead': False,
                       'stop': False, 'restart': False, 'normalQuit': False,
                       'persistentTempEnvironmentUnchanged': False, 'finalUninstall': False},
            'limitations': {'officialBaselineInstalledButNotLaunched': True, 'syntheticRuntimeReadyProfile': True,
                            'unsignedCandidate': True, 'authenticodeTrustTested': False,
                            'interactiveFinishAutoLaunchTested': False, 'windows10And11Tested': False,
                            'realProviderTested': False},
            'operations': [], 'scope': 'Real official-A to fixed-B NSIS replacement, retained synthetic historical SQLite/profile, installed B UI and Gateway send/tool/Stop/normal Quit/restart with a loopback provider. A profile is a declared fixture, not evidence of A runtime behavior. No signing-trust, UAC, real-provider, or Windows 10/11 acceptance claim.',
        }
        self.save()

    def save(self) -> None:
        write_json(self.evidence / 'result.json', self.report)

    def product_processes(self) -> list[dict]:
        return [item for item in self.win.processes().values() if item['name'].casefold() in {'opensquilla.exe', 'opensquilla-gateway.exe'}]

    def require_no_product_processes(self, stage: str) -> None:
        found = self.product_processes()
        self.report[stage + 'ProductProcesses'] = found
        self.save()
        require(not found, f'Unexpected OpenSquilla/Gateway processes before {stage}; refusing name-based termination')

    def check_profiles(self, stage: str) -> None:
        actual = {name: digest(Path(name)) if Path(name).is_file() else None for name in self.sentinels}
        self.report[stage + 'ProfileHashes'] = actual
        self.save()
        require(actual == self.sentinels, f'Synthetic profile bytes changed during {stage}')
        if self.profile_before and not self.runtime_started:
            changes = manifest_changes(self.profile_before, manifest(self.user_data))
            self.report[stage + 'RetainedProfileChanges'] = changes
            self.save()
            require(not any(changes.values()), f'Complete synthetic retained profile changed during {stage}')

    def installer_arguments(self) -> list[str]:
        # Omitting /D is essential: the default-path cell tests NSIS selection.
        result = ['/S', '/currentuser']
        if self.args.install_path == 'custom':
            result.append('/D=' + str(self.install))
        return result

    def check_environment_registry(self, stage: str) -> None:
        actual = environment_registry()
        self.report[stage + 'EnvironmentRegistry'] = actual
        self.save()
        require(actual == self.environment_before, f'Installer leaked TEMP/TMP into persistent environment during {stage}')

    def run(self, label: str, executable: Path, arguments: list[str], temp: Path) -> dict:
        require(executable.is_file(), f'Missing executable: {executable}')
        require(within(temp, self.root), 'Child TEMP must be inside this audit task root')
        operation = {'label': label, 'executable': str(executable), 'arguments': arguments, 'childTemp': str(temp), 'processes': [], 'pluginDirectories': [], 'directoryEvents': [], 'dialogs': [], 'timedOut': False, 'tempEnvironmentSamples': [], 'tempEnvironmentErrors': [], 'oldUninstallerEnvironmentPairs': []}
        self.report['operations'].append(operation)
        self.report['stage'] = label
        self.save()
        print(json.dumps({'stage': label, 'event': 'start', 'childTemp': str(temp)}), flush=True)
        watch_roots = list(dict.fromkeys([temp, self.install.parent]))
        existing_temp = {root: {entry.name for entry in root.iterdir()} for root in watch_roots}
        known: dict[tuple[int, int], dict] = {}
        held_process_handles: dict[tuple[int, int], int] = {}
        observed_dirs: dict[str, dict] = {}
        observed_dialogs: dict[tuple[int, int], dict] = {}
        environment_pairs: dict[tuple, dict] = {}
        started = time.monotonic()
        watchers = []
        try:
            for root in watch_roots:
                watchers.append((root, DirectoryEvents(self.win, root)))
        except Exception:
            for _root, watcher in watchers:
                watcher.close()
            raise

        def close_watchers() -> None:
            for _root, watcher in watchers:
                watcher.close()

        def consume_directory_events() -> None:
            for root, watcher in watchers:
                for event in watcher.drain():
                    operation['directoryEvents'].append(dict(event, watchRoot=str(root)))
                    directory = root / Path(event['relativePath']).parts[0]
                    key = str(directory)
                    record = observed_dirs.setdefault(key, {'path': key, 'firstObservedSeconds': event['observedSeconds'], 'oldInstallObserved': False, 'oldUninstallerObserved': False})
                    record['lastObservedSeconds'] = event['observedSeconds']
                    if event['action'] in {1, 5}:  # ADDED or RENAMED_NEW_NAME
                        record['oldInstallObserved'] = True
                        record['oldInstallCreationEventObserved'] = True
                    if self.fault_relative is not None:
                        destination = directory / 'old-install' / self.fault_relative
                        record['faultDestination'] = str(destination)
                        record['faultDestinationLength'] = len(str(destination))

        stdout_file = (self.evidence / (label + '-stdout.log')).open('wb')
        stderr_file = (self.evidence / (label + '-stderr.log')).open('wb')
        try:
            child = subprocess.Popen([str(executable), *arguments], env=dict(os.environ, TEMP=str(temp), TMP=str(temp)), stdout=stdout_file, stderr=stderr_file, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            close_watchers()
            stdout_file.close()
            stderr_file.close()
            raise
        birth = self.win.birth(int(child._handle))
        if birth is None:
            # Popen's live handle identifies the exact child even when birth
            # lookup fails; no PID lookup or name-based termination occurs.
            self.win.k.TerminateProcess(int(child._handle), 125)
            child.wait(timeout=15)
            close_watchers()
            stdout_file.close()
            stderr_file.close()
            raise RuntimeError('Cannot determine installer process identity')
        root_process = {'pid': child.pid, 'parentPid': os.getpid(), 'name': executable.name, 'image': str(executable), 'birth': birth, 'depth': 0}
        known[(child.pid, birth)] = root_process
        quiet_since: float | None = None
        reported_root_exit_wait = False
        completed = False
        try:
            while True:
                now = time.monotonic()
                consume_directory_events()
                snapshot = self.win.processes()
                # Record descendants only when the parent's live identity
                # matches. Persistent identity is retained after observed exit.
                changed = True
                while changed:
                    changed = False
                    for item in snapshot.values():
                        if item['birth'] is None or (item['pid'], item['birth']) in known:
                            continue
                        # Toolhelp retains a child's numeric parent PID after
                        # that parent exits. A newer process may reuse the PID;
                        # the older child then cannot belong to that new parent.
                        parents = [parent for key, parent in known.items() if parent['pid'] == item['parentPid'] and parent['birth'] <= item['birth'] and key == (item['parentPid'], snapshot.get(item['parentPid'], {}).get('birth'))]
                        if parents:
                            value = dict(item, depth=max(parent['depth'] for parent in parents) + 1)
                            known[(item['pid'], item['birth'])] = value
                            changed = True
                for key, item in known.items():
                    if key not in held_process_handles and key == (item['pid'], snapshot.get(item['pid'], {}).get('birth')):
                        handle = self.win.k.OpenProcess(0x1000, False, item['pid'])
                        if handle:
                            if self.win.birth(handle) == item['birth']:
                                held_process_handles[key] = handle
                            else:
                                self.win.k.CloseHandle(handle)
                    handle = held_process_handles.get(key)
                    if handle:
                        exit_code = wintypes.DWORD()
                        if self.win.k.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != 259:
                            item['observedExitCode'] = exit_code.value
                for root in watch_roots:
                    for directory in root.iterdir():
                        if directory.name in existing_temp[root] or not directory.name.casefold().startswith('ns') or not directory.name.casefold().endswith('.tmp') or not directory.is_dir():
                            continue
                        key = str(directory)
                        record = observed_dirs.setdefault(key, {'path': key, 'firstObservedSeconds': round(now - started, 3), 'oldInstallObserved': False, 'oldUninstallerObserved': False})
                        record['lastObservedSeconds'] = round(now - started, 3)
                        record['oldInstallObserved'] |= (directory / 'old-install').is_dir()
                        record['oldUninstallerObserved'] |= (directory / 'old-uninstaller.exe').is_file()
                        if self.fault_relative is not None:
                            destination = directory / 'old-install' / self.fault_relative
                            record['faultDestination'] = str(destination)
                            record['faultDestinationLength'] = len(str(destination))
                active = [item for key, item in known.items() if key == (item['pid'], snapshot.get(item['pid'], {}).get('birth'))]
                current_environments: dict[tuple[int, int], dict] = {}
                for item in active:
                    # Only our recorded NSIS/probe tree is inspected. Save the
                    # first sample and value transitions, not every poll.
                    try:
                        values = self.win.temp_environment(item)
                        current_environments[(item['pid'], item['birth'])] = values
                        if item.get('lastTempEnvironment') != values:
                            operation['tempEnvironmentSamples'].append({'pid': item['pid'], 'birth': item['birth'], 'image': item['image'], 'observedSeconds': round(now - started, 3), **values})
                            item['lastTempEnvironment'] = values
                    except (OSError, RuntimeError, UnicodeError) as error:
                        if not item.get('environmentReadErrorRecorded'):
                            operation['tempEnvironmentErrors'].append({'pid': item['pid'], 'birth': item['birth'], 'error': str(error)})
                            item['environmentReadErrorRecorded'] = True
                for item in active:
                    if not item['image'] or Path(item['image']).name.casefold() != 'old-uninstaller.exe':
                        continue
                    parent = next((value for value in active if value['pid'] == item['parentPid'] and value['birth'] <= item['birth']), None)
                    if parent is None:
                        continue
                    child_values = current_environments.get((item['pid'], item['birth']))
                    parent_values = current_environments.get((parent['pid'], parent['birth']))
                    if child_values is None or parent_values is None:
                        continue
                    # Fence after both reads. A child that has already exited
                    # must not be paired with its parent preparing a later
                    # attempt. This neither opens nor terminates by image name.
                    if not self.win.is_running_exact(item):
                        continue
                    key = (item['pid'], item['birth'], parent['pid'], parent['birth'],
                           child_values['TEMP'], child_values['TMP'], parent_values['TEMP'], parent_values['TMP'])
                    seconds = round(time.monotonic() - started, 3)
                    pair = environment_pairs.setdefault(key, {
                        'childPid': item['pid'], 'childBirth': item['birth'], 'childImage': item['image'],
                        'parentPid': parent['pid'], 'parentBirth': parent['birth'], 'parentImage': parent['image'],
                        'childEnvironment': child_values, 'parentEnvironment': parent_values,
                        'childStillRunningAfterBothReads': True, 'observedSeconds': seconds, 'observations': 0,
                    })
                    pair['lastObservedSeconds'] = seconds
                    pair['observations'] += 1
                if child.poll() is not None and any(item['pid'] != child.pid for item in active) and not reported_root_exit_wait:
                    print(json.dumps({'stage': label, 'event': 'root-exited-awaiting-descendants', 'exitCode': child.returncode, 'active': active}), flush=True)
                    reported_root_exit_wait = True
                for dialog in self.win.dialogs(active):
                    key = (dialog['hwnd'], dialog['birth'])
                    record = observed_dialogs.setdefault(key, dict(dialog, firstObservedSeconds=round(now - started, 3), autoAcknowledged=False))
                    record['lastObservedSeconds'] = round(now - started, 3)
                    ok_buttons = [control for control in dialog['controls'] if control['class'] == 'Button' and control['text'].replace('&', '').strip().casefold() == 'ok' and control['id'] > 0]
                    # Acknowledge only this exact expected error, on an observed
                    # installer descendant's own dialog. Other windows are read
                    # and recorded; no arbitrary dialog receives an action.
                    expected_error = any('Failed to uninstall old application files.' in control['text'] and re.search(r':\s*2\s*$', control['text']) is not None for control in dialog['controls'])
                    if expected_error and label.startswith('candidate-'):
                        operation['unexpectedSilentUninstallDialog'] = record
                        raise RuntimeError('Fixed candidate /S displayed an uninstall-failed dialog; the regression gate never acknowledges it')
                    if expected_error and len(ok_buttons) == 1 and not record['autoAcknowledged'] and now - started - record['firstObservedSeconds'] >= 1:
                        record['acknowledgmentReason'] = 'Expected uninstall-failed code 2 dialog blocks silent installer before SetErrorLevel 2; explicitly invoking the observed OK control on this owned dialog'
                        record['acknowledgedControlId'] = ok_buttons[0]['id']
                        record['autoAcknowledged'] = bool(self.win.u.PostMessageW(dialog['hwnd'], 0x111, ok_buttons[0]['id'], ok_buttons[0]['hwnd']))
                        record['acknowledgedSeconds'] = round(now - started, 3)
                if child.poll() is not None and not active:
                    quiet_since = quiet_since or now
                    if now - quiet_since >= 2:
                        completed = True
                        break
                else:
                    quiet_since = None
                if now - started > self.args.timeout_seconds:
                    operation['timedOut'] = True
                    print(json.dumps({'stage': label, 'event': 'timeout-active-processes', 'active': active}), flush=True)
                    operation['processes'] = list(known.values())
                    operation['dialogs'] = list(observed_dialogs.values())
                    self.save()
                    # No taskkill /IM or process-name kill. Every handle is
                    # compared against its recorded process-start identity.
                    operation['termination'] = [self.win.stop_exact(item) for item in sorted(active, key=lambda item: item['depth'], reverse=True)]
                    break
                time.sleep(0.25)
        finally:
            if not completed:
                current = self.win.processes()
                remaining = [item for key, item in known.items() if key == (item['pid'], current.get(item['pid'], {}).get('birth'))]
                operation.setdefault('termination', []).extend(self.win.stop_exact(item) for item in sorted(remaining, key=lambda item: item['depth'], reverse=True))
            if child.poll() is None:
                operation.setdefault('termination', []).append(self.win.stop_exact(root_process))
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                operation['rootStillRunning'] = True
            stdout_file.close()
            stderr_file.close()
            close_watchers()
            consume_directory_events()
            operation['directoryObserverError'] = [dict(root=str(root), error=watcher.error) for root, watcher in watchers if watcher.error]
            operation['exitCode'] = child.returncode
            operation['elapsedSeconds'] = round(time.monotonic() - started, 3)
            operation['processes'] = list(known.values())
            operation['pluginDirectories'] = list(observed_dirs.values())
            operation['dialogs'] = list(observed_dialogs.values())
            operation['oldUninstallerEnvironmentPairs'] = list(environment_pairs.values())
            for key, handle in held_process_handles.items():
                exit_code = wintypes.DWORD()
                if self.win.k.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != 259:
                    known[key]['observedExitCode'] = exit_code.value
                self.win.k.CloseHandle(handle)
            self.save()
            print(json.dumps({'stage': label, 'event': 'finished', 'exitCode': child.returncode, 'timedOut': operation['timedOut'], 'elapsedSeconds': operation['elapsedSeconds'], 'observedProcesses': len(known), 'observedPluginDirectories': len(observed_dirs), 'acknowledgedDialogs': sum(bool(item['autoAcknowledged']) for item in observed_dialogs.values())}), flush=True)
        require(not operation['timedOut'], f'{label} timed out; this is not an observed exit-code-2 reproduction')
        require(not operation['directoryObserverError'], f'{label} TEMP observer failed: {operation["directoryObserverError"]}')
        require(not operation.get('rootStillRunning'), f'{label} installer process could not be stopped')
        return operation

    def state(self, label: str, full: bool = False) -> dict:
        app = self.install / 'OpenSquilla.exe'
        asar = self.install / 'resources' / 'app.asar'
        result = {'registry': installed_registry(), 'appExists': app.is_file(), 'asarExists': asar.is_file()}
        if app.is_file():
            result['version'] = version_info(app)
            result['executableSha256'] = digest(app)
        if asar.is_file():
            result['asarSha256'] = digest(asar)
        if full:
            files = manifest(self.install)
            path = self.evidence / (label + '-file-manifest.json')
            write_json(path, files)
            result['fileManifestPath'] = str(path)
            result['fileCount'] = len(files)
        self.report[label] = result
        self.save()
        return result

    def assert_version(self, state: dict, version: str) -> None:
        require(state['appExists'] and state['asarExists'], 'Installed executable or app.asar is missing')
        require(version_matches(state['version']['productVersion'], version), f'Unexpected installed ProductVersion: {state.get("version")}')
        matching = [record for record in state['registry'] if str(record['values'].get('DisplayName', '')).casefold().startswith('opensquilla')]
        require(matching, 'No OpenSquilla uninstall registration was published')
        for record in matching:
            values = record['values']
            require(version_matches(str(values.get('DisplayVersion', '')), version), f'Unexpected registered version: {values}')
            uninstall = str(values.get('UninstallString', ''))
            require(str(self.install).casefold() in uninstall.casefold(), f'Registered uninstaller points outside fixture: {uninstall}')

    def seed_retained_profile(self) -> None:
        helper = profile_helper()
        sys.path.insert(0, str(PROFILE_HELPER.parent))
        try:
            helper.seed_profile(self.profile, self.seed_label, external_root=self.external,
                                signed_retained=True, baseline_version=self.args.baseline_version)
        finally:
            sys.path.pop(0)
        # This is declared fixture construction before A installation, never a
        # rewrite of the upgraded profile. The retained probe pins config bytes
        # through both B launches, so seed the exact runtime-ready format that
        # its shared preservation helper already accepts.
        (self.profile / 'config.toml').write_text(
            helper._runtime_config_text(self.profile, signed_retained=True), encoding='utf-8', newline='')
        credential = {
            'provider': 'ollama', 'model': 'opensquilla-release-session-recovery-smoke',
            'baseUrl': 'http://127.0.0.1:11434', 'apiKeyEnv': '', 'encryptedApiKey': '',
            'modelRoutingMode': 'direct', 'routerMode': 'disabled', 'routerDefaultTier': 'c1',
            'routerTiers': {}, 'searchProvider': 'duckduckgo', 'searchApiKeyEnv': '',
            'encryptedSearchApiKey': '', 'encryption': 'plain', 'disableNetworkObservability': False,
            'createdAt': '2026-09-14T00:00:00.000Z', 'updatedAt': '2026-09-14T00:00:00.000Z',
        }
        write_json(self.user_data / 'desktop-credential.json', credential)
        self.profile_before = manifest(self.user_data)
        write_json(self.evidence / 'retained-profile-before-install-manifest.json', self.profile_before)
        self.report['retainedProfileFixture'] = {
            'userDataDir': str(self.user_data), 'home': str(self.profile), 'label': self.seed_label,
            'externalSentinelsDir': str(self.external), 'baselineSchemaVersion': self.args.baseline_version,
            'configuration': 'Known runtime-ready synthetic configuration constructed before installing A',
            'manifestPath': str(self.evidence / 'retained-profile-before-install-manifest.json'),
            'credentialSha256': digest(self.user_data / 'desktop-credential.json'),
            'configSha256': digest(self.profile / 'config.toml'),
        }
        self.verify_retained_profile('seeded')
        self.save()

    def verify_retained_profile(self, stage: str) -> None:
        helper = profile_helper()
        helper.verify_profile(self.profile, self.seed_label, runtime_migrated=True,
                              signed_retained=True, external_root=self.external)
        fixture = self.report['retainedProfileFixture']
        require(digest(self.user_data / 'desktop-credential.json') == fixture['credentialSha256'], 'Retained credential bytes changed')
        require(digest(self.profile / 'config.toml') == fixture['configSha256'], 'Retained configuration bytes changed')
        self.report.setdefault('retainedProfileVerifiedStages', []).append(stage)
        self.save()

    def assert_scoped_environment(self, operation: dict, redirected: bool = False) -> None:
        expected = str(Path(operation['childTemp']).resolve()).casefold()
        samples = operation['tempEnvironmentSamples']
        require(samples, 'No owned-process TEMP/TMP evidence was observed')

        def original(item: dict) -> bool:
            return all(isinstance(item[name], str) and str(Path(item[name]).resolve()).casefold() == expected for name in ('TEMP', 'TMP'))

        def short_parent(item: dict) -> bool:
            # Path.resolve expands 8.3 aliases on Windows, so aliases and long
            # paths must denote the same existing installation parent.
            return all(isinstance(item[name], str) and Path(item[name]).resolve() == self.install.parent.resolve() for name in ('TEMP', 'TMP'))

        if redirected:
            pairs = [pair for pair in operation['oldUninstallerEnvironmentPairs']
                     if short_parent(pair['childEnvironment']) and not original(pair['childEnvironment'])]
            require(pairs, 'No running old-uninstaller with its custom short TEMP/TMP and direct parent was observed')
            for pair in pairs:
                require(pair['childStillRunningAfterBothReads'] is True, 'Old-uninstaller exited before the paired environment observation completed')
                require(original(pair['parentEnvironment']), 'Direct NSIS parent TEMP/TMP differed from original while the old-uninstaller child was still running')
                require(Path(pair['parentImage']).resolve() == Path(operation['executable']).resolve(), 'Short-TEMP child was not directly owned by this candidate installer')
                child = next((item for item in operation['processes'] if item['pid'] == pair['childPid'] and item['birth'] == pair['childBirth']), None)
                parent = next((item for item in operation['processes'] if item['pid'] == pair['parentPid'] and item['birth'] == pair['parentBirth']), None)
                require(child and parent and child['parentPid'] == parent['pid'] and child['birth'] >= parent['birth'], 'Paired environment evidence lacks the direct parent identity and birth-order fence')
            operation['shortChildWithOriginalParentEnvironmentObserved'] = pairs
            self.report['proofs']['legacyUninstallerTempIsolated'] = True
        else:
            require(any(original(item) for item in samples), 'The original child TEMP/TMP was not observed')
        self.check_environment_registry(operation['label'])
        self.save()

    def run_retained_interaction(self, temp: Path) -> None:
        fixture = self.report['retainedProfileFixture']
        marker = {
            'schemaVersion': 1, 'purpose': 'opensquilla-synthetic-signed-update-audit',
            'auditId': uuid.uuid4().hex, 'seedLabel': self.seed_label,
            'userDataDir': str(self.user_data), 'executablePath': str(self.install / 'OpenSquilla.exe'),
            'expectedVersion': self.args.candidate_version, 'sourceSha': self.args.candidate_source_sha,
            'executableSha256': self.args.candidate_executable_sha256.lower(),
            'credentialSha256': fixture['credentialSha256'], 'configSha256': fixture['configSha256'],
            'externalSentinelsDir': str(self.external),
        }
        marker_path = self.user_data / 'retained-interaction-audit.json'
        require(not marker_path.exists(), 'Retained interaction marker already exists')
        write_json(marker_path, marker)
        self.runtime_started = True
        output = self.evidence / 'retained-interaction'
        operation = self.run('retained-interaction', Path(self.args.node).resolve(), [
            str(INTERACTION_PROBE), '--audit-manifest', str(marker_path), '--output-dir', str(output),
            '--python', sys.executable,
        ], temp)
        require(operation['exitCode'] == 0, f'Installed B retained interaction probe failed: {operation["exitCode"]}')
        result_path = output / 'report.json'
        result = json.loads(result_path.read_text(encoding='utf-8-sig'))
        require(result.get('ok') is True and result.get('status') == 'passed', 'Retained interaction report did not pass')
        for field in ('auditId', 'sourceSha', 'executableSha256', 'credentialSha256', 'configSha256'):
            require(result.get(field) == marker[field], f'Retained interaction report mismatched {field}')
        for proof in ('credentialPreserved', 'configPreserved', 'oldSessionsVerified', 'oldSessionsUiVerified',
                      'firstSendVerified', 'toolReadVerified', 'stopVerified', 'restartVerified', 'normalQuitVerified'):
            require(result.get(proof) is True, f'Retained interaction report lacks {proof}')
        client_samples = [item for item in operation['tempEnvironmentSamples'] if item['image'] and Path(item['image']).name.casefold() in {'opensquilla.exe', 'opensquilla-gateway.exe'}]
        require(client_samples, 'No actual installed-client TEMP/TMP evidence was captured')
        for item in client_samples:
            require(all(item[name] is not None and Path(item[name]).resolve() == temp.resolve() for name in ('TEMP', 'TMP')),
                    'Installed client/Gateway inherited an unexpected temporary directory')
        self.report['retainedInteraction'] = {'reportPath': str(result_path), 'reportSha256': digest(result_path),
                                            'auditId': marker['auditId'], 'ok': True, 'clientTempEnvironmentSamples': client_samples}
        self.report['proofs'].update(realClientStarted=True, firstSend=True, toolRead=True,
                                     stop=True, restart=True, normalQuit=True)
        self.require_no_product_processes('afterRetainedInteraction')
        self.verify_retained_profile('afterRetainedInteraction')
        self.check_environment_registry('afterRetainedInteraction')
        self.save()

    def execute(self) -> None:
        args = self.args
        baseline = Path(args.baseline_installer).resolve()
        candidate = Path(args.candidate_installer).resolve()
        require(baseline.is_file() and candidate.is_file(), 'Both complete installer files must exist')
        require(not installed_registry(), 'Preexisting OpenSquilla registration: use a fresh runner')
        require(not within(Path(sys.executable), self.install), 'Lock-holder Python must be outside the installation directory')
        self.require_no_product_processes('initial')
        self.report['inputs'] = {
            'baselineInstaller': str(baseline), 'baselineInstallerSha256': digest(baseline),
            'candidateInstaller': str(candidate), 'candidateInstallerSha256': digest(candidate),
            'expectedCandidateAsarSha256': args.candidate_asar_sha256,
            'expectedCandidateExecutableSha256': args.candidate_executable_sha256,
            'expectedCandidateInstallerSha256': args.candidate_installer_sha256,
            'candidateSourceSha': args.candidate_source_sha,
        }
        require(args.baseline_version in OFFICIAL_BASELINE_SHA256, 'Only pinned official 0.5.3 and 0.5.4 baselines are accepted')
        require(self.report['inputs']['baselineInstallerSha256'] == OFFICIAL_BASELINE_SHA256[args.baseline_version], 'Baseline bytes do not match the pinned official GitHub asset')
        require(self.report['inputs']['candidateInstallerSha256'] == args.candidate_installer_sha256.lower(), 'Candidate installer differs from the pinned build manifest')
        require(Path(args.node).is_file() and INTERACTION_PROBE.is_file(), 'Node or retained interaction probe is missing')
        for file in ('desktop-gateway-ownership.js', 'gateway-lifecycle.js'):
            require((REPOSITORY / 'desktop/electron/dist' / file).is_file(), 'Run desktop/electron npm ci and npm run build before this gate')
        self.save()
        roots = [Path(os.environ['APPDATA']) / '@opensquilla' / 'desktop-electron', Path(os.environ['APPDATA']) / 'OpenSquilla' / 'opensquilla', Path(os.environ['USERPROFILE']) / '.opensquilla']
        for root in roots:
            require(not root.exists(), f'Preexisting application profile directory: {root}; fresh runner required')
        for root in roots:
            root.mkdir(parents=True)
            sentinel = root / ('nsis-1441-' + self.root.name + '.sentinel')
            sentinel.write_bytes(('synthetic retained profile sentinel ' + self.root.name + '\n').encode())
            self.sentinels[str(sentinel)] = digest(sentinel)
        self.report['profileSentinels'] = self.sentinels
        self.seed_retained_profile()
        self.save()
        installed = self.run('install-baseline', baseline, self.installer_arguments(), self.short_temp)
        require(installed['exitCode'] == 0, f'Baseline installer failed with {installed["exitCode"]}')
        self.require_no_product_processes('afterBaseline')
        before = self.state('baseline-installed')
        self.assert_version(before, args.baseline_version)
        self.check_profiles('afterBaseline')
        self.verify_retained_profile('afterBaseline')
        self.check_environment_registry('afterBaseline')
        asar = self.install / 'resources' / 'app.asar'
        fault = None
        if args.case == 'longpath':
            relative_length = min(200, 250 - len(str(self.install)) - 1,
                                  255 - len(str(self.install.parent / 'ns123456.tmp' / 'old-install')) - 1)
            require(relative_length >= 80, 'Installation parent is too long for the bounded long-path fixture')
            pieces = ['__osq1441_probe__']
            remaining = relative_length - len(pieces[0]) - 1
            while remaining > 65:
                pieces.append('d' * 35)
                remaining -= 36
            pieces.append('s' * (remaining - 4) + '.bin')
            self.fault_relative = Path(*pieces)
            fault = self.install / self.fault_relative
            require(len(str(fault)) <= 250, 'Injected source path exceeds the promised 250-character bound')
            require(len(str(self.normal_temp / 'nsX.tmp' / 'old-install' / self.fault_relative)) > 260, 'Long TEMP cannot guarantee an overlong destination')
            require(len(str(self.short_temp / 'ns123456.tmp' / 'old-install' / self.fault_relative)) < 260, 'Short TEMP control still has an overlong destination')
            require(len(str(self.install.parent / 'ns123456.tmp' / 'old-install' / self.fault_relative)) < 260, 'Fixed destination still has an overlong path')
            fault.parent.mkdir(parents=True)
            fault.write_bytes(b'synthetic NSIS long-path sentinel\n')
            self.report['fault'] = {'source': str(fault), 'sourceLength': len(str(fault)), 'relativePath': str(self.fault_relative), 'relativeLength': len(str(self.fault_relative)), 'sha256': digest(fault),
                                    'unfixedDestinationMinimumLength': len(str(self.normal_temp / 'nsX.tmp' / 'old-install' / self.fault_relative)),
                                    'fixedDestinationMaximumLength': len(str(self.install.parent / 'ns123456.tmp' / 'old-install' / self.fault_relative))}
        elif args.case == 'readlock':
            self.fault_relative = Path('resources') / 'app.asar'
            self.lock = self.win.lock_readable(asar)
            self.report['fault'] = {'source': str(asar), 'lockHolderPid': os.getpid(), 'lockHolderExecutable': sys.executable, 'shareFlags': 3, 'shareDelete': False}
        # Perform the report's readability check while the deliberate lock is
        # actually held, not before acquiring it.
        with asar.open('rb') as readable:
            self.report['appAsarReadableImmediatelyBeforeCandidate'] = bool(readable.read(1))
        self.require_no_product_processes('beforeCandidate')
        old_state = self.state('before-candidate', full=True)
        old_manifest = json.loads(Path(old_state['fileManifestPath']).read_text(encoding='utf-8'))
        first_temp = self.normal_temp if args.case == 'longpath' else self.short_temp
        first = self.run('candidate-first', candidate, self.installer_arguments(), first_temp)
        failure_expected = args.case == 'readlock'
        if failure_expected:
            require(first['exitCode'] == 2, f'Expected real candidate exit 2, got {first["exitCode"]}; do not report this as a reproduced failure')
            failed = self.state('after-expected-failure', full=True)
            self.assert_version(failed, args.baseline_version)
            failed_manifest = json.loads(Path(failed['fileManifestPath']).read_text(encoding='utf-8'))
            changes = manifest_changes(old_manifest, failed_manifest)
            self.report['failureOldTreeChanges'] = changes
            self.save()
            require(not any(changes.values()), 'Old application files changed despite failed upgrade; see complete manifest diff')
            self.check_profiles('afterFailure')
            self.verify_retained_profile('afterFailure')
            self.report['proofs']['readLockFailedWithoutDataLoss'] = True
            self.assert_scoped_environment(first)
            if self.lock is not None:
                self.win.k.CloseHandle(self.lock)
                self.lock = None
            self.require_no_product_processes('beforeRecovery')
            recovery_temp = self.short_temp
            recovered = self.run('candidate-recovery', candidate, self.installer_arguments(), recovery_temp)
            require(recovered['exitCode'] == 0, f'Recovery with the same candidate failed: {recovered["exitCode"]}')
            self.report['proofs']['sameCandidateRecovery'] = True
            self.assert_scoped_environment(recovered)
        else:
            require(first['exitCode'] == 0, f'Candidate upgrade failed: {first["exitCode"]}')
            self.assert_scoped_environment(first, redirected=args.case == 'longpath')
            if args.case == 'longpath':
                observed = [item for item in first['pluginDirectories'] if item['oldInstallObserved']
                            and Path(item['path']).parent.resolve() == self.install.parent.resolve()
                            and item.get('faultDestinationLength', 999) < 260]
                require(observed, 'Fixed upgrade did not observe the actual short old-uninstaller destination')
                require(first['childTemp'] == str(self.normal_temp), 'Long-TEMP regression must not change the input environment to make the candidate pass')
                self.report['longPathFirstAttemptFixed'] = True
                self.report['proofs']['longPathFirstAttempt'] = True
        self.require_no_product_processes('afterCandidate')
        after = self.state('candidate-installed', full=True)
        self.assert_version(after, args.candidate_version)
        if args.candidate_asar_sha256:
            require(after['asarSha256'] == args.candidate_asar_sha256.lower(), 'Installed app.asar differs from the current-main build manifest')
        else:
            require(after['asarSha256'] != before['asarSha256'], 'No expected asar hash supplied and asar did not change; cannot prove candidate replacement')
        if args.candidate_executable_sha256:
            require(after['executableSha256'] == args.candidate_executable_sha256.lower(), 'Installed application executable differs from the candidate build manifest')
        if fault is not None:
            require(not fault.exists(), 'Successful candidate replacement retained the injected old-only sentinel')
        self.check_profiles('afterCandidate')
        self.report['proofs']['fixedUpgrade'] = True
        self.verify_retained_profile('afterCandidate')
        self.run_retained_interaction(first_temp)
        before_uninstall_profile = manifest(self.user_data)
        write_json(self.evidence / 'retained-profile-before-uninstall-manifest.json', before_uninstall_profile)
        uninstallers = list(self.install.glob('Uninstall*.exe'))
        require(len(uninstallers) == 1, 'Expected exactly one fixture uninstaller')
        require(uninstallers[0].parent.resolve() == self.install.resolve(), 'Uninstaller escaped the declared fixture installation')
        uninstalled = self.run('uninstall-candidate', uninstallers[0], ['/S'], self.short_temp)
        require(uninstalled['exitCode'] == 0, f'Final uninstall failed: {uninstalled["exitCode"]}')
        deadline = time.monotonic() + 60
        while (self.install / 'OpenSquilla.exe').exists() or installed_registry():
            require(time.monotonic() < deadline, 'Uninstall did not remove executable and registration within 60 seconds')
            time.sleep(0.5)
        self.check_profiles('afterUninstall')
        changes = manifest_changes(before_uninstall_profile, manifest(self.user_data))
        self.report['uninstallRetainedProfileChanges'] = changes
        require(not any(changes.values()), 'Uninstall changed the actual interacted-with retained profile')
        self.verify_retained_profile('afterUninstall')
        self.check_environment_registry('afterUninstall')
        self.report['proofs'].update(persistentTempEnvironmentUnchanged=True, finalUninstall=True)
        self.report['finalRegistry'] = installed_registry()
        self.report['stage'] = 'complete'
        self.report['ok'] = True
        self.save()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-installer', required=True)
    parser.add_argument('--candidate-installer', required=True)
    parser.add_argument('--baseline-version', required=True)
    parser.add_argument('--candidate-version', default='0.5.4')
    parser.add_argument('--candidate-asar-sha256', required=True)
    parser.add_argument('--candidate-executable-sha256', required=True)
    parser.add_argument('--candidate-installer-sha256', required=True)
    parser.add_argument('--candidate-source-sha', required=True)
    parser.add_argument('--case', required=True, choices=['baseline', 'readlock', 'longpath'])
    parser.add_argument('--install-path', required=True, choices=['default', 'custom'])
    parser.add_argument('--node', default=shutil.which('node'), help='Absolute Node executable; defaults to PATH discovery')
    parser.add_argument('--evidence-root', required=True)
    parser.add_argument('--timeout-seconds', type=int, default=900)
    args = parser.parse_args()
    if re.fullmatch(r'\d+\.\d+\.\d+\.0', args.candidate_version):
        args.candidate_version = args.candidate_version[:-2]
    for value in [args.baseline_version, args.candidate_version]:
        parser.error('Versions must be stable X.Y.Z') if not re.fullmatch(r'\d+\.\d+\.\d+', value) else None
    for value in [args.candidate_asar_sha256, args.candidate_executable_sha256, args.candidate_installer_sha256]:
        parser.error('Expected hashes must be SHA-256 hex') if value and not re.fullmatch(r'[a-fA-F0-9]{64}', value) else None
    if not re.fullmatch(r'[a-f0-9]{40}', args.candidate_source_sha):
        parser.error('Candidate source must be a full lowercase Git commit SHA')
    if not args.node or not Path(args.node).is_file():
        parser.error('An existing Node executable is required')
    if args.timeout_seconds < 30 or args.timeout_seconds > 1800:
        parser.error('Timeout must be between 30 and 1800 seconds')
    return args


def main() -> int:
    args = parse_args()
    require(sys.platform == 'win32', 'This audit only runs on Windows')
    require(ctypes.sizeof(ctypes.c_void_p) == 8, 'This audit requires x64 Python')
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and bool(os.environ.get('RUNNER_TEMP')), 'Refusing local execution: GITHUB_ACTIONS=true and RUNNER_TEMP are required')
    require(os.environ.get('RUNNER_OS') == 'Windows', 'RUNNER_OS must be Windows')
    require(Path(os.environ['RUNNER_TEMP']).is_dir(), 'RUNNER_TEMP must exist')
    audit = Audit(args)
    try:
        audit.execute()
    except Exception as error:
        audit.report['error'] = str(error)
        audit.report['traceback'] = traceback.format_exc()
        audit.save()
        print(json.dumps({'ok': False, 'stage': audit.report['stage'], 'error': str(error), 'evidence': str(audit.evidence)}), flush=True)
        return 1
    finally:
        if audit.lock is not None:
            audit.win.k.CloseHandle(audit.lock)
            audit.lock = None
    print(json.dumps({'ok': True, 'case': args.case, 'evidence': str(audit.evidence)}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
