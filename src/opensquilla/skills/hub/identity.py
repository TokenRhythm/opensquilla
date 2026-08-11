"""Exact installed-package identity matching for Community search results."""

from __future__ import annotations

from opensquilla.skills.hub.github import package_identifier_for
from opensquilla.skills.hub.lockfile import LockEntry, Lockfile
from opensquilla.skills.hub.source import SkillMeta


def _clawhub_package_identifier(identifier: str) -> str:
    value = identifier.strip()
    if not value.startswith("@") or "/" not in value:
        return ""
    owner, slug = value[1:].split("/", 1)
    if "@" in slug:
        slug = slug.rsplit("@", 1)[0]
    if not owner or not slug or "/" in slug:
        return ""
    return f"clawhub:@{owner.lower()}/{slug}"


def package_identity_for_meta(meta: SkillMeta) -> str:
    """Return a version-independent package identity when one is derivable."""

    identifier = meta.canonical_identifier or meta.identifier
    if not identifier or not meta.source_id:
        return ""
    if meta.source_id == "github":
        package = package_identifier_for(identifier)
        return f"github:{package}" if package else ""
    if meta.source_id == "clawhub":
        return _clawhub_package_identifier(identifier)
    return ""


def _entry_identifiers(entry: LockEntry) -> set[str]:
    return {
        value
        for value in (
            entry.requested_identifier,
            entry.identifier,
            entry.resolved_identifier,
        )
        if value
    }


def _entry_clawhub_package_identity(entry: LockEntry) -> str:
    for identifier in (
        entry.source_package_id,
        entry.resolved_identifier,
        entry.requested_identifier,
        entry.identifier,
    ):
        package = _clawhub_package_identifier(identifier.removeprefix("clawhub:"))
        if package:
            return package
    return ""


def is_skill_meta_installed(meta: SkillMeta, lockfile: Lockfile) -> bool:
    """Match a registry row to a lock entry without using its display name.

    v2 package identities are authoritative.  Exact source identifiers remain
    a bounded compatibility path for v1 locks and registry hand-off references
    whose publisher identity cannot be derived without another network call.
    """

    if lockfile.mutation_blocked or not meta.source_id:
        return False
    row_identifiers = {
        value for value in (meta.canonical_identifier, meta.identifier) if value
    }
    row_package = package_identity_for_meta(meta)
    for entry in lockfile.installed.values():
        if entry.source != meta.source_id:
            continue
        if entry.source_package_id and row_package:
            entry_package = entry.source_package_id
            if entry.source == "github":
                package = package_identifier_for(entry_package.removeprefix("github:"))
                if package:
                    entry_package = f"github:{package}"
            elif entry.source == "clawhub":
                entry_package = _entry_clawhub_package_identity(entry)
            if entry_package == row_package:
                return True
            # Two explicit but different package identities must never fall
            # back to a same-name or coincidentally similar identifier match.
            continue
        if row_package and entry.source == "clawhub":
            entry_package = _entry_clawhub_package_identity(entry)
            if entry_package == row_package:
                return True
            # An ownerless v1 bare slug does not prove which publisher-owned
            # registry row it belongs to. Do not mark every same-slug owner as
            # installed; a subsequent bare-slug update can bind the identity.
            if entry_package:
                continue
        if row_identifiers & _entry_identifiers(entry):
            return True
    return False


__all__ = ["is_skill_meta_installed", "package_identity_for_meta"]
