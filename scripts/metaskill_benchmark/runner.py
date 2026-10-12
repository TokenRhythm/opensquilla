"""MetaSkill-only repeated execution. Generated inputs, state, and scores stay local."""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from uuid import uuid4

from .evaluate import audit_trace, evaluate_artifacts, summarize
from .prepare import validate_prepared

ROOT = Path(__file__).resolve().parents[2]


def io_path(path):
    """Use extended Windows paths only for filesystem operations, not prompts."""
    path = Path(path).resolve()
    value = str(path)
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def read_json(path):
    return json.loads(io_path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = io_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path):
    return hashlib.sha256(io_path(path).read_bytes()).hexdigest()


def runtime_fingerprint():
    digest = hashlib.sha256()
    files = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for directory in ("scripts", "src", "migrations"):
        files.extend(p for p in (ROOT / directory).rglob("*")
                     if p.is_file() and p.suffix in {".py", ".sql"})
    for path in sorted(files):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def selected_tasks(prepared, tasks=None):
    chosen = list(dict.fromkeys(tasks or prepared["tasks"]))
    unknown = set(chosen) - set(prepared["tasks"])
    if not chosen or unknown:
        raise ValueError(f"Unknown or empty task selection: {sorted(unknown)}")
    return chosen


def check(prepared_path, config_path, tasks=None):
    from run_metaskill import read_config, provider_config
    prepared = validate_prepared(Path(prepared_path))
    selected = selected_tasks(prepared, tasks)
    config = read_config(Path(config_path))
    model = config.get("model", {})
    errors, missing, requirements = [], [], {}
    if model.get("provider") not in {"openrouter", "tokenrhythm"}:
        errors.append("Benchmark model.provider must be openrouter or tokenrhythm")
    try:
        provider_config(model)
        if config.get("vision"):
            provider_config(config["vision"])
    except ValueError as exc:
        errors.append(str(exc))
    if config.get("tools", {}).get("run_mode") != "full":
        errors.append("Benchmark config must explicitly set tools.run_mode = 'full'")
    for module in ("jsonschema", "fitz", "docx", "pptx", "openpyxl", "pdfplumber"):
        if importlib.util.find_spec(module) is None:
            errors.append(f"Missing evaluator module {module}: uv sync --locked --extra benchmark --python 3.12")
    # These are preconditions of the downloaded Skill scripts, not alternative benchmark methods.
    programs = {
        "latex-posters": [("pdflatex", "lualatex")],
        "scientific-slides": [("pdflatex", "lualatex")],
        "manimce-best-practices": [("manim",), ("ffmpeg",), ("latex",)],
        "manim": [("manim",), ("ffmpeg",), ("latex",)],
        "media-processing": [("ffmpeg",), ("magick",)],
        "docx": [("node",), ("pandoc",), ("soffice",), ("pdftoppm",)],
        "pptx": [("node",), ("soffice",), ("pdftoppm",)],
        "xlsx": [("soffice",)],
        "dev-browser": [("node",), ("npm",)],
        "frontend-design": [("node",), ("npm",)],
        "scroll-experience": [("node",), ("npm",)],
    }
    if os.name == "nt":
        programs["dev-browser"].append(("bash",))
    for task_id in selected:
        root = Path(prepared["tasks"][task_id]["skills_dir"])
        names = {p.parent.name for p in root.glob("*/SKILL.md")}
        needed = []
        for skill in sorted(names):
            for alternatives in programs.get(skill, []):
                item = "/".join(alternatives)
                needed.append(item)
                if not any(shutil.which(tool) for tool in alternatives):
                    missing.append({"task_id": task_id, "skill": skill, "requirement": item})
            if skill in {"generate-image", "scientific-slides", "latex-posters"}:
                needed.append("OPENROUTER_API_KEY (Skill image scripts)")
                if not os.environ.get("OPENROUTER_API_KEY", "").strip():
                    missing.append({"task_id": task_id, "skill": skill, "requirement": "OPENROUTER_API_KEY (image scripts)"})
            if skill == "firecrawl-scraper":
                needed.append("FIRECRAWL_API_KEY (Skill API)")
                if not os.environ.get("FIRECRAWL_API_KEY", "").strip():
                    missing.append({"task_id": task_id, "skill": skill, "requirement": "FIRECRAWL_API_KEY"})
        requirements[task_id] = sorted(set(needed))
    return {"tasks": selected, "default_trials": len(selected) * 3, "errors": errors,
            "missing_tools": missing, "skill_requirements": requirements,
            "notes": ["Local preflight makes no paid model requests and cannot verify service access.",
                      "Install Skill-specific npm packages, browser binaries, TeX packages, and fonts as documented in README."]}


@contextmanager
def batch_lock(output):
    output.mkdir(parents=True, exist_ok=True)
    path = output / ".batch.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError(f"Batch locked: {path}. If interrupted, verify the recorded process is stopped before removing this lock.") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "started_at": now()}, stream)
        yield
    finally:
        path.unlink(missing_ok=True)


def terminate_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@contextmanager
def process_lifetime(process):
    """Confine Windows descendants to a kill-on-close Job, including daemons."""
    if os.name != "nt":
        try:
            yield
        finally:
            # The launched process owns this new session/process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        terminate_tree(process)
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        yield
    except BaseException:
        terminate_tree(process)
        raise
    finally:
        kernel.CloseHandle(job)


def execution_environment(directory):
    """Keep external Skill tools' profiles and caches inside this attempt."""
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    profile = directory.resolve() / "tool-profile"
    config = profile / ".config"
    cache = profile / ".cache"
    local = profile / "AppData/Local"
    roaming = profile / "AppData/Roaming"
    temporary = profile / "tmp"
    for path in (config, cache, local, roaming, temporary):
        io_path(path).mkdir(parents=True, exist_ok=True)
    libreoffice = profile / ("Library/Application Support/LibreOffice/4" if sys.platform == "darwin"
                             else ".config/libreoffice/4")
    environment.update(HOME=str(profile), USERPROFILE=str(profile), APPDATA=str(roaming),
                       LOCALAPPDATA=str(local), XDG_CONFIG_HOME=str(config), XDG_CACHE_HOME=str(cache),
                       XDG_DATA_HOME=str(profile / ".local/share"), TEMP=str(temporary),
                       TMP=str(temporary), TMPDIR=str(temporary), UserInstallation=libreoffice.as_uri())
    # Skill shell commands find the same locked Python environment as the host.
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    return environment


def execute(command, directory, timeout):
    """Persist output without keeping a potentially huge transcript in RAM."""
    environment = execution_environment(directory)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, **options)
        with process_lifetime(process):
            try:
                return {"exit_code": process.wait(timeout=timeout), "timed_out": False}
            except subprocess.TimeoutExpired:
                terminate_tree(process)
                return {"exit_code": process.returncode, "timed_out": True}
            except BaseException:
                terminate_tree(process)
                raise


def safe_destination(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError(f"Invalid asset destination: {relative}")
    return path


def stage(task, attempt):
    workspace = attempt / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    skills = workspace / "skills"
    shutil.copytree(io_path(task["skills_dir"]), io_path(skills))
    from .skill_setup import prepare_skill_runtime
    write_json(attempt / "skill-runtime-setup.json", prepare_skill_runtime(io_path(skills)))
    template = io_path(skills / task["meta_name"] / "SKILL.md")
    if not template.is_file():
        source = task.get("meta_skill_path")
        if not source:
            raise ValueError("Prepared task is missing its rendered MetaSkill")
        template.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, template)
    template.write_text(template.read_text(encoding="utf-8").replace("__WORKSPACE__", workspace.as_posix()), encoding="utf-8")
    for asset in task.get("input_assets", []):
        source = Path(asset["source"])
        destination = safe_destination(workspace, asset["relative_destination"])
        if destination.is_relative_to(skills) or destination.is_relative_to(workspace / ".metaskill-state"):
            raise ValueError("Input asset overlaps runtime paths")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(io_path(source), io_path(destination))
        else:
            shutil.copy2(io_path(source), io_path(destination))
    (attempt / "prompt.txt").write_text(task["prompt"], encoding="utf-8")
    shutil.copy2(io_path(task["plan_path"]), io_path(attempt / "canonical-plan.json"))
    return workspace, skills


def retain_state(attempt, record):
    """Keep package fixtures and runtime records out of the artifact evaluation tree."""
    workspace = Path(record["workspace"])
    for name, destination in (("skills", "retained-skills"), (".metaskill-state", "runtime-state")):
        source = workspace / name
        if source.exists():
            shutil.move(str(io_path(source)), str(io_path(attempt / destination)))
    trace = attempt / "runtime-state" / "benchmark-trace.json"
    record["trace_path"] = str(trace)


def evaluate_record(record, upstream_root):
    plan = read_json(record["canonical_plan_path"]) if Path(record["canonical_plan_path"]).is_file() else {}
    trace_path = Path(record["trace_path"])
    try:
        trace = read_json(trace_path) if trace_path.is_file() else None
    except (OSError, ValueError):
        trace = None
    record["conformance"] = audit_trace(plan, trace)
    if trace:
        record["native_run_id"] = trace.get("run_id")
        record["provider_usage"] = trace.get("provider_usage")
    record["objective"] = evaluate_artifacts(Path(record["task_config_path"]), Path(record["workspace"]), Path(upstream_root))
    execution = record.get("execution_status", record["status"])
    if execution == "completed":
        record["status"] = "completed" if record["objective"]["status"] == "completed" else "evaluation_failed"
    record["evaluated_at"] = now()
    return record


def records_in(output):
    return [read_json(p) for p in sorted(output.glob("runs/*/trial-*/attempt-*/run.json"))]


def save_summary(output, batch):
    records = records_in(output)
    result = summarize(records)
    latest = {}
    for record in records:
        key = (record["task_id"], record["trial"])
        if key not in latest or record["attempt"] > latest[key]["attempt"]:
            latest[key] = record
    result["coverage"] = {"task_count": len(batch["tasks"]), "repeats": batch["repeats"],
                          "expected_trials": len(batch["tasks"]) * batch["repeats"],
                          "recorded_trials": len(latest),
                          "completed_trials": sum(r["status"] == "completed" for r in latest.values()),
                          "missing_trials": [{"task_id": t, "trial": r} for r in range(1, batch["repeats"] + 1)
                                             for t in batch["tasks"] if (t, r) not in latest]}
    result["protocol"] = "MetaSkill-only; official objective checks and execution conformance; no comparative quality ranking"
    result["model"] = batch["model"]
    write_json(output / "summary.json", result)
    fields = ["task_id", "trial", "attempt", "status", "execution_status", "exit_code", "duration_seconds",
              "objective_status", "objective_score", "conformance_status", "error"]
    with (output / "trials.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for key, record in sorted(latest.items()):
            row = {field: record.get(field) for field in fields}
            row.update(objective_status=(record.get("objective") or {}).get("status"),
                       objective_score=(record.get("objective") or {}).get("total_score"),
                       conformance_status=(record.get("conformance") or {}).get("conformance_status"))
            writer.writerow(row)
    write_json(output / "attempts.json", records)
    return result


def run_batch(prepared_path, config_path, output, *, tasks=None, repeats=3, timeout=5400,
              resume=False, retry_failed=False, allow_missing_tools=False):
    if repeats < 1 or timeout < 1:
        raise ValueError("repeats and timeout must be positive")
    if retry_failed and not resume:
        raise ValueError("--retry-failed requires --resume")
    prepared_path, config_path, output = (Path(p).resolve() for p in (prepared_path, config_path, output))
    prepared = validate_prepared(prepared_path)
    selected = selected_tasks(prepared, tasks)
    report = check(prepared_path, config_path, selected)
    if report["errors"] or (report["missing_tools"] and not allow_missing_tools):
        raise ValueError("Preflight did not pass; run the check command for details. Missing Skill tools can only be overridden explicitly with --allow-missing-tools.")
    from run_metaskill import read_config
    config = read_config(config_path)
    model = config["model"]
    fingerprint = {"prepared_sha256": sha256(prepared_path), "config_sha256": sha256(config_path),
                   "tasks": selected, "repeats": repeats, "timeout": timeout,
                   "runtime_sha256": runtime_fingerprint(), "python": platform.python_version()}
    if output.exists() and any(output.iterdir()) and not resume:
        raise ValueError("Output is not empty; choose a new output or explicitly --resume")
    with batch_lock(output):
        batch_path = output / "batch.json"
        deployment_path = output / "deployment.toml"
        if batch_path.exists():
            batch = read_json(batch_path)
            if batch["fingerprint"] != fingerprint:
                raise ValueError("Resume requires identical tasks, prepared inputs, config, runtime, repeats, and timeout")
            if not deployment_path.is_file() or sha256(deployment_path) != batch["deployment_sha256"]:
                raise ValueError("The batch's frozen deployment configuration changed")
        else:
            if resume:
                raise ValueError("No batch.json to resume")
            import tomli_w
            # Resolve configuration once, so edits to the user's source config
            # cannot silently change later trials in an already-running batch.
            deployment_path.write_text(tomli_w.dumps(config), encoding="utf-8")
            batch = {"schema_version": 1, "created_at": now(), "fingerprint": fingerprint,
                     "deployment_sha256": sha256(deployment_path),
                     "prepared_path": str(prepared_path), "upstream_root": prepared["upstream_root"],
                     "tasks": selected, "repeats": repeats,
                     "model": {key: model.get(key) for key in ("provider", "name", "base_url", "api_key_env")},
                     "runtime": config.get("runtime", {}), "tools": config.get("tools", {}),
                     "platform": platform.platform(), "preflight": report}
            write_json(batch_path, batch)
        previous = records_in(output)
        for trial in range(1, repeats + 1):
            for task_id in selected:
                matching = [r for r in previous if r["task_id"] == task_id and r["trial"] == trial]
                last = max(matching, key=lambda r: r["attempt"]) if matching else None
                if last and (last["status"] == "completed" or not retry_failed):
                    continue
                attempt_number = (last["attempt"] + 1) if last else 1
                attempt = output / "runs" / task_id / f"trial-{trial:02d}" / f"attempt-{attempt_number:02d}"
                attempt.mkdir(parents=True, exist_ok=False)
                task = prepared["tasks"][task_id]
                record = {"schema_version": 1, "task_id": task_id, "trial": trial, "attempt": attempt_number,
                          "run_id": uuid4().hex, "status": "running", "started_at": now(),
                          "model": model["name"], "provider": model["provider"],
                          "plan_sha256": sha256(task["plan_path"]), "task_config_path": task["task_config_path"],
                          "workspace": str(attempt / "workspace"), "canonical_plan_path": str(attempt / "canonical-plan.json"),
                          "trace_path": str(attempt / "workspace/.metaskill-state/benchmark-trace.json"),
                          "exit_code": None, "error": None}
                record_path = attempt / "run.json"
                write_json(record_path, record)
                start = time.monotonic()
                interrupted = False
                print(f"[{trial}/{repeats}] {task_id} attempt {attempt_number}", flush=True)
                try:
                    if sha256(deployment_path) != batch["deployment_sha256"]:
                        raise ValueError("The batch's frozen deployment configuration changed")
                    workspace, skills = stage(task, attempt)
                    command = [sys.executable, str(ROOT / "scripts/run_metaskill.py"), "--skills-dir", str(skills),
                               "--name", task["meta_name"], "--input-file", str(attempt / "prompt.txt"),
                               "--workspace", str(workspace), "--config", str(deployment_path),
                               "--trace-json", record["trace_path"], "--max-trace-field-bytes", "8388608"]
                    result = execute(command, attempt, timeout)
                    record["exit_code"] = result["exit_code"]
                    record["status"] = "timeout" if result["timed_out"] else ("completed" if result["exit_code"] == 0 else "failed")
                    if record["status"] != "completed":
                        record["error"] = f"Runtime {record['status']}; inspect this attempt's stderr.log"
                except KeyboardInterrupt:
                    record["status"], record["error"], interrupted = "failed", "Interrupted by user", True
                except (OSError, ValueError, RuntimeError) as exc:
                    record["status"], record["error"] = "preparation_failed", str(exc)
                    record["noinput"] = isinstance(exc, FileNotFoundError)
                finally:
                    record["execution_status"] = record["status"]
                    record["duration_seconds"] = round(time.monotonic() - start, 3)
                    record["finished_at"] = now()
                    try:
                        retain_state(attempt, record)
                        if record["status"] != "preparation_failed":
                            evaluate_record(record, prepared["upstream_root"])
                    except Exception as exc:
                        if record["status"] == "completed":
                            record["status"] = "evaluation_failed"
                        record["evaluation_error"] = str(exc)
                    write_json(record_path, record)
                    previous.append(record)
                    save_summary(output, batch)
                if interrupted:
                    raise KeyboardInterrupt
        return save_summary(output, batch)


def evaluate_batch(output):
    output = Path(output).resolve()
    batch = read_json(output / "batch.json")
    validate_prepared(Path(batch["prepared_path"]))
    with batch_lock(output):
        for path in sorted(output.glob("runs/*/trial-*/attempt-*/run.json")):
            record = read_json(path)
            if record.get("execution_status", record["status"]) not in {"preparation_failed", "running"}:
                try:
                    evaluate_record(record, batch["upstream_root"])
                    record.pop("evaluation_error", None)
                except Exception as exc:
                    if record.get("execution_status", record["status"]) == "completed":
                        record["status"] = "evaluation_failed"
                    record["objective"] = {"status": "error", "total_score": None, "error": str(exc)}
                    record["conformance"] = {"conformance_status": "unknown"}
                    record["evaluation_error"] = str(exc)
                write_json(path, record)
        return save_summary(output, batch)
