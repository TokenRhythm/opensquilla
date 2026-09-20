"""Check production collectors against an immutable release source before publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MANIFEST_PATH = "src/opensquilla/telemetry/contracts/protocol-manifest.v1.json"
CLIENT_PATHS = ("src/opensquilla/telemetry", "desktop/electron/src/telemetry")
HEALTH_URLS = {
    "reliability": "https://telemetry.opensquilla.ai/v1/reliability/healthz",
    "growth": "https://telemetry.opensquilla.ai/v1/growth/healthz",
}
MAX_RESPONSE_BYTES = 4096
REQUEST_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3

# A reviewed, one-way compatibility pair: the newer server accepts events
# without the optional device field. Unknown protocol differences fail closed.
COMPATIBLE_PAIRS = frozenset(
    {
        (
            "c05f4afd7bea0c9a3f110698aa2209994348479b45f105f9f80af2b4a2175d18",
            "9e5d0501e6614fdcd4cf78f8a177db94b739fad156a0409f330809e5b2a5719f",
        )
    }
)


def strict_json(raw: bytes) -> object:
    def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError("Invalid JSON constant")

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=invalid_constant
        )
    except (ValueError, RecursionError) as exc:
        raise ValueError("Invalid protocol JSON") from exc


def manifest_fingerprint(raw: bytes) -> str:
    manifest = strict_json(raw)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("manifest_version")) is not int
        or manifest["manifest_version"] not in {1, 2}
        or type(manifest.get("batch_version")) is not int
        or manifest["batch_version"] != 1
        or not isinstance(manifest.get("events"), list)
        or not manifest["events"]
    ):
        raise ValueError("Unsupported release protocol manifest")
    encoded = json.dumps(
        manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def git(source_repo: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(source_repo), *args], stderr=subprocess.PIPE, timeout=20
    )


def source_fingerprint(source_repo: Path, source_sha: str) -> str | None:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Release source must be a full commit SHA")
    resolved = git(source_repo, "rev-parse", "--verify", f"{source_sha}^{{commit}}")
    if resolved.decode("ascii").strip() != source_sha:
        raise ValueError("Release source must identify a commit directly")
    paths = git(source_repo, "ls-tree", "-r", "--name-only", source_sha, *CLIENT_PATHS)
    names = paths.decode("utf-8").splitlines()
    if MANIFEST_PATH not in names:
        if names:
            raise ValueError("Release client protocol manifest is missing")
        return None
    return manifest_fingerprint(git(source_repo, "show", f"{source_sha}:{MANIFEST_PATH}"))


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        raise ValueError("Collector health redirects are forbidden")


def read_health(url: str) -> object:
    request = Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    with build_opener(RejectRedirects()).open(
        request, timeout=REQUEST_TIMEOUT_SECONDS
    ) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("Unexpected collector health response")
        if response.headers.get_content_type() != "application/json":
            raise ValueError("Collector health response is not JSON")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Collector health response exceeds the size limit")
    return strict_json(raw)


def validate_health(payload: object, scope: str, fingerprint: str) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"ok", "scope", "schema_version", "protocol_fingerprint"}
        or payload["ok"] is not True
        or payload["scope"] != scope
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload["protocol_fingerprint"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["protocol_fingerprint"])
    ):
        raise ValueError(f"{scope}: invalid collector health contract")
    server = payload["protocol_fingerprint"]
    if server != fingerprint and (server, fingerprint) not in COMPATIBLE_PAIRS:
        raise ValueError(f"{scope}: collector protocol does not support this release")


def check_collectors(fingerprint: str) -> None:
    for scope, url in HEALTH_URLS.items():
        for attempt in range(MAX_ATTEMPTS):
            try:
                payload = read_health(url)
            except HTTPError as exc:
                exc.close()
                if exc.code != 429 and not 500 <= exc.code <= 599:
                    raise ValueError(f"{scope}: collector health request rejected") from exc
            except (URLError, TimeoutError, OSError):
                pass
            else:
                validate_health(payload, scope, fingerprint)
                print(f"{scope}: release protocol accepted")
                break
            if attempt == MAX_ATTEMPTS - 1:
                raise ValueError(f"{scope}: collector health unavailable after bounded retries")
            time.sleep(attempt + 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-repo", type=Path, default=Path("."))
    args = parser.parse_args()
    fingerprint = source_fingerprint(args.source_repo, args.source_sha)
    if fingerprint is None:
        print(f"Release source {args.source_sha} predates the collector clients; no gate required")
        return
    print(f"Checking release source {args.source_sha}; protocol {fingerprint}")
    check_collectors(fingerprint)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError):
        # Do not print remote response bodies, subprocess output, or request details.
        print("Release protocol check failed; publication is blocked.", file=sys.stderr)
        sys.exit(1)
