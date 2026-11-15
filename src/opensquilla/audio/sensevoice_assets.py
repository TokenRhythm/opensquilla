"""On-demand download of the pinned local SenseVoice model assets.

Mirrors the proven DSH speech-to-text preparation design: every file is pinned
to one upstream revision with an exact byte size and SHA-256, downloads race a
set of Hugging Face-compatible origins (official + mirror) with a short HEAD
probe, stream into a unique ``.part`` file under a size/hash guard, and publish
atomically only after verification. Already-verified files are reused, so a
retry or a second profile never re-downloads a good cache.

Only the two files the sherpa-onnx provider needs are managed here (the int8
weights and the token table); no VAD or archive machinery is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

# One pinned upstream revision (the standard multilingual SenseVoiceSmall, not
# a language-specific fine-tune). Values are the authoritative release pins.
_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
_REVISION = "2365baeacb507f821a0c8120fcee3d484dba7a07"

DEFAULT_ORIGINS: tuple[str, ...] = (
    "https://huggingface.co",
    "https://hf-mirror.com",
)
_PROBE_TIMEOUT_SECONDS = 3.0
_DOWNLOAD_TIMEOUT_SECONDS = 600.0
_CHUNK_BYTES = 1024 * 256


@dataclass(frozen=True)
class SenseVoiceAsset:
    name: str
    repo_path: str
    bytes: int
    sha256: str

    def url(self, origin: str) -> str:
        return f"{origin.rstrip('/')}/{self.repo_path}"


MODEL_INT8 = SenseVoiceAsset(
    name="model.int8.onnx",
    repo_path=f"{_REPO}/resolve/{_REVISION}/model.int8.onnx",
    bytes=239233841,
    sha256="c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
)
TOKENS = SenseVoiceAsset(
    name="tokens.txt",
    repo_path=f"{_REPO}/resolve/{_REVISION}/tokens.txt",
    bytes=315894,
    sha256="f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
)
ASSETS: tuple[SenseVoiceAsset, ...] = (MODEL_INT8, TOKENS)


class SenseVoiceDownloadError(RuntimeError):
    """A preparation failure with a public, credential-free reason category."""

    def __init__(self, resource: str, source: str, reason: str, status: int | None = None) -> None:
        self.resource = resource
        self.source = source
        self.reason = reason
        self.status = status
        detail = f"{reason}" + (f" (HTTP {status})" if status is not None else "")
        super().__init__(f"Unable to prepare {resource} from {source}: {detail}")


def default_model_dir(config_path: str | None) -> Path:
    """Profile-scoped cache directory beside the active config file."""
    if config_path:
        return Path(config_path).expanduser().absolute().parent / "models" / "sensevoice-onnx"
    from opensquilla.onboarding.config_store import default_opensquilla_home

    return default_opensquilla_home() / "models" / "sensevoice-onnx"


def asset_matches(path: Path, asset: SenseVoiceAsset) -> bool:
    """True when a cached file has the pinned size and SHA-256."""
    try:
        if not path.is_file() or path.stat().st_size != asset.bytes:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
        return digest.hexdigest() == asset.sha256
    except OSError:
        return False


def model_ready(model_dir: Path) -> bool:
    return all(asset_matches(model_dir / asset.name, asset) for asset in ASSETS)


async def order_origins(
    asset: SenseVoiceAsset,
    origins: Sequence[str],
    *,
    client: httpx.AsyncClient,
    probe_timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> list[str]:
    """Prefer the first origin answering a HEAD probe; keep the rest as fallback.

    A single origin needs no probe. When every probe fails the configured order
    is preserved so the download still attempts each source in turn.
    """
    urls = list(dict.fromkeys(asset.url(origin) for origin in origins))
    if len(urls) <= 1:
        return urls

    async def probe(url: str) -> str:
        response = await client.head(url, timeout=probe_timeout, follow_redirects=True)
        if response.status_code >= 400:
            raise SenseVoiceDownloadError(asset.name, url, "http", response.status_code)
        return url

    tasks = [asyncio.create_task(probe(url)) for url in urls]
    preferred: str | None = None
    try:
        for finished in asyncio.as_completed(tasks, timeout=probe_timeout + 1.0):
            try:
                preferred = await finished
                break
            except Exception:  # noqa: BLE001 - a failed probe just loses the race
                continue
    except TimeoutError:
        preferred = None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if preferred is None:
        return urls
    return [preferred, *[url for url in urls if url != preferred]]


async def _download_from(
    asset: SenseVoiceAsset,
    url: str,
    destination: Path,
    *,
    client: httpx.AsyncClient,
    progress: Callable[[int, int], None],
    cancelled: Callable[[], bool],
) -> None:
    partial = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.part")
    source = url
    try:
        async with client.stream(
            "GET", url, timeout=_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
        ) as response:
            source = str(response.url)
            if response.status_code >= 400:
                raise SenseVoiceDownloadError(asset.name, source, "http", response.status_code)
            digest = hashlib.sha256()
            completed = 0
            progress(completed, asset.bytes)
            with partial.open("wb") as output:
                async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                    if cancelled():
                        raise SenseVoiceDownloadError(asset.name, source, "cancelled")
                    completed += len(chunk)
                    if completed > asset.bytes:
                        raise SenseVoiceDownloadError(asset.name, source, "integrity")
                    digest.update(chunk)
                    output.write(chunk)
                    progress(completed, asset.bytes)
                output.flush()
                os.fsync(output.fileno())
        if completed != asset.bytes or digest.hexdigest() != asset.sha256:
            raise SenseVoiceDownloadError(asset.name, source, "integrity")
        if cancelled():
            raise SenseVoiceDownloadError(asset.name, source, "cancelled")
        os.replace(partial, destination)
    except SenseVoiceDownloadError:
        raise
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise SenseVoiceDownloadError(asset.name, source, "timeout") from exc
    except OSError as exc:
        raise SenseVoiceDownloadError(asset.name, source, "storage") from exc
    except httpx.HTTPError as exc:
        raise SenseVoiceDownloadError(asset.name, source, "network") from exc
    finally:
        partial.unlink(missing_ok=True)


async def download_asset(
    asset: SenseVoiceAsset,
    model_dir: Path,
    *,
    origins: Sequence[str] = DEFAULT_ORIGINS,
    client: httpx.AsyncClient | None = None,
    progress: Callable[[int, int], None] = lambda done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """Ensure one pinned asset exists under ``model_dir``, reusing a good cache."""
    destination = model_dir / asset.name
    if asset_matches(destination, asset):
        progress(asset.bytes, asset.bytes)
        return destination
    model_dir.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(trust_env=False)
    try:
        urls = await order_origins(asset, origins, client=client)
        last: SenseVoiceDownloadError | None = None
        for index, url in enumerate(urls):
            if cancelled():
                raise SenseVoiceDownloadError(asset.name, url, "cancelled")
            try:
                await _download_from(
                    asset, url, destination, client=client, progress=progress, cancelled=cancelled,
                )
                return destination
            except SenseVoiceDownloadError as exc:
                last = exc
                # Storage/cancel/unknown failures are not origin problems; stop.
                if exc.reason in {"storage", "cancelled", "unknown"} or index == len(urls) - 1:
                    raise
        raise last or SenseVoiceDownloadError(asset.name, urls[-1], "unknown")
    finally:
        if owns_client:
            await client.aclose()


async def prepare_sensevoice_model(
    model_dir: Path,
    *,
    origins: Sequence[str] = DEFAULT_ORIGINS,
    progress: Callable[[str, int, int], None] = lambda resource, done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """Download every missing asset and return the verified model directory."""
    async with httpx.AsyncClient(trust_env=False) as client:
        for asset in ASSETS:
            await download_asset(
                asset,
                model_dir,
                origins=origins,
                client=client,
                progress=lambda done, total, name=asset.name: progress(name, done, total),
                cancelled=cancelled,
            )
    if not model_ready(model_dir):
        raise SenseVoiceDownloadError("sensevoice", str(model_dir), "integrity")
    return model_dir


__all__ = [
    "ASSETS",
    "DEFAULT_ORIGINS",
    "MODEL_INT8",
    "TOKENS",
    "SenseVoiceAsset",
    "SenseVoiceDownloadError",
    "asset_matches",
    "default_model_dir",
    "download_asset",
    "model_ready",
    "order_origins",
    "prepare_sensevoice_model",
]
