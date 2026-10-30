"""Domain-specific exceptions used by the foundation layer."""


class RouterTrainingError(Exception):
    """Base class for expected router-training failures."""


class CanonicalizationError(RouterTrainingError):
    """A value cannot be represented as deterministic canonical JSON."""
