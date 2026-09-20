"""Run the unmodified user case corpus through isolated real-provider Gateways.

Opt-in only. Credentials are read from an existing config, held by the local
relay, and never written into test profiles or passed to the agent process.
Reports distinguish transport/delivery evidence from product-content quality.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from opensquilla.gateway_client import GatewayRPCClient  # noqa: E402
from scripts.live_deliverable_acceptance import rows, snapshot, validate_bytes  # noqa: E402
from scripts.live_harness_security import child_environment, sanitize_report  # noqa: E402
from scripts.live_tokenrhythm_budget import (  # noqa: E402
    BudgetRejectedError,
    BudgetRelay,
    FunctionalRequestLog,
)
from scripts.smoke_v4_phase3_router import _free_port, _stop_gateway  # noqa: E402


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    prompt: str
    required_skill: str | None
    max_calls: int
    timeout_seconds: int


def read_cases(path: Path) -> list[Case]:
    """Extract only fenced task text; commentary in the corpus is not a prompt."""
    text = path.read_text(encoding="utf-8-sig")
    headings = list(re.finditer(r"^## Case ([A-Z]?\d+)\s*[：:]\s*(.+)$", text, re.M))
    cases = []
    seen = set()
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        blocks = re.findall(r"^```text\s*\n(.*?)^```\s*$", text[heading.end() : end], re.M | re.S)
        if len(blocks) != 1 or heading[1] in seen:
            raise ValueError("each case needs one task block and a unique id")
        seen.add(heading[1])
        prompt = blocks[0].strip()
        skill = re.search(r"必须使用\s*`([^`]+)`\s*skill", prompt)
        long = heading[1] in {"6", "7", "8", "9", "10", "11", "A5", "B3", "B4", "B5"}
        cases.append(
            Case(
                heading[1],
                heading[2],
                prompt,
                skill[1] if skill else None,
                60,
                1800 if long else 1200,
            )
        )
    if not cases:
        raise ValueError("no fenced case prompts found")
    return cases


class CaseRelay(BudgetRelay):
    def __init__(self, *, model: str, max_calls: int, request_interval: float = 0, **kwargs):
        super().__init__(None, {}, **kwargs)
        self.model = model
        self.max_calls = max_calls
        self.calls = 0
        self.request_interval = request_interval
        self.last_request_at = 0.0
        self.http_failures = []
        self._serial = threading.Lock()

    @contextlib.contextmanager
    def forward(self, body, headers=None):
        # A caller can submit the next request immediately after an SSE done
        # event, before the preceding HTTP response finishes its log transaction.
        with self._serial:
            with self._forward_serial(body, headers) as response:
                yield response

    @contextlib.contextmanager
    def _forward_serial(self, body, headers=None):
        request = json.loads(body)
        limits = [request[key] for key in ("max_tokens", "max_completion_tokens") if key in request]
        with self._lock:
            if self.calls >= self.max_calls or request.get("model") != self.model:
                raise BudgetRejectedError("case_request_limit")
            if (
                not limits
                or any(type(limit) is not int or not 1 <= limit <= 16384 for limit in limits)
                or len(set(limits)) != 1
                or type(request.get("n", 1)) is not int
                or request.get("n", 1) != 1
            ):
                raise BudgetRejectedError("case_output_limit")
            self.calls += 1
        delay = self.last_request_at + self.request_interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last_request_at = time.monotonic()
        with super().forward(body, headers) as response:
            if response.status_code >= 400:
                self.http_failures.append(
                    {
                        "status": response.status_code,
                        "retry_after": response.headers.get("retry-after"),
                    }
                )
            yield response


def load_credential(config_path: Path) -> tuple[str, str]:
    data = tomllib.loads(config_path.read_text(encoding="utf-8-sig")).get("llm", {})
    if data.get("provider") != "tokenrhythm":
        raise ValueError("this harness supports the configured TokenRhythm provider only")
    key = str(
        data.get("api_key") or os.environ.get(data.get("api_key_env", "TOKENRHYTHM_API_KEY"), "")
    )
    if not key:
        raise ValueError("configured provider credential is unavailable")
    return key, str(data.get("model") or "deepseek-v4-pro-0813")


def save(path: Path, payload, secret: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            sanitize_report(payload, secrets={"provider": secret}), ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def source_manifest(checkout: Path, run: Path, phase: str) -> dict:
    """Keep exact source provenance even while independent UI work continues."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
    ).strip()
    diff = subprocess.check_output(["git", "diff", "HEAD", "--binary"], cwd=checkout)
    (run / f"source-{phase}.patch").write_bytes(diff)
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "--modified", "--others", "--exclude-standard", "-z"], cwd=checkout
        )
        .decode("utf-8")
        .split("\0")
    )
    entries = []
    for relative in sorted(set(paths) - {""}):
        path = checkout / relative
        if path.is_file():
            data = path.read_bytes()
            copied = run / f"source-{phase}-files" / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes(data)
            entries.append(
                {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            )
    result = {
        "checkout": str(checkout),
        "revision": revision,
        "patch_sha256": hashlib.sha256(diff).hexdigest(),
        "changed_files": entries,
    }
    save(run / f"source-{phase}.json", result)
    return result


def parse_file(data: bytes, name: str) -> dict:
    suffix = Path(name).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return {"format": image.format, "width": image.width, "height": image.height}
    if suffix == ".svg":
        element = ElementTree.fromstring(data)
        if element.tag.rsplit("}", 1)[-1] != "svg":
            raise ValueError("not an SVG document")
        return {"format": "svg", "width": element.get("width"), "height": element.get("height")}
    if suffix == ".json":
        value = json.loads(data)
        facts = {"format": "json", "value_type": type(value).__name__}
        if isinstance(value, dict) and value.get("manifest_version"):
            paths = []
            for script in value.get("content_scripts", []):
                paths.extend(script.get("js", []))
                paths.extend(script.get("css", []))
            paths.extend(value.get("icons", {}).values())
            for section, field in [
                ("background", "service_worker"),
                ("action", "default_popup"),
                ("options_ui", "page"),
            ]:
                if value.get(section, {}).get(field):
                    paths.append(value[section][field])
            if value.get("options_page"):
                paths.append(value["options_page"])
            facts.update(manifest_version=value["manifest_version"], required_files=paths)
        return facts
    return validate_bytes(data, name)


def tool_errors(value) -> list[dict]:
    """Preserve nested tool failures even when the outer task says succeeded."""
    result = []
    if isinstance(value, dict):
        if (
            value.get("isError") is True
            or value.get("is_error") is True
            or (type(value.get("exit_code")) is int and value["exit_code"] != 0)
        ):
            result.append(value)
        for item in value.values():
            if isinstance(item, str) and item.strip().startswith(("{", "[")):
                with contextlib.suppress(ValueError):
                    result.extend(tool_errors(json.loads(item)))
            elif isinstance(item, (dict, list)):
                result.extend(tool_errors(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(tool_errors(item))
    return result


def pending_input(run: Path, task_id: str) -> dict | None:
    for path in sorted((run / "turn-calls").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
                payload = event.get("payload", {})
                if event.get("turn_id") != task_id or event.get("kind") != "tool_response":
                    continue
                result = payload.get("result", {})
                if isinstance(result, str):
                    result = json.loads(result)
                if (
                    isinstance(result, dict)
                    and result.get("status") == "input_required"
                    and result.get("paused")
                ):
                    return {"tool": payload.get("name"), "result": result}
            except (TypeError, ValueError):
                continue
    return None


async def abort_case(base_url: str, session_key: str, task_id: str):
    client = GatewayRPCClient()
    await client.connect(base_url.replace("http:", "ws:") + "/ws")
    try:
        return await client.call("chat.abort", {"sessionKey": session_key, "runId": task_id})
    finally:
        await client.close()


def evidence_files(current: dict) -> list[dict]:
    root = Path(current["workspace"]["root"]).resolve()
    files = []
    for item in current["files"]:
        path = (root / item["path"]).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("output escaped its workspace")
        if any(part in {"node_modules", ".git", ".venv", "__pycache__"} for part in path.parts):
            continue
        data = path.read_bytes()
        record = dict(item)
        try:
            record["parsed"] = parse_file(data, item["path"])
        except Exception as exc:
            record["parse_error"] = type(exc).__name__
        files.append(record)
    return files


def _gateway_config(model: str, case: Case) -> str:
    return f"""host = "127.0.0.1"
debug = false
llm_request_timeout_seconds = 120
agent_runtime_timeout_seconds = {case.timeout_seconds}
agent_max_iterations = {case.max_calls}
agent_max_provider_retries = 0
[auth]
mode = "none"
[control_ui]
enabled = true
[rate_limit]
enabled = false
[privacy]
disable_network_observability = true
[naming]
enabled = false
[memory]
source = "state"
[sandbox]
run_mode = "full"
[tools]
profile = "full"
deny = ["sessions_spawn", "sessions_send", "message", "cron"]
[task_runtime]
turn_hard_deadline_s = {case.timeout_seconds + 30}
[llm]
provider = "tokenrhythm"
model = {json.dumps(model)}
api_key_env = "TOKENRHYTHM_API_KEY"
base_url = "https://tokenrhythm.studio/v1"
max_tokens = 16384
thinking = "off"
[squilla_router]
enabled = false
"""


def workspace_access(client, session_key: str, files: list[dict]) -> dict:
    """Resolve real workspace files, then re-read bytes through the public API."""
    headers = {"x-opensquilla-session-key": session_key}
    records = []
    for offset in range(0, len(files), 32):
        batch = files[offset : offset + 32]
        response = client.post(
            "/api/v1/workspace-files/resolve",
            headers=headers,
            json={"paths": [item["path"] for item in batch]},
        )
        if response.status_code != 200:
            return {"resolve_http_status": response.status_code, "files": records}
        payload = response.json()
        expected = {item["path"]: item for item in batch}
        for item in payload.get("files", []):
            resource = item.get("contentUrl", "")
            if not resource.startswith("/api/v1/workspace-files/content?"):
                records.append({"path": item.get("path"), "error": "unexpected_content_url"})
                continue
            content = client.get(resource, headers=headers)
            digest = hashlib.sha256(content.content).hexdigest()
            original = expected.get(item.get("requestedPath"), {})
            records.append(
                {
                    "path": item.get("path"),
                    "requested_path": item.get("requestedPath"),
                    "content_url": resource,
                    "kind": item.get("kind"),
                    "http_status": content.status_code,
                    "sha256": digest,
                    "bytes": len(content.content),
                    "matches_workspace": content.status_code == 200
                    and digest == original.get("sha256"),
                }
            )
    return {"resolve_http_status": 200, "files": records}


def final_answer(history: dict) -> str:
    assistants = [item for item in history.get("messages", []) if item.get("role") == "assistant"]
    if not assistants:
        return ""
    message = assistants[-1]
    answers = [
        part.get("text", "")
        for part in message.get("tool_calls", [])
        if part.get("type") == "text" and part.get("presentation") == "answer"
    ]
    return "\n".join(answers) if answers else str(message.get("text") or "")


def final_file_candidates(text: str) -> list[str]:
    text = re.sub(r"^```.*?^```\s*$", "", text, flags=re.M | re.S)
    values = re.findall(r"`([^`\r\n]+)`", text)
    values += re.findall(r"\[[^\]\r\n]+\]\(([^)\r\n]+)\)", text)
    return list(
        dict.fromkeys(
            value
            for value in values
            if re.search(r"\.[A-Za-z0-9]{1,16}$", value) and not re.match(r"https?://", value)
        )
    )


def required_file_groups(case_id: str) -> list[set[str]]:
    if case_id in {"5", "9", "B3"}:
        groups = [{".png", ".jpg", ".jpeg", ".svg", ".webp"}]
        if case_id in {"9", "B3"}:
            groups += [{".json"}, {".md"}]
        return groups
    if case_id in {"6", "8", "10", "A5", "B4"}:
        return [{".pptx"}] + ([{".md"}] if case_id in {"A5", "B4"} else [])
    if case_id in {"3", "A3"}:
        return [{".json"}, {".js"}, {".css"}]
    if case_id in {"11", "B5"}:
        return [{".md"}, {".json"}]
    if case_id == "7":
        return [{".py"}]
    return [{".html", ".vue", ".tsx", ".jsx"}]


def review_delivery(case_id: str, files: list[dict], access: dict, downloads: list[dict]) -> dict:
    generated = {Path(item["path"]).suffix.lower() for item in files if not item.get("parse_error")}
    delivered = {
        Path(item.get("path", "")).suffix.lower()
        for item in access.get("files", [])
        if item.get("matches_workspace")
    }
    delivered.update(
        Path(item["name"]).suffix.lower()
        for item in downloads
        if item.get("matches_artifact") and not item.get("parse_error")
    )
    required = required_file_groups(case_id)
    result = {
        "file_types_status": "present"
        if all(group & generated for group in required)
        else "missing_required_output",
        "delivery_status": "required_types_accessible"
        if all(group & delivered for group in required)
        else "missing_required_delivery",
        "required_type_groups": [sorted(group) for group in required],
        "content_status": "requires_case_specific_review",
    }
    if case_id in {"3", "A3"}:
        readable_paths = {
            item["path"].replace("\\", "/")
            for item in access.get("files", [])
            if item.get("matches_workspace")
        }
        published_hashes = {item["sha256"] for item in downloads if item.get("matches_artifact")}
        readable_paths.update(
            item["path"].replace("\\", "/")
            for item in files
            if item.get("sha256") in published_hashes
        )
        required_paths = set()
        for item in files:
            if Path(item["path"]).name != "manifest.json":
                continue
            name = item["path"].replace("\\", "/")
            required_paths.add(name)
            prefix = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
            required_paths.update(
                prefix + value for value in item.get("parsed", {}).get("required_files", [])
            )
        missing = sorted(required_paths - readable_paths)
        result["unavailable_extension_files"] = missing
        if missing or not required_paths:
            result["delivery_status"] = "incomplete_extension_delivery"
    return result


def final_reference_files(candidates: list[str], files: list[dict], root: str) -> list[dict]:
    """Use exact paths only; never guess that a bare name names a nested file."""
    lookup = {item["path"].replace("\\", "/"): item for item in files}
    normalized_root = root.replace("\\", "/").rstrip("/") + "/"
    result = []
    for candidate in candidates:
        relative = candidate.replace("\\", "/")
        if relative.startswith(normalized_root):
            relative = relative[len(normalized_root) :]
        original = lookup.get(relative.removeprefix("./"), {})
        result.append({"path": candidate, "sha256": original.get("sha256", "")})
    return result


def run_case(
    case: Case, run: Path, key: str, model: str, checkout: Path = ROOT, request_interval: float = 0
) -> dict:
    run.mkdir(parents=True, exist_ok=False)
    result = {**asdict(case), "status": "running", "content_status": "not_evaluated"}
    result["source_start"] = source_manifest(checkout, run, "start")
    save(run / "result.json", result, key)
    (run / "prompt.txt").write_text(case.prompt, encoding="utf-8")
    log = FunctionalRequestLog(run / "requests.sqlite3", enabled=True)
    log.select_phase(variant="new", case_id=case.id)
    relay = CaseRelay(
        model=model,
        max_calls=case.max_calls,
        api_key=key,
        request_log=log,
        request_interval=request_interval,
    )
    relay_url = relay.start()
    process = None
    started = time.monotonic()
    try:
        profile = run / "profile"
        profile.mkdir()
        injection = run / "transport"
        injection.mkdir()
        (injection / "live_workspace_file_transport.py").write_bytes(
            (ROOT / "scripts/live_workspace_file_transport.py").read_bytes()
        )
        (injection / "sitecustomize.py").write_text(
            "try:\n from live_workspace_file_transport import install_from_env\n"
            " install_from_env()\nexcept BaseException:\n"
            " raise SystemExit('acceptance transport unavailable')\n",
            encoding="utf-8",
        )
        config = run / "gateway.toml"
        config.write_text(_gateway_config(model, case), encoding="utf-8")
        env = child_environment("tokenrhythm", {"TOKENRHYTHM_API_KEY": relay.client_key})
        env.update(
            {
                "PYTHONPATH": os.pathsep.join(map(str, [injection, checkout / "src", checkout])),
                "PATH": str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", os.defpath),
                "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(config),
                "OPENSQUILLA_STATE_DIR": str(profile),
                "OPENSQUILLA_USER_STATE_DIR": str(run / "user-state"),
                "OPENSQUILLA_TEST_PROFILE_LOCK_ROOT": "1",
                "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
                "OPENSQUILLA_TURN_CALL_LOG": "1",
                "OPENSQUILLA_TURN_CALL_LOG_DIR": str(run / "turn-calls"),
                "OPENSQUILLA_LIVE_TRANSPORT": "1",
                "OPENSQUILLA_LIVE_RELAY_URL": relay_url,
                "OPENSQUILLA_LIVE_RELAY_CLIENT_KEY": relay.client_key,
                "USERPROFILE": str(run / "user-profile"),
            }
        )
        port = _free_port()
        with (run / "gateway.log").open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "opensquilla.cli.main",
                    "gateway",
                    "run",
                    "--port",
                    str(port),
                    "--bind",
                    "127.0.0.1",
                ],
                cwd=run,
                env=env,
                stdout=output,
                stderr=output,
            )
        base_url = f"http://127.0.0.1:{port}"
        result["gateway_url"] = base_url
        result["session_key"] = session_key = "agent:main:webchat:case" + uuid.uuid4().hex[:12]
        with httpx.Client(base_url=base_url, timeout=30, trust_env=False) as client:
            for _ in range(120):
                try:
                    if client.get("/api/system/status").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("isolated gateway exited during startup")
                time.sleep(0.5)
            else:
                raise RuntimeError("isolated gateway startup timeout")
            response = client.post(
                "/api/chat",
                json={
                    "sessionKey": session_key,
                    "message": case.prompt,
                    "intent": "new_chat",
                    "clientRequestId": uuid.uuid4().hex,
                },
            )
            response.raise_for_status()
            result["admission"] = response.json()
            task_id = result["admission"].get("taskId") or result["admission"].get("task_id")
            if not task_id:
                raise RuntimeError("admission returned no task id")
            save(run / "result.json", result, key)
            print(
                json.dumps({"case": case.id, "status": "accepted", "gateway": base_url}), flush=True
            )
            db = profile / "state" / "sessions.db"
            deadline = time.monotonic() + case.timeout_seconds + 60
            while time.monotonic() < deadline:
                paused = pending_input(run, task_id)
                if paused and "pending_input" not in result:
                    result["pending_input"] = paused
                    result["cancel_receipt"] = asyncio.run(
                        abort_case(base_url, session_key, task_id)
                    )
                tasks = rows(
                    db,
                    "SELECT task_id,status,terminal_reason,error_class FROM agent_tasks "
                    "WHERE task_id=? AND session_key=?",
                    (task_id, session_key),
                )
                if tasks and tasks[0]["status"] in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "timeout",
                    "abandoned",
                }:
                    result["task"] = tasks[0]
                    break
                time.sleep(1)
            else:
                result["task"] = {"status": "harness_timeout"}
            history = client.get(
                "/api/chat/history", params={"sessionKey": session_key, "limit": 500}
            ).json()
            save(run / "history.json", history, key)
            result["inner_errors"] = tool_errors(history)
            result["final_answer"] = final_answer(history)
            result["final_file_candidates"] = final_file_candidates(result["final_answer"])
            current = snapshot(db, session_key)
            save(run / "snapshot.json", current, key)
            result["files"] = evidence_files(current) if current["workspace"] else []
            result["workspace_access"] = workspace_access(client, session_key, result["files"])
            result["downloads"] = []
            for artifact in current["artifacts"]:
                response = client.get(
                    "/api/v1/artifacts/" + artifact["id"], params={"sessionKey": session_key}
                )
                item = {
                    "id": artifact["id"],
                    "name": artifact["name"],
                    "http_status": response.status_code,
                }
                if response.status_code == 200:
                    item.update(
                        bytes=len(response.content),
                        sha256=hashlib.sha256(response.content).hexdigest(),
                    )
                    item["matches_artifact"] = (
                        item["sha256"] == artifact["sha256"] and item["bytes"] == artifact["size"]
                    )
                    destination = run / "downloads" / artifact["id"] / Path(artifact["name"]).name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(response.content)
                    item["saved_to"] = str(destination)
                    try:
                        item["parsed"] = parse_file(response.content, artifact["name"])
                    except Exception as exc:
                        item["parse_error"] = type(exc).__name__
                result["downloads"].append(item)
            result["working_previews"] = []
            for source in current["sources"]:
                response = client.get(
                    f"/api/v1/artifact-documents/{source['document_id']}/working-file",
                    headers={"x-opensquilla-session-key": session_key},
                )
                result["working_previews"].append(
                    {
                        "document_id": source["document_id"],
                        "http_status": response.status_code,
                        "bytes": len(response.content),
                        "sha256": hashlib.sha256(response.content).hexdigest(),
                    }
                )
            result["status"] = "completed" if result["task"]["status"] == "succeeded" else "failed"
            if result.get("pending_input"):
                result["status"] = "needs_user_input"
            final_files = final_reference_files(
                result["final_file_candidates"],
                result["files"],
                (current.get("workspace") or {}).get("root", ""),
            )
            result["final_reference_access"] = workspace_access(client, session_key, final_files)
            result.update(
                review_delivery(
                    case.id, result["files"], result["final_reference_access"], result["downloads"]
                )
            )
    except Exception as exc:
        result.update(status="harness_error", error_type=type(exc).__name__, error=str(exc))
    finally:
        if process is not None:
            _stop_gateway(process)
        relay.close()
        result["provider_calls"] = relay.calls
        result["provider_http_failures"] = relay.http_failures
        result["elapsed_seconds"] = round(time.monotonic() - started, 1)
        result["source_end"] = source_manifest(checkout, run, "end")
        save(run / "result.json", result, key)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-file", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--credential-config", type=Path)
    parser.add_argument("--checkout", type=Path, default=ROOT)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--request-interval", type=float, default=6)
    parser.add_argument("--model")
    parser.add_argument("--cases", nargs="*")
    parser.add_argument("--enable-live", action="store_true")
    args = parser.parse_args(argv)
    if not math.isfinite(args.request_interval) or not 0 <= args.request_interval <= 60:
        parser.error("--request-interval must be finite and between 0 and 60 seconds")
    checkout = args.checkout.resolve(strict=True)
    if not (checkout / "src/opensquilla").is_dir():
        parser.error("--checkout must identify the repository to run")
    cases = read_cases(args.case_file)
    selected = args.cases or [case.id for case in cases]
    by_id = {case.id: case for case in cases}
    if set(selected) - by_id.keys():
        parser.error("unknown case id")
    run = args.run_root.resolve()
    report = {
        "source_checkout": str(checkout),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
        ).strip(),
        "case_file_sha256": hashlib.sha256(args.case_file.read_bytes()).hexdigest(),
        "cases": [
            {**asdict(case), "status": "queued" if case.id in selected else "not_selected"}
            for case in cases
        ],
        "limitations": [
            "Live generation and file access do not prove all app interactions or factual claims."
        ],
    }
    save(run / "corpus.json", report)
    if not args.enable_live:
        print(json.dumps({"cases": len(cases), "selected": selected, "status": "inventory_only"}))
        return 0
    if args.credential_config is None:
        parser.error("--enable-live requires --credential-config")
    key, configured_model = load_credential(args.credential_config)
    model = args.model or configured_model
    report["model"] = model

    def execute(case_id):
        case = by_id[case_id]
        target = run / ("case-" + case.id)
        if (target / "result.json").exists():
            return json.loads((target / "result.json").read_text(encoding="utf-8"))
        missing = (
            case.required_skill
            and not (
                checkout / "src/opensquilla/skills/bundled" / case.required_skill / "SKILL.md"
            ).is_file()
        )
        result = run_case(case, target, key, model, checkout, args.request_interval)
        if missing:
            result["prerequisite_status"] = "exact_required_skill_unavailable"
            result["content_status"] = "prerequisite_blocked"
            save(target / "result.json", result, key)
        return result

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(execute, case_id): case_id for case_id in selected}
        for future in as_completed(futures):
            case_id = futures[future]
            result = future.result()
            report["cases"] = [result if row["id"] == case_id else row for row in report["cases"]]
            save(run / "corpus.json", report, key)
            print(
                json.dumps(
                    {
                        "case": case_id,
                        "status": result["status"],
                        "provider_calls": result.get("provider_calls", 0),
                    }
                ),
                flush=True,
            )
    return int(any(row["status"] not in {"completed", "not_selected"} for row in report["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
