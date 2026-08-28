"""Same-origin guard and HTTP auth helpers shared by gateway HTTP routes.

A hostile web page can make a loopback victim's browser fire state-changing
requests at the gateway (classic cross-site request forgery): simple POSTs
execute server-side even when the browser withholds the response from the
page. The diagnostics-bundle route shipped the first same-origin check; this
module is the single shared implementation for every state-changing or
sensitive owner route.

Policy (matches the bundle-route precedent):

* Requests without an ``Origin`` header pass — curl, the CLI, and the desktop
  client's Node fetch are not browser-mediated and never send one.
* Same-origin requests pass — the gateway serves the Web UI itself, so its
  ``Origin`` always matches the request's own scheme/host/port.
* Origins explicitly listed in ``cors.allowed_origins`` pass — an operator who
  deliberately configured a separate frontend keeps a working deployment. The
  ``"*"`` wildcard never bypasses the guard; it would reopen the exact
  drive-by exposure this module exists to close.
* The exact registered ``opensquilla-app://desktop`` origin may reach a
  loopback HTTP Gateway. Normal web pages cannot forge their browser-controlled
  Origin, while this keeps Desktop development overrides interoperable.
* Everything else — including the opaque ``"null"`` origin and unparsable
  values — is rejected with 403 ``FORBIDDEN_ORIGIN``.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from opensquilla.gateway.config import GatewayConfig

_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_BROWSER_ORIGIN_SCHEMES = frozenset({"http", "https"})
_WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::"})
_DESKTOP_RENDERER_ORIGIN = "opensquilla-app://desktop"


def extract_http_token(request: Request | None) -> str | None:
    """Pull the gateway token from an HTTP request (header or query string)."""
    if request is None:
        return None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    token_header = request.headers.get("x-opensquilla-token")
    if token_header:
        return token_header
    return request.query_params.get("token")


def request_principal_is_owner(config: GatewayConfig, request: Request) -> bool:
    """Resolve the request's principal and report whether it is the owner."""
    from opensquilla.gateway.auth import resolve_auth

    auth_params: dict[str, str] = {}
    token = extract_http_token(request)
    if token:
        auth_params["token"] = token
    peer_ip = request.client.host if request.client is not None else None
    principal = resolve_auth(config, auth_params, "operator", peer_ip=peer_ip)
    return bool(principal and principal.is_owner)


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return _DEFAULT_SCHEME_PORTS.get(scheme)


def _http_equivalent_scheme(scheme: str) -> str:
    return {"ws": "http", "wss": "https"}.get(scheme, scheme)


def _normalized_hostname(hostname: str | None) -> str | None:
    if hostname is None:
        return None
    value = hostname.strip().rstrip(".").casefold()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).compressed
    except ValueError:
        return value


def _is_loopback_hostname(hostname: str | None) -> bool:
    normalized = _normalized_hostname(hostname)
    if normalized is None:
        return False
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_ip_literal(hostname: str | None) -> bool:
    normalized = _normalized_hostname(hostname)
    if normalized is None:
        return False
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return True


def _request_authority_matches_config(
    *,
    request_scheme: str,
    request_hostname: str,
    request_port: int | None,
    config: GatewayConfig,
) -> bool:
    """Validate Host against the configured bind instead of trusting it.

    A browser-controlled DNS name can resolve to loopback and make both Host
    and Origin agree (DNS rebinding).  Same-origin comparison is therefore
    meaningful only after the request authority is proven to be one the
    gateway is expected to serve.
    """

    request_host = _normalized_hostname(request_hostname)
    bind_host = _normalized_hostname(config.host)
    if request_host is None or bind_host is None:
        return False
    if bind_host in _WILDCARD_BIND_HOSTS:
        # A wildcard listener has no configured DNS authority. Trusting an
        # arbitrary same-origin Host here would let a hostile hostname that
        # resolves to the gateway pass both the Host and Origin comparison
        # (DNS rebinding). IP literals and localhost names are unambiguous;
        # operators using a custom hostname or reverse proxy must list that
        # exact browser origin in ``cors.allowed_origins``.
        if _effective_port(_http_equivalent_scheme(request_scheme), request_port) != config.port:
            return False
        return _is_ip_literal(request_host) or _is_loopback_hostname(request_host)
    if _effective_port(_http_equivalent_scheme(request_scheme), request_port) != config.port:
        return False
    if _is_loopback_hostname(bind_host):
        return _is_loopback_hostname(request_host)
    if _is_ip_literal(bind_host):
        return request_host == bind_host
    return request_host == bind_host


def _parsed_browser_origin(origin: str):
    """Parse a serialized browser Origin, rejecting non-origin URL material."""
    if not origin or origin == "null":
        return None
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in _BROWSER_ORIGIN_SCHEMES
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed, port


def _desktop_renderer_origin_allowed(
    *,
    origin: str,
    request_scheme: str,
    request_hostname: str | None,
    request_port: int | None,
    config: GatewayConfig | None,
) -> bool:
    """Allow the registered Desktop origin to reach only a loopback Gateway.

    A regular web page cannot choose its browser-controlled Origin header, and
    native clients could already omit Origin entirely. Keeping this exact
    custom origin interoperable also lets Desktop attach to a separately
    launched development Gateway without weakening any remote listener.
    """
    if origin != _DESKTOP_RENDERER_ORIGIN or not _is_loopback_hostname(request_hostname):
        return False
    normalized_request_scheme = _http_equivalent_scheme(request_scheme)
    if normalized_request_scheme != "http":
        return False
    return config is None or _request_authority_matches_config(
        request_scheme=normalized_request_scheme,
        request_hostname=request_hostname or "",
        request_port=request_port,
        config=config,
    )


def _origin_allowed(
    *,
    origin: str | None,
    request_scheme: str,
    request_hostname: str | None,
    request_port: int | None,
    config: GatewayConfig | None,
) -> bool:
    if origin is None:
        return True
    if _desktop_renderer_origin_allowed(
        origin=origin,
        request_scheme=request_scheme,
        request_hostname=request_hostname,
        request_port=request_port,
        config=config,
    ):
        return True
    parsed_origin = _parsed_browser_origin(origin)
    if parsed_origin is None or request_hostname is None:
        return False
    parsed, parsed_port = parsed_origin
    if config is not None and any(
        allowed == origin for allowed in config.cors.allowed_origins if allowed != "*"
    ):
        return True
    normalized_request_scheme = _http_equivalent_scheme(request_scheme)
    if config is not None and not _request_authority_matches_config(
        request_scheme=normalized_request_scheme,
        request_hostname=request_hostname,
        request_port=request_port,
        config=config,
    ):
        return False
    return (
        parsed.scheme == normalized_request_scheme
        and _normalized_hostname(parsed.hostname)
        == _normalized_hostname(request_hostname)
        and _effective_port(parsed.scheme, parsed_port)
        == _effective_port(normalized_request_scheme, request_port)
    )


def request_origin_allowed(request: Request, config: GatewayConfig | None = None) -> bool:
    """Reject browser requests whose Origin is not the gateway itself.

    Browsers always attach ``Origin`` to cross-origin fetches and to
    same-origin POSTs; the gateway-served Web UI is same-origin so its
    ``Origin`` matches the request's own host. Requests without an ``Origin``
    header (curl, the desktop node client) are not browser-mediated and pass.
    Origins the operator explicitly listed in ``cors.allowed_origins`` pass
    too, except the ``"*"`` wildcard, which never bypasses the guard.
    """
    request_url = request.url
    try:
        request_port = request_url.port
    except ValueError:
        return False
    return _origin_allowed(
        origin=request.headers.get("origin"),
        request_scheme=request_url.scheme,
        request_hostname=request_url.hostname,
        request_port=request_port,
        config=config,
    )


def websocket_origin_allowed(websocket: WebSocket, config: GatewayConfig | None = None) -> bool:
    """Apply the HTTP same-origin policy to a WebSocket upgrade.

    Browser WebSocket handshakes carry the embedding page's HTTP(S) Origin
    while the request URL itself uses WS(S), so the request scheme is mapped
    to its HTTP equivalent before comparison. Origin-less native clients
    remain compatible.
    """

    headers = getattr(websocket, "headers", None)
    origin = headers.get("origin") if headers is not None else None
    url = getattr(websocket, "url", None)
    if url is None:
        return origin is None
    try:
        request_port = getattr(url, "port", None)
    except ValueError:
        return False
    return _origin_allowed(
        origin=origin,
        request_scheme=str(getattr(url, "scheme", "") or ""),
        request_hostname=getattr(url, "hostname", None),
        request_port=request_port,
        config=config,
    )


def forbidden_origin_response() -> JSONResponse:
    """The uniform 403 payload for a rejected cross-origin request."""
    return JSONResponse(
        {"error": "cross-origin requests are not allowed", "code": "FORBIDDEN_ORIGIN"},
        status_code=403,
    )
