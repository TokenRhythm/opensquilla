"""Domain normalization and safety checks for sandbox managed network."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

DomainStatus = Literal["allowed", "blocked"]


@dataclass(frozen=True)
class DomainDecision:
    status: DomainStatus
    normalized: str
    reason: str


_ALLOWED_WILDCARD_SUFFIXES = {"pythonhosted.org"}
_DNS_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_IPV4_NUMERIC_ALIAS_LABEL_RE = re.compile(r"(?:\d+|0x[0-9a-f]+)")
_PORT_RE = re.compile(r"[0-9]{1,5}")


def normalize_domain(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        try:
            parsed = urlparse(text)
            if "[" in parsed.netloc or "]" in parsed.netloc:
                return ""
            text = parsed.hostname or ""
        except ValueError:
            return ""
    else:
        text = text.split("/", 1)[0]
        if text.startswith("["):
            bracket_end = text.find("]")
            text = text[: bracket_end + 1] if bracket_end != -1 else text
        elif text.count(":") == 1:
            host, port = text.rsplit(":", 1)
            if _is_valid_port(port):
                text = host
    return text.lower()


def validate_domain_pattern(raw: str) -> DomainDecision:
    normalized, extraction_error = _extract_validation_host(raw)
    if not normalized:
        return DomainDecision("blocked", normalized, "empty_domain")
    if extraction_error is not None:
        return DomainDecision("blocked", normalized, extraction_error)
    if _is_ip_literal(normalized):
        return DomainDecision("blocked", normalized, "ip_literal")
    if normalized.startswith("*."):
        suffix = normalized[2:]
        if suffix.count(".") < 1:
            return DomainDecision("blocked", normalized, "broad_wildcard")
        if not _is_valid_dns_name(suffix):
            return DomainDecision("blocked", normalized, "invalid_domain")
        if suffix not in _ALLOWED_WILDCARD_SUFFIXES:
            return DomainDecision("blocked", normalized, "broad_wildcard")
        return DomainDecision("allowed", normalized, "wildcard_domain")
    if "*" in normalized:
        return DomainDecision("blocked", normalized, "invalid_wildcard")
    normalized = _normalize_exact_validation_host(normalized)
    if not normalized:
        return DomainDecision("blocked", normalized, "empty_domain")
    if _is_ip_literal(normalized):
        return DomainDecision("blocked", normalized, "ip_literal")
    if "." not in normalized:
        return DomainDecision("blocked", normalized, "not_fqdn")
    if not _is_valid_dns_name(normalized):
        return DomainDecision("blocked", normalized, "invalid_domain")
    return DomainDecision("allowed", normalized, "exact_domain")


def domain_matches(pattern: str, host: str) -> bool:
    decision = validate_domain_pattern(pattern)
    if decision.status != "allowed":
        return False
    normalized_pattern = decision.normalized
    normalized_host, extraction_error = _extract_validation_host(host)
    if extraction_error is not None:
        return False
    normalized_host = _normalize_exact_validation_host(normalized_host)
    if _is_ip_literal(normalized_host) or not _is_valid_dns_name(normalized_host):
        return False
    if normalized_pattern.startswith("*."):
        suffix = normalized_pattern[2:]
        return normalized_host.endswith(f".{suffix}")
    return normalized_host == normalized_pattern


def validate_policy_domain_pattern(raw: str) -> DomainDecision:
    """Validate user allow/deny entries without the legacy wildcard allowlist."""
    normalized, extraction_error = _extract_validation_host(raw)
    if not normalized:
        return DomainDecision("blocked", normalized, "empty_domain")
    if extraction_error is not None:
        return DomainDecision("blocked", normalized, extraction_error)
    if normalized.startswith("*."):
        suffix = normalized[2:].rstrip(".")
        if suffix.count(".") < 1:
            return DomainDecision("blocked", normalized, "broad_wildcard")
        if not _is_valid_dns_name(suffix):
            return DomainDecision("blocked", normalized, "invalid_domain")
        return DomainDecision("allowed", f"*.{suffix}", "wildcard_domain")
    if "*" in normalized:
        return DomainDecision("blocked", normalized, "invalid_wildcard")
    normalized = _normalize_exact_validation_host(normalized)
    if _is_ip_literal(normalized):
        return DomainDecision("blocked", normalized, "ip_literal")
    if "." not in normalized or not _is_valid_dns_name(normalized):
        return DomainDecision("blocked", normalized, "invalid_domain")
    return DomainDecision("allowed", normalized, "exact_domain")


def policy_domain_matches(pattern: str, host: str) -> bool:
    decision = validate_policy_domain_pattern(pattern)
    if decision.status != "allowed":
        return False
    host_decision = validate_domain_pattern(host)
    if host_decision.status != "allowed" or "*" in host_decision.normalized:
        return False
    if decision.normalized.startswith("*."):
        return host_decision.normalized.endswith(f".{decision.normalized[2:]}")
    return host_decision.normalized == decision.normalized


def _extract_validation_host(raw: str) -> tuple[str, str | None]:
    text = str(raw or "").strip().lower()
    if not text:
        return "", None
    if "://" in text:
        try:
            parsed = urlparse(text)
        except ValueError:
            return "", "invalid_domain"
        try:
            hostname = parsed.hostname or ""
        except ValueError:
            return "", "invalid_domain"
        try:
            parsed.port
        except ValueError:
            return hostname, "invalid_port"
        if "[" in parsed.netloc or "]" in parsed.netloc:
            return hostname, "invalid_domain"
        return hostname, None

    host = text.split("/", 1)[0]
    if host.startswith("["):
        bracket_end = host.find("]")
        if bracket_end == -1:
            return host, "invalid_domain"
        bracketed_host = host[: bracket_end + 1]
        remainder = host[bracket_end + 1 :]
        if remainder:
            if not remainder.startswith(":"):
                return host, "invalid_domain"
            if not _is_valid_port(remainder[1:]):
                return bracketed_host, "invalid_port"
        return bracketed_host, None
    if host.count(":") == 1:
        host_part, port = host.rsplit(":", 1)
        if not _is_valid_port(port):
            return host_part, "invalid_port"
        return host_part, None
    return host, None


def _normalize_exact_validation_host(value: str) -> str:
    if value.endswith(".") and not value.startswith("*."):
        return value[:-1]
    return value


def _is_valid_port(value: str) -> bool:
    if _PORT_RE.fullmatch(value) is None:
        return False
    port = int(value)
    return 0 <= port <= 65535


def _is_valid_dns_name(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    labels = value.split(".")
    for label in labels:
        if not label or len(label) > 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if any(char not in _DNS_LABEL_CHARS for char in label):
            return False
    return True


def _is_ip_literal(value: str) -> bool:
    candidate = value.strip("[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return _is_ipv4_numeric_alias(candidate)
    return True


def _is_ipv4_numeric_alias(value: str) -> bool:
    labels = value.split(".")
    if not 1 <= len(labels) <= 4:
        return False
    return all(_IPV4_NUMERIC_ALIAS_LABEL_RE.fullmatch(label) is not None for label in labels)


__all__ = [
    "DomainDecision",
    "domain_matches",
    "normalize_domain",
    "policy_domain_matches",
    "validate_domain_pattern",
    "validate_policy_domain_pattern",
]
