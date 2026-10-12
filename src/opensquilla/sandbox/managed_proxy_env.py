"""Managed network proxy environment shared by sandbox backends."""

from __future__ import annotations

PROXY_ACTIVE_ENV_KEY = "CODEX_NETWORK_PROXY_ACTIVE"
ALLOW_LOCAL_BINDING_ENV_KEY = "CODEX_NETWORK_ALLOW_LOCAL_BINDING"
OPENSQUILLA_NETWORK_ENV_KEY = "OPENSQUILLA_SANDBOX_NETWORK"

PROXY_ENV_KEYS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "YARN_HTTP_PROXY",
    "YARN_HTTPS_PROXY",
    "npm_config_http_proxy",
    "npm_config_https_proxy",
    "npm_config_proxy",
    "NPM_CONFIG_HTTP_PROXY",
    "NPM_CONFIG_HTTPS_PROXY",
    "NPM_CONFIG_PROXY",
    "BUNDLE_HTTP_PROXY",
    "BUNDLE_HTTPS_PROXY",
    "PIP_PROXY",
    "DOCKER_HTTP_PROXY",
    "DOCKER_HTTPS_PROXY",
    "WS_PROXY",
    "WSS_PROXY",
    "ws_proxy",
    "wss_proxy",
    "ALL_PROXY",
    "all_proxy",
    "FTP_PROXY",
    "ftp_proxy",
)

NO_PROXY_ENV_KEYS: tuple[str, ...] = (
    "NO_PROXY",
    "no_proxy",
    "npm_config_noproxy",
    "NPM_CONFIG_NOPROXY",
    "YARN_NO_PROXY",
    "BUNDLE_NO_PROXY",
)

PROXY_CONTROL_ENV: tuple[tuple[str, str], ...] = (
    (PROXY_ACTIVE_ENV_KEY, "1"),
    (ALLOW_LOCAL_BINDING_ENV_KEY, "0"),
    ("NODE_USE_ENV_PROXY", "1"),
    ("ELECTRON_GET_USE_PROXY", "true"),
    (OPENSQUILLA_NETWORK_ENV_KEY, "proxy_allowlist"),
)

WINDOWS_GIT_SSL_ENV: tuple[tuple[str, str], ...] = (
    ("GIT_CONFIG_COUNT", "1"),
    ("GIT_CONFIG_KEY_0", "http.sslBackend"),
    ("GIT_CONFIG_VALUE_0", "openssl"),
)

DEFAULT_NO_PROXY_VALUE = (
    "localhost,127.0.0.1,::1,"
    "10.0.0.0/8,"
    "172.16.0.0/12,"
    "192.168.0.0/16"
)


def managed_proxy_env(
    host: str,
    port: int,
    *,
    windows_git_ssl_backend: bool = False,
) -> dict[str, str]:
    proxy_url = f"http://{host}:{port}"
    env = {key: proxy_url for key in PROXY_ENV_KEYS}
    env.update({key: DEFAULT_NO_PROXY_VALUE for key in NO_PROXY_ENV_KEYS})
    env.update(PROXY_CONTROL_ENV)
    if windows_git_ssl_backend:
        env.update(WINDOWS_GIT_SSL_ENV)
    return env


def managed_proxy_env_allowlist(
    *,
    include_windows_git: bool = False,
) -> tuple[str, ...]:
    return (
        *PROXY_ENV_KEYS,
        *NO_PROXY_ENV_KEYS,
        *(key for key, _ in PROXY_CONTROL_ENV),
        *((key for key, _ in WINDOWS_GIT_SSL_ENV) if include_windows_git else ()),
    )


def managed_proxy_env_names_upper(
    *,
    include_windows_git: bool = False,
) -> frozenset[str]:
    return frozenset(
        key.upper()
        for key in managed_proxy_env_allowlist(
            include_windows_git=include_windows_git,
        )
    )


__all__ = [
    "ALLOW_LOCAL_BINDING_ENV_KEY",
    "DEFAULT_NO_PROXY_VALUE",
    "NO_PROXY_ENV_KEYS",
    "OPENSQUILLA_NETWORK_ENV_KEY",
    "PROXY_ACTIVE_ENV_KEY",
    "PROXY_CONTROL_ENV",
    "PROXY_ENV_KEYS",
    "WINDOWS_GIT_SSL_ENV",
    "managed_proxy_env",
    "managed_proxy_env_allowlist",
    "managed_proxy_env_names_upper",
]
