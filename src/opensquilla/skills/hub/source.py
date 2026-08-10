"""SkillSource ABC and Community source data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SkillMeta:
    """Metadata for a skill in a Community source listing."""

    name: str
    description: str = ""
    version: str = ""
    author: str = ""
    source_id: str = ""
    trust_level: str = "community"  # "builtin" | "trusted" | "community"
    identifier: str = ""  # source-specific ID (e.g. slug@version)
    homepage: str = ""
    license: str = ""
    tags: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    # Publisher handle; disambiguates duplicate slugs on registries that
    # allow the same slug under multiple publishers (e.g. clawhub).
    owner_handle: str = ""
    # For aggregated listings (e.g. clawhub entries mirrored from skills.sh),
    # the registry download endpoint may not serve the artifact at all.
    # fallback_source names the upstream registry and fallback_url points at
    # a directly downloadable artifact (e.g. a GitHub archive zip).
    fallback_source: str = ""
    fallback_url: str = ""


class SkillFetchError(Exception):
    """Raised by a SkillSource when a fetch fails for a known, actionable reason.

    The installer surfaces ``reason`` to the user so a 404, an ambiguous slug,
    or a rate limit is not hidden behind one generic message.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason



@dataclass
class SkillBundle:
    """Downloaded skill ready for installation."""

    name: str
    files: dict[str, str | bytes] = field(default_factory=dict)  # relative_path → content
    meta: SkillMeta | None = None

    @property
    def skill_md(self) -> str | None:
        content = self.files.get("SKILL.md")
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


class SkillSource(ABC):
    """Abstract base class for skill Community sources."""

    @abstractmethod
    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        """Search for skills matching query."""

    @abstractmethod
    async def fetch(self, identifier: str) -> SkillBundle | None:
        """Download a skill by its source-specific identifier."""

    @abstractmethod
    async def inspect(self, identifier: str) -> SkillMeta | None:
        """Get metadata for a skill without downloading."""

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Unique identifier for this source (e.g. 'clawhub', 'github')."""

    @property
    @abstractmethod
    def trust_level(self) -> str:
        """Trust level: 'builtin', 'trusted', or 'community'."""
