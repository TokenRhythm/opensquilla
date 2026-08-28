"""Shared CLI helpers for local path and attachment commands."""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from opensquilla.contracts.attachments import (
    DOCX_MIME,
    EML_MIME,
    IMAGE_ATTACHMENT_BYTES,
    MAX_STAGED_PDF_BYTES,
    MAX_STAGED_TEXT_BYTES,
    MBOX_MIME,
    MSG_MIME,
    OFFICE_ATTACHMENT_BYTES,
    OPAQUE_ATTACHMENT_BYTES,
    OPAQUE_MIME,
    PPTX_MIME,
    TEXT_ATTACHMENT_BYTES,
    XLSX_MIME,
    attachment_category,
    can_stage_attachment_mime,
    normalize_attachment_mime,
)
from opensquilla.contracts.attachments import (
    attachment_size_limit_for_mime as _policy_attachment_size_limit_for_mime,
)

CLI_INLINE_THRESHOLD_BYTES = TEXT_ATTACHMENT_BYTES
CLI_TEXT_ATTACHMENT_BYTES = TEXT_ATTACHMENT_BYTES
CLI_IMAGE_ATTACHMENT_BYTES = IMAGE_ATTACHMENT_BYTES
CLI_STAGED_PDF_BYTES = MAX_STAGED_PDF_BYTES

CLI_IMAGE_MIMES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

CLI_ALLOWED_FILE_MIMES: dict[str, str] = {
    **CLI_IMAGE_MIMES,
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "html": "text/html",
    "htm": "text/html",
    "csv": "text/csv",
    "json": "application/json",
    "docx": DOCX_MIME,
    "xlsx": XLSX_MIME,
    "pptx": PPTX_MIME,
    "eml": EML_MIME,
    "mbox": MBOX_MIME,
    "msg": MSG_MIME,
}
PATH_REMOTE_GATEWAY_MESSAGE = "Use /file to upload from this CLI machine"
PATH_TEXT_EXTENSIONS = {
    ".txt",
    ".log",
    ".md",
    ".markdown",
    ".json",
    ".html",
    ".htm",
}
PATH_SPREADSHEET_EXTENSIONS = {".csv", ".tsv", ".xlsx"}
PATH_IMAGE_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".tiff",
    ".tif",
}
PATH_OBVIOUS_BINARY_EXTENSIONS = {
    ".7z",
    ".bin",
    ".bz2",
    ".dll",
    ".dmg",
    ".doc",
    ".docx",
    ".dylib",
    ".exe",
    ".gz",
    ".msi",
    ".ppt",
    ".pptx",
    ".rar",
    ".so",
    ".tar",
    ".xls",
    ".zip",
    *PATH_IMAGE_BINARY_EXTENSIONS,
}

UploadCallable = Callable[[Path, str, str], str]
AsyncUploadCallable = Callable[[Path, str, str], Awaitable[str]]


def attachment_size_limit_for_mime(mime: str) -> int:
    return _policy_attachment_size_limit_for_mime(
        mime, staged=can_stage_attachment_mime(mime)
    )


def _can_stage_mime(mime: str) -> bool:
    return can_stage_attachment_mime(mime)


_TEXT_FALLBACK_MAX_SNIFF_BYTES = 4 * 1000 * 1000


def _looks_like_utf8_text(path: Path) -> bool:
    """True if the file is decodable UTF-8 with no NUL byte, mirroring the
    gateway's unknown-text fallback so it is reachable from the CLI too."""

    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0 or size > _TEXT_FALLBACK_MAX_SNIFF_BYTES:
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def mime_for_path(path: Path) -> str:
    """Resolve a best-effort MIME label for any local file (never raises).

    Known extensions map to their rendered mime; unknown extensions become
    text/plain when the bytes are decodable UTF-8, otherwise an opaque label
    (best-guess from the extension table, or the generic binary type). The
    gateway re-validates against the actual bytes either way.
    """

    ext = path.suffix.lower().lstrip(".")
    mime = CLI_ALLOWED_FILE_MIMES.get(ext)
    if mime:
        return mime
    if _looks_like_utf8_text(path):
        return "text/plain"
    guessed, _encoding = mimetypes.guess_type(path.name)
    return normalize_attachment_mime(guessed) or OPAQUE_MIME


def _ensure_existing_file(path: Path) -> None:
    if not path.exists():
        raise ValueError(f"File not found: {path}")
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")


def _inline_attachment(path: Path, mime: str) -> dict[str, Any]:
    return {
        "type": mime,
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        "name": path.name,
    }


def _check_size_policy(path: Path, mime: str) -> int:
    size = path.stat().st_size
    limit = attachment_size_limit_for_mime(mime)
    if size > limit:
        category = attachment_category(mime)
        if category == "pdf":
            detail = f"{CLI_STAGED_PDF_BYTES} byte PDF limit"
        elif category == "text":
            detail = (
                f"{MAX_STAGED_TEXT_BYTES} byte staged text limit; "
                "use /path for bounded local reads"
            )
        elif category == "image":
            detail = f"{CLI_IMAGE_ATTACHMENT_BYTES} byte image attachment limit"
        elif category == "office":
            detail = f"{OFFICE_ATTACHMENT_BYTES} byte office document limit"
        elif category == "email":
            detail = (
                f"{CLI_TEXT_ATTACHMENT_BYTES} byte email limit "
                "(emails are extracted as bounded text and are never staged)"
            )
        else:
            detail = f"{OPAQUE_ATTACHMENT_BYTES} byte attachment limit"
        raise ValueError(f"File too large: {path.name} is {size} bytes; max is {detail}")
    return size


def _parse_path_prompt(command: str, prefix: str, usage: str) -> tuple[Path, str]:
    rest = command[len(prefix) :].strip()
    if not rest:
        raise ValueError(usage)

    if rest[0] in {'"', "'"}:
        quote = rest[0]
        end = rest.find(quote, 1)
        if end == -1:
            raise ValueError(f"{usage} (unclosed quote)")
        token = rest[1:end]
        prompt = rest[end + 1 :].strip()
    else:
        parts = rest.split(None, 1)
        token = parts[0]
        prompt = parts[1] if len(parts) > 1 else ""

    if not token:
        raise ValueError(usage)
    return Path(token).expanduser(), prompt


def build_file_attachment(
    path: str | Path,
    *,
    upload_callable: UploadCallable | None = None,
) -> dict[str, Any]:
    local = Path(path).expanduser()
    _ensure_existing_file(local)
    mime = mime_for_path(local)
    size = _check_size_policy(local, mime)
    if size <= CLI_INLINE_THRESHOLD_BYTES:
        return _inline_attachment(local, mime)
    if not _can_stage_mime(mime):
        # Unreachable for email in practice (the size policy caps email at the
        # inline threshold), kept as a fail-closed guard for future categories.
        raise ValueError(
            f"File too large to attach directly ({size} bytes > "
            f"{CLI_INLINE_THRESHOLD_BYTES}); this attachment type is not staged. "
            "Use /path for bounded local reads."
        )
    if upload_callable is None:
        raise ValueError(
            f"File too large to inline ({size} bytes > {CLI_INLINE_THRESHOLD_BYTES}); "
            "gateway bridge upload is required for this file"
        )
    try:
        file_uuid = upload_callable(local, mime, local.name)
    except Exception as exc:  # noqa: BLE001 - caller gets a CLI-facing error
        raise ValueError(
            f"File too large to inline ({size} bytes); gateway upload endpoint unavailable: {exc}"
        ) from exc
    return {"type": mime, "file_uuid": file_uuid, "name": local.name, "mime": mime}


async def build_file_attachment_async(
    path: str | Path,
    *,
    upload_callable: AsyncUploadCallable | None = None,
) -> dict[str, Any]:
    local = Path(path).expanduser()
    _ensure_existing_file(local)
    mime = mime_for_path(local)
    size = _check_size_policy(local, mime)
    if size <= CLI_INLINE_THRESHOLD_BYTES:
        return _inline_attachment(local, mime)
    if not _can_stage_mime(mime):
        # Unreachable for email in practice (the size policy caps email at the
        # inline threshold), kept as a fail-closed guard for future categories.
        raise ValueError(
            f"File too large to attach directly ({size} bytes > "
            f"{CLI_INLINE_THRESHOLD_BYTES}); this attachment type is not staged. "
            "Use /path for bounded local reads."
        )
    if upload_callable is None:
        raise ValueError(
            f"File too large to inline ({size} bytes > {CLI_INLINE_THRESHOLD_BYTES}); "
            "gateway bridge upload is required for this file"
        )
    try:
        file_uuid = await upload_callable(local, mime, local.name)
    except Exception as exc:  # noqa: BLE001 - caller gets a CLI-facing error
        raise ValueError(
            f"File too large to inline ({size} bytes); gateway upload endpoint unavailable: {exc}"
        ) from exc
    return {"type": mime, "file_uuid": file_uuid, "name": local.name, "mime": mime}


def file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: UploadCallable | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    path, prompt = _parse_path_prompt(command, "/file ", "Usage: /file <path> [prompt]")
    prompt = prompt or "Read this file"
    return prompt, [build_file_attachment(path, upload_callable=upload_callable)]


async def async_file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: AsyncUploadCallable | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    path, prompt = _parse_path_prompt(command, "/file ", "Usage: /file <path> [prompt]")
    prompt = prompt or "Read this file"
    return prompt, [await build_file_attachment_async(path, upload_callable=upload_callable)]


def attachments_from_paths(paths: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    return [build_file_attachment(path) for path in paths]


def image_prompt_from_command(command: str) -> str:
    _path, prompt = _parse_path_prompt(command, "/image ", "Usage: /image <path> [prompt]")
    return prompt or "Describe this image"


def image_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, str]]]:
    path, prompt = _parse_path_prompt(command, "/image ", "Usage: /image <path> [prompt]")
    prompt = prompt or "Describe this image"
    _ensure_existing_file(path)

    ext = path.suffix.lower().lstrip(".")
    media_type = CLI_IMAGE_MIMES.get(ext)
    if not media_type:
        raise ValueError(f"Unsupported format: {ext}. Use png/jpg/gif/webp")
    _check_size_policy(path, media_type)

    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return prompt, [{"type": media_type, "data": data, "name": path.name}]


def parse_path_command(command: str) -> tuple[Path, str]:
    rest = command[len("/path") :].strip()
    if not rest:
        raise ValueError("Usage: /path <path> [prompt]")
    if "\r" in rest or "\n" in rest:
        raise ValueError("Invalid /path path token: '<', '>', CR, and LF are not allowed.")

    if rest[0] in {'"', "'"}:
        quote = rest[0]
        end = rest.find(quote, 1)
        if end == -1:
            raise ValueError("Usage: /path <path> [prompt] (unclosed quote)")
        token = rest[1:end]
        prompt = rest[end + 1 :].strip()
    else:
        words = rest.split()
        token = ""
        prompt = ""
        for count in range(len(words), 0, -1):
            candidate = " ".join(words[:count])
            if Path(candidate).expanduser().exists():
                token = candidate
                prompt = " ".join(words[count:]).strip()
                break
        if not token:
            token = words[0]
            prompt = rest[len(token) :].strip()

    if not token:
        raise ValueError("Usage: /path <path> [prompt]")
    if any(ch in token for ch in ("<", ">", "\r", "\n")):
        raise ValueError("Invalid /path path token: '<', '>', CR, and LF are not allowed.")
    return Path(token).expanduser(), prompt


def path_strategy_hint(path: Path) -> str:
    if not path.exists():
        raise ValueError(f"File not found: {path}")
    if path.is_dir():
        return (
            "This is a directory. Start with list_dir(path=...), then use "
            "glob_search(pattern=..., path=...) and grep_search(pattern=..., path=...) "
            "to inspect relevant files without uploading bytes."
        )

    ext = path.suffix.lower()
    if ext == ".pdf":
        raise ValueError("PDF path analysis is not supported by /path. Use /file <path> instead.")
    if ext in PATH_SPREADSHEET_EXTENSIONS:
        return (
            "This looks like a spreadsheet. Use read_spreadsheet(path=..., offset=..., "
            "limit=...) to inspect bounded rows."
        )
    if ext in PATH_OBVIOUS_BINARY_EXTENSIONS:
        raise ValueError(
            f"{path.name} looks like a binary/container file and is not suitable for /path. "
            "Use /file if upload is intended."
        )

    try:
        with path.open("rb") as fh:
            sample = fh.read(8192)
    except OSError as exc:
        raise ValueError(f"Cannot inspect path: {path} ({exc})") from exc
    if b"\x00" in sample:
        raise ValueError(
            f"{path.name} appears to contain binary NUL bytes and is not suitable for /path. "
            "Use /file if upload is intended."
        )

    if ext in PATH_TEXT_EXTENSIONS:
        return (
            "This looks like a text/log/markdown/json/html file. Use read_file(path=..., "
            "offset=..., limit=...) for bounded windows and grep_search(pattern=..., path=...) "
            "to find relevant lines."
        )
    return (
        "This appears to be a local UTF-8-compatible file. Use read_file(path=..., "
        "offset=..., limit=...) for bounded windows and grep_search(pattern=..., path=...) "
        "for targeted search; if a tool reports binary content, stop and ask the user to use /file."
    )


def path_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, Any]]]:
    path, user_prompt = parse_path_command(command)
    absolute = path.resolve(strict=False)
    strategy = path_strategy_hint(absolute)
    prompt = user_prompt or "Analyze this local path."
    full_prompt = (
        f"{prompt}\n\n"
        "Local path analysis request (no upload):\n"
        f"- Path: {absolute}\n"
        "- The CLI did not upload or attach file bytes; attachments=[] for this turn.\n"
        "- The path string above is sent in this chat prompt and may be stored in the "
        "conversation transcript.\n"
        "- Use local filesystem tools on this same machine only; prefer bounded reads "
        "for large files.\n"
        f"- Suggested strategy: {strategy}"
    )
    return full_prompt, []
