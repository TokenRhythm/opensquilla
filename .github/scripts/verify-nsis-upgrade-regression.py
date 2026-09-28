#!/usr/bin/env python3
"""Accept a fixed NSIS candidate on an otherwise empty GitHub Windows runner.

This script deliberately installs/uninstalls real packages and may hold a file
open while upgrading. It refuses every non-GitHub-runner invocation. Each matrix
cell must get a fresh runner. Baseline and long-TEMP replacement must succeed;
a genuine read lock must fail without data loss and recover with the same B.
The installed B then runs the real retained-profile interaction/quit/restart
probe. Fresh-install cells instead run the empty-profile first-send/quit probe.
Only synthetic profiles are used; no process is killed by name. This is
an unsigned-package regression gate, not a signing, UAC, or real-provider gate.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

OFFICIAL_BASELINE_SHA256 = {
    '0.5.3': '0a5869c7cee68317b98ee05cc3b0decbddb321c0d6ef57418ccb0b15a164e562',
    '0.5.4': '15205b147b274f2260c86d2b7bf2091d5dfaa6e057d4e9217f1fa3580f714b19',
}

REPOSITORY = Path(__file__).resolve().parents[2]
PROFILE_HELPER = REPOSITORY / '.github/scripts/verify-release-profile-preservation.py'
INTERACTION_PROBE = REPOSITORY / 'desktop/electron/scripts/test-packaged-retained-interaction.mjs'
FIRST_SEND_PROBE = REPOSITORY / 'desktop/electron/scripts/test-packaged-first-send-renderer.mjs'
FRESH_ITERATIONS = 20
EXPECTED_SHUTDOWN_CANCELLATION = '[useSessions] session directory error: Connection closed'
PLAYWRIGHT_SANDBOX_ERRORS = {
    'Electron sandboxed_renderer.bundle.js script failed to run',
    "TypeError: Cannot destructure property 'preloadScripts' of 'binding.startupData' "
    "as it is null.",
}


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


def sample_nsis_plugin_directory(
    directory: Path, observed_dirs: dict[str, dict], errors: list[dict], seconds: float,
) -> dict | None:
    """Supplement directory events without aborting on a Windows TEMP stat race.

    Access-denied samples are diagnostic only, never positive path evidence.
    Required event observation and final installer assertions remain separate.
    """
    def sample(path: Path, method: str) -> bool:
        try:
            return path.is_dir() if method == 'is_dir' else path.is_file()
        except PermissionError as error:
            if getattr(error, 'winerror', None) != 5:
                raise
            # An NSIS temporary directory can become inaccessible while it is
            # being removed. Keep bounded diagnostics and try on the next poll.
            diagnostic = next((item for item in errors
                               if item['path'] == str(path) and item['sample'] == method), None)
            if diagnostic is None:
                diagnostic = {
                    'path': str(path), 'sample': method, 'errno': error.errno, 'winerror': 5,
                    'firstObservedSeconds': seconds, 'observations': 0,
                }
                errors.append(diagnostic)
            diagnostic['lastObservedSeconds'] = seconds
            diagnostic['observations'] += 1
            return False

    if not sample(directory, 'is_dir'):
        return None
    record = observed_dirs.setdefault(str(directory), {
        'path': str(directory), 'firstObservedSeconds': seconds,
        'oldInstallObserved': False, 'oldUninstallerObserved': False,
    })
    record['lastObservedSeconds'] = seconds
    record['oldInstallObserved'] |= sample(directory / 'old-install', 'is_dir')
    record['oldUninstallerObserved'] |= sample(directory / 'old-uninstaller.exe', 'is_file')
    return record


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


def _log_records(source: str) -> list[dict]:
    records = [json.loads(line) for line in source.lstrip('\ufeff').splitlines() if line.strip()]
    require(all(isinstance(record, dict) for record in records), 'Fresh log has invalid records')
    return records


def _fresh_log_summary(summary: object, name: str, source: bytes | None = None) -> list[dict]:
    require(isinstance(summary, dict), f'Fresh {name} log summary is missing')
    require(summary.get('complete') is True and type(summary.get('malformedRecords')) is int
            and summary['malformedRecords'] == 0 and not summary.get('diagnosticError')
            and summary.get('rotated') is not True,
            f'Fresh {name} log is incomplete or malformed')
    require(type(summary.get('bytes')) is int and summary['bytes'] > 0
            and isinstance(summary.get('sha256'), str)
            and re.fullmatch('[a-f0-9]{64}', summary['sha256']),
            f'Fresh {name} log lacks integrity evidence')
    records = summary.get('records')
    require(isinstance(records, list) and records
            and all(isinstance(record, dict) for record in records),
            f'Fresh {name} log records are missing')
    for record in records:
        require(isinstance(record.get('event'), str) and record['event']
                and record.get('detail_omitted') is not True,
                f'Fresh {name} log has invalid or truncated records')
        _fresh_time(record.get('at'))
    if source is not None:
        require(source.endswith(b'\n') and summary['bytes'] == len(source)
                and summary['sha256'] == hashlib.sha256(source).hexdigest()
                and records == _log_records(source.decode('utf-8-sig')),
                f'Fresh {name} log does not match its preserved evidence')
    return records


def _fresh_time(value: object) -> float:
    require(isinstance(value, str), 'Fresh console observation has no timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None, 'Fresh console timestamp has no timezone')
        return parsed.timestamp()
    except ValueError as error:
        raise RuntimeError('Fresh console observation has an invalid timestamp') from error


def _fresh_console_matches(result: dict, desktop: list[dict], journal: list[dict]) -> None:
    """Recheck each allowed error against both original observation streams."""
    renderer, observation, acceptance = (
        result.get('renderer'), result.get('observation'), result.get('acceptance'),
    )
    require(isinstance(renderer, dict) and isinstance(observation, dict)
            and isinstance(acceptance, dict), 'Fresh renderer observation is missing')
    require(type(acceptance.get('version')) is int and acceptance['version'] == 1
            and acceptance.get('cleanupSucceeded') is True,
            'Fresh renderer acceptance contract is unsupported or cleanup failed')
    for field in ('failures', 'unexpectedConsoleIndices', 'unexpectedMainRecordIndices',
                  'unexpectedDesktopRecordIndices'):
        require(acceptance.get(field) == [], f'Fresh renderer acceptance failed: {field}')
    require(observation.get('completed') is True and observation.get('errors') == [],
            'Fresh renderer observation did not complete')
    target_page = observation.get('targetPageId')
    target_contents = observation.get('targetWebContentsId')
    require(type(target_page) is int and target_page > 0
            and type(target_contents) is int and target_contents > 0,
            'Fresh renderer target identity is missing')
    subframes = observation.get('subframePageIds')
    require(isinstance(subframes, list)
            and all(type(page_id) is int and page_id > 0 for page_id in subframes)
            and len(set(subframes)) == len(subframes),
            'Fresh renderer subframe observation is missing or invalid')
    require(type(renderer.get('pageErrors')) is int and renderer['pageErrors'] == 0
            and renderer.get('pageErrorDetails') == [], 'Fresh renderer page errors were observed')
    consoles = renderer.get('consoleErrorDetails')
    require(isinstance(consoles, list) and type(renderer.get('consoleErrors')) is int
            and renderer['consoleErrors'] == len(consoles),
            'Fresh renderer console evidence is incomplete')
    starts = [i for i, record in enumerate(journal) if record.get('event') == 'observation-start']
    cleanups = [i for i, record in enumerate(journal) if record.get('event') == 'cleanup-start']
    observation_exits = [i for i, record in enumerate(journal)
                         if record.get('event') == 'observation-exit']
    require(len(starts) == len(cleanups) == len(observation_exits) == 1
            and starts[0] == 0 < cleanups[0] < observation_exits[0] == len(journal) - 1,
            'Fresh main console observation has incomplete boundaries')
    observation_exit = journal[observation_exits[0]]
    require(type(observation_exit.get('code')) is int and observation_exit['code'] == 0
            and type(observation_exit.get('writeErrors')) is int
            and observation_exit['writeErrors'] == 0,
            'Fresh main console observation did not finish without errors')
    require(all(type(record.get('index')) is int and record['index'] == index
                and record.get('event') in {
                    'observation-start', 'cleanup-start', 'observation-exit', 'console',
                }
                for index, record in enumerate(journal)),
            'Fresh main console observation contains invalid records')
    main = [(i, record) for i, record in enumerate(journal) if record.get('event') == 'console']
    require(observation.get('mainConsoleRecords') == [record for _, record in main],
            'Fresh main console records differ from the preserved journal')
    requested = next(i for i, record in enumerate(desktop)
                     if record.get('event') == 'quit_gateway_shutdown_requested')
    exited = next(i for i, record in enumerate(desktop)
                  if record.get('event') == 'quit_gateway_exit')
    desktop_errors = [(i, record) for i, record in enumerate(desktop)
                      if record.get('event') == 'renderer_console'
                      and record.get('message') not in PLAYWRIGHT_SANDBOX_ERRORS]
    require(len(consoles) == len(main) == len(desktop_errors),
            'Fresh console observations do not match one-to-one')
    expected_matches = []
    for index, (console, (main_index, main_record), (desktop_index, desktop_record)) in enumerate(
            zip(consoles, main, desktop_errors, strict=True)):
        require(isinstance(console, dict) and type(console.get('index')) is int
                and console['index'] == index and type(console.get('pageId')) is int
                and console['pageId'] == target_page
                and console.get('frameIsolationProven') is True and not subframes
                and console.get('phase') == 'electron-cleanup-start',
                'Fresh console error was not observed on the isolated target page during cleanup')
        require(type(main_record.get('index')) is int and main_record['index'] == main_index
                and type(main_record.get('webContentsId')) is int
                and main_record['webContentsId'] == target_contents
                and main_record.get('mainFrame') is True
                and cleanups[0] < main_index < observation_exits[0]
                and main_record.get('phase') == 'electron-cleanup'
                and requested < desktop_index < exited,
                'Fresh console error is outside the verified target shutdown interval')
        require(console.get('message') == main_record.get('message')
                == desktop_record.get('message') == EXPECTED_SHUTDOWN_CANCELLATION
                and main_record.get('level') == desktop_record.get('level') == 'error',
                'Fresh console error is not the exact expected cancellation')
        require(isinstance(console.get('source'), str)
                and console['source'].startswith('opensquilla-app://desktop/')
                and console['source'] == main_record.get('source') == desktop_record.get('source')
                and type(console.get('line')) is int and console['line'] >= 1
                and type(main_record.get('line')) is int and type(desktop_record.get('line')) is int
                and console['line'] == main_record.get('line') == desktop_record.get('line'),
                'Fresh console error source does not match its main-frame observation')
        location = urlsplit(console['source'])
        require(location.scheme == 'opensquilla-app' and location.netloc == 'desktop'
                and not location.query and not location.fragment,
                'Fresh console error source was not normalized')
        # Playwright receipt can be delayed by IPC or runner scheduling. Native
        # logs share a process clock; match their accepted-shutdown interval,
        # with exact identity and ordered one-to-one coverage in all streams.
        _fresh_time(console.get('observedAt'))
        require(_fresh_time(desktop[requested].get('at'))
                <= _fresh_time(main_record.get('at')) <= _fresh_time(desktop[exited].get('at')),
                'Fresh native console error is outside the accepted shutdown interval')
        expected_matches.append({'consoleIndex': index, 'mainRecordIndex': main_index,
                                 'desktopRecordIndex': desktop_index})
    matches = acceptance.get('consoleMatches')
    require(isinstance(matches, list)
            and all(isinstance(match, dict) and all(type(value) is int for value in match.values())
                    for match in matches)
            and matches == expected_matches,
            'Fresh renderer acceptance contains invalid console associations')


def fresh_interaction_result(
    output: str, *, desktop_source: bytes | None = None, console_source: bytes | None = None,
) -> dict:
    """Read the probe's JSON phase stream and require its final success report."""
    decoder = json.JSONDecoder()
    remaining = output.lstrip('\ufeff').strip()
    result = None
    while remaining:
        result, end = decoder.raw_decode(remaining)
        remaining = remaining[end:].strip()
    require(isinstance(result, dict) and result.get('ok') is True,
            'Fresh interaction report did not pass')
    require(result.get('reportType') == 'packaged-first-send'
            and type(result.get('schemaVersion')) is int and result['schemaVersion'] == 2,
            'Fresh interaction report schema is unsupported')
    require(result.get('iterations') == FRESH_ITERATIONS,
            'Fresh interaction iteration count changed')
    expected_rpc = {'chatSend': FRESH_ITERATIONS * 2, 'uniqueSessions': FRESH_ITERATIONS}
    require(result.get('rpc') == expected_rpc,
            'Fresh interaction did not complete every send and session')
    provider = result.get('provider')
    require(isinstance(provider, dict)
            and provider.get('chatRequestCount') == FRESH_ITERATIONS * 2,
            'Fresh interaction provider completions do not match sends')
    require(type(result.get('externalRendererRequests')) is int
            and result['externalRendererRequests'] == 0,
            'Fresh interaction renderer/network checks failed')
    desktop = result.get('desktopLog', {})
    require(isinstance(desktop, dict)
            and type(desktop.get('forbiddenErrorCount')) is int
            and desktop['forbiddenErrorCount'] == 0
            and type(desktop.get('unexpectedRendererErrorCount')) is int
            and desktop['unexpectedRendererErrorCount'] == 0,
            'Fresh interaction desktop log contains errors')
    events = desktop.get('eventCounts', {})
    require(isinstance(events, dict), 'Fresh interaction lacks normal Quit event counts')
    for event in ('before_quit', 'quit_gateway_shutdown_requested', 'quit_gateway_exit'):
        require(type(events.get(event)) is int and events[event] >= 1,
                f'Fresh interaction lacks normal Quit evidence: {event}')
    records = _fresh_log_summary(desktop, 'desktop', desktop_source)
    observed_events: dict[str, int] = {}
    for record in records:
        event = record.get('event') if isinstance(record.get('event'), str) else 'unknown'
        observed_events[event] = observed_events.get(event, 0) + 1
    require(events == observed_events, 'Fresh desktop event counts disagree with its records')
    forbidden = (
        r'(?:emitsOptions|\bexposed\b|nextSibling|getNextHostNode|Teleport\.process|\[ErrorBoundary\])'
    )
    require(not re.search(forbidden,
                          json.dumps(records), re.IGNORECASE),
            'Fresh desktop log contains a forbidden renderer failure')
    fresh_shutdown_evidence('\n'.join(json.dumps(record) for record in records))
    observation = result.get('observation')
    require(isinstance(observation, dict), 'Fresh renderer observation is missing')
    journal = _fresh_log_summary(observation.get('journal'), 'main console', console_source)
    _fresh_console_matches(result, records, journal)
    return result


def fresh_shutdown_evidence(output: str) -> dict:
    """Require clean Gateway exits followed by the desktop's committed exit."""
    records = _log_records(output)
    quit_events = ('before_quit', 'quit_gateway_shutdown_requested', 'quit_gateway_exit')
    positions = [[index for index, item in enumerate(records) if item.get('event') == event]
                 for event in quit_events]
    require(all(len(indices) == 1 for indices in positions),
            'Fresh normal Quit requires one complete shutdown attempt')
    before, requested, exited = [indices[0] for indices in positions]
    require(before < requested < exited and records[requested].get('accepted') is True
            and records[requested].get('alreadyStopping') is False,
            'Fresh normal Quit lacks an ordered accepted shutdown request')
    require(not any(item.get('event') in {
        'quit_gateway_drain_failed', 'quit_gateway_still_running', 'renderer_unresponsive',
        'renderer_process_gone', 'renderer_console_suppressed',
    } or (item.get('event') == 'desktop_exit_phase' and item.get('to') == 'running')
        for item in records), 'Fresh normal Quit contains failed or incomplete observations')
    exits = [(index, item) for index, item in enumerate(records)
             if item.get('event') == 'quit_gateway_exit']
    require(exits and all(item.get('exited') is True and item.get('hardTerminated') is False
                         for _, item in exits),
            'Fresh Gateway did not exit without hard termination')
    committed_indices = [index for index, item in enumerate(records)
                      if item.get('event') == 'desktop_exit_phase' and item.get('to') == 'committed'
                      and item.get('reason') == 'all lifecycle-owned Gateways exited']
    require(len(committed_indices) == 1, 'Fresh desktop lacks one committed exit')
    committed = committed_indices[0]
    require(committed > exits[-1][0], 'Fresh desktop did not commit exit after all Gateway exits')
    return {'gatewayExitCount': len(exits), 'allGatewayExitsClean': True,
            'committedAfterGatewayExits': True}


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
        self.user_data = self.root / (
            'fresh-user-data' if args.case == 'fresh' else 'retained-user-data'
        )
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
            'proofs': {'fixedUpgrade': None if args.case == 'fresh' else False,
                       'freshInstall': False if args.case == 'fresh' else None,
                       'readLockFailedWithoutDataLoss': None,
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
        if args.case == 'fresh':
            self.report['proofs'].update(toolRead=None, stop=None, restart=None)
            self.report['limitations'].update(officialBaselineInstalledButNotLaunched=False,
                                             syntheticRuntimeReadyProfile=False,
                                             sameProfileRestartTested=False)
            self.report['scope'] = (
                'Real candidate-B NSIS fresh installation, then installed B UI/Gateway first-send '
                'and normal Quit with a new isolated profile and loopback provider. '
                'No old baseline/profile is installed or seeded. Retained data, tools, Stop '
                'and same-profile restart are covered separately by the upgrade matrix. '
                'No signing-trust, UAC, real-provider, or Windows 10/11 acceptance claim.'
            )
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
        operation['pluginPathObservationErrors'] = []
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
                        if (directory.name in existing_temp[root]
                                or not directory.name.casefold().startswith('ns')
                                or not directory.name.casefold().endswith('.tmp')):
                            continue
                        record = sample_nsis_plugin_directory(
                            directory, observed_dirs, operation['pluginPathObservationErrors'],
                            round(now - started, 3),
                        )
                        if record is None:
                            continue
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
        client_samples = self.verify_client_temp(operation, temp)
        self.report['retainedInteraction'] = {'reportPath': str(result_path), 'reportSha256': digest(result_path),
                                            'auditId': marker['auditId'], 'ok': True, 'clientTempEnvironmentSamples': client_samples}
        self.report['proofs'].update(realClientStarted=True, firstSend=True, toolRead=True,
                                     stop=True, restart=True, normalQuit=True)
        self.require_no_product_processes('afterRetainedInteraction')
        self.verify_retained_profile('afterRetainedInteraction')
        self.check_environment_registry('afterRetainedInteraction')
        self.save()

    def verify_client_temp(self, operation: dict, temp: Path) -> list[dict]:
        client_samples = [item for item in operation['tempEnvironmentSamples']
                          if item['image'] and Path(item['image']).name.casefold()
                          in {'opensquilla.exe', 'opensquilla-gateway.exe'}]
        require(client_samples, 'No actual installed-client TEMP/TMP evidence was captured')
        for item in client_samples:
            require(all(item[name] is not None and Path(item[name]).resolve() == temp.resolve()
                        for name in ('TEMP', 'TMP')),
                    'Installed client/Gateway inherited an unexpected temporary directory')
        return client_samples

    def preserve_fresh_interaction_diagnostics(self) -> dict:
        """Keep probe evidence even when execution or validation raises.

        The runner already writes stdout/stderr directly to the evidence root.
        Copy profile logs before checking exit status so a failed assertion or
        timeout cannot prevent their upload. Diagnostic failures never replace
        the original probe failure; successful probes must still have evidence.
        """
        diagnostics: dict = {'files': {}, 'errors': []}
        sources = {
            'stdout': self.evidence / 'fresh-first-interaction-stdout.log',
            'stderr': self.evidence / 'fresh-first-interaction-stderr.log',
            'desktopLog': self.user_data / 'logs/desktop.log',
            'mainConsoleLog': self.user_data / 'first-send-main-console.jsonl',
        }
        try:
            for path in sorted((self.user_data / 'logs').glob('desktop.log.*')):
                if re.fullmatch(r'desktop\.log\.\d+', path.name):
                    sources['rotated-' + path.name] = path
        except Exception as error:
            diagnostics['errors'].append({'file': 'rotatedDesktopLogs', 'error': str(error)})
        for name, source in sources.items():
            fallback_name = (
                'fresh-interaction-' + source.name if name.startswith('rotated-') else source.name
            )
            destination = self.evidence / {
                'desktopLog': 'fresh-interaction-desktop.log',
                'mainConsoleLog': 'fresh-interaction-main-console.jsonl',
            }.get(name, fallback_name)
            try:
                if source != destination:
                    shutil.copyfile(source, destination)
                diagnostics['files'][name] = {
                    'path': str(destination), 'bytes': destination.stat().st_size,
                    'sha256': digest(destination),
                }
            except Exception as error:
                diagnostics['errors'].append({'file': name, 'error': str(error)})
        self.report['freshInteractionDiagnostics'] = diagnostics
        try:
            self.save()
        except Exception as error:
            diagnostics['errors'].append({'file': 'result.json', 'error': str(error)})
        return diagnostics

    def run_fresh_interaction(self, temp: Path) -> None:
        require(not self.user_data.exists(),
                'Fresh first interaction requires a new, unseeded profile directory')
        self.report['freshProfileBeforeLaunch'] = {'path': str(self.user_data), 'exists': False}
        self.runtime_started = True
        try:
            operation = self.run('fresh-first-interaction', Path(self.args.node).resolve(), [
                str(FIRST_SEND_PROBE), '--executable', str(self.install / 'OpenSquilla.exe'),
                '--user-data-dir', str(self.user_data), '--iterations', str(FRESH_ITERATIONS),
            ], temp)
        finally:
            diagnostics = self.preserve_fresh_interaction_diagnostics()
        require(operation['exitCode'] == 0,
                f'Installed B fresh interaction probe failed: {operation["exitCode"]}')
        require(not diagnostics['errors'],
                f'Fresh interaction diagnostic evidence is incomplete: {diagnostics["errors"]}')
        stdout = self.evidence / 'fresh-first-interaction-stdout.log'
        result = fresh_interaction_result(
            stdout.read_text(encoding='utf-8-sig'),
            desktop_source=(self.evidence / 'fresh-interaction-desktop.log').read_bytes(),
            console_source=(self.evidence / 'fresh-interaction-main-console.jsonl').read_bytes(),
        )
        require(result.get('executable') == 'OpenSquilla.exe',
                'Fresh interaction ran an unexpected executable')
        require(self.user_data.is_dir(), 'Fresh interaction did not create its isolated profile')
        desktop_log_path = self.evidence / 'fresh-interaction-desktop.log'
        desktop_log = desktop_log_path.read_text(encoding='utf-8-sig')
        shutdown = fresh_shutdown_evidence(desktop_log)
        result_path = self.evidence / 'fresh-interaction-report.json'
        write_json(result_path, result)
        client_samples = self.verify_client_temp(operation, temp)
        self.report['freshInteraction'] = {
            'reportPath': str(result_path), 'reportSha256': digest(result_path),
            'sourceSha': self.args.candidate_source_sha,
            'executableSha256': self.args.candidate_executable_sha256.lower(),
            'shutdown': shutdown, 'desktopLogPath': str(desktop_log_path),
            'desktopLogSha256': digest(desktop_log_path),
            'ok': True, 'clientTempEnvironmentSamples': client_samples,
        }
        self.report['proofs'].update(realClientStarted=True, firstSend=True, normalQuit=True)
        self.require_no_product_processes('afterFreshInteraction')
        self.check_environment_registry('afterFreshInteraction')
        self.save()

    def verify_candidate_installation(self) -> None:
        self.require_no_product_processes('afterCandidate')
        after = self.state('candidate-installed', full=True)
        self.assert_version(after, self.args.candidate_version)
        require(after['asarSha256'] == self.args.candidate_asar_sha256.lower(),
                'Installed app.asar differs from the candidate build manifest')
        require(after['executableSha256'] == self.args.candidate_executable_sha256.lower(),
                'Installed application executable differs from the candidate build manifest')
        inventory_path = self.install / 'resources/runtime/gateway/dependency-inventory.json'
        require(inventory_path.is_file(), 'Installed candidate lacks its dependency inventory')
        require(digest(inventory_path) == self.args.candidate_dependency_inventory_sha256.lower(),
                'Installed dependency inventory differs from the audited candidate')
        self.report['proofs']['auditedDependenciesInstalled'] = True

    def execute_fresh(self, candidate: Path) -> None:
        require(not self.user_data.exists(), 'Fresh installation requires an absent test profile')
        self.report['freshProfileBeforeInstall'] = {'path': str(self.user_data), 'exists': False}
        installed = self.run('candidate-fresh', candidate,
                             self.installer_arguments(), self.short_temp)
        require(installed['exitCode'] == 0,
                f'Fresh candidate installation failed: {installed["exitCode"]}')
        self.verify_candidate_installation()
        self.check_environment_registry('afterFreshInstall')
        self.report['proofs']['freshInstall'] = True
        self.run_fresh_interaction(self.short_temp)
        self.uninstall_candidate(retained_profile=False)
        self.save()

    def execute(self) -> None:
        args = self.args
        baseline = Path(args.baseline_installer).resolve() if args.baseline_installer else None
        candidate = Path(args.candidate_installer).resolve()
        require(candidate.is_file() and (baseline is None or baseline.is_file()),
                'Complete installer files must exist')
        require(not installed_registry(), 'Preexisting OpenSquilla registration: use a fresh runner')
        require(not within(Path(sys.executable), self.install), 'Lock-holder Python must be outside the installation directory')
        self.require_no_product_processes('initial')
        self.report['inputs'] = {
            'baselineInstaller': str(baseline) if baseline else None,
            'baselineInstallerSha256': digest(baseline) if baseline else None,
            'candidateInstaller': str(candidate), 'candidateInstallerSha256': digest(candidate),
            'expectedCandidateAsarSha256': args.candidate_asar_sha256,
            'expectedCandidateExecutableSha256': args.candidate_executable_sha256,
            'expectedCandidateInstallerSha256': args.candidate_installer_sha256,
            'expectedCandidateDependencyInventorySha256': args.candidate_dependency_inventory_sha256,
            'candidateSourceSha': args.candidate_source_sha,
        }
        if args.case != 'fresh':
            require(baseline is not None and args.baseline_version in OFFICIAL_BASELINE_SHA256,
                    'Only pinned official 0.5.3 and 0.5.4 baselines are accepted')
            require(self.report['inputs']['baselineInstallerSha256']
                    == OFFICIAL_BASELINE_SHA256[args.baseline_version],
                    'Baseline bytes do not match the pinned official GitHub asset')
        require(self.report['inputs']['candidateInstallerSha256'] == args.candidate_installer_sha256.lower(), 'Candidate installer differs from the pinned build manifest')
        interaction_probe = FIRST_SEND_PROBE if args.case == 'fresh' else INTERACTION_PROBE
        require(Path(args.node).is_file() and interaction_probe.is_file(),
                'Node or interaction probe is missing')
        for file in ('desktop-gateway-ownership.js', 'gateway-lifecycle.js'):
            require((REPOSITORY / 'desktop/electron/dist' / file).is_file(), 'Run desktop/electron npm ci and npm run build before this gate')
        self.save()
        roots = [Path(os.environ['APPDATA']) / '@opensquilla' / 'desktop-electron', Path(os.environ['APPDATA']) / 'OpenSquilla' / 'opensquilla', Path(os.environ['USERPROFILE']) / '.opensquilla']
        for root in roots:
            require(not root.exists(), f'Preexisting application profile directory: {root}; fresh runner required')
        if args.case == 'fresh':
            self.report['freshStandardProfilesBeforeInstall'] = [
                {'path': str(root), 'exists': False} for root in roots
            ]
            self.execute_fresh(candidate)
            return
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
        self.verify_candidate_installation()
        if fault is not None:
            require(not fault.exists(), 'Successful candidate replacement retained the injected old-only sentinel')
        self.check_profiles('afterCandidate')
        self.report['proofs']['fixedUpgrade'] = True
        self.verify_retained_profile('afterCandidate')
        self.run_retained_interaction(first_temp)
        self.uninstall_candidate(retained_profile=True)

    def uninstall_candidate(self, *, retained_profile: bool) -> None:
        before_uninstall_profile = manifest(self.user_data)
        profile_label = 'retained' if retained_profile else 'fresh'
        write_json(self.evidence / f'{profile_label}-profile-before-uninstall-manifest.json',
                   before_uninstall_profile)
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
        changes_key = ('uninstallRetainedProfileChanges' if retained_profile
                       else 'uninstallFreshProfileChanges')
        self.report[changes_key] = changes
        require(not any(changes.values()), 'Uninstall changed the actual interacted-with profile')
        if retained_profile:
            self.verify_retained_profile('afterUninstall')
        self.check_environment_registry('afterUninstall')
        self.report['proofs'].update(persistentTempEnvironmentUnchanged=True, finalUninstall=True)
        self.report['finalRegistry'] = installed_registry()
        self.report['stage'] = 'complete'
        self.report['ok'] = True
        self.save()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-installer')
    parser.add_argument('--candidate-installer', required=True)
    parser.add_argument('--baseline-version')
    parser.add_argument('--candidate-version', default='0.5.4')
    parser.add_argument('--candidate-asar-sha256', required=True)
    parser.add_argument('--candidate-executable-sha256', required=True)
    parser.add_argument('--candidate-dependency-inventory-sha256', required=True)
    parser.add_argument('--candidate-installer-sha256', required=True)
    parser.add_argument('--candidate-source-sha', required=True)
    parser.add_argument('--case', required=True,
                        choices=['fresh', 'baseline', 'readlock', 'longpath'])
    parser.add_argument('--install-path', required=True, choices=['default', 'custom'])
    parser.add_argument('--node', default=shutil.which('node'), help='Absolute Node executable; defaults to PATH discovery')
    parser.add_argument('--evidence-root', required=True)
    parser.add_argument('--timeout-seconds', type=int, default=900)
    args = parser.parse_args(argv)
    if args.case == 'fresh':
        if args.baseline_installer is not None or args.baseline_version is not None:
            parser.error('Fresh installation must not specify an old baseline')
    elif not args.baseline_installer or args.baseline_version not in OFFICIAL_BASELINE_SHA256:
        parser.error('Upgrade cases require a baseline installer and pinned 0.5.3 or 0.5.4 version')
    if re.fullmatch(r'\d+\.\d+\.\d+\.0', args.candidate_version):
        args.candidate_version = args.candidate_version[:-2]
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.candidate_version):
        parser.error('Versions must be stable X.Y.Z')
    for value in [args.candidate_asar_sha256, args.candidate_executable_sha256,
                  args.candidate_installer_sha256, args.candidate_dependency_inventory_sha256]:
        if not re.fullmatch(r'[a-fA-F0-9]{64}', value):
            parser.error('Expected hashes must be SHA-256 hex')
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
