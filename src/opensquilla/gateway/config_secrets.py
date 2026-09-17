"""Compatibility imports for the shared configuration secret policy."""

from opensquilla.application.config_secrets import (
    REDACTED_PUBLIC_VALUE as REDACTED_PUBLIC_VALUE,
)
from opensquilla.application.config_secrets import (
    clear_runtime_secret_paths as clear_runtime_secret_paths,
)
from opensquilla.application.config_secrets import (
    collect_paths as collect_paths,
)
from opensquilla.application.config_secrets import (
    forget_secret_provenance_paths as forget_secret_provenance_paths,
)
from opensquilla.application.config_secrets import (
    inherit_runtime_secrets as inherit_runtime_secrets,
)
from opensquilla.application.config_secrets import (
    inherit_then_clear_explicit as inherit_then_clear_explicit,
)
from opensquilla.application.config_secrets import (
    is_sensitive_redacted_path as is_sensitive_redacted_path,
)
from opensquilla.application.config_secrets import (
    restore_redacted_values as restore_redacted_values,
)
