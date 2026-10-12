"""Run the public CLI against temporary Skill packages, without network access."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "run_metaskill.py"
EXAMPLE_SKILLS = ROOT / "examples" / "skills"


class MetaSkillCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="metaskill smoke ")
        self.addCleanup(self.temp.cleanup)
        self.temp_root = Path(self.temp.name)
        self.skills = self.temp_root / "Skill packages 中文"
        shutil.copytree(EXAMPLE_SKILLS, self.skills)
        self.workspace = self.temp_root / "task workspace 中文"

    def run_cli(
        self, *, name: str = "meta-text-demo", text: str = "Hello MetaSkill",
        extra_args: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--skills-dir", str(self.skills),
                "--name", name,
                "--input", text,
                "--workspace", str(self.workspace),
                *extra_args,
            ],
            cwd=self.temp_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_example_runs_from_unicode_and_space_paths(self) -> None:
        text = 'Hello "MetaSkill" 中文\nSecond line'
        result = self.run_cli(text=text)
        self.assert_success(result)
        self.assertEqual(
            result.stdout.strip(),
            f"UPPER: {text.upper()}\nlower: {text.lower()}",
        )

    def test_independent_steps_overlap_before_the_merge(self) -> None:
        # Each real subprocess must see the other one start before it can
        # finish. A sequential scheduler fails rather than passing a loose
        # timing threshold on a fast or slow CI machine.
        script = self.skills / "text-transform" / "scripts" / "transform.py"
        script.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "import time\n"
            "mode = sys.argv[1]\n"
            "Path(mode + '.started').write_text('started', encoding='utf-8')\n"
            "other = 'lower' if mode == 'upper' else 'upper'\n"
            "deadline = time.monotonic() + 10\n"
            "while not Path(other + '.started').is_file():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise RuntimeError('independent steps did not overlap')\n"
            "    time.sleep(0.02)\n"
            "text = sys.stdin.read()\n"
            "print(text.upper() if mode == 'upper' else text.lower())\n",
            encoding="utf-8",
        )
        result = self.run_cli(text="Parallel Join")
        self.assert_success(result)
        self.assertEqual(
            result.stdout.strip(), "UPPER: PARALLEL JOIN\nlower: parallel join"
        )
        self.assertTrue((self.workspace / "upper.started").is_file())
        self.assertTrue((self.workspace / "lower.started").is_file())

    def test_step_failure_returns_nonzero_and_does_not_merge(self) -> None:
        script = self.skills / "text-transform" / "scripts" / "transform.py"
        script.write_text("raise SystemExit(17)\n", encoding="utf-8")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("UPPER:", result.stdout)
        self.assertIn("MetaSkill failed at", result.stderr)
        self.assertIn("17", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_cyclic_dependencies_are_rejected(self) -> None:
        skill = self.skills / "meta-text-demo" / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        text = text.replace(
            "    - id: upper\n", "    - id: upper\n      depends_on: [merge]\n"
        )
        skill.write_text(text, encoding="utf-8")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cycl", (result.stdout + result.stderr).lower())
        self.assertNotIn("Traceback", result.stderr)

    def test_model_example_requires_explicit_configuration(self) -> None:
        result = self.run_cli(
            name="meta-writing-demo", text="Announce a local Python study group."
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pass --config", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_node_definitions_are_rejected_before_scripts_run(self) -> None:
        skill = self.skills / "meta-text-demo" / "SKILL.md"
        original = skill.read_text(encoding="utf-8")
        script = self.skills / "text-transform" / "scripts" / "transform.py"
        script.write_text(
            "from pathlib import Path\nPath('should-not-run').touch()\n",
            encoding="utf-8",
        )
        cases = (
            (original.replace("    - id: lower\n", "    - id: upper\n"), "duplicate"),
            (original.replace("depends_on: [upper, lower]", "depends_on: [upper, missing]"), "undefined"),
        )
        for text, message in cases:
            with self.subTest(message=message):
                skill.write_text(text, encoding="utf-8")
                result = self.run_cli()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr.lower())
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse((self.workspace / "should-not-run").exists())

    def test_trace_destination_must_stay_inside_workspace(self) -> None:
        destination = self.temp_root / "outside-trace.json"
        result = self.run_cli(extra_args=("--trace-json", str(destination)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside --workspace", result.stderr)
        self.assertFalse(destination.exists())

    def test_tool_call_steps_use_native_handlers_and_bind_outputs(self) -> None:
        skill = self.skills / "native-file-dag"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: native-file-dag\nkind: meta\n"
            "description: Exercise a local write-read-handoff chain.\n"
            "final_text_mode: step:handoff\ncomposition:\n  steps:\n"
            "    - id: write\n      kind: tool_call\n      tool: write_file\n"
            "      tool_args:\n        path: source.txt\n"
            "        content: '{{ inputs.user_message }}'\n"
            "    - id: read\n      kind: tool_call\n      tool: read_file\n"
            "      depends_on: [write]\n      tool_args:\n        path: source.txt\n"
            "    - id: handoff\n      kind: tool_call\n      tool: write_file\n"
            "      depends_on: [read]\n      tool_args:\n        path: handoff.txt\n"
            "        content: '{{ outputs.read }}'\n---\n",
            encoding="utf-8",
        )
        result = self.run_cli(name="native-file-dag", text="Bound content 中文", extra_args=("--run-mode", "full"))
        self.assert_success(result)
        self.assertEqual((self.workspace / "source.txt").read_text(encoding="utf-8"), "Bound content 中文")
        self.assertIn("Bound content 中文", (self.workspace / "handoff.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
