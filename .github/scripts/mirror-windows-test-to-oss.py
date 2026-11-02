"""Mirror one checked Windows test artifact, without touching release channels."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def mirror(assets_dir: Path) -> None:
    required = (
        "ALIYUN_OSS_BUCKET",
        "OSS_REGION",
        "OSS_ACCESS_KEY_ID",
        "OSS_ACCESS_KEY_SECRET",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
    )
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ValueError("Missing OSS test mirror configuration: " + ", ".join(missing))
    bucket = os.environ["ALIYUN_OSS_BUCKET"]
    region = os.environ["OSS_REGION"]
    prefix = os.environ.get("ALIYUN_OSS_PREFIX", "").strip("/") or "releases"
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Invalid OSS bucket name")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", region):
        raise ValueError("Invalid OSS region")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", prefix) or any(
        part in {"", ".", ".."} for part in prefix.split("/")
    ):
        raise ValueError("Unsafe OSS prefix")
    if not run_id.isdecimal() or not attempt.isdecimal():
        raise ValueError("GitHub run ID and attempt must be numeric")

    installers = list(assets_dir.glob("OpenSquilla-*-test-*-win-x64.exe"))
    if len(installers) != 1:
        raise ValueError("Expected exactly one Windows test installer")
    installer = installers[0]
    if not re.fullmatch(
        r"OpenSquilla-[0-9A-Za-z.+-]+-test-(?:production-)?[0-9]{8}-[0-9]{6}-win-x64[.]exe",
        installer.name,
    ):
        raise ValueError("Unsafe Windows test installer filename")
    checksum = installer.with_suffix(".exe.sha256")
    files = (installer, checksum)
    if set(assets_dir.iterdir()) != set(files) or any(
        file.is_symlink() or not file.is_file() for file in files
    ):
        raise ValueError("Expected only the installer and its SHA256 file")
    with installer.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    expected_checksum = f"{digest}  {installer.name}"
    if checksum.read_text(encoding="utf-8-sig").strip() != expected_checksum:
        raise ValueError("Windows test installer SHA256 mismatch")

    endpoint = os.environ.get("OSS_ENDPOINT", "")
    endpoint_host = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}").hostname
    style = os.environ.get("OSS_ADDRESSING_STYLE") or (
        "cname" if endpoint_host and not endpoint_host.endswith(".aliyuncs.com") else "virtual"
    )
    if style not in {"virtual", "path", "cname"}:
        raise ValueError("Invalid OSS addressing style")
    command = ["ossutil", "--addressing-style", style]
    test_prefix = f"{prefix}/test/windows/"
    key_prefix = f"{test_prefix}{run_id}-{attempt}/"
    destination = f"oss://{bucket}/{key_prefix}"
    public_root = f"https://{bucket}.oss-{region}.aliyuncs.com/{key_prefix}"
    for file in files:
        # ossutil validates transfer CRC64 by default; avoid a second large download.
        subprocess.run(
            [
                *command,
                "cp",
                "--force",
                "--parallel",
                "4",
                "--part-size",
                "8388608",
                "--cache-control",
                "public,max-age=86400",
                str(file),
                destination + file.name,
            ],
            check=True,
        )

    # Check anonymous access as well as upload success before advertising the links.
    with urlopen(Request(public_root + installer.name, method="HEAD"), timeout=30) as response:
        if int(response.headers.get("Content-Length", "-1")) != installer.stat().st_size:
            raise ValueError("Public OSS installer size mismatch")
    with urlopen(public_root + checksum.name, timeout=30) as response:
        if response.read(4096).decode("utf-8-sig").strip() != expected_checksum:
            raise ValueError("Public OSS SHA256 file mismatch")
    environment = "生产环境" if "-test-production-" in installer.name else "UAT"
    summary = (
        f"\n### Windows {environment} 测试包国内下载\n\n"
        f"- [下载安装包]({public_root}{installer.name})\n"
        f"- [SHA256 校验文件]({public_root}{checksum.name})\n"
        f"- SHA256: `{digest}`\n"
        f"- 构建提交：`{os.environ.get('GITHUB_SHA', '')}`；构建：`{run_id}-{attempt}`\n"
        "- 测试包会在后续成功同步时清理，请及时下载。\n"
    )
    print(summary)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as output:
            output.write(summary)

    # Only prune this dedicated test prefix, never formal releases or update manifests.
    try:
        subprocess.run(
            [
                *command,
                "rm",
                f"oss://{bucket}/{test_prefix}",
                "--recursive",
                "--force",
                "--min-age",
                "14d",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        warning = (
            "Old OSS test installer cleanup failed; downloads are available, "
            "but storage may accumulate."
        )
        print(f"::warning::{warning}")
        if summary_path:
            with Path(summary_path).open("a", encoding="utf-8") as output:
                output.write(f"\n⚠️ {warning}\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: mirror-windows-test-to-oss.py ASSETS_DIR")
    try:
        mirror(Path(sys.argv[1]))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
