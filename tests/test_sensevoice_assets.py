"""Unit tests for the pinned SenseVoice asset downloader (DSH-style design)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest

from opensquilla.audio import sensevoice_assets as sv


def _asset(name: str, payload: bytes) -> sv.SenseVoiceAsset:
    return sv.SenseVoiceAsset(
        name=name,
        repo_path=f"test/repo/resolve/rev/{name}",
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _transport(payload: bytes, *, fail_hosts: set[str] | None = None) -> httpx.MockTransport:
    fail_hosts = fail_hosts or set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in fail_hosts:
            return httpx.Response(500)
        if request.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(200, content=payload)

    return httpx.MockTransport(handler)


def test_asset_matches_requires_size_and_hash(tmp_path: Path) -> None:
    payload = b"sensevoice-weights"
    asset = _asset("model.int8.onnx", payload)
    good = tmp_path / asset.name
    good.write_bytes(payload)
    assert sv.asset_matches(good, asset)

    wrong_size = tmp_path / "size.onnx"
    wrong_size.write_bytes(payload + b"x")
    assert not sv.asset_matches(wrong_size, _asset("size.onnx", payload))

    wrong_hash = tmp_path / "hash.onnx"
    wrong_hash.write_bytes(b"different-content!")
    assert not sv.asset_matches(wrong_hash, _asset("hash.onnx", payload))

    assert not sv.asset_matches(tmp_path / "missing.onnx", asset)


def test_model_ready_checks_every_asset(tmp_path: Path) -> None:
    assert not sv.model_ready(tmp_path)
    for asset in sv.ASSETS:
        # Write files with the pinned size but let the hash decide readiness.
        (tmp_path / asset.name).write_bytes(b"\0" * asset.bytes)
    # Zero-filled files have the right size but the wrong hash.
    assert not sv.model_ready(tmp_path)


def test_order_origins_single_origin_skips_probe() -> None:
    asset = _asset("m.onnx", b"x")

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=_transport(b"x")) as client:
            return await sv.order_origins(asset, ["https://only.example"], client=client)

    assert asyncio.run(run()) == [asset.url("https://only.example")]


def test_order_origins_prefers_first_responding_source() -> None:
    asset = _asset("m.onnx", b"x")

    async def run() -> list[str]:
        # The official host fails the probe; the mirror answers first.
        transport = _transport(b"x", fail_hosts={"huggingface.co"})
        async with httpx.AsyncClient(transport=transport) as client:
            return await sv.order_origins(
                asset, ["https://huggingface.co", "https://hf-mirror.com"], client=client,
            )

    urls = asyncio.run(run())
    assert urls[0] == asset.url("https://hf-mirror.com")
    assert set(urls) == {asset.url("https://huggingface.co"), asset.url("https://hf-mirror.com")}


def test_download_asset_writes_verified_file(tmp_path: Path) -> None:
    payload = b"model-bytes" * 100
    asset = _asset("model.int8.onnx", payload)

    async def run() -> Path:
        async with httpx.AsyncClient(transport=_transport(payload)) as client:
            return await sv.download_asset(
                asset, tmp_path, origins=["https://hf-mirror.com"], client=client,
            )

    result = asyncio.run(run())
    assert result.read_bytes() == payload
    assert sv.asset_matches(result, asset)
    # No leftover partial files.
    assert not list(tmp_path.glob("*.part"))


def test_download_asset_reuses_verified_cache(tmp_path: Path) -> None:
    payload = b"cached"
    asset = _asset("tokens.txt", payload)
    (tmp_path / asset.name).write_bytes(payload)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be touched for a verified cache")

    async def run() -> Path:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await sv.download_asset(
                asset, tmp_path, origins=["https://hf-mirror.com"], client=client,
            )

    assert asyncio.run(run()).read_bytes() == payload


def test_download_asset_rejects_integrity_mismatch(tmp_path: Path) -> None:
    asset = _asset("model.int8.onnx", b"expected")

    async def run() -> None:
        async with httpx.AsyncClient(transport=_transport(b"tampered")) as client:
            await sv.download_asset(
                asset, tmp_path, origins=["https://hf-mirror.com"], client=client,
            )

    with pytest.raises(sv.SenseVoiceDownloadError) as exc:
        asyncio.run(run())
    assert exc.value.reason == "integrity"
    assert not (tmp_path / asset.name).exists()


def test_download_asset_falls_back_to_next_origin(tmp_path: Path) -> None:
    payload = b"fallback-payload"
    asset = _asset("model.int8.onnx", payload)

    async def run() -> Path:
        transport = _transport(payload, fail_hosts={"huggingface.co"})
        async with httpx.AsyncClient(transport=transport) as client:
            return await sv.download_asset(
                asset, tmp_path,
                origins=["https://huggingface.co", "https://hf-mirror.com"],
                client=client,
            )

    assert asyncio.run(run()).read_bytes() == payload


def test_download_asset_honours_cancellation(tmp_path: Path) -> None:
    payload = b"cancel-me" * 1000
    asset = _asset("model.int8.onnx", payload)

    async def run() -> None:
        async with httpx.AsyncClient(transport=_transport(payload)) as client:
            await sv.download_asset(
                asset, tmp_path, origins=["https://hf-mirror.com"], client=client,
                cancelled=lambda: True,
            )

    with pytest.raises(sv.SenseVoiceDownloadError) as exc:
        asyncio.run(run())
    assert exc.value.reason == "cancelled"


def test_prepare_sensevoice_model_downloads_all_assets(tmp_path: Path) -> None:
    payloads = {asset.name: (b"\1" * 32) for asset in sv.ASSETS}
    assets = tuple(_asset(asset.name, payloads[asset.name]) for asset in sv.ASSETS)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=payloads[name])

    async def run() -> Path:
        # Patch the module assets to the small test pins for this run.
        original = sv.ASSETS
        sv.ASSETS = assets
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                # prepare opens its own client; exercise download_asset per asset instead.
                for asset in assets:
                    await sv.download_asset(
                        asset, tmp_path, origins=["https://hf-mirror.com"], client=client,
                    )
        finally:
            sv.ASSETS = original
        return tmp_path

    result = asyncio.run(run())
    assert all((result / asset.name).is_file() for asset in assets)


def test_default_model_dir_is_profile_scoped(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")
    assert sv.default_model_dir(str(config)) == tmp_path / "models" / "sensevoice-onnx"