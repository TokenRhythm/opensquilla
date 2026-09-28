"""Host-owned, bounded HTTP capabilities exposed over a private Unix socket."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web


class HTTPBrokerError(ValueError):
    """A capability or bounded transport request was rejected."""


@dataclass(frozen=True)
class HTTPRoute:
    method: str
    path_pattern: str
    query_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HTTPServiceGrant:
    name: str
    origin: str
    routes: tuple[HTTPRoute, ...]
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30
    max_request_bytes: int = 1024 * 1024
    max_response_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        try:
            origin = urlsplit(self.origin)
            port = origin.port
        except ValueError:
            raise HTTPBrokerError("Service origin is invalid") from None
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self.name)
            or origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or any(character.isspace() for character in self.origin)
            or (port is not None and port == 0)
            or (
                origin.scheme == "http" and origin.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise HTTPBrokerError("Service needs a fixed HTTPS or loopback HTTP origin")
        if not self.routes or any(route.method not in {"GET", "POST"} for route in self.routes):
            raise HTTPBrokerError("Explicit read-only service routes are required")
        for route in self.routes:
            re.compile(route.path_pattern)
        if (
            not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 120
            or type(self.max_request_bytes) is not int
            or not 1 <= self.max_request_bytes <= 16 * 1024 * 1024
            or type(self.max_response_bytes) is not int
            or not 1 <= self.max_response_bytes <= 32 * 1024 * 1024
        ):
            raise HTTPBrokerError("Invalid service request, response or time budget")
        if self.api_key is not None and (
            not isinstance(self.api_key, str) or "\r" in self.api_key or "\n" in self.api_key
        ):
            raise HTTPBrokerError("Service credential must be single-line")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise HTTPBrokerError("Duplicate broker request keys")
        result[key] = value
    return result


class SkillHTTPBroker:
    """A host-created capability; no caller-selected origins, headers or credentials."""

    def __init__(
        self,
        *,
        services: tuple[HTTPServiceGrant, ...],
        socket_directory: Path,
        receipt_directory: Path,
        execution_id: str,
        caller_binding: str,
        max_calls: int = 256,
        max_receipt_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self.services = {service.name: service for service in services}
        if (
            not services
            or len(self.services) != len(services)
            or not execution_id
            or not caller_binding
        ):
            raise HTTPBrokerError(
                "Unique services and host execution/caller identities are required"
            )
        if not 1 <= max_calls <= 4096 or not 1024 <= max_receipt_bytes <= 1024 * 1024 * 1024:
            raise HTTPBrokerError("Invalid broker lifetime budget")
        self.socket_directory = socket_directory.resolve(strict=True)
        self.receipt_directory = receipt_directory.resolve(strict=True)
        if (
            not self.socket_directory.is_dir()
            or not self.receipt_directory.is_dir()
            or self.socket_directory.is_relative_to(self.receipt_directory)
            or self.receipt_directory.is_relative_to(self.socket_directory)
        ):
            raise HTTPBrokerError("Private socket and receipt directories must be disjoint")
        if len(os.fsencode(self.socket_directory)) + 14 >= 108:
            raise HTTPBrokerError("Host socket directory must use a shorter Unix socket path")
        self.execution_id = execution_id
        self.caller_binding = caller_binding
        self.max_calls = max_calls
        self.max_receipt_bytes = max_receipt_bytes
        self._calls = 0
        self._receipt_bytes = 0
        self._call_lock = asyncio.Lock()
        self._serve_lock = asyncio.Lock()

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[Path]:
        async with self._serve_lock:
            maximum = max(grant.max_request_bytes for grant in self.services.values())
            app = web.Application(client_max_size=maximum * 4 // 3 + 8192)
            app.router.add_post("/call", self._handle)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            with tempfile.TemporaryDirectory(prefix="b-", dir=self.socket_directory) as temp:
                path = Path(temp) / "s"
                await runner.setup()
                try:
                    await web.UnixSite(runner, str(path)).start()
                    path.chmod(0o600)
                    yield path
                finally:
                    await runner.cleanup()

    def _decode(self, value: Any) -> tuple[HTTPServiceGrant, str, str, bytes | None]:
        if not isinstance(value, dict) or set(value) != {"service", "method", "path", "bodyBase64"}:
            raise HTTPBrokerError("Broker request must contain only the granted wire fields")
        service, method, path = value["service"], value["method"], value["path"]
        if not isinstance(service, str) or service not in self.services:
            raise HTTPBrokerError("Service is not granted")
        if not isinstance(method, str) or not isinstance(path, str) or len(path) > 8192:
            raise HTTPBrokerError("Invalid method or path")
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or "%" in parsed.path
            or any(character.isspace() or ord(character) < 32 for character in path)
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise HTTPBrokerError("Only a relative public service route is allowed")
        queries = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=32)
        keys = [key for key, _ in queries]
        grant = self.services[service]
        if len(set(keys)) != len(keys) or not any(
            route.method == method
            and re.fullmatch(route.path_pattern, parsed.path)
            and set(keys).issubset(route.query_keys)
            for route in grant.routes
        ):
            raise HTTPBrokerError("Service method, route or query is not granted")
        encoded = value["bodyBase64"]
        if encoded is None:
            body = None
        elif isinstance(encoded, str):
            if len(encoded) > (grant.max_request_bytes + 2) // 3 * 4:
                raise HTTPBrokerError("Service request exceeds its body budget")
            try:
                body = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise HTTPBrokerError("Service request body is not base64") from None
            if len(body) > grant.max_request_bytes:
                raise HTTPBrokerError("Service request exceeds its body budget")
        else:
            raise HTTPBrokerError("Service request body must be base64 or null")
        if method == "GET" and body is not None:
            raise HTTPBrokerError("Read requests cannot contain a GET body")
        return grant, method, path, body

    async def _handle(self, request: web.Request) -> web.Response:
        try:
            value = json.loads(await request.read(), object_pairs_hook=_unique_object)
            grant, method, path, body = self._decode(value)
        except (ValueError, TypeError, RecursionError, web.HTTPRequestEntityTooLarge):
            return web.json_response(
                {"error": "Broker request is outside the host grant"}, status=400
            )
        async with self._call_lock:
            if self._calls >= self.max_calls or self._receipt_bytes >= self.max_receipt_bytes:
                return web.json_response({"error": "Broker lifetime budget exhausted"}, status=429)
            self._calls += 1
            started = datetime.now(UTC).isoformat()
            response: dict[str, Any] | None = None
            error: str | None = None
            cancelled = False
            try:
                headers = {"Accept": "*/*", "Accept-Encoding": "identity"}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                if grant.api_key:
                    headers["Authorization"] = f"Bearer {grant.api_key}"
                async with (
                    ClientSession(
                        trust_env=False,
                        auto_decompress=False,
                        timeout=ClientTimeout(total=grant.timeout_seconds),
                    ) as session,
                    session.request(
                        method,
                        grant.origin.rstrip("/") + path,
                        data=body,
                        headers=headers,
                        allow_redirects=False,
                    ) as upstream,
                ):
                    if 300 <= upstream.status < 400:
                        raise HTTPBrokerError("Service redirects are forbidden")
                    if (
                        upstream.content_length is not None
                        and upstream.content_length > grant.max_response_bytes
                    ):
                        raise HTTPBrokerError("Service response exceeds its body budget")
                    content = bytearray()
                    async for chunk in upstream.content.iter_chunked(65536):
                        content.extend(chunk)
                        if len(content) > grant.max_response_bytes:
                            raise HTTPBrokerError("Service response exceeds its body budget")
                    data = bytes(content)
                    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
                    if grant.api_key and (
                        grant.api_key.encode() in data or grant.api_key in content_type
                    ):
                        raise HTTPBrokerError("Service response contains a protected credential")
                    response = {
                        "status": upstream.status,
                        "contentType": content_type,
                        "bodyBase64": base64.b64encode(data).decode("ascii"),
                        "bodySha256": hashlib.sha256(data).hexdigest(),
                    }
            except asyncio.CancelledError:
                error, cancelled = "Service request cancelled", True
            except (ClientError, TimeoutError, HTTPBrokerError, OSError):
                error = "Service request failed or exceeded the host boundary"
            receipt = {
                "schemaVersion": "skill-http-receipt/1",
                "executionId": self.execution_id,
                "callerBinding": self.caller_binding,
                "service": grant.name,
                "startedAt": started,
                "finishedAt": datetime.now(UTC).isoformat(),
                "request": {
                    "method": method,
                    "path": path,
                    "bodyBase64": base64.b64encode(body).decode("ascii")
                    if body is not None
                    else None,
                    "bodySha256": hashlib.sha256(body or b"").hexdigest(),
                },
                "response": response,
                "error": error,
            }
            try:
                receipt_id = self._save_receipt(receipt)
            except (OSError, HTTPBrokerError):
                return web.json_response({"error": "Host receipt persistence failed"}, status=502)
            if cancelled:
                raise asyncio.CancelledError
            if response is None:
                return web.json_response({"error": error, "receiptId": receipt_id}, status=502)
            return web.json_response(
                {
                    "status": response["status"],
                    "contentType": response["contentType"],
                    "bodyBase64": response["bodyBase64"],
                    "receiptId": receipt_id,
                }
            )

    def _save_receipt(self, receipt: dict[str, Any]) -> str:
        data = _json_bytes(receipt)
        if self._receipt_bytes + len(data) > self.max_receipt_bytes:
            raise HTTPBrokerError("Receipt lifetime budget exhausted")
        digest = hashlib.sha256(data).hexdigest()
        descriptor = os.open(
            self.receipt_directory / f"{digest}.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self._receipt_bytes += len(data)
        return digest
