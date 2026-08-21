"""Generated artifact material references and storage helpers."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import stat
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from opensquilla.attachment_refs import _atomic_write_bytes, _link_or_copy, _validate_sha256
from opensquilla.paths import native_io_path
from opensquilla.profile_import_io import reparse_tag_redirects

_log = logging.getLogger(__name__)

ARTIFACT_REF_KIND = "artifact_ref"
ARTIFACT_STORE = "artifacts"
ARTIFACT_SESSION_BUCKET = "s"
ARTIFACT_MATERIAL_NAME = "data"
ARTIFACT_BUNDLE_MANIFEST_NAME = "bundle.json"
ARTIFACT_BUNDLE_BLOBS_DIR = "blobs"
ARTIFACT_BUNDLE_VERSION = 1
ARTIFACT_THUMBNAIL_NAME = "thumb.webp"
ARTIFACT_INTERNAL_MARKER_NAME = ".internal-revision"
ARTIFACT_OWNERSHIP_MARKER_NAME = ".artifact-id"
ARTIFACT_THUMBNAIL_MAX_EDGE = 512
ARTIFACT_THUMBNAIL_QUALITY = 80
ARTIFACT_STORE_TOKEN_CHARS = 12
LEGACY_ARTIFACT_STORE_TOKEN_CHARS = 16
DEFAULT_ARTIFACT_MAX_BYTES = 30 * 1024 * 1024
DEFAULT_ARTIFACT_DISK_BUDGET_BYTES = 512 * 1024 * 1024
DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_ARTIFACT_BUNDLE_MAX_FILES = 2000
DEFAULT_ARTIFACT_BUNDLE_MAX_DEPTH = 32
INSTALLER_ARTIFACT_MAX_BYTES = DEFAULT_ARTIFACT_DISK_BUDGET_BYTES
INSTALLER_ARTIFACT_SUFFIXES = frozenset(
    {
        ".appimage",
        ".deb",
        ".dmg",
        ".exe",
        ".msi",
        ".rpm",
        ".snap",
        ".zip",
    }
)
_INSTALLER_MIME_BY_SUFFIX = {
    ".appimage": "application/octet-stream",
    ".deb": "application/vnd.debian.binary-package",
    ".dmg": "application/x-apple-diskimage",
    ".exe": "application/vnd.microsoft.portable-executable",
    ".msi": "application/x-msi",
    ".rpm": "application/x-rpm",
    ".snap": "application/octet-stream",
    ".zip": "application/zip",
}

_UNSAFE_FILENAME_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_MIME_RE = re.compile(r"^[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+$")
_PERCENT_PATH_HAZARD_RE = re.compile(r"%[0-9a-f]{2}", re.IGNORECASE)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CSS_URL_RE = re.compile(
    r"""url\(\s*(?:"([^"]+)"|'([^']+)'|([^)"'\s]+))\s*\)""",
    re.IGNORECASE,
)
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?!url\s*\()(?:"([^"]+)"|'([^']+)'|([^"'();\s]+))""",
    re.IGNORECASE,
)
_JS_SERVICE_WORKER_RECEIVER_RE = (
    r"""\b(?:(?:navigator\s*(?:\.\s*serviceWorker|\?\.\s*serviceWorker"""
    r"""|\[\s*["']serviceWorker["']\s*\]"""
    r"""|\?\.\s*\[\s*["']serviceWorker["']\s*\]))|serviceWorker)"""
)
_JS_SERVICE_WORKER_REGISTER_CALL_RE = (
    _JS_SERVICE_WORKER_RECEIVER_RE
    + r"""(?:\s*\.\s*register|\s*\?\.\s*register"""
    + r"""|\s*\[\s*["']register["']\s*\]"""
    + r"""|\s*\?\.\s*\[\s*["']register["']\s*\])\s*\("""
)
_JS_REFERENCE_RES = (
    re.compile(
        r"""\b(?:import|export)\s+(?:[^"'();]*?\s+from\s+)?["']([^"']+)["']""",
        re.MULTILINE,
    ),
    re.compile(r"""\bimport\s*\(\s*["']([^"']+)["']\s*\)"""),
    re.compile(r"""\bfetch\s*\(\s*["']([^"']+)["']"""),
    re.compile(r"""\b(?:new\s+)?(?:Shared)?Worker\s*\(\s*["']([^"']+)["']"""),
    re.compile(
        r"""\bnew\s+URL\s*\(\s*["']([^"']+)["']\s*,\s*import\.meta\.url\s*\)"""
    ),
    re.compile(
        _JS_SERVICE_WORKER_REGISTER_CALL_RE + r"""\s*["']([^"']+)["']"""
    ),
)
_JS_DYNAMIC_REFERENCE_RES = (
    re.compile(
        r"""\b(?:import|fetch)\s*\((?!\s*["'])"""
        r"""|\b(?:Shared)?Worker\s*\((?!\s*["'])"""
    ),
    re.compile(_JS_SERVICE_WORKER_REGISTER_CALL_RE + r"""\s*(?!["'])"""),
)
_JS_NEW_URL_CALL_RE = re.compile(r"""\bnew\s+URL\s*\(""")
_JS_IMPORT_META_URL_EXPRESSION_RE = re.compile(r"""import\s*\.\s*meta\s*\.\s*url""")
_JS_STATIC_STRING_EXPRESSION_RE = re.compile(
    r"""(?:"[^"\\\r\n]*"|'[^'\\\r\n]*')"""
)
_BUNDLE_PARSE_SUFFIXES = {
    ".cjs": "javascript",
    ".css": "css",
    ".htm": "html",
    ".html": "html",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".xhtml": "html",
}
_SENSITIVE_DIRECTORY_NAMES = frozenset({".aws", ".git", ".hg", ".ssh", ".svn"})
_SENSITIVE_EXACT_FILENAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "client_secret.json",
        "client_secrets.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets",
        "secrets.json",
        "secrets.toml",
        "secrets.yaml",
        "secrets.yml",
        "service-account-key.json",
        "service-account.json",
        "service_account_key.json",
        "service_account.json",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".cer", ".crt", ".key", ".p12", ".pem", ".pfx"})
_SENSITIVE_STRUCTURED_FILENAME_RE = re.compile(
    r"""(?:^|.*[-_.])"""
    r"""(?:client[-_.]?secrets?|credentials|secrets?|service[-_.]?account)"""
    r"""(?:[-_.].*)?\.(?:json|toml|ya?ml)"""
)
_ARTIFACT_MARKER_RE = re.compile(
    # Anchor on the marker's guaranteed ` (mime)]` tail rather than stopping at
    # the first ']' — a name may legitimately contain ']'. Do NOT consume
    # surrounding whitespace (that glued adjacent words/lines together);
    # strip_artifact_markers_from_text collapses residual spacing afterwards.
    r"\[generated artifact omitted:\s*[^\n]+? "
    r"\((?:[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+|artifact)\)\]",
    re.IGNORECASE,
)
_PUBLIC_ARTIFACT_FIELDS = (
    "id",
    "kind",
    "sha256",
    "name",
    "mime",
    "size",
    "session_id",
    "source",
    "created_at",
    "store",
)


class ArtifactError(ValueError):
    """Base class for artifact store errors."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when an artifact id is absent for the requested session."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when material bytes no longer match artifact metadata."""


class ArtifactBundleUnsupportedError(ArtifactIntegrityError):
    """Raised when a bundle sidecar uses an unknown manifest version."""


class ArtifactBudgetError(ArtifactError):
    """Raised when artifact publication exceeds file or disk budgets."""


class ArtifactPathError(ArtifactError):
    """Raised when a tool tries to publish a disallowed path."""


@dataclass(frozen=True)
class ArtifactBundleFile:
    """One immutable logical file recorded in an artifact bundle manifest."""

    path: str
    mime: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArtifactBundleFile:
        mime = payload.get("mime")
        if not isinstance(mime, str) or _safe_mime(mime) != mime:
            raise ArtifactIntegrityError("artifact bundle MIME type is invalid")
        return cls(
            path=_normalize_bundle_logical_path(payload.get("path")),
            mime=mime,
            sha256=_validate_sha256(payload.get("sha256")),
            size=_validate_size(payload.get("size")),
        )


@dataclass(frozen=True)
class ArtifactBundleManifest:
    """Versioned, content-addressed description of a static webpage bundle."""

    version: int
    entrypoint: str
    files: tuple[ArtifactBundleFile, ...]
    collection_status: str
    warning_codes: tuple[str, ...]
    total_size: int
    file_count: int
    bundle_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "entrypoint": self.entrypoint,
            "files": [item.to_dict() for item in self.files],
            "collection_status": self.collection_status,
            "warning_codes": list(self.warning_codes),
            "total_size": self.total_size,
            "file_count": self.file_count,
            "bundle_digest": self.bundle_digest,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArtifactBundleManifest:
        version = payload.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != ARTIFACT_BUNDLE_VERSION
        ):
            raise ArtifactBundleUnsupportedError(
                "artifact bundle version is unsupported"
            )
        entrypoint = _normalize_bundle_logical_path(payload.get("entrypoint"))
        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ArtifactIntegrityError("artifact bundle file list is invalid")
        files = tuple(
            ArtifactBundleFile.from_dict(item)
            for item in raw_files
            if isinstance(item, dict)
        )
        if len(files) != len(raw_files):
            raise ArtifactIntegrityError("artifact bundle file list is invalid")
        paths = [item.path for item in files]
        if (
            paths != sorted(paths)
            or len(paths) != len(set(paths))
            or len(paths) != len({path.casefold() for path in paths})
        ):
            raise ArtifactIntegrityError("artifact bundle file paths are not canonical")
        if entrypoint not in set(paths):
            raise ArtifactIntegrityError("artifact bundle entrypoint is missing")
        collection_status = payload.get("collection_status")
        if collection_status not in {"complete", "partial"}:
            raise ArtifactIntegrityError("artifact bundle collection status is invalid")
        raw_warnings = payload.get("warning_codes")
        if not isinstance(raw_warnings, list) or any(
            not isinstance(item, str) or not item for item in raw_warnings
        ):
            raise ArtifactIntegrityError("artifact bundle warning codes are invalid")
        warning_codes = tuple(sorted(set(raw_warnings)))
        if list(warning_codes) != raw_warnings:
            raise ArtifactIntegrityError("artifact bundle warning codes are not canonical")
        total_size = _validate_size(payload.get("total_size"))
        file_count = _validate_size(payload.get("file_count"))
        if total_size != sum(item.size for item in files) or file_count != len(files):
            raise ArtifactIntegrityError("artifact bundle totals do not match its files")
        digest = _validate_sha256(payload.get("bundle_digest"))
        manifest = cls(
            version=ARTIFACT_BUNDLE_VERSION,
            entrypoint=entrypoint,
            files=files,
            collection_status=collection_status,
            warning_codes=warning_codes,
            total_size=total_size,
            file_count=file_count,
            bundle_digest=digest,
        )
        if _artifact_bundle_digest(manifest.to_dict()) != digest:
            raise ArtifactIntegrityError("artifact bundle manifest digest mismatch")
        return manifest


def is_complete_single_file_preview_bundle(
    manifest: ArtifactBundleManifest | None,
) -> bool:
    """Return whether a bundle can be handled as its sole canonical source file.

    A warning or partial collection means referenced material may be absent even
    when the manifest currently contains one file.  Those cases must retain the
    conservative multi-file bundle behavior.
    """

    return bool(
        manifest is not None
        and manifest.collection_status == "complete"
        and not manifest.warning_codes
        and _is_single_file_preview_bundle(manifest)
    )


def _is_single_file_preview_bundle(manifest: ArtifactBundleManifest) -> bool:
    return bool(
        manifest.file_count == 1
        and len(manifest.files) == 1
        and manifest.files[0].path == manifest.entrypoint
    )


@dataclass(frozen=True)
class ArtifactBundleSourceFile:
    """In-memory source file used while atomically publishing a bundle."""

    path: str
    mime: str
    data: bytes


@dataclass(frozen=True)
class ArtifactBundle:
    """A validated snapshot ready for publication."""

    entrypoint: str
    files: tuple[ArtifactBundleSourceFile, ...]
    collection_status: str = "complete"
    warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactPreviewResource:
    """Resolved preview file with integrity-checked metadata."""

    ref: ArtifactRef
    logical_path: str
    mime: str
    sha256: str
    size: int
    path: Path


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    sha256: str
    name: str
    mime: str
    size: int
    session_id: str
    session_key: str
    source: str
    created_at: str
    download_url: str
    kind: str = ARTIFACT_REF_KIND
    store: str = ARTIFACT_STORE
    has_thumbnail: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArtifactRef:
        return cls(
            id=_validate_artifact_id(payload.get("id")),
            sha256=_validate_sha256(payload.get("sha256")),
            name=_safe_filename(str(payload.get("name") or "artifact")),
            mime=_safe_mime(payload.get("mime")),
            size=_validate_size(payload.get("size")),
            session_id=_validate_non_empty("session_id", payload.get("session_id")),
            session_key=_validate_non_empty("session_key", payload.get("session_key")),
            source=str(payload.get("source") or "unknown"),
            created_at=str(payload.get("created_at") or ""),
            download_url=str(payload.get("download_url") or ""),
            kind=str(payload.get("kind") or ARTIFACT_REF_KIND),
            store=str(payload.get("store") or ARTIFACT_STORE),
            has_thumbnail=bool(payload.get("has_thumbnail")),
        )


@dataclass(frozen=True)
class ArtifactRefPage:
    """One backwards-pagination page from a session's on-disk artifact index."""

    refs: tuple[ArtifactRef, ...]
    has_more: bool
    total_count: int


def artifact_marker(ref: dict[str, Any] | ArtifactRef) -> str:
    payload = ref.to_dict() if isinstance(ref, ArtifactRef) else ref
    name = payload.get("name") if isinstance(payload.get("name"), str) else "artifact"
    mime = payload.get("mime") if isinstance(payload.get("mime"), str) else "artifact"
    return f"[generated artifact omitted: {name} ({mime})]"


def strip_artifact_markers_from_text(text: str) -> str:
    if "[generated artifact omitted:" not in text:
        return text
    cleaned = _ARTIFACT_MARKER_RE.sub("", text.replace("\r\n", "\n"))
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def artifact_payload(event_or_ref: Any) -> dict[str, Any]:
    if isinstance(event_or_ref, ArtifactRef):
        raw = event_or_ref.to_dict()
    elif isinstance(event_or_ref, dict):
        raw = dict(event_or_ref)
    else:
        raw = {
            field: getattr(event_or_ref, field)
            for field in (*_PUBLIC_ARTIFACT_FIELDS, "download_url", "has_thumbnail")
            if hasattr(event_or_ref, field)
        }
    payload = {field: raw[field] for field in _PUBLIC_ARTIFACT_FIELDS if field in raw}
    artifact_id = payload.get("id")
    if artifact_id:
        payload["id"] = _validate_artifact_id(artifact_id)
        payload["download_url"] = artifact_download_url(payload["id"])
        # The public payload drops the internal ``has_thumbnail`` boolean and only
        # carries the reconstructed ``thumbnail_url`` string. A persisted transcript
        # artifact is therefore a public payload replayed through this helper: honor
        # an already-present ``thumbnail_url`` so the thumbnail survives history replay,
        # falling back to reconstruction from ``has_thumbnail`` for live events.
        if raw.get("has_thumbnail") or raw.get("thumbnail_url"):
            payload["thumbnail_url"] = artifact_thumbnail_url(payload["id"])
    return payload


def artifact_download_url(artifact_id: str) -> str:
    return f"/api/v1/artifacts/{_validate_artifact_id(artifact_id)}"


def artifact_cursor(ref: ArtifactRef) -> str:
    """Return the stable opaque cursor used by artifact list pagination."""

    return _validate_artifact_id(ref.id)


def validate_artifact_cursor(value: Any) -> str:
    """Validate and normalize a client-provided artifact pagination cursor."""

    return _validate_artifact_id(value)


def is_installer_artifact_name(name: str | Path) -> bool:
    return Path(str(name)).suffix.casefold() in INSTALLER_ARTIFACT_SUFFIXES


def installer_artifact_mime(name: str | Path) -> str | None:
    return _INSTALLER_MIME_BY_SUFFIX.get(Path(str(name)).suffix.casefold())


def artifact_mime_for_name(name: str | Path) -> str:
    return (
        installer_artifact_mime(name)
        or mimetypes.guess_type(str(name))[0]
        or "application/octet-stream"
    )


def artifact_publish_max_bytes_for_name(
    name: str | Path,
    configured_max_bytes: int | None,
) -> int | None:
    if not is_installer_artifact_name(name):
        return configured_max_bytes
    if configured_max_bytes is None:
        return None
    return max(configured_max_bytes, INSTALLER_ARTIFACT_MAX_BYTES)


def artifact_thumbnail_url(artifact_id: str) -> str:
    return f"{artifact_download_url(artifact_id)}?variant=thumb"


def enrich_artifact_event_dict(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Add a client-facing ``thumbnail_url`` to a serialized artifact event dict.

    The event dataclass carries the ``has_thumbnail`` boolean; this rebuilds the
    public variant URL from the artifact id when a thumbnail exists. The internal
    boolean is dropped so the wire payload matches the public artifact contract.
    """

    has_thumbnail = bool(event_dict.pop("has_thumbnail", False))
    artifact_id = event_dict.get("id")
    if has_thumbnail and isinstance(artifact_id, str) and artifact_id:
        try:
            event_dict["thumbnail_url"] = artifact_thumbnail_url(artifact_id)
        except ValueError:
            pass
    return event_dict


def _normalize_bundle_logical_path(value: Any) -> str:
    if not isinstance(value, str):
        raise ArtifactPathError("artifact bundle path is invalid")
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized.startswith(("/", "\\"))
        or "\\" in normalized
        or _WINDOWS_DRIVE_RE.match(normalized)
        or _PERCENT_PATH_HAZARD_RE.search(normalized)
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise ArtifactPathError("artifact bundle path is invalid")
    parts = normalized.split("/")
    if (
        len(parts) > DEFAULT_ARTIFACT_BUNDLE_MAX_DEPTH
        or any(not part or part in {".", ".."} or ":" in part for part in parts)
    ):
        raise ArtifactPathError("artifact bundle path is invalid")
    return "/".join(parts)


def _artifact_bundle_digest(payload: dict[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("bundle_digest", None)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sensitive_bundle_path(logical_path: str) -> bool:
    parts = logical_path.split("/")
    folded_parts = [part.casefold() for part in parts]
    if any(part in _SENSITIVE_DIRECTORY_NAMES for part in folded_parts):
        return True
    filename = folded_parts[-1]
    if filename.startswith(".env"):
        return True
    if filename in _SENSITIVE_EXACT_FILENAMES:
        return True
    if Path(filename).suffix in _SENSITIVE_SUFFIXES:
        return True
    return _SENSITIVE_STRUCTURED_FILENAME_RE.fullmatch(filename) is not None


def _is_reparse_point(path: Path) -> bool:
    try:
        value = native_io_path(path).lstat()
    except OSError:
        return False
    attributes = int(getattr(value, "st_file_attributes", 0))
    if not attributes & 0x400:
        return False
    return reparse_tag_redirects(int(getattr(value, "st_reparse_tag", 0)))


def _read_regular_bundle_file(path: Path) -> bytes:
    native_path = native_io_path(path)
    before = native_path.lstat()
    if stat.S_ISLNK(before.st_mode) or _is_reparse_point(path):
        raise ArtifactPathError("artifact bundle cannot contain links or reparse points")
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactPathError("artifact bundle can contain regular files only")
    data = native_path.read_bytes()
    after = native_path.lstat()
    if (
        stat.S_ISLNK(after.st_mode)
        or _is_reparse_point(path)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(data) != after.st_size
    ):
        raise ArtifactPathError("artifact bundle file changed while it was collected")
    return data


def _ensure_no_bundle_link_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ArtifactPathError("artifact bundle path escapes its root") from exc
    current = root
    for component in relative.parts:
        current /= component
        try:
            if native_io_path(current).is_symlink() or _is_reparse_point(current):
                raise ArtifactPathError(
                    "artifact bundle cannot contain links or reparse points"
                )
        except OSError as exc:
            raise ArtifactPathError("artifact bundle path cannot be inspected") from exc


class _BundleHTMLReferenceParser(HTMLParser):
    _URL_ATTRIBUTES = frozenset({"data", "href", "poster", "src"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.dynamic_reference = False
        self._text_mode: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered_tag = tag.casefold()
        attributes = {key.casefold(): value for key, value in attrs}
        if lowered_tag == "base":
            base_href = (attributes.get("href") or "").strip()
            if base_href not in {"", ".", "./", "/"}:
                self.dynamic_reference = True
        else:
            for key in self._URL_ATTRIBUTES:
                value = attributes.get(key)
                if value:
                    self.references.append(value)
        srcset = attributes.get("srcset")
        if srcset:
            for candidate in srcset.split(","):
                value = candidate.strip().split(maxsplit=1)[0]
                if value:
                    self.references.append(value)
        inline_style = attributes.get("style")
        if inline_style:
            self.references.extend(_extract_css_references(inline_style))
        if lowered_tag == "style":
            self._text_mode = "css"
            self._text_parts = []
        elif lowered_tag == "script" and not attributes.get("src"):
            script_type = (attributes.get("type") or "").casefold()
            if script_type == "importmap":
                self._text_mode = "importmap"
            elif (
                not script_type
                or script_type == "module"
                or "javascript" in script_type
                or "ecmascript" in script_type
            ):
                self._text_mode = "javascript"
            else:
                self._text_mode = None
            self._text_parts = []

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self._text_mode = None

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._text_mode is not None:
            text = "".join(self._text_parts)
            if self._text_mode == "css":
                self.references.extend(_extract_css_references(text))
            elif self._text_mode == "javascript":
                references, dynamic = _extract_javascript_references(text)
                self.references.extend(references)
                self.dynamic_reference = self.dynamic_reference or dynamic
            elif self._text_mode == "importmap":
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    self.dynamic_reference = True
                else:
                    self.references.extend(_import_map_references(payload))
            self._text_mode = None
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._text_mode is not None:
            self._text_parts.append(data)


def _extract_css_references(text: str) -> list[str]:
    references: list[str] = []
    for pattern in (_CSS_URL_RE, _CSS_IMPORT_RE):
        for match in pattern.finditer(text):
            value = next((group for group in match.groups() if group is not None), "")
            if value:
                references.append(value.strip())
    return references


def _javascript_call_arguments(
    text: str,
    opening_parenthesis: int,
) -> tuple[str, ...] | None:
    """Split one JavaScript call without executing or fully parsing its source."""

    closing_for = {"(": ")", "[": "]", "{": "}"}
    stack = ["("]
    arguments: list[str] = []
    argument_start = opening_parenthesis + 1
    quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    index = argument_start
    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_line_comment:
            if character in "\r\n":
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            if character == "*" and following == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is not None:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = None
            index += 1
            continue
        if character == "/" and following == "/":
            in_line_comment = True
            index += 2
            continue
        if character == "/" and following == "*":
            in_block_comment = True
            index += 2
            continue
        if character in {'"', "'", "`"}:
            quote = character
            index += 1
            continue
        if character in closing_for:
            stack.append(character)
            index += 1
            continue
        if character in {")", "]", "}"}:
            if not stack or closing_for[stack[-1]] != character:
                return None
            if len(stack) == 1:
                arguments.append(text[argument_start:index])
                return tuple(arguments)
            stack.pop()
            index += 1
            continue
        if character == "," and len(stack) == 1:
            arguments.append(text[argument_start:index])
            argument_start = index + 1
        index += 1
    return None


def _has_dynamic_import_meta_url_reference(text: str) -> bool:
    for match in _JS_NEW_URL_CALL_RE.finditer(text):
        arguments = _javascript_call_arguments(text, match.end() - 1)
        if arguments is None or len(arguments) < 2:
            continue
        second_argument = arguments[1].strip()
        if _JS_IMPORT_META_URL_EXPRESSION_RE.fullmatch(second_argument) is None:
            continue
        first_argument = arguments[0].strip()
        if (
            len(arguments) != 2
            or _JS_STATIC_STRING_EXPRESSION_RE.fullmatch(first_argument) is None
        ):
            return True
    return False


def _extract_javascript_references(text: str) -> tuple[list[str], bool]:
    references: list[str] = []
    for pattern in _JS_REFERENCE_RES:
        references.extend(match.group(1) for match in pattern.finditer(text))
    has_dynamic_reference = any(
        pattern.search(text) is not None for pattern in _JS_DYNAMIC_REFERENCE_RES
    )
    return references, (
        has_dynamic_reference or _has_dynamic_import_meta_url_reference(text)
    )


def _import_map_references(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    references: list[str] = []
    imports = payload.get("imports")
    if isinstance(imports, dict):
        references.extend(value for value in imports.values() if isinstance(value, str))
    scopes = payload.get("scopes")
    if isinstance(scopes, dict):
        for mappings in scopes.values():
            if isinstance(mappings, dict):
                references.extend(
                    value for value in mappings.values() if isinstance(value, str)
                )
    return references


def _extract_bundle_references(
    path: str,
    data: bytes,
    *,
    force_html: bool = False,
) -> tuple[list[str], bool]:
    parser_kind = (
        "html" if force_html else _BUNDLE_PARSE_SUFFIXES.get(Path(path).suffix.casefold())
    )
    if parser_kind is None:
        return [], False
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ArtifactPathError("artifact bundle source is not valid UTF-8") from exc
    if parser_kind == "css":
        return _extract_css_references(text), False
    if parser_kind == "javascript":
        return _extract_javascript_references(text)
    parser = _BundleHTMLReferenceParser()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError):
        parser.dynamic_reference = True
    return parser.references, parser.dynamic_reference


def legacy_html_bundle_warning_codes(
    entrypoint: str,
    data: bytes,
) -> tuple[str, ...]:
    """Report legacy single-file HTML that cannot satisfy local dependencies.

    Historical artifacts have no bundle manifest, so the preview server can
    only serve the entry HTML. Keep pure inline/CDN documents compatible, but
    surface a deterministic partial-preview warning when the document contains
    a literal local dependency, an unsafe local-looking path, a dynamic
    dependency expression, or content that cannot be inspected as UTF-8.
    """

    try:
        references, dynamic_reference = _extract_bundle_references(
            entrypoint,
            data,
            force_html=True,
        )
    except ArtifactPathError:
        return ("legacy_single_file_dependencies_unavailable",)
    if dynamic_reference:
        return ("legacy_single_file_dependencies_unavailable",)
    for reference in references:
        try:
            if _resolve_bundle_reference(reference, source_path=entrypoint) is not None:
                return ("legacy_single_file_dependencies_unavailable",)
        except ArtifactPathError:
            return ("legacy_single_file_dependencies_unavailable",)
    return ()


def _resolve_bundle_reference(reference: str, *, source_path: str) -> str | None:
    raw = reference.strip()
    if not raw or raw.startswith(("#", "//")):
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ArtifactPathError("artifact bundle dependency URL is invalid") from exc
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    decoded = unquote(parsed.path)
    if _PERCENT_PATH_HAZARD_RE.search(decoded):
        raise ArtifactPathError("artifact bundle dependency uses unsafe double encoding")
    if "\\" in decoded or "\x00" in decoded:
        raise ArtifactPathError("artifact bundle dependency path is invalid")
    if decoded.startswith("/"):
        combined = decoded.lstrip("/")
    else:
        combined = posixpath.join(posixpath.dirname(source_path), decoded)
    normalized = posixpath.normpath(combined)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise ArtifactPathError("artifact bundle dependency escapes its root")
    return _normalize_bundle_logical_path(normalized)


def _validate_bundle_file_set(
    files: list[ArtifactBundleSourceFile],
    *,
    entrypoint: str,
    max_bytes: int,
    max_files: int,
) -> None:
    if not files:
        raise ArtifactBudgetError("artifact bundle is empty")
    if len(files) > max_files:
        raise ArtifactBudgetError(
            f"artifact bundle exceeds file-count budget ({len(files)} > {max_files})"
        )
    total_size = 0
    collision_keys: set[str] = set()
    paths: set[str] = set()
    for item in files:
        logical_path = _normalize_bundle_logical_path(item.path)
        if logical_path != item.path:
            raise ArtifactPathError("artifact bundle paths must be normalized")
        collision_key = logical_path.casefold()
        if collision_key in collision_keys or logical_path in paths:
            raise ArtifactPathError("artifact bundle contains a path collision")
        collision_keys.add(collision_key)
        paths.add(logical_path)
        if _is_sensitive_bundle_path(logical_path):
            raise ArtifactPathError("artifact bundle contains a sensitive path")
        if item.path == entrypoint and len(item.data) > DEFAULT_ARTIFACT_MAX_BYTES:
            raise ArtifactBudgetError(
                "artifact bundle entrypoint exceeds per-file budget "
                f"({len(item.data)} > {DEFAULT_ARTIFACT_MAX_BYTES})"
            )
        total_size += len(item.data)
    if entrypoint not in paths:
        raise ArtifactPathError("artifact bundle entrypoint is missing")
    if total_size > max_bytes:
        raise ArtifactBudgetError(
            f"artifact bundle exceeds total budget ({total_size} > {max_bytes})"
        )


def collect_artifact_bundle(
    entry_path: str | Path,
    *,
    workspace_root: str | Path,
    mode: str = "auto",
    bundle_root: str | Path | None = None,
    entry_mime: str | None = None,
    max_bytes: int = DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES,
    max_files: int = DEFAULT_ARTIFACT_BUNDLE_MAX_FILES,
) -> ArtifactBundle | None:
    """Collect a safe, deterministic static webpage snapshot without executing code.

    ``auto`` follows statically discoverable local HTML/CSS/JavaScript references.
    ``directory`` snapshots every regular file below an explicit dedicated root.
    Non-HTML ``auto`` publications and ``none`` remain legacy single-file artifacts.
    """

    if mode not in {"auto", "directory", "none"}:
        raise ArtifactPathError("artifact bundle mode must be auto, directory, or none")
    if mode == "none":
        if bundle_root is not None:
            raise ArtifactPathError("bundle_root is only valid with directory mode")
        return None
    workspace = Path(workspace_root).resolve()
    raw_entry = Path(entry_path)
    if not raw_entry.is_absolute():
        raw_entry = workspace / raw_entry
    raw_entry = Path(os.path.abspath(raw_entry))
    entry_is_html = raw_entry.suffix.casefold() in {".htm", ".html", ".xhtml"} or (
        entry_mime is not None
        and _safe_mime(entry_mime) in {"application/xhtml+xml", "text/html"}
    )
    if not entry_is_html and mode == "auto":
        if bundle_root is not None:
            raise ArtifactPathError("bundle_root is only valid with directory mode")
        return None
    _ensure_no_bundle_link_components(workspace, raw_entry)
    entry = raw_entry.resolve()
    try:
        entry.relative_to(workspace)
    except ValueError as exc:
        raise ArtifactPathError("artifact bundle entrypoint is outside workspace") from exc
    if mode == "directory":
        if bundle_root is None:
            raise ArtifactPathError("directory bundle mode requires bundle_root")
        raw_root = Path(bundle_root)
        if not raw_root.is_absolute():
            raw_root = workspace / raw_root
        raw_root = Path(os.path.abspath(raw_root))
        _ensure_no_bundle_link_components(workspace, raw_root)
        root = raw_root.resolve()
        try:
            root.relative_to(workspace)
            entry.relative_to(root)
        except ValueError as exc:
            raise ArtifactPathError(
                "bundle_root must contain the entrypoint and be inside workspace"
            ) from exc
        if root == workspace:
            raise ArtifactPathError("bundle_root must be a dedicated workspace subdirectory")
        if (
            not native_io_path(root).is_dir()
            or native_io_path(root).is_symlink()
            or _is_reparse_point(root)
        ):
            raise ArtifactPathError("bundle_root must be a regular directory")
    else:
        if bundle_root is not None:
            raise ArtifactPathError("bundle_root is only valid with directory mode")
        root = entry.parent

    root_relative = root.relative_to(workspace)
    if root_relative.parts and _is_sensitive_bundle_path(root_relative.as_posix()):
        raise ArtifactPathError("artifact bundle root is sensitive")

    entrypoint = _normalize_bundle_logical_path(entry.relative_to(root).as_posix())
    files: list[ArtifactBundleSourceFile] = []
    seen_collision_keys: dict[str, tuple[str, Path]] = {}

    def append_source(source: Path, logical_path: str) -> None:
        normalized = _normalize_bundle_logical_path(logical_path)
        if _is_sensitive_bundle_path(normalized):
            raise ArtifactPathError("artifact bundle contains a sensitive path")
        collision_key = normalized.casefold()
        previous = seen_collision_keys.get(collision_key)
        if previous is not None:
            previous_path, previous_source = previous
            if previous_path != normalized or previous_source != source:
                raise ArtifactPathError("artifact bundle contains a path collision")
            return
        _ensure_no_bundle_link_components(root, source)
        data = _read_regular_bundle_file(source)
        if normalized == entrypoint and len(data) > DEFAULT_ARTIFACT_MAX_BYTES:
            raise ArtifactBudgetError(
                "artifact bundle entrypoint exceeds per-file budget "
                f"({len(data)} > {DEFAULT_ARTIFACT_MAX_BYTES})"
            )
        if len(files) + 1 > max_files:
            raise ArtifactBudgetError(
                f"artifact bundle exceeds file-count budget ({len(files) + 1} > {max_files})"
            )
        next_total = sum(len(item.data) for item in files) + len(data)
        if next_total > max_bytes:
            raise ArtifactBudgetError(
                f"artifact bundle exceeds total budget ({next_total} > {max_bytes})"
            )
        files.append(
            ArtifactBundleSourceFile(
                path=normalized,
                mime=(
                    _safe_mime(entry_mime)
                    if normalized == entrypoint and entry_mime is not None
                    else artifact_mime_for_name(normalized)
                ),
                data=data,
            )
        )
        seen_collision_keys[collision_key] = (normalized, source)

    if mode == "directory":
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(native_io_path(directory)) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError as exc:
                raise ArtifactPathError("artifact bundle directory cannot be read") from exc
            for directory_entry in entries:
                source = directory / directory_entry.name
                logical_path = _normalize_bundle_logical_path(
                    source.relative_to(root).as_posix()
                )
                if _is_sensitive_bundle_path(logical_path):
                    raise ArtifactPathError("artifact bundle contains a sensitive path")
                if directory_entry.is_symlink() or _is_reparse_point(source):
                    raise ArtifactPathError(
                        "artifact bundle cannot contain links or reparse points"
                    )
                if directory_entry.is_dir(follow_symlinks=False):
                    stack.append(source)
                elif directory_entry.is_file(follow_symlinks=False):
                    append_source(source, logical_path)
                else:
                    raise ArtifactPathError(
                        "artifact bundle can contain regular files only"
                    )
        files.sort(key=lambda item: item.path)
        _validate_bundle_file_set(
            files,
            entrypoint=entrypoint,
            max_bytes=max_bytes,
            max_files=max_files,
        )
        return ArtifactBundle(entrypoint=entrypoint, files=tuple(files))

    warnings: set[str] = set()
    queue: list[str] = [entrypoint]
    visited: set[str] = set()
    while queue:
        logical_path = queue.pop(0)
        if logical_path in visited:
            continue
        visited.add(logical_path)
        source = root / Path(*logical_path.split("/"))
        try:
            source.relative_to(root)
            append_source(source, logical_path)
        except FileNotFoundError:
            warnings.add("missing_dependency")
            continue
        except ArtifactBudgetError:
            raise
        except (ArtifactPathError, OSError):
            if logical_path == entrypoint:
                raise
            warnings.add("unsafe_dependency")
            continue
        try:
            references, has_dynamic_reference = _extract_bundle_references(
                logical_path,
                files[-1].data,
                force_html=logical_path == entrypoint and entry_is_html,
            )
        except ArtifactPathError:
            warnings.add("unsupported_dependency_encoding")
            continue
        if has_dynamic_reference:
            warnings.add("dynamic_dependency")
        for reference in references:
            try:
                dependency = _resolve_bundle_reference(
                    reference,
                    source_path=logical_path,
                )
            except ArtifactPathError:
                warnings.add("outside_or_unsafe_dependency")
                continue
            if dependency is None:
                continue
            # Bare JavaScript package specifiers are resolved by import maps or the
            # browser, not by walking arbitrary sibling names.
            if (
                Path(logical_path).suffix.casefold()
                in {".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"}
                and "/" not in reference
                and not reference.startswith(".")
                and not Path(reference).suffix
            ):
                continue
            if dependency not in visited and dependency not in queue:
                queue.append(dependency)

    files.sort(key=lambda item: item.path)
    _validate_bundle_file_set(
        files,
        entrypoint=entrypoint,
        max_bytes=max_bytes,
        max_files=max_files,
    )
    warning_codes = tuple(sorted(warnings))
    return ArtifactBundle(
        entrypoint=entrypoint,
        files=tuple(files),
        collection_status="partial" if warning_codes else "complete",
        warning_codes=warning_codes,
    )


def artifact_bundle_manifest(bundle: ArtifactBundle) -> ArtifactBundleManifest:
    if bundle.collection_status not in {"complete", "partial"}:
        raise ArtifactPathError("artifact bundle collection status is invalid")
    warning_codes = tuple(sorted(set(bundle.warning_codes)))
    if warning_codes != bundle.warning_codes:
        raise ArtifactPathError("artifact bundle warning codes must be canonical")
    files = tuple(
        ArtifactBundleFile(
            path=item.path,
            mime=_safe_mime(item.mime),
            sha256=hashlib.sha256(item.data).hexdigest(),
            size=len(item.data),
        )
        for item in sorted(bundle.files, key=lambda item: item.path)
    )
    unsigned: dict[str, Any] = {
        "version": ARTIFACT_BUNDLE_VERSION,
        "entrypoint": _normalize_bundle_logical_path(bundle.entrypoint),
        "files": [item.to_dict() for item in files],
        "collection_status": bundle.collection_status,
        "warning_codes": list(warning_codes),
        "total_size": sum(item.size for item in files),
        "file_count": len(files),
    }
    return ArtifactBundleManifest(
        version=ARTIFACT_BUNDLE_VERSION,
        entrypoint=unsigned["entrypoint"],
        files=files,
        collection_status=bundle.collection_status,
        warning_codes=warning_codes,
        total_size=unsigned["total_size"],
        file_count=unsigned["file_count"],
        bundle_digest=_artifact_bundle_digest(unsigned),
    )


class ArtifactStore:
    """Session-scoped artifact store rooted outside the web static tree."""

    def __init__(self, media_root: str | Path) -> None:
        self.media_root = Path(media_root)

    @staticmethod
    def allocate_artifact_id() -> str:
        """Allocate an opaque ID that can be journaled before publication."""

        return _validate_artifact_id(f"art-{secrets.token_urlsafe(18)}")

    def publish_bytes(
        self,
        payload: bytes,
        *,
        session_id: str,
        session_key: str,
        name: str,
        mime: str,
        source: str,
        max_bytes: int | None = DEFAULT_ARTIFACT_MAX_BYTES,
        disk_budget_bytes: int | None = DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
        visibility: str = "listed",
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        if visibility not in {"listed", "internal"}:
            raise ArtifactError("artifact visibility must be listed or internal")
        if len(payload) == 0:
            raise ArtifactBudgetError("artifact payload is empty")
        if max_bytes is not None and len(payload) > max_bytes:
            raise ArtifactBudgetError(
                f"artifact exceeds per-file budget ({len(payload)} > {max_bytes})"
            )
        if disk_budget_bytes is not None:
            current = self._disk_usage_bytes()
            if current + len(payload) > disk_budget_bytes:
                raise ArtifactBudgetError(
                    "artifact material exceeds disk budget "
                    f"({current} + {len(payload)} > {disk_budget_bytes})"
                )

        session_id = _validate_non_empty("session_id", session_id)
        session_key = _validate_non_empty("session_key", session_key)
        artifact_id = (
            self.allocate_artifact_id()
            if artifact_id is None
            else _validate_artifact_id(artifact_id)
        )
        safe_name = _safe_filename(name)
        safe_mime = _safe_mime(mime)
        sha = hashlib.sha256(payload).hexdigest()
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        thumbnail_bytes = _build_thumbnail(payload, safe_mime)
        ref = ArtifactRef(
            id=artifact_id,
            sha256=sha,
            name=safe_name,
            mime=safe_mime,
            size=len(payload),
            session_id=session_id,
            session_key=session_key,
            source=source,
            created_at=created_at,
            download_url=artifact_download_url(artifact_id),
            has_thumbnail=thumbnail_bytes is not None,
        )

        artifact_dir = self._artifact_dir(session_id, artifact_id)
        native_artifact_dir = native_io_path(artifact_dir)
        native_artifact_dir.mkdir(parents=True, exist_ok=False)
        try:
            # This full-ID marker is the ownership proof used by restart
            # recovery when a hard crash occurs before meta.json is durable.
            # It is intentionally the first file written after mkdir.
            _atomic_write_bytes(
                artifact_dir / ARTIFACT_OWNERSHIP_MARKER_NAME,
                (artifact_id + "\n").encode("ascii"),
            )
            _atomic_write_bytes(artifact_dir / ARTIFACT_MATERIAL_NAME, payload)
            if thumbnail_bytes is not None:
                _atomic_write_bytes(artifact_dir / ARTIFACT_THUMBNAIL_NAME, thumbnail_bytes)
            _atomic_write_bytes(
                artifact_dir / "meta.json",
                json.dumps(ref.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            if visibility == "internal":
                _atomic_write_bytes(artifact_dir / ARTIFACT_INTERNAL_MARKER_NAME, b"1\n")
        except BaseException:
            for path in sorted(native_artifact_dir.glob("*"), reverse=True):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                native_artifact_dir.rmdir()
            except OSError:
                pass
            raise
        return ref

    def publish_file(
        self,
        path: str | Path,
        *,
        session_id: str,
        session_key: str,
        name: str | None = None,
        mime: str = "application/octet-stream",
        source: str,
        max_bytes: int | None = DEFAULT_ARTIFACT_MAX_BYTES,
        disk_budget_bytes: int | None = DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
        visibility: str = "listed",
    ) -> ArtifactRef:
        payload = native_io_path(path).read_bytes()
        return self.publish_bytes(
            payload,
            session_id=session_id,
            session_key=session_key,
            name=name or Path(path).name,
            mime=mime,
            source=source,
            max_bytes=max_bytes,
            disk_budget_bytes=disk_budget_bytes,
            visibility=visibility,
        )

    def publish_bundle(
        self,
        bundle: ArtifactBundle,
        *,
        session_id: str,
        session_key: str,
        name: str,
        mime: str,
        source: str,
        max_bytes: int | None = DEFAULT_ARTIFACT_MAX_BYTES,
        disk_budget_bytes: int | None = DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
        bundle_max_bytes: int = DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES,
        bundle_max_files: int = DEFAULT_ARTIFACT_BUNDLE_MAX_FILES,
        visibility: str = "listed",
    ) -> ArtifactRef:
        """Atomically publish a static bundle while retaining the legacy entry file."""

        if visibility not in {"listed", "internal"}:
            raise ArtifactError("artifact visibility must be listed or internal")
        files = list(bundle.files)
        _validate_bundle_file_set(
            files,
            entrypoint=bundle.entrypoint,
            max_bytes=bundle_max_bytes,
            max_files=bundle_max_files,
        )
        manifest = artifact_bundle_manifest(bundle)
        source_by_path = {item.path: item for item in files}
        entry_source = source_by_path[manifest.entrypoint]
        payload = entry_source.data
        safe_name = _safe_filename(name)
        safe_mime = _safe_mime(mime)
        if len(payload) == 0:
            raise ArtifactBudgetError("artifact payload is empty")
        if max_bytes is not None and len(payload) > max_bytes:
            raise ArtifactBudgetError(
                f"artifact exceeds per-file budget ({len(payload)} > {max_bytes})"
            )

        manifest_bytes = json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        unique_blob_bytes = {
            hashlib.sha256(item.data).hexdigest(): item.data for item in files
        }
        added_disk_bytes = (
            len(payload)
            + len(manifest_bytes)
            + sum(len(item) for item in unique_blob_bytes.values())
        )
        if disk_budget_bytes is not None:
            current = self._disk_usage_bytes()
            if current + added_disk_bytes > disk_budget_bytes:
                raise ArtifactBudgetError(
                    "artifact material exceeds disk budget "
                    f"({current} + {added_disk_bytes} > {disk_budget_bytes})"
                )

        session_id = _validate_non_empty("session_id", session_id)
        session_key = _validate_non_empty("session_key", session_key)
        artifact_id = f"art-{secrets.token_urlsafe(18)}"
        sha = hashlib.sha256(payload).hexdigest()
        entry_manifest_file = next(
            item for item in manifest.files if item.path == manifest.entrypoint
        )
        if (
            entry_manifest_file.sha256 != sha
            or entry_manifest_file.size != len(payload)
            or entry_manifest_file.mime != safe_mime
        ):
            raise ArtifactIntegrityError("artifact bundle entrypoint metadata mismatch")
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        thumbnail_bytes = _build_thumbnail(payload, safe_mime)
        ref = ArtifactRef(
            id=artifact_id,
            sha256=sha,
            name=safe_name,
            mime=safe_mime,
            size=len(payload),
            session_id=session_id,
            session_key=session_key,
            source=source,
            created_at=created_at,
            download_url=artifact_download_url(artifact_id),
            has_thumbnail=thumbnail_bytes is not None,
        )

        artifact_dir = self._artifact_dir(session_id, artifact_id)
        native_artifact_dir = native_io_path(artifact_dir)
        native_artifact_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_dir = artifact_dir.with_name(
            f".{artifact_dir.name}.tmp-{secrets.token_urlsafe(6)}"
        )
        native_staging_dir = native_io_path(staging_dir)
        native_staging_dir.mkdir(parents=False, exist_ok=False)
        try:
            _atomic_write_bytes(staging_dir / ARTIFACT_MATERIAL_NAME, payload)
            native_io_path(staging_dir / ARTIFACT_BUNDLE_BLOBS_DIR).mkdir()
            for digest, data in sorted(unique_blob_bytes.items()):
                _atomic_write_bytes(
                    staging_dir / ARTIFACT_BUNDLE_BLOBS_DIR / digest,
                    data,
                )
            _atomic_write_bytes(
                staging_dir / ARTIFACT_BUNDLE_MANIFEST_NAME,
                manifest_bytes,
            )
            if thumbnail_bytes is not None:
                _atomic_write_bytes(
                    staging_dir / ARTIFACT_THUMBNAIL_NAME,
                    thumbnail_bytes,
                )
            _atomic_write_bytes(
                staging_dir / "meta.json",
                json.dumps(ref.to_dict(), ensure_ascii=False, sort_keys=True).encode(
                    "utf-8"
                ),
            )
            if visibility == "internal":
                _atomic_write_bytes(
                    staging_dir / ARTIFACT_INTERNAL_MARKER_NAME,
                    b"1\n",
                )
            os.replace(native_staging_dir, native_artifact_dir)
        except BaseException:
            shutil.rmtree(native_staging_dir, ignore_errors=True)
            raise
        return ref

    def resolve_for_download(
        self,
        artifact_id: str,
        *,
        session_id: str,
    ) -> tuple[ArtifactRef, Path]:
        artifact_id = _validate_artifact_id(artifact_id)
        meta_path = self._resolve_meta_path(session_id, artifact_id)
        native_meta_path = native_io_path(meta_path)
        if not native_meta_path.exists():
            raise ArtifactNotFoundError("artifact not found")
        ref = ArtifactRef.from_dict(json.loads(native_meta_path.read_text(encoding="utf-8")))
        if ref.session_id != session_id:
            raise ArtifactNotFoundError("artifact not found")
        path = self.path_for(ref)
        native_path = native_io_path(path)
        if not native_path.exists():
            raise ArtifactNotFoundError("artifact material not found")
        payload = native_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != ref.sha256:
            raise ArtifactIntegrityError("artifact material hash mismatch")
        if len(payload) != ref.size:
            raise ArtifactIntegrityError("artifact material size mismatch")
        return ref, path

    def find_existing_ref(
        self,
        *,
        session_id: str,
        session_key: str,
        sha256: str,
        name: str,
        mime: str | None = None,
        bundle_digest: str | None = None,
        require_single_file: bool = False,
    ) -> ArtifactRef | None:
        """Find a previously published logical deliverable in the same session."""

        session_id = _validate_non_empty("session_id", session_id)
        session_key = _validate_non_empty("session_key", session_key)
        sha256 = _validate_sha256(sha256)
        safe_name = _safe_filename(name)
        safe_mime = _safe_mime(mime) if mime else None
        safe_bundle_digest = (
            _validate_sha256(bundle_digest) if bundle_digest is not None else None
        )
        if safe_bundle_digest is not None and require_single_file:
            raise ValueError("bundle_digest and require_single_file are mutually exclusive")
        for root in self._artifact_session_roots(session_id):
            native_root = native_io_path(root)
            if not native_root.exists():
                continue
            for meta_path in sorted(native_root.glob("*/meta.json")):
                try:
                    ref = ArtifactRef.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if ref.session_id != session_id or ref.session_key != session_key:
                    continue
                if ref.sha256 != sha256 or ref.name != safe_name:
                    continue
                if safe_mime is not None and ref.mime != safe_mime:
                    continue
                try:
                    resolved_ref, material_path = self.resolve_for_download(
                        ref.id,
                        session_id=session_id,
                    )
                except (ArtifactNotFoundError, ArtifactIntegrityError):
                    continue
                try:
                    manifest = self._describe_preview_bundle_for_ref(
                        resolved_ref,
                        material_path,
                    )
                except (ArtifactNotFoundError, ArtifactIntegrityError, ArtifactPathError):
                    continue
                if require_single_file and manifest is not None:
                    continue
                if safe_bundle_digest is not None and (
                    manifest is None or manifest.bundle_digest != safe_bundle_digest
                ):
                    continue
                return ref
        return None

    def get_ref(self, *, session_id: str, artifact_id: str) -> ArtifactRef:
        """Return session-scoped artifact metadata without reading material bytes."""

        session_id = _validate_non_empty("session_id", session_id)
        artifact_id = _validate_artifact_id(artifact_id)
        layout = self._preferred_artifact_layout(
            session_id,
            artifact_id,
        )
        if layout is None or not layout[2]:
            raise ArtifactNotFoundError("artifact not found")
        artifact_dir, _material_name, _safe = layout
        meta_path = artifact_dir / "meta.json"
        native_meta_path = native_io_path(meta_path)
        try:
            meta_stat = native_meta_path.lstat()
            if (
                not stat.S_ISREG(meta_stat.st_mode)
                or stat.S_ISLNK(meta_stat.st_mode)
                or _is_reparse_point(meta_path)
            ):
                raise ArtifactNotFoundError("artifact not found")
            raw = json.loads(native_meta_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ArtifactNotFoundError("artifact not found")
            ref = ArtifactRef.from_dict(raw)
        except ArtifactNotFoundError:
            raise
        except (OSError, ValueError, json.JSONDecodeError):
            raise ArtifactNotFoundError("artifact not found") from None
        if ref.id != artifact_id or ref.session_id != session_id:
            raise ArtifactNotFoundError("artifact not found")
        material_path, material_exists = self._preferred_material_path_for_ref(ref)
        if not material_exists or material_path is None:
            raise ArtifactNotFoundError("artifact not found")
        return ref

    def promote_internal_ref(
        self,
        *,
        session_id: str,
        artifact_id: str,
        expected_sha256: str,
    ) -> ArtifactRef:
        """Make an immutable internal candidate visible as a listed deliverable.

        Publication callers first journal the candidate, write it as internal,
        commit the durable publication receipt, and only then remove the marker.
        Repeating this method is safe and provides response-loss recovery.
        """

        expected_sha256 = _validate_sha256(expected_sha256)
        ref, _path = self.resolve_for_download(artifact_id, session_id=session_id)
        if ref.sha256 != expected_sha256:
            raise ArtifactIntegrityError("artifact promotion sha256 mismatch")
        layout = self._preferred_artifact_layout(session_id, ref.id)
        if layout is None or not layout[2]:
            raise ArtifactNotFoundError("artifact not found")
        artifact_dir, _material_name, _safe = layout
        marker = native_io_path(artifact_dir / ARTIFACT_INTERNAL_MARKER_NAME)
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            raise ArtifactError("artifact promotion failed") from exc
        if os.name != "nt":
            directory_fd: int | None = None
            try:
                directory_fd = os.open(native_io_path(artifact_dir), os.O_RDONLY)
                os.fsync(directory_fd)
            except OSError:
                # The durable DB receipt remains the recovery authority. A later
                # idempotent replay rechecks the marker and promotion state.
                pass
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
        return ref

    def list_refs(
        self,
        *,
        session_id: str,
        limit: int,
        before: str | None = None,
    ) -> ArtifactRefPage:
        """List one backwards page of valid artifact metadata for a session.

        Results inside a page remain oldest-to-newest. Corrupt metadata,
        cross-session refs, duplicate ids, links/reparse points, and refs whose
        material is missing are ignored. Listing deliberately checks only material
        existence; it does not read or hash artifact bytes.
        """

        session_id = _validate_non_empty("session_id", session_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ArtifactError("artifact page limit must be a positive integer")
        before_id = validate_artifact_cursor(before) if before is not None else None

        refs_by_id: dict[str, ArtifactRef] = {}
        for meta_path in self._iter_session_meta_paths_for_listing(session_id):
            try:
                native_meta_path = native_io_path(meta_path)
                meta_stat = native_meta_path.lstat()
                if (
                    not stat.S_ISREG(meta_stat.st_mode)
                    or stat.S_ISLNK(meta_stat.st_mode)
                    or _is_reparse_point(meta_path)
                ):
                    continue
                raw = json.loads(native_meta_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                ref = ArtifactRef.from_dict(raw)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if ref.session_id != session_id:
                continue
            # Root-level errors from layout selection are session index failures
            # and deliberately remain outside the per-metadata recovery block.
            layout = self._preferred_artifact_layout(session_id, ref.id)
            if layout is None or not layout[2]:
                continue
            artifact_dir, _material_name, _safe = layout
            if native_io_path(artifact_dir / ARTIFACT_INTERNAL_MARKER_NAME).exists():
                continue
            selected_meta_path = artifact_dir / "meta.json"
            if native_io_path(selected_meta_path) != native_meta_path:
                continue
            _material_path, material_exists = self._preferred_material_path_for_ref(ref)
            if ref.id in refs_by_id or not material_exists:
                continue
            refs_by_id[ref.id] = ref

        refs = sorted(refs_by_id.values(), key=lambda ref: (ref.created_at, ref.id))
        total_count = len(refs)
        if before_id is not None:
            before_index = next(
                (index for index, ref in enumerate(refs) if ref.id == before_id),
                None,
            )
            if before_index is None:
                raise ArtifactError("artifact cursor not found")
            refs = refs[:before_index]
        page_refs = refs[-limit:]
        return ArtifactRefPage(
            refs=tuple(page_refs),
            has_more=len(refs) > len(page_refs),
            total_count=total_count,
        )

    def describe_preview_bundle(
        self,
        artifact_id: str,
        *,
        session_id: str,
    ) -> ArtifactBundleManifest | None:
        """Return and validate the optional bundle manifest for an artifact."""

        ref, material_path = self.resolve_for_download(
            artifact_id,
            session_id=session_id,
        )
        return self._describe_preview_bundle_for_ref(ref, material_path)

    def supports_single_file_editing(
        self,
        artifact_id: str,
        *,
        session_id: str,
    ) -> bool:
        """Return whether an artifact can be copied into one editable document.

        Bundle status is derived metadata. Older collectors could persist a
        false ``missing_dependency`` warning for a remote CSS ``@import
        url(...)``. Preserve the immutable manifest, but safely reclassify a
        partial one-file HTML bundle from its integrity-checked entrypoint.
        Real local, dynamic, unsafe, or undecodable dependencies remain closed.
        """

        ref, material_path = self.resolve_for_download(
            artifact_id,
            session_id=session_id,
        )
        manifest = self._describe_preview_bundle_for_ref(ref, material_path)
        if manifest is None or is_complete_single_file_preview_bundle(manifest):
            return True
        if (
            manifest.collection_status != "partial"
            or manifest.warning_codes != ("missing_dependency",)
        ):
            return False
        if not _is_single_file_preview_bundle(manifest):
            return False
        entry = manifest.files[0]
        if entry.mime not in {"application/xhtml+xml", "text/html"} and (
            Path(entry.path).suffix.casefold() not in {".htm", ".html", ".xhtml"}
        ):
            return False
        try:
            payload = _read_regular_bundle_file(material_path)
        except (OSError, ArtifactPathError) as exc:
            raise ArtifactIntegrityError("artifact bundle entrypoint is unreadable") from exc
        if len(payload) != ref.size or hashlib.sha256(payload).hexdigest() != ref.sha256:
            raise ArtifactIntegrityError("artifact bundle entrypoint does not match artifact")
        return not legacy_html_bundle_warning_codes(manifest.entrypoint, payload)

    def _describe_preview_bundle_for_ref(
        self,
        ref: ArtifactRef,
        material_path: Path,
    ) -> ArtifactBundleManifest | None:
        manifest_path = material_path.parent / ARTIFACT_BUNDLE_MANIFEST_NAME
        native_manifest_path = native_io_path(manifest_path)
        if not native_manifest_path.exists():
            return None
        try:
            manifest_bytes = _read_regular_bundle_file(native_manifest_path)
            raw = json.loads(manifest_bytes.decode("utf-8"))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ArtifactPathError,
        ) as exc:
            raise ArtifactIntegrityError("artifact bundle manifest is unreadable") from exc
        if not isinstance(raw, dict):
            raise ArtifactIntegrityError("artifact bundle manifest is invalid")
        try:
            manifest = ArtifactBundleManifest.from_dict(raw)
        except ArtifactIntegrityError:
            raise
        except (ArtifactPathError, ValueError) as exc:
            raise ArtifactIntegrityError("artifact bundle manifest is invalid") from exc
        if (
            manifest.file_count > DEFAULT_ARTIFACT_BUNDLE_MAX_FILES
            or manifest.total_size > DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES
        ):
            raise ArtifactIntegrityError("artifact bundle manifest exceeds safety limits")
        entry = next(
            item for item in manifest.files if item.path == manifest.entrypoint
        )
        if entry.size > DEFAULT_ARTIFACT_MAX_BYTES:
            raise ArtifactIntegrityError("artifact bundle entrypoint exceeds safety limits")
        if entry.sha256 != ref.sha256 or entry.size != ref.size:
            raise ArtifactIntegrityError("artifact bundle entrypoint does not match artifact")
        return manifest

    @staticmethod
    def _resolve_preview_bundle_member(
        ref: ArtifactRef,
        material_path: Path,
        item: ArtifactBundleFile,
    ) -> ArtifactPreviewResource:
        blob_path = material_path.parent / ARTIFACT_BUNDLE_BLOBS_DIR / item.sha256
        native_blob_path = native_io_path(blob_path)
        try:
            payload = _read_regular_bundle_file(native_blob_path)
        except (FileNotFoundError, OSError, ArtifactPathError) as exc:
            # The manifest already declared this content-addressed member.
            # Its absence or replacement is corruption, not an unknown URL.
            raise ArtifactIntegrityError("artifact bundle blob is unavailable") from exc
        if len(payload) != item.size or hashlib.sha256(payload).hexdigest() != item.sha256:
            raise ArtifactIntegrityError("artifact preview resource hash mismatch")
        return ArtifactPreviewResource(
            ref=ref,
            logical_path=item.path,
            mime=item.mime,
            sha256=item.sha256,
            size=item.size,
            path=blob_path,
        )

    def validate_preview_bundle(
        self,
        artifact_id: str,
        *,
        session_id: str,
    ) -> ArtifactBundleManifest | None:
        """Verify every manifest member before a preview capability is issued."""

        ref, material_path = self.resolve_for_download(
            artifact_id,
            session_id=session_id,
        )
        manifest = self._describe_preview_bundle_for_ref(ref, material_path)
        if manifest is None:
            return None
        for item in manifest.files:
            self._resolve_preview_bundle_member(ref, material_path, item)
        return manifest

    def resolve_preview_resource(
        self,
        artifact_id: str,
        *,
        session_id: str,
        logical_path: str | None = None,
    ) -> ArtifactPreviewResource:
        """Resolve one preview resource without exposing unchecked filesystem paths."""

        ref, material_path = self.resolve_for_download(
            artifact_id,
            session_id=session_id,
        )
        manifest = self._describe_preview_bundle_for_ref(ref, material_path)
        requested = (logical_path or "").lstrip("/")
        if manifest is None:
            if requested not in {"", ref.name}:
                raise ArtifactNotFoundError("artifact preview resource not found")
            return ArtifactPreviewResource(
                ref=ref,
                logical_path=ref.name,
                mime=ref.mime,
                sha256=ref.sha256,
                size=ref.size,
                path=material_path,
            )

        if not requested:
            requested = manifest.entrypoint
        try:
            requested = _normalize_bundle_logical_path(unquote(requested))
        except ArtifactPathError as exc:
            raise ArtifactNotFoundError("artifact preview resource not found") from exc
        item = next(
            (candidate for candidate in manifest.files if candidate.path == requested),
            None,
        )
        if item is None:
            raise ArtifactNotFoundError("artifact preview resource not found")
        return self._resolve_preview_bundle_member(ref, material_path, item)

    def copy_session_artifacts(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        target_session_key: str,
        artifact_ids: set[str] | frozenset[str] | None = None,
    ) -> int:
        """Duplicate selected artifacts owned by ``source_session_id`` into the target.

        Used when a session is forked: the child transcript references each artifact by
        its stable id and a session-less download URL, but the store is session-scoped
        and ``resolve_for_download`` rejects a mismatched session id, so the child needs
        its own copy. ``artifact_ids`` can restrict this to the copied history's reachable
        subset. Each artifact keeps its id; the copied ``meta.json`` is rebound to the
        child's session id/key and the material (plus any thumbnail) is materialized under
        the child's session bucket. Idempotent and best-effort: already-copied or unreadable
        artifacts are skipped. Returns the number of artifacts copied.
        """
        source_session_id = _validate_non_empty("source_session_id", source_session_id)
        target_session_id = _validate_non_empty("target_session_id", target_session_id)
        target_session_key = _validate_non_empty("target_session_key", target_session_key)
        if target_session_id == source_session_id:
            return 0
        selected_ids = None if artifact_ids is None else set(artifact_ids)
        copied = 0
        for meta_path in self._iter_session_meta_paths(source_session_id):
            if native_io_path(
                meta_path.parent / ARTIFACT_INTERNAL_MARKER_NAME
            ).exists():
                continue
            try:
                ref = ArtifactRef.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if ref.session_id != source_session_id:
                continue
            if selected_ids is not None and ref.id not in selected_ids:
                continue
            try:
                if self._copy_one_artifact(ref, target_session_id, target_session_key):
                    copied += 1
            except (OSError, ValueError):
                # Best-effort: a single bad artifact (filesystem error, invalid sha)
                # must not stop the rest. ArtifactError is a ValueError subclass.
                continue
        return copied

    def copy_artifact_heads_for_fork(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        target_session_key: str,
        artifact_ids: Sequence[str],
    ) -> int:
        """Copy an explicit set of document heads into a forked session.

        Unlike :meth:`copy_session_artifacts`, this method intentionally accepts
        internal revision blobs because a document's current head may be hidden
        from the legacy chat-artifact list. Visibility is preserved in the child,
        and every requested artifact must be resolvable after the copy.
        """

        source_session_id = _validate_non_empty("source_session_id", source_session_id)
        target_session_id = _validate_non_empty("target_session_id", target_session_id)
        target_session_key = _validate_non_empty("target_session_key", target_session_key)
        if target_session_id == source_session_id:
            raise ArtifactError("source and target session ids must differ")
        copied = 0
        for artifact_id in dict.fromkeys(artifact_ids):
            ref = self.get_ref(
                session_id=source_session_id,
                artifact_id=artifact_id,
            )
            source_dir = self.path_for(ref).parent
            source_is_internal = native_io_path(
                source_dir / ARTIFACT_INTERNAL_MARKER_NAME
            ).exists()
            if self._copy_one_artifact(ref, target_session_id, target_session_key):
                copied += 1
            child_ref, child_path = self.resolve_for_download(
                artifact_id,
                session_id=target_session_id,
            )
            if child_ref.sha256 != ref.sha256:
                raise ArtifactIntegrityError("forked artifact metadata does not match source")
            child_marker = child_path.parent / ARTIFACT_INTERNAL_MARKER_NAME
            if source_is_internal and not native_io_path(child_marker).exists():
                _atomic_write_bytes(child_marker, b"1\n")
        return copied

    def delete_ref(self, *, session_id: str, artifact_id: str) -> bool:
        """Delete one session-owned artifact bucket without following links.

        ArtifactSession calls this when a newly materialized candidate loses a
        database compare-and-set.  Both identifiers are validated and resolved
        through the store layout before deletion; callers never supply a path.
        """

        session_id = _validate_non_empty("session_id", session_id)
        artifact_id = _validate_artifact_id(artifact_id)
        try:
            ref = self.get_ref(session_id=session_id, artifact_id=artifact_id)
        except ArtifactNotFoundError:
            return False
        if ref.session_id != session_id:
            return False
        layout = self._preferred_artifact_layout(session_id, artifact_id)
        if layout is None or not layout[2]:
            return False
        artifact_dir = layout[0]
        native_dir = native_io_path(artifact_dir)
        if native_dir.is_symlink() or _is_reparse_point(artifact_dir):
            return False
        shutil.rmtree(native_dir)
        return True

    def delete_reserved_bucket(self, *, session_id: str, artifact_id: str) -> bool:
        """Delete a journal-owned bucket even when publication died before metadata.

        The compact on-disk layout hashes artifact IDs, so a derived path alone
        is not proof of ownership. Deletion requires an exact full-ID marker,
        exact safe metadata, or an empty directory left between mkdir and the
        first marker write. Unsafe or mismatched buckets fail closed.
        """

        session_id = _validate_non_empty("session_id", session_id)
        artifact_id = _validate_artifact_id(artifact_id)
        artifact_dir = self._artifact_dir(session_id, artifact_id)
        native_dir = native_io_path(artifact_dir)
        try:
            root_stat = native_io_path(artifact_dir.parent).lstat()
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_ISLNK(root_stat.st_mode)
            or _is_reparse_point(artifact_dir.parent)
        ):
            raise ArtifactIntegrityError("reserved artifact parent is unsafe")
        try:
            directory_stat = native_dir.lstat()
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_ISLNK(directory_stat.st_mode)
            or _is_reparse_point(artifact_dir)
        ):
            raise ArtifactIntegrityError("reserved artifact bucket is unsafe")

        marker = artifact_dir / ARTIFACT_OWNERSHIP_MARKER_NAME
        ownership_proven = False
        try:
            marker_stat = native_io_path(marker).lstat()
        except FileNotFoundError:
            marker_stat = None
        if marker_stat is not None:
            if (
                not stat.S_ISREG(marker_stat.st_mode)
                or stat.S_ISLNK(marker_stat.st_mode)
                or _is_reparse_point(marker)
            ):
                raise ArtifactIntegrityError("reserved artifact ownership marker is unsafe")
            try:
                marker_id = native_io_path(marker).read_text(encoding="ascii").strip()
            except (OSError, UnicodeError) as exc:
                raise ArtifactIntegrityError(
                    "reserved artifact ownership marker is unreadable"
                ) from exc
            if marker_id != artifact_id:
                raise ArtifactIntegrityError("reserved artifact ownership marker mismatches")
            ownership_proven = True
        else:
            try:
                ref = self.get_ref(session_id=session_id, artifact_id=artifact_id)
            except ArtifactNotFoundError:
                ref = None
            if ref is not None:
                if ref.id != artifact_id or ref.session_id != session_id:
                    raise ArtifactIntegrityError("reserved artifact metadata mismatches")
                ownership_proven = True

        if not ownership_proven:
            try:
                if next(native_dir.iterdir(), None) is None:
                    native_dir.rmdir()
                    return True
            except OSError as exc:
                raise ArtifactIntegrityError(
                    "reserved artifact bucket could not be inspected"
                ) from exc
            raise ArtifactIntegrityError("reserved artifact bucket lacks ownership proof")

        shutil.rmtree(native_dir)
        return True

    def delete_session_artifacts(self, session_id: str) -> int:
        """Remove every current and legacy artifact bucket owned by a session.

        Session deletion is the lifecycle boundary for potentially large
        bundle blobs.  The target names are derived locally (hashes or a
        sanitized legacy token), and links/reparse points are never followed.
        Returns the number of buckets removed.
        """

        session_id = _validate_non_empty("session_id", session_id)
        legacy_token = _safe_token(session_id)
        roots = (
            *self._artifact_session_roots(session_id),
            self.media_root / ARTIFACT_STORE / legacy_token,
        )
        removed = 0
        seen: set[Path] = set()
        for root in roots:
            native_root = native_io_path(root)
            absolute = Path(os.path.abspath(native_root))
            if absolute in seen:
                continue
            seen.add(absolute)
            if (
                root.name in {"", ".", ".."}
                or native_root.is_symlink()
                or _is_reparse_point(root)
                or not native_root.is_dir()
            ):
                continue
            # Deletion is best-effort and can race another cleanup/recovery
            # pass; a vanished child must not block deletion of the session.
            shutil.rmtree(native_root, ignore_errors=True)
            removed += 1
        return removed

    def delete_session_internal_artifacts(self, session_id: str) -> int:
        """Remove only hidden ArtifactSession revision/candidate buckets.

        Listed ``ArtifactRef`` objects are a legacy/public contract and survive
        session reset/delete.  Internal bytes are identified exclusively by the
        marker written atomically by ``publish_* visibility='internal'``; names,
        sources, and database references are not trusted as visibility signals.
        Link and reparse-point buckets are never followed.
        """

        session_id = _validate_non_empty("session_id", session_id)
        removed = 0
        touched_roots: set[Path] = set()
        for meta_path in tuple(self._iter_session_meta_paths_for_listing(session_id)):
            artifact_dir = meta_path.parent
            marker_path = artifact_dir / ARTIFACT_INTERNAL_MARKER_NAME
            try:
                marker_stat = native_io_path(marker_path).lstat()
                if (
                    not stat.S_ISREG(marker_stat.st_mode)
                    or stat.S_ISLNK(marker_stat.st_mode)
                    or _is_reparse_point(marker_path)
                ):
                    continue
                ref = ArtifactRef.from_dict(
                    json.loads(native_io_path(meta_path).read_text(encoding="utf-8"))
                )
                if ref.session_id != session_id:
                    continue
                artifact_stat = native_io_path(artifact_dir).lstat()
                if (
                    not stat.S_ISDIR(artifact_stat.st_mode)
                    or stat.S_ISLNK(artifact_stat.st_mode)
                    or _is_reparse_point(artifact_dir)
                ):
                    continue
                shutil.rmtree(native_io_path(artifact_dir))
                removed += 1
                touched_roots.add(artifact_dir.parent)
            except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
                # Cleanup is post-commit and retryable. A raced/malformed bucket
                # is safer to leak than to infer visibility and delete it.
                continue

        # Empty hashed session roots have no user-visible contents left.  Use
        # rmdir rather than recursive deletion so a concurrently-created listed
        # artifact can never be removed by this cleanup pass.
        for root in sorted(touched_roots, key=lambda item: len(item.parts), reverse=True):
            try:
                native_io_path(root).rmdir()
            except OSError:
                pass
        return removed

    def _iter_session_meta_paths(self, session_id: str) -> Iterator[Path]:
        """Yield every artifact ``meta.json`` for ``session_id`` across all store layouts."""
        roots = (
            *self._artifact_session_roots(session_id),
            self.media_root
            / ARTIFACT_STORE
            / _safe_token(_validate_non_empty("session_id", session_id)),
        )
        seen: set[Path] = set()
        for root in roots:
            native_root = native_io_path(root)
            if not native_root.exists():
                continue
            for meta_path in sorted(native_root.glob("*/meta.json")):
                resolved = meta_path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield meta_path

    def _iter_session_meta_paths_for_listing(self, session_id: str) -> Iterator[Path]:
        """Yield safe metadata paths while preserving directory-level failures."""

        roots = (
            *self._artifact_session_roots(session_id),
            self.media_root
            / ARTIFACT_STORE
            / _safe_token(_validate_non_empty("session_id", session_id)),
        )
        seen: set[Path] = set()
        for root in roots:
            native_root = native_io_path(root)
            try:
                root_stat = native_root.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(root_stat.st_mode)
                or _is_reparse_point(root)
            ):
                continue
            # Materialize the child list so a directory-level iteration error is
            # raised before any partial page can be returned.
            artifact_dirs = sorted(native_root.iterdir(), key=lambda path: path.name)
            for artifact_dir in artifact_dirs:
                try:
                    artifact_dir_stat = artifact_dir.lstat()
                except OSError:
                    continue
                if (
                    not stat.S_ISDIR(artifact_dir_stat.st_mode)
                    or stat.S_ISLNK(artifact_dir_stat.st_mode)
                    or _is_reparse_point(artifact_dir)
                ):
                    continue
                meta_path = artifact_dir / "meta.json"
                try:
                    meta_stat = meta_path.lstat()
                except OSError:
                    continue
                if (
                    not stat.S_ISREG(meta_stat.st_mode)
                    or stat.S_ISLNK(meta_stat.st_mode)
                    or _is_reparse_point(meta_path)
                ):
                    continue
                if meta_path in seen:
                    continue
                seen.add(meta_path)
                yield meta_path

    def _copy_one_artifact(
        self,
        ref: ArtifactRef,
        target_session_id: str,
        target_session_key: str,
    ) -> bool:
        """Materialize one artifact under the child session; return True when copied."""
        source_material = self.path_for(ref)
        if not native_io_path(source_material).exists():
            return False
        source_manifest = self.describe_preview_bundle(
            ref.id,
            session_id=ref.session_id,
        )
        target_dir = self._artifact_dir(target_session_id, ref.id)
        target_material = target_dir / ARTIFACT_MATERIAL_NAME
        target_meta = target_dir / "meta.json"
        target_bundle_manifest = target_dir / ARTIFACT_BUNDLE_MANIFEST_NAME
        if native_io_path(target_meta).exists() and native_io_path(target_material).exists():
            if source_manifest is None or native_io_path(target_bundle_manifest).exists():
                return False
        native_io_path(target_dir).mkdir(parents=True, exist_ok=True)
        if not native_io_path(target_material).exists():
            _link_or_copy(source_material, target_material)
        if source_manifest is not None:
            source_dir = source_material.parent
            target_blobs_dir = target_dir / ARTIFACT_BUNDLE_BLOBS_DIR
            native_io_path(target_blobs_dir).mkdir(parents=True, exist_ok=True)
            for item in source_manifest.files:
                source_blob = source_dir / ARTIFACT_BUNDLE_BLOBS_DIR / item.sha256
                target_blob = target_blobs_dir / item.sha256
                if native_io_path(target_blob).exists():
                    continue
                # Resolve every logical member before copying so a corrupt source
                # bundle cannot be propagated to a fork.
                self.resolve_preview_resource(
                    ref.id,
                    session_id=ref.session_id,
                    logical_path=item.path,
                )
                _link_or_copy(source_blob, target_blob)
            _atomic_write_bytes(
                target_bundle_manifest,
                json.dumps(
                    source_manifest.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8"),
            )
        has_thumbnail = False
        if ref.has_thumbnail:
            target_thumb = target_dir / ARTIFACT_THUMBNAIL_NAME
            source_thumb = self.thumbnail_path_for(ref)
            if native_io_path(target_thumb).exists():
                has_thumbnail = True
            elif native_io_path(source_thumb).exists():
                _link_or_copy(source_thumb, target_thumb)
                has_thumbnail = True
        # Only advertise a thumbnail the child actually has on disk: a source whose
        # sidecar cannot be located (e.g. a legacy layout without one) is copied
        # without it rather than leaving a dangling has_thumbnail in the child meta.
        child_ref = replace(
            ref,
            session_id=target_session_id,
            session_key=target_session_key,
            has_thumbnail=has_thumbnail,
        )
        _atomic_write_bytes(
            target_meta,
            json.dumps(child_ref.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        return True

    def resolve_thumbnail_for_download(
        self,
        artifact_id: str,
        *,
        session_id: str,
    ) -> tuple[ArtifactRef, Path] | None:
        """Return the webp thumbnail sidecar for an artifact, or None if absent.

        Validates and resolves the artifact exactly like ``resolve_for_download`` so
        auth/session scoping is identical, then returns the thumbnail path only when
        the sidecar exists. Older artifacts without a thumbnail yield None so callers
        can fall back to the full file.
        """

        ref, _path = self.resolve_for_download(artifact_id, session_id=session_id)
        if not ref.has_thumbnail:
            return None
        thumb_path = self.thumbnail_path_for(ref)
        if not native_io_path(thumb_path).exists():
            return None
        return ref, thumb_path

    def path_for(self, ref: ArtifactRef) -> Path:
        _validate_sha256(ref.sha256)
        for artifact_dir in (
            self._artifact_dir(ref.session_id, ref.id),
            self._legacy_short_artifact_dir(ref.session_id, ref.id),
        ):
            material_path = artifact_dir / ARTIFACT_MATERIAL_NAME
            if native_io_path(material_path).exists():
                return material_path
        return self._legacy_artifact_dir(ref.session_id, ref.id) / ref.sha256

    def thumbnail_path_for(self, ref: ArtifactRef) -> Path:
        for artifact_dir in (
            self._artifact_dir(ref.session_id, ref.id),
            self._legacy_short_artifact_dir(ref.session_id, ref.id),
        ):
            thumbnail_path = artifact_dir / ARTIFACT_THUMBNAIL_NAME
            if native_io_path(thumbnail_path).exists():
                return thumbnail_path
        return self._artifact_dir(ref.session_id, ref.id) / ARTIFACT_THUMBNAIL_NAME

    def _artifact_dir(self, session_id: str, artifact_id: str) -> Path:
        return self._short_artifact_dir(
            session_id,
            artifact_id,
            token_chars=ARTIFACT_STORE_TOKEN_CHARS,
        )

    def _legacy_short_artifact_dir(self, session_id: str, artifact_id: str) -> Path:
        return self._short_artifact_dir(
            session_id,
            artifact_id,
            token_chars=LEGACY_ARTIFACT_STORE_TOKEN_CHARS,
        )

    def _short_artifact_dir(self, session_id: str, artifact_id: str, *, token_chars: int) -> Path:
        return (
            self.media_root
            / ARTIFACT_STORE
            / ARTIFACT_SESSION_BUCKET
            / _session_store_token(session_id, chars=token_chars)
            / _artifact_store_token(artifact_id, chars=token_chars)
        )

    def _artifact_session_roots(self, session_id: str) -> tuple[Path, ...]:
        return (
            self.media_root
            / ARTIFACT_STORE
            / ARTIFACT_SESSION_BUCKET
            / _session_store_token(session_id, chars=ARTIFACT_STORE_TOKEN_CHARS),
            self.media_root
            / ARTIFACT_STORE
            / ARTIFACT_SESSION_BUCKET
            / _session_store_token(session_id, chars=LEGACY_ARTIFACT_STORE_TOKEN_CHARS),
        )

    def _artifact_layout_candidates(
        self,
        session_id: str,
        artifact_id: str,
    ) -> tuple[tuple[Path, str | None], ...]:
        """Return layouts in precedence order with their material naming convention."""

        return (
            (self._artifact_dir(session_id, artifact_id), ARTIFACT_MATERIAL_NAME),
            (self._legacy_short_artifact_dir(session_id, artifact_id), ARTIFACT_MATERIAL_NAME),
            (self._legacy_artifact_dir(session_id, artifact_id), None),
        )

    def _preferred_artifact_layout(
        self,
        session_id: str,
        artifact_id: str,
    ) -> tuple[Path, str | None, bool] | None:
        """Select the same first-on-disk metadata layout as downloads.

        The boolean reports whether every traversed layout component and the
        selected metadata file are ordinary, non-link filesystem objects. An
        unsafe higher-priority layout blocks legacy fallback, keeping list/get
        visibility consistent with ``_resolve_meta_path`` without following it.
        """

        for artifact_dir, material_name in self._artifact_layout_candidates(
            session_id,
            artifact_id,
        ):
            root = artifact_dir.parent
            try:
                root_stat = native_io_path(root).lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(root_stat.st_mode)
                or _is_reparse_point(root)
            ):
                return artifact_dir, material_name, False
            try:
                artifact_dir_stat = native_io_path(artifact_dir).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return artifact_dir, material_name, False
            if (
                not stat.S_ISDIR(artifact_dir_stat.st_mode)
                or stat.S_ISLNK(artifact_dir_stat.st_mode)
                or _is_reparse_point(artifact_dir)
            ):
                return artifact_dir, material_name, False
            meta_path = artifact_dir / "meta.json"
            try:
                meta_stat = native_io_path(meta_path).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return artifact_dir, material_name, False
            return (
                artifact_dir,
                material_name,
                stat.S_ISREG(meta_stat.st_mode)
                and not stat.S_ISLNK(meta_stat.st_mode)
                and not _is_reparse_point(meta_path),
            )
        return None

    def _preferred_material_path_for_ref(
        self,
        ref: ArtifactRef,
    ) -> tuple[Path | None, bool]:
        """Select ``path_for(ref)`` precedence without following filesystem links."""

        _validate_sha256(ref.sha256)
        candidates = (
            self._artifact_dir(ref.session_id, ref.id) / ARTIFACT_MATERIAL_NAME,
            self._legacy_short_artifact_dir(ref.session_id, ref.id)
            / ARTIFACT_MATERIAL_NAME,
            self._legacy_artifact_dir(ref.session_id, ref.id) / ref.sha256,
        )
        for index, material_path in enumerate(candidates):
            artifact_dir = material_path.parent
            root = artifact_dir.parent
            try:
                root_stat = native_io_path(root).lstat()
            except FileNotFoundError:
                if index < len(candidates) - 1:
                    continue
                return material_path, False
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(root_stat.st_mode)
                or _is_reparse_point(root)
            ):
                return material_path, False
            try:
                artifact_dir_stat = native_io_path(artifact_dir).lstat()
            except FileNotFoundError:
                if index < len(candidates) - 1:
                    continue
                return material_path, False
            except OSError:
                return material_path, False
            if (
                not stat.S_ISDIR(artifact_dir_stat.st_mode)
                or stat.S_ISLNK(artifact_dir_stat.st_mode)
                or _is_reparse_point(artifact_dir)
            ):
                return material_path, False
            try:
                material_stat = native_io_path(material_path).lstat()
            except FileNotFoundError:
                if index < len(candidates) - 1:
                    continue
                return material_path, False
            except OSError:
                return material_path, False
            return (
                material_path,
                stat.S_ISREG(material_stat.st_mode)
                and not stat.S_ISLNK(material_stat.st_mode)
                and not _is_reparse_point(material_path),
            )
        return None, False

    def _legacy_artifact_dir(self, session_id: str, artifact_id: str) -> Path:
        return (
            self.media_root
            / ARTIFACT_STORE
            / _safe_token(_validate_non_empty("session_id", session_id))
            / _validate_artifact_id(artifact_id)
        )

    def _resolve_meta_path(self, session_id: str, artifact_id: str) -> Path:
        for artifact_dir in (
            self._artifact_dir(session_id, artifact_id),
            self._legacy_short_artifact_dir(session_id, artifact_id),
            self._legacy_artifact_dir(session_id, artifact_id),
        ):
            meta_path = artifact_dir / "meta.json"
            if native_io_path(meta_path).exists():
                return meta_path
        return self._artifact_dir(session_id, artifact_id) / "meta.json"

    def _disk_usage_bytes(self) -> int:
        root = self.media_root / ARTIFACT_STORE
        native_root = native_io_path(root)
        if not native_root.exists():
            return 0
        total = 0
        for path in native_root.rglob("*"):
            try:
                if path.is_file() and path.name != "meta.json":
                    total += path.stat().st_size
            except OSError:
                continue
        return total


def _build_thumbnail(payload: bytes, mime: str) -> bytes | None:
    """Render a small webp thumbnail for image artifacts.

    Returns the encoded webp bytes, or None when the artifact is not an image,
    Pillow is unavailable, or the bytes cannot be decoded. Any failure here is
    non-fatal: the caller publishes the artifact without a thumbnail.
    """

    if not mime.startswith("image/"):
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            if image.mode in ("RGBA", "LA", "P"):
                source = image.convert("RGBA")
            else:
                source = image.convert("RGB")
            source.thumbnail(
                (ARTIFACT_THUMBNAIL_MAX_EDGE, ARTIFACT_THUMBNAIL_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )
            out = io.BytesIO()
            source.save(out, format="WEBP", quality=ARTIFACT_THUMBNAIL_QUALITY)
            return out.getvalue()
    except Exception:
        _log.debug("artifact thumbnail generation failed for mime=%s", mime, exc_info=True)
        return None


def _safe_filename(name: str) -> str:
    cleaned = Path(name).name.strip() or "artifact"
    cleaned = _UNSAFE_FILENAME_RE.sub("_", cleaned).strip()
    return cleaned[:160] or "artifact"


def _safe_mime(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.split(";", 1)[0].strip()
        if _SAFE_MIME_RE.fullmatch(normalized):
            return normalized
    return "application/octet-stream"


def _safe_token(value: str) -> str:
    cleaned = _SAFE_TOKEN_RE.sub("_", value.strip())
    if cleaned in {".", ".."}:
        return "session"
    return cleaned[:180] or "session"


def _session_store_token(session_id: str, *, chars: int = ARTIFACT_STORE_TOKEN_CHARS) -> str:
    raw = _validate_non_empty("session_id", session_id)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:chars]


def _artifact_store_token(artifact_id: str, *, chars: int = ARTIFACT_STORE_TOKEN_CHARS) -> str:
    raw = _validate_artifact_id(artifact_id)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:chars]


def _validate_artifact_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("art-"):
        raise ValueError("artifact id is invalid")
    if _safe_token(value) != value:
        raise ValueError("artifact id is invalid")
    return value


def _validate_non_empty(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value


def _validate_size(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("artifact size is invalid")
    return value
