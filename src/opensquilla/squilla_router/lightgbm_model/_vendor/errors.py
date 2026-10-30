"""Domain-specific exceptions used by the foundation layer."""


class RouterTrainingError(Exception):
    """Base class for expected router-training failures."""


class ConfigError(RouterTrainingError):
    """A configuration file is missing, malformed, or invalid."""


class ContractValidationError(RouterTrainingError):
    """A payload violates a versioned contract."""


class CanonicalizationError(RouterTrainingError):
    """A value cannot be represented as deterministic canonical JSON."""


class IdentityError(RouterTrainingError):
    """A stable identifier cannot be derived without guessing."""


class ArtifactError(RouterTrainingError):
    """Base class for artifact-store errors."""


class ArtifactConflictError(ArtifactError):
    """An immutable artifact path already contains different content."""


class ArtifactIntegrityError(ArtifactError):
    """An artifact or committed stage no longer matches its manifest."""


class MetadataError(RouterTrainingError):
    """Base class for metadata-store errors."""


class MetadataConflictError(MetadataError):
    """An immutable metadata identifier maps to a different payload."""


class InvalidStateTransition(MetadataError):
    """A registry state transition violates the state machine."""


class SourceInspectionError(RouterTrainingError):
    """A source cannot pass non-mutating local inspection."""
