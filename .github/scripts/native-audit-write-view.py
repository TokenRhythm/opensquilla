"""Bounded synthetic marker I/O only; never opens an OpenSquilla profile."""

from __future__ import annotations

import ctypes
import json
import os
import re
import stat
import sys
from pathlib import Path


def same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def final_path(path: Path) -> Path:
    if os.name != "nt":
        return path.resolve(strict=True)
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                              wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if length == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if length >= len(buffer):
            raise RuntimeError("Unexpected final path length")
        result = buffer.value
        if result.startswith("\\\\?\\UNC\\"):
            result = "\\\\" + result[8:]
        elif result.startswith("\\\\?\\"):
            result = result[4:]
        return Path(result)
    finally:
        kernel.CloseHandle(handle)


def identity(info: os.stat_result) -> list[str]:
    # Windows Python 3.12 lstat and fstat disagree on legacy st_ctime semantics.
    # Birth time is consistent there; POSIX ctime remains a conservative check.
    return [str(info.st_dev), str(info.st_ino),
            str(getattr(info, "st_birthtime_ns", info.st_ctime_ns))]


def ordinary(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"Reparse/symlink input refused: {path}")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError(f"Unexpected file type: {path}")
    return info


def validate(request: dict) -> tuple[Path, Path, bytes]:
    audit_id = request["auditId"]
    if not isinstance(audit_id, str) or not re.fullmatch(
        r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}", audit_id
    ):
        raise ValueError("Invalid fresh probe UUID")
    role = request["role"]
    if role not in {"node", "python"}:
        raise ValueError("Unknown probe writer")
    roaming = Path(request["roamingParent"])
    if not roaming.is_absolute():
        raise ValueError("Roaming parent must be absolute")
    requested = roaming / f"opensquilla-native-write-{role}-{audit_id}"
    marker = f"OpenSquilla native write-view v1 {audit_id} {role}\n".encode("ascii")
    return roaming, requested, marker


def allowed_destination(roaming: Path, requested: Path, actual: Path) -> None:
    # Only the original new sibling or the exact per-package LocalCache layout
    # can be cleaned. An unexpected destination is preserved for operator review.
    if same_path(actual, requested):
        return
    packages = roaming.parent / "Local" / "Packages"
    try:
        relative = actual.relative_to(packages)
    except ValueError as error:
        raise ValueError(f"Unexpected destination; preserve it: {actual}") from error
    if len(relative.parts) != 4 or relative.parts[1:] != ("LocalCache", "Roaming", requested.name):
        raise ValueError(f"Unexpected destination; preserve it: {actual}")


def snapshot(request: dict, actual_override: Path | None = None) -> dict:
    roaming, requested, marker = validate(request)
    actual = final_path(requested) if actual_override is None else actual_override
    allowed_destination(roaming, requested, actual)
    directory_info = ordinary(actual, directory=True)
    if not same_path(final_path(actual), actual):
        raise ValueError(f"Actual probe path changed or is redirected: {actual}")
    marker_path = actual / "marker.txt"
    marker_info = ordinary(marker_path, directory=False)
    if not same_path(final_path(marker_path), marker_path):
        raise ValueError("Actual marker path changed or is redirected")
    with marker_path.open("rb") as stream:
        if identity(os.fstat(stream.fileno())) != identity(marker_info):
            raise ValueError("Marker identity changed before reading")
        if stream.read(len(marker) + 1) != marker:
            raise ValueError("Probe nonce/role marker changed")
    return {
        "role": request["role"], "requested": str(requested), "actual": str(actual),
        "markerActual": str(marker_path), "directoryIdentity": identity(directory_info),
        "markerIdentity": identity(marker_info), "native": same_path(actual, requested),
    }


def handle(request: dict) -> dict:
    roaming, requested, marker = validate(request)
    action = request["action"]
    if action == "create":
        # Match the seeder's resolve-before-create behavior. Never reuse a tree.
        if os.path.lexists(requested):
            raise ValueError("Probe directory already exists")
        destination = requested.resolve()
        allowed_destination(roaming, requested, destination)
        destination.mkdir()  # parents=False, exist_ok=False
        request["_createdPath"] = {"requested": str(requested), "actual": None}
        actual = final_path(destination)
        request["_createdPath"]["actual"] = str(actual)
        allowed_destination(roaming, requested, actual)
        ordinary(actual, directory=True)
        with (destination / "marker.txt").open("xb") as stream:
            stream.write(marker)
            stream.flush()
            os.fsync(stream.fileno())
        return {"receipt": snapshot(request)}
    if action == "inspect":
        return {"receipt": snapshot(request)}
    if action == "cleanup":
        receipt = request["receipt"]
        if receipt["role"] != request["role"] or not same_path(receipt["requested"], requested):
            raise ValueError("Cleanup receipt belongs to another probe")
        actual = Path(receipt["actual"])
        if not actual.is_absolute():
            raise ValueError("Cleanup path must be absolute")
        current = snapshot(request, actual)
        if current != receipt:
            raise ValueError("Probe path or file identity changed; preserve it")
        if sorted(child.name for child in actual.iterdir()) != ["marker.txt"]:
            raise ValueError("Probe has unexpected entries; preserve it")
        # Recheck after enumeration. No recursive deletion, wildcard, or removal
        # through the virtual alias; remove only this exact marker and empty dir.
        if snapshot(request, actual) != receipt:
            raise ValueError("Probe changed before cleanup; preserve it")
        (actual / "marker.txt").unlink()
        actual.rmdir()
        return {"removed": str(actual)}
    raise ValueError("Unknown probe action")


def main() -> None:
    request = json.loads(sys.stdin.read(16384))
    try:
        result = handle(request)
        result.update(ok=True, executable=sys.executable)
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}",
                  "executable": sys.executable, "createdPath": request.get("_createdPath")}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
