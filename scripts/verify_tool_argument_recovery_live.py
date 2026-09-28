"""Inject a completed bad call, then let a real API correct it in the same Agent.

Run explicitly with --config <existing config.toml> --output <NEW directory>.
Credentials stay in memory. Only sandboxed test tools and fresh test files are
exposed; no Gateway, existing session, or user's workspace is modified.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import time
import tomllib
from pathlib import Path


def sse(deltas, *, finish="tool_calls"):
    chunks = [{"choices": [{"delta": delta, "finish_reason": None}]} for delta in deltas]
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    return b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks) + (
        b"data: [DONE]\n\n"
    )


def native_call(arguments, call_id="injected-bad"):
    return {"tool_calls": [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": "write_file", "arguments": arguments},
    }]}


async def scenario(llm, model, output, case):
    import httpx

    from opensquilla.engine import Agent, AgentConfig
    from opensquilla.provider import ToolDefinition, ToolInputSchema
    from opensquilla.provider.openai import OpenAIProvider
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import ToolRegistry
    from opensquilla.tools.types import ToolContext, ToolSpec

    case_dir = output / case
    case_dir.mkdir()
    effects = []
    real_calls = []
    injected_calls = []
    call_index = 0
    started = time.monotonic()
    registry = ToolRegistry()

    async def write_file(path: str, content: str):
        # Only these two disposable fixtures can be touched, once each.
        if path not in {"callback.html", "prior.txt"}:
            raise ValueError("Only callback.html and prior.txt are writable in this test")
        target = case_dir / path
        if target.exists():
            raise AssertionError("Duplicate write attempted")
        target.write_text(content, encoding="utf-8")
        effects.append({"path": path, "chars": len(content)})
        return json.dumps({"status": "written", "path": path, "chars": len(content)})

    parameters = {"path": {"type": "string"}, "content": {"type": "string"}}
    spec = ToolSpec(
        name="write_file", description="Write a NEW file exactly once.",
        parameters=parameters, required=["path", "content"],
    )
    registry.register(spec, write_file)
    context = ToolContext(is_owner=True, workspace_dir=str(case_dir))
    synthetic = []
    if case == "prior_success":
        synthetic.append(sse([native_call(
            json.dumps({"path": "prior.txt", "content": "previous operation completed"}),
            "injected-prior",
        )]))
    deltas = []
    if case in {"native_text", "prior_success"}:
        deltas.append({"content": "我来生成回调演示页面。\n"})
    if case == "native_reasoning":
        deltas.append({"reasoning_content": "Prepare the callback demonstration file."})
    if case == "text_schema":
        deltas.append({"content": (
            '<｜DSML｜tool_calls><｜DSML｜invoke name="write_file">'
            '<｜DSML｜parameter name="content" string="true">missing required path'
            '</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>'
        )})
        synthetic.append(sse(deltas, finish="stop"))
    else:
        deltas.append(native_call('{"path":"callback.html","content":"unterminated'))
        synthetic.append(sse(deltas))

    class InjectThenLive(httpx.AsyncBaseTransport):
        def __init__(self):
            self.real = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            nonlocal call_index
            payload = json.loads(request.content)
            index = call_index
            call_index += 1
            if index < len(synthetic):
                injected_calls.append({"index": index, "type": "synthetic_completed_response"})
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"},
                    content=synthetic[index], request=request,
                )
            if len(real_calls) >= 4:
                raise AssertionError("Real API call budget exhausted")
            if not real_calls:
                assert "[Runtime tool argument feedback]" in str(payload["messages"])
                assert "No tools in THIS batch ran" in str(payload["messages"])
                assert not (case_dir / "callback.html").exists()
                if case == "prior_success":
                    assert [e["path"] for e in effects] == ["prior.txt"]
                    assert "written" in str(payload["messages"])
            real_calls.append({
                "index": index, "model": payload["model"],
                "feedback_present": "[Runtime tool argument feedback]" in str(payload["messages"]),
            })
            print(json.dumps({"case": case, "real_api_request": len(real_calls)}), flush=True)
            return await self.real.handle_async_request(request)

        async def aclose(self):
            await self.real.aclose()

    real_client = httpx.AsyncClient

    def instrumented_client(*args, **kwargs):
        kwargs["transport"] = InjectThenLive()
        return real_client(*args, **kwargs)

    httpx.AsyncClient = instrumented_client
    try:
        provider = OpenAIProvider(
            api_key=llm["api_key"], model=model, base_url=llm["base_url"],
            provider_kind=llm["provider"], proxy="",
        )
        agent = Agent(
            provider=provider,
            config=AgentConfig(
                max_iterations=5, max_provider_retries=0, max_turn_llm_calls=7,
                max_tokens=8192, timeout=180, request_timeout=120,
                model_id=model, provider_id=llm["provider"],
                retry_base_backoff_ms=0, retry_max_backoff_ms=0,
                system_prompt=(
                    "Complete the requested files using write_file. After successful writes, "
                    "answer briefly. Never rewrite a file whose tool result already says written."
                ),
            ),
            tool_definitions=[ToolDefinition(
                name=spec.name, description=spec.description,
                input_schema=ToolInputSchema(properties=parameters, required=spec.required),
            )],
            tool_handler=build_tool_handler(registry, context), tool_context=context,
        )
        prompt = (
            "用 write_file 创建 callback.html：一个简短但完整的中文离线 HTML 页面，"
            "说明后端 callback，有同步调用与 setTimeout 回调按钮和状态区。"
            "不依赖外部资源，内容不超过3000字符。只生成文件，再简短报告完成。"
        )
        if case == "prior_success":
            prompt = "先创建 prior.txt，内容 previous operation completed，再完成：" + prompt
        events = [event async for event in agent.run_turn(prompt)]
    finally:
        httpx.AsyncClient = real_client

    errors = [event.code for event in events if event.kind == "error"]
    tool_errors = [event.tool_use_id for event in events if (
        event.kind == "tool_result" and event.is_error
    )]
    artifact = case_dir / "callback.html"
    content = artifact.read_text(encoding="utf-8") if artifact.exists() else ""
    done = [event for event in events if event.kind == "done"]
    expected_paths = (
        ["prior.txt", "callback.html"] if case == "prior_success" else ["callback.html"]
    )
    result = {
        "case": case, "model": model, "synthetic_responses": injected_calls,
        "real_api_requests": real_calls, "effects": effects,
        "errors": errors, "tool_errors": tool_errors,
        "artifact": str(artifact), "artifact_bytes": artifact.stat().st_size if content else 0,
        "sha256": hashlib.sha256(content.encode()).hexdigest() if content else "",
        "complete_html": "</html>" in content.lower() and "setTimeout" in content,
        "done_text": done[-1].text if done else "",
        "seconds": round(time.monotonic() - started, 2),
    }
    result["passed"] = bool(
        not errors and not tool_errors and real_calls and done and content
        and [effect["path"] for effect in effects] == expected_paths
        and result["complete_html"]
    )
    (case_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps({
        "case": case, "passed": result["passed"], "seconds": result["seconds"],
    }), flush=True)
    return result


async def main(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    for key, subdir in (
        ("OPENSQUILLA_STATE_DIR", "state"), ("OPENSQUILLA_LOG_DIR", "logs"),
        ("OPENSQUILLA_USER_STATE_DIR", "user-state"),
    ):
        os.environ[key] = str(output / subdir)
    os.environ["OPENSQUILLA_TURN_CALL_LOG"] = "0"
    llm = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))["llm"]
    # Kimi has no authorized textual tool dialect. Exercise the configured
    # DeepSeek DSML adapter for the text-schema case without broadening Kimi.
    results = []
    for case in args.cases.split(","):
        model = args.text_model if case == "text_schema" else (args.model or llm["model"])
        results.append(await scenario(llm, model, output, case))
    report = {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "tracked_diff_sha256": hashlib.sha256(subprocess.check_output(
            ["git", "-c", "core.safecrlf=false", "diff", "HEAD", "--", "src"],
        )).hexdigest(),
        "scope": (
            "Synthetic completed bad response; real API correction via "
            "OpenAIProvider/Agent/dispatch"
        ),
        "cases": results,
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    if not all(result["passed"] for result in results):
        raise SystemExit("Live validation failed; see report.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--text-model", default="deepseek-v4-pro-0813")
    parser.add_argument(
        "--cases", default="native_text,native_reasoning,native_none,text_schema,prior_success",
    )
    asyncio.run(main(parser.parse_args()))
