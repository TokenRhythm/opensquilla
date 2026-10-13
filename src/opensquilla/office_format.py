"""Modern, unencrypted Office packages supported by the local read-only viewer.

Reuse the hardened OPC/ZIP checks used for artifact delivery.  This validates a
package, not rendering fidelity or permission to mutate a document.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from opensquilla import artifact_validation as opc

OFFICE_MIMES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
MAX_OFFICE_BYTES = 30 * 1024 * 1024
_MAIN_TYPES = {
    "docx": ("wordprocessingml.document", "document"),
    "xlsx": ("spreadsheetml.sheet", "workbook"),
    "pptx": ("presentationml.presentation", "presentation"),
}


class OfficeFormatError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def office_format(name: str, mime: str = "") -> str | None:
    suffix = Path(name).suffix.lower().removeprefix(".")
    media = mime.split(";", 1)[0].strip().lower()
    by_mime = next((key for key, value in OFFICE_MIMES.items() if value == media), None)
    if suffix in OFFICE_MIMES:
        # A conflicting modern Office MIME must not select a different parser.
        if by_mime is not None and suffix != by_mime:
            return None
        return suffix
    return by_mime if not suffix or suffix == "bin" else None


def validate_office_bytes(payload: bytes, format: str) -> None:
    """Bounded ZIP/OPC validation without Office, network access or conversion."""
    if format not in OFFICE_MIMES:
        raise OfficeFormatError("office_format_unsupported")
    if not payload or len(payload) > MAX_OFFICE_BYTES:
        raise OfficeFormatError("office_size_unsupported")
    try:
        central = opc._validate_zip_envelope(payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) != len(central):
                raise OfficeFormatError("office_invalid_package")
            parts = opc._validate_zip_members(archive, infos, central)
            content_types = opc._parse_content_types(archive, parts)
            relationships = opc._parse_relationships(
                archive,
                parts,
                "_rels/.rels",
                missing_reason="office_missing_relationships",
            )
            roots = [
                item
                for item in relationships.values()
                if item.relationship_type in opc._OFFICE_DOCUMENT_RELATIONSHIP_TYPES
            ]
            if len(roots) != 1 or roots[0].target_mode == "External":
                raise OfficeFormatError("office_invalid_document_relationship")
            main_part = opc._resolve_relationship_target(source_part=None, target=roots[0].target)
            type_family, root_name = _MAIN_TYPES[format]
            expected_type = f"application/vnd.openxmlformats-officedocument.{type_family}.main+xml"
            if content_types.for_part(main_part) != expected_type:
                raise OfficeFormatError("office_content_type_mismatch")
            root = opc._read_xml(
                archive, parts, main_part, missing_reason="office_missing_document"
            )
            namespace, local_name = opc._split_qname(root.tag)
            family = type_family.split(".", 1)[0]
            if local_name != root_name or namespace not in {
                f"http://schemas.openxmlformats.org/{family}/2006/main",
                f"http://purl.oclc.org/ooxml/{family}/main",
            }:
                raise OfficeFormatError("office_invalid_document")
            opc._validate_reachable_relationship_targets(
                archive,
                parts,
                content_types,
                relationships,
                presentation_part=main_part,
            )
    except OfficeFormatError:
        raise
    except opc.ArtifactValidationError as exc:
        raise OfficeFormatError(exc.reason_code.replace("pptx_", "office_", 1)) from exc
    except (
        ValueError,
        OSError,
        KeyError,
        RuntimeError,
        zipfile.BadZipFile,
        NotImplementedError,
    ) as exc:
        raise OfficeFormatError("office_invalid_package") from exc
