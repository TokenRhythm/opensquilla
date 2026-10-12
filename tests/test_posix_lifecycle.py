"""Verify real POSIX termination of CLI-owned native background trees."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from metaskill_benchmark import runner


def running(pid):
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return not (stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z"))
    except ProcessLookupError:
        return False


@unittest.skipUnless(os.name == "posix", "Exercises POSIX sessions and SIGTERM")
class PosixLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="MetaSkill lifecycle 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        self.workspace = self.attempt / "workspace"
        self.skills = self.root / "skills"
        package = self.skills / "hold"
        package.mkdir(parents=True)
        background = self.root / "owned background.py"
        background.write_text(
            "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
            "label = sys.argv[1]\n"
            "code = \"import signal, time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); \" "
            "+ f\"Path('{label}-child-ready').write_text('ready'); time.sleep(60)\"\n"
            "child = subprocess.Popen([sys.executable, '-c', code])\n"
            "while not Path(label + '-child-ready').exists(): time.sleep(0.01)\n"
            "Path(label + '.json').write_text(json.dumps({'parent':os.getpid(), 'child':child.pid, "
            "'group':os.getpgrp()}), encoding='utf-8')\ntime.sleep(60)\n", encoding="utf-8")
        hold = package / "hold.py"
        hold.write_text("from pathlib import Path\nimport time\nPath('ready').write_text('ready')\ntime.sleep(60)\n",
                        encoding="utf-8")
        header = {"name": "hold", "description": "Synthetic wait for termination.",
                  "entrypoint": {"command": "python", "args": [str(hold)],
                                 "timeout": 60, "parse": "text"}}
        (package / "SKILL.md").write_text("---\n" + yaml.safe_dump(header) + "---\nSynthetic wait.\n", encoding="utf-8")
        meta = self.skills / "meta-lifecycle"
        meta.mkdir()
        # Six resistant groups cross the runner's five-second TERM grace if
        # cleanup waits for each native session sequentially.
        self.owned_labels = [f"owned-{index}" for index in range(6)]
        self.background_steps = [
            {"id": label, "kind": "tool_call", "tool": "background_process",
             "tool_args": {"command": shlex.join([sys.executable, str(background), label]),
                           "workdir": "{{ inputs.workspace_dir }}", "timeout": 60}}
            for label in self.owned_labels
        ]
        header = {"name": "meta-lifecycle", "description": "Synthetic background cancellation.",
                  "kind": "meta", "final_text_mode": "raw", "composition": {"steps": [
                      *self.background_steps,
                      {"id": "hold", "kind": "skill_exec", "skill": "hold", "depends_on": self.owned_labels},
                  ]}}
        self.meta_file = meta / "SKILL.md"
        self.meta_file.write_text("---\n" + yaml.safe_dump(header) + "---\nSynthetic graph.\n", encoding="utf-8")
        self.command = [sys.executable, str(ROOT / "scripts/run_metaskill.py"), "--skills-dir", str(self.skills),
                        "--name", "meta-lifecycle", "--workspace", str(self.workspace), "--run-mode", "full",
                        "--trace-json", str(self.workspace / "trace.json")]
        self.foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(self.clean_processes)

    def clean_processes(self):
        for label in self.owned_labels:
            path = self.workspace / (label + ".json")
            if not path.is_file():
                continue
            group = json.loads(path.read_text())["group"]
            if group > 1 and group != os.getpgrp():
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.foreign.poll() is None:
            self.foreign.terminate()
        self.foreign.wait(timeout=10)

    def ready(self, process):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            paths = [self.workspace / (label + ".json") for label in self.owned_labels]
            if (self.workspace / "ready").exists() and all(path.exists() for path in paths):
                data = [json.loads(path.read_text()) for path in paths]
                for item in data:
                    self.assertTrue(running(item["parent"]))
                    self.assertTrue(running(item["child"]))
                return data
            if process.poll() is not None:
                self.fail(f"CLI exited before readiness: {process.returncode}")
            time.sleep(0.05)
        self.fail("Native background and dependent wait did not become ready")

    def assert_owned_stopped(self, data):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(running(item[key]) for item in data for key in ("parent", "child")):
            time.sleep(0.05)
        for item in data:
            for key in ("parent", "child"):
                self.assertFalse(running(item[key]), f"CLI-owned {key} survived termination: {item[key]}")
        self.assertIsNone(self.foreign.poll(), "Termination affected a foreign session")
        databases = list((self.workspace / ".metaskill-state").glob("trace-*.sqlite3"))
        self.assertEqual(len(databases), 1)
        with sqlite3.connect(databases[0].as_uri() + "?mode=ro", uri=True) as database:
            self.assertEqual(database.execute("SELECT status FROM meta_skill_runs").fetchall(), [("cancelled",)])

    def test_sigterm_drains_native_background_cleanup(self):
        with (self.attempt / "stdout.log").open("wb") as stdout, (self.attempt / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(self.command, cwd=ROOT, env=runner.execution_environment(self.attempt),
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            with runner.process_lifetime(process):
                try:
                    data = self.ready(process)
                    os.killpg(process.pid, signal.SIGTERM)
                    time.sleep(0.05)
                    if process.poll() is None:
                        os.kill(process.pid, signal.SIGTERM)
                    exit_code = process.wait(timeout=10)
                finally:
                    runner.terminate_tree(process)
        self.assert_owned_stopped(data)
        self.assertEqual(exit_code, 128 + signal.SIGTERM)
        self.assertIn("MetaSkill terminated.", (self.attempt / "stderr.log").read_text())

    def test_batch_timeout_cleans_detached_native_background_group(self):
        result = runner.execute(self.command, self.attempt, timeout=8)
        self.assertTrue((self.workspace / "ready").exists(), (self.attempt / "stderr.log").read_text())
        data = [json.loads((self.workspace / (label + ".json")).read_text()) for label in self.owned_labels]
        self.assertTrue(result["timed_out"])
        self.assert_owned_stopped(data)
        self.assertEqual(result["exit_code"], 128 + signal.SIGTERM)

    def test_timeout_cancels_long_exec_and_its_term_resistant_child(self):
        script = self.root / "owned exec.py"
        script.write_text(
            "import json, os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
            "code = \"import signal, time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path('exec-child-ready').write_text('ready'); time.sleep(60)\"\n"
            "child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "while not Path('exec-child-ready').exists(): time.sleep(0.01)\n"
            "Path('exec.json').write_text(json.dumps({'parent':os.getpid(), 'child':child.pid, 'group':os.getpgrp()}))\n"
            "Path('ready').write_text('ready')\ntime.sleep(60)\n", encoding="utf-8")
        header = {"name": "meta-lifecycle", "description": "Synthetic native exec cancellation.",
                  "kind": "meta", "final_text_mode": "raw", "composition": {"steps": [
                      *self.background_steps,
                      {"id": "exec", "kind": "tool_call", "tool": "exec_command",
                       "depends_on": self.owned_labels.copy(),
                       "tool_args": {"command": shlex.join([sys.executable, str(script)]),
                                     "workdir": "{{ inputs.workspace_dir }}", "timeout": 60}},
                  ]}}
        self.meta_file.write_text("---\n" + yaml.safe_dump(header) + "---\nSynthetic graph.\n", encoding="utf-8")
        self.owned_labels.append("exec")
        result = runner.execute(self.command, self.attempt, timeout=8)
        self.assertTrue((self.workspace / "ready").exists(), (self.attempt / "stderr.log").read_text())
        data = [json.loads((self.workspace / (label + ".json")).read_text()) for label in self.owned_labels]
        self.assertTrue(result["timed_out"])
        self.assert_owned_stopped(data)
        self.assertEqual(result["exit_code"], 128 + signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
