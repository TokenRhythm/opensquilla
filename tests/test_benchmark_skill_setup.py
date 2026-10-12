"""Check local package adaptation without npm, networking or model calls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.metaskill_benchmark import skill_setup

ROOT = Path(__file__).resolve().parents[1]


class SkillRuntimeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="writable Skills 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.skills = Path(self.temp.name) / "skills"
        self.browser = self.skills / "dev-browser"
        scripts = self.browser / "scripts"
        scripts.mkdir(parents=True)
        for name in ("start-server.ts", "start-relay.ts"):
            (scripts / name).write_text("// synthetic script\n", encoding="utf-8")
        self.lock = self.browser / "bun.lock"
        self.manifest = self.browser / "package.json"

    def synthetic_lock(self):
        self.lock.write_text('''{
  // Synthetic fixture; no upstream packages are installed.
  "workspaces": {"": {
    "name": "synthetic-browser",
    "dependencies": {"synthetic-http": "^1.0.0",},
    "devDependencies": {"tsx": "^4.0.0", "synthetic-test": "^2.0.0",},
  },},
  /* URLs and comment-like data inside strings must remain exact. */
  "packages": {"synthetic": ["https://example.test/a//b", "quote\\\"/*text*/,}",],},
}''', encoding="utf-8")

    def test_missing_manifest_restores_workspace_declarations_and_records_hashes(self):
        self.synthetic_lock()
        before = self.lock.read_bytes()
        result = skill_setup.prepare_skill_runtime(self.skills)
        package = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "synthetic-browser")
        self.assertTrue(package["private"])
        self.assertEqual(package["type"], "module")
        self.assertEqual(package["dependencies"], {"synthetic-http": "^1.0.0"})
        self.assertEqual(package["devDependencies"], {"tsx": "^4.0.0", "synthetic-test": "^2.0.0"})
        self.assertEqual(package["scripts"], {"start": "tsx scripts/start-server.ts",
                                               "start-extension": "tsx scripts/start-relay.ts"})
        record = result["adaptations"][0]
        self.assertEqual(record["path"], "dev-browser/package.json")
        self.assertEqual(record["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(record["generated_sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(self.lock.read_bytes(), before)
        self.assertEqual(skill_setup.prepare_skill_runtime(self.skills)["adaptations"], [])

    def test_jsonc_parser_preserves_strings_and_rejects_broken_comment(self):
        self.synthetic_lock()
        lock = skill_setup._bun_json(self.lock.read_text(encoding="utf-8"))
        self.assertEqual(lock["packages"]["synthetic"], ["https://example.test/a//b", 'quote"/*text*/,}'])
        with self.assertRaises(ValueError):
            skill_setup._bun_json('{"workspaces": {} /* unfinished')

    def test_existing_manifest_is_not_rewritten_or_reparsed(self):
        original = b'{"name":"existing","type":"commonjs","custom":true}\r\n'
        self.manifest.write_bytes(original)
        self.lock.write_text("not a lock", encoding="utf-8")
        self.assertEqual(skill_setup.prepare_skill_runtime(self.skills)["adaptations"], [])
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_missing_or_invalid_lock_cannot_generate_a_successful_manifest(self):
        with self.assertRaisesRegex(ValueError, "both package.json and bun.lock"):
            skill_setup.prepare_skill_runtime(self.skills)
        for lock in ('{"workspaces":{}}', '{"workspaces":{"":{"name":"fixture","devDependencies":{}}}}',
                     '{"workspaces":{"":{"name":"fixture","dependencies":["bad"],"devDependencies":{"tsx":"1"}}}}'):
            self.lock.write_text(lock, encoding="utf-8")
            with self.subTest(lock=lock), self.assertRaises(ValueError):
                skill_setup.prepare_skill_runtime(self.skills)
            self.assertFalse(self.manifest.exists())

    def test_posix_chmod_adds_owner_execute_without_changing_script_content(self):
        script = self.browser / "server.sh"
        original = b"#!/bin/bash\necho synthetic\n"
        script.write_bytes(original)
        with patch.object(Path, "rglob", return_value=iter([script])), \
                patch.object(Path, "stat", return_value=type("Mode", (), {"st_mode": stat.S_IFREG | 0o640})()), \
                patch.object(Path, "chmod") as chmod:
            # Exercise the POSIX adjustment itself on either test platform.
            changed = skill_setup._ensure_shell_executable(self.skills)
        self.assertEqual(changed, ["dev-browser/server.sh"])
        chmod.assert_called_once_with(0o740)
        self.assertEqual(script.read_bytes(), original)

    def test_original_public_lock_when_locally_cached(self):
        source = ROOT / "workspace/benchmark/cache/inputs/skills/dev-browser/bun.lock"
        if not source.is_file():
            self.skipTest("Pinned public dev-browser lock is not cached; synthetic cases still run")
        original = source.read_bytes()
        shutil.copy2(source, self.lock)
        result = skill_setup.prepare_skill_runtime(self.skills)
        package = json.loads(self.manifest.read_text(encoding="utf-8"))
        workspace = skill_setup._bun_json(original.decode("utf-8"))["workspaces"][""]
        self.assertEqual(package["name"], workspace["name"])
        self.assertEqual(package["dependencies"], workspace["dependencies"])
        self.assertEqual(package["devDependencies"], workspace["devDependencies"])
        self.assertEqual(package["type"], "module")
        self.assertEqual(result["adaptations"][0]["source_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
