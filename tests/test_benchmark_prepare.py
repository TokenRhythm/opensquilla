"""Input preparation and original MetaSkill graph semantics; no model/network."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import yaml

from scripts.metaskill_benchmark import prepare as preparation
from scripts.metaskill_benchmark.plans import render_plan


class FrozenPlanTests(unittest.TestCase):
    def test_all_twenty_graphs_parse_and_keep_dependency_bindings(self):
        from opensquilla.skills.meta.parser import parse_meta_plan
        from opensquilla.skills.types import SkillLayer, SkillSpec

        manifest = preparation._manifest()
        runtime_names = {name: skill["runtime_name"] for name, skill in manifest["skills"].items()}
        nodes = edges = authored = 0
        for task_id in manifest["task_ids"]:
            with self.subTest(task_id=task_id):
                canonical = json.loads((preparation.DATA / "plans" / f"{task_id}.json").read_text(encoding="utf-8"))
                text = render_plan({"task_id": task_id}, canonical, runtime_names)
                front = yaml.safe_load(text.split("---", 2)[1])
                spec = SkillSpec(
                    name=front["name"], description=front["description"],
                    layer=SkillLayer.WORKSPACE, always=False, triggers=front["triggers"],
                    content=text, kind="meta", composition_raw=front["composition"],
                )
                plan = parse_meta_plan(spec)
                self.assertEqual(len(plan.steps), len(canonical["nodes"]))
                for original, rendered in zip(canonical["nodes"], front["composition"]["steps"]):
                    nodes += 1
                    dependencies = original.get("depends_on", [])
                    edges += len(dependencies)
                    self.assertEqual(rendered.get("depends_on", []), dependencies)
                    self.assertEqual(set(rendered["with"].get("prior_outputs", {})), set(dependencies))
                    for predecessor in dependencies:
                        self.assertEqual(rendered["with"]["prior_outputs"][predecessor],
                                         "{{ outputs." + predecessor + " | truncate(6000) }}")
                    self.assertEqual(rendered["read_only"], not original.get("write_access", True))
                    self.assertEqual(rendered["write_access"], original.get("write_access", True))
                    self.assertEqual(rendered["network_access"], original.get("network_access", False))
                    authored += sum(value.count("outputs.") for value in original.get("inputs", []))
                self.assertIn("__WORKSPACE__", text)
        self.assertEqual((nodes, edges, authored), (88, 94, 90))

    def test_unknown_dependency_and_cycle_are_rejected(self):
        manifest = preparation._manifest()
        task_id = manifest["task_ids"][0]
        canonical = json.loads((preparation.DATA / "plans" / f"{task_id}.json").read_text(encoding="utf-8"))
        names = {name: skill["runtime_name"] for name, skill in manifest["skills"].items()}
        for dependency in ("missing-node", canonical["nodes"][0]["id"]):
            modified = copy.deepcopy(canonical)
            modified["nodes"][0]["depends_on"] = [dependency]
            with self.assertRaises(ValueError):
                render_plan({"task_id": task_id}, modified, names)


class ArchiveTests(unittest.TestCase):
    def test_selected_content_is_verified_with_text_line_endings(self):
        with tempfile.TemporaryDirectory(prefix="MetaSkill inputs 中文 ") as directory:
            root = Path(directory)
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("repository-commit/skill/SKILL.md", b"line\r\n")
                zipped.writestr("repository-commit/unused.txt", b"not extracted")
            expected = {"skill/SKILL.md": {"mode": "utf8-lf", "sha256": preparation._digest(b"line\n")}}
            preparation._extract_selected(archive, root / "verified", expected)
            self.assertEqual((root / "verified/skill/SKILL.md").read_bytes(), b"line\r\n")
            self.assertFalse((root / "verified/unused.txt").exists())
            expected["skill/SKILL.md"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "differs from pinned"):
                preparation._extract_selected(archive, root / "changed", expected)

    def test_traversal_and_symlink_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, filename in enumerate(("../outside", "repository/../../outside", "repository/C:/outside", "repository/CON.txt")):
                archive = root / f"bad{index}.zip"
                with zipfile.ZipFile(archive, "w") as zipped:
                    zipped.writestr(filename, "untrusted")
                with self.assertRaises(ValueError):
                    preparation._extract_selected(archive, root / f"out{index}", {})
            archive = root / "symlink.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                link = zipfile.ZipInfo("repository/link")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                zipped.writestr(link, "outside")
            with self.assertRaisesRegex(ValueError, "Symlinks"):
                preparation._extract_selected(archive, root / "link-output", {})


class ManimDownloadTests(unittest.TestCase):
    def spec(self):
        return {
            "repository": "example/manim-skill", "revision": "b" * 40,
            "files": {"skill/SKILL.md": {
                "mode": "utf8-lf", "sha256": preparation._digest(b"Pinned synthetic Skill.\n"),
            }},
        }

    def test_archive_network_failure_fetches_same_pinned_raw_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root, spec, urls = Path(directory), self.spec(), []

            def download(url, destination, **kwargs):
                urls.append(url)
                if url.startswith("https://codeload.github.com/"):
                    destination.write_bytes(b"interrupted archive")
                    raise RuntimeError("Could not download source: transport timeout")
                destination.write_bytes(b"Pinned synthetic Skill.\r\n")

            with patch.object(preparation, "_download", side_effect=download), patch.object(preparation, "_extract_selected") as extract:
                preparation._fetch_manim_source(spec, root / "source", root / "archive.zip")
            extract.assert_not_called()
            self.assertEqual(urls, [
                f"https://codeload.github.com/{spec['repository']}/zip/{spec['revision']}",
                f"https://raw.githubusercontent.com/{spec['repository']}/{spec['revision']}/skill/SKILL.md",
            ])
            self.assertEqual((root / "source/skill/SKILL.md").read_bytes(), b"Pinned synthetic Skill.\r\n")

    def test_raw_fallback_still_rejects_wrong_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root, spec = Path(directory), self.spec()

            def download(url, destination, **kwargs):
                if url.startswith("https://codeload.github.com/"):
                    raise RuntimeError("Could not download source: transport timeout")
                destination.write_bytes(b"Changed synthetic Skill.\n")

            with patch.object(preparation, "_download", side_effect=download):
                with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                    preparation._fetch_manim_source(spec, root / "source", root / "archive.zip")

    def test_archive_wrong_fingerprint_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root, spec = Path(directory), self.spec()

            def download(url, destination, **kwargs):
                with zipfile.ZipFile(destination, "w") as zipped:
                    zipped.writestr("repository-commit/skill/SKILL.md", b"Changed synthetic Skill.\n")

            with patch.object(preparation, "_download", side_effect=download), patch.object(preparation, "_fetch_selected") as raw:
                with self.assertRaisesRegex(ValueError, "differs from pinned"):
                    preparation._fetch_manim_source(spec, root / "source", root / "archive.zip")
            raw.assert_not_called()

    def test_content_runtime_error_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root, spec = Path(directory), self.spec()
            with patch.object(preparation, "_download"), patch.object(
                preparation, "_extract_selected", side_effect=RuntimeError("Encrypted archive content")
            ), patch.object(preparation, "_fetch_selected") as raw:
                with self.assertRaisesRegex(RuntimeError, "Encrypted archive content"):
                    preparation._fetch_manim_source(spec, root / "source", root / "archive.zip")
            raw.assert_not_called()


class PreparationTests(unittest.TestCase):
    def test_offline_preparation_is_reusable_and_rejects_changes(self):
        # A tiny 20-task source fixture exercises the real preparation pipeline
        # without copying the private handoff or downloading upstream files.
        with tempfile.TemporaryDirectory(prefix="benchmark preparation 中文 ") as directory:
            root = Path(directory)
            data, upstream, cache = root / "data", root / "upstream", root / "cache"
            (data / "plans").mkdir(parents=True)
            source_files, plan_files, task_ids = {}, {}, []
            skill_name = "example-skill"
            skill_content = "---\nname: example-skill\ndescription: Fixture\n---\nRead task inputs.\n"
            skill_path = "data/skill_seeds/example-skill/SKILL.md"
            files = {skill_path: skill_content}
            for index in range(20):
                task_id = f"fixture_{index}"
                task_ids.append(task_id)
                task = {"task_id": task_id, "prompt": f"Input {index}", "skills": [skill_name]}
                files[f"benchmark/AgentSkillOS_bench/tasks/{task_id}.json"] = json.dumps(task)
                plan = {"task_id": task_id, "nodes": [{"id": "produce", "skill": skill_name, "objective": "Produce text"}]}
                path = data / "plans" / f"{task_id}.json"
                path.write_text(json.dumps(plan), encoding="utf-8")
                plan_files[path.name] = {"mode": "raw", "sha256": preparation._digest(path.read_bytes())}
            for name, text in files.items():
                path = upstream / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                source_files[name] = {"mode": "raw", "sha256": preparation._digest(path.read_bytes())}
            manifest = {
                "schema_version": 1, "task_ids": task_ids, "plans": plan_files,
                "sources": {"agentskillos": {"repository": "test/fixture", "revision": "a" * 40, "files": source_files}},
                "skills": {skill_name: {"source": "agentskillos", "path": "data/skill_seeds/example-skill", "runtime_name": skill_name}},
            }
            (data / "sources.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(preparation, "DATA", data), patch.object(preparation, "_download", side_effect=AssertionError("Network forbidden")):
                marker = preparation.prepare(cache, upstream=upstream)
                payload = preparation.validate_prepared(marker)
                self.assertEqual(len(payload["tasks"]), 20)
                self.assertEqual(preparation.prepare(cache), marker)
                self.assertTrue(Path(payload["tasks"]["fixture_0"]["skills_dir"]).is_absolute())
                altered = json.loads(marker.read_text(encoding="utf-8"))
                altered["tasks"]["fixture_0"]["prompt"] = "changed task"
                marker.write_text(json.dumps(altered), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "prompt or ID"):
                    preparation.validate_prepared(marker)
                marker.write_text(json.dumps(payload), encoding="utf-8")
                injected = Path(payload["tasks"]["fixture_0"]["skills_dir"]) / "extra-skill" / "SKILL.md"
                injected.parent.mkdir()
                injected.write_text("not a pinned input", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "untracked files"):
                    preparation.validate_prepared(marker)
                injected.unlink()
                injected.parent.rmdir()
                copied = Path(payload["tasks"]["fixture_0"]["skills_dir"]) / skill_name / "SKILL.md"
                copied.write_text("modified", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                    preparation.prepare(cache)
                self.assertEqual((upstream / skill_path).read_text(encoding="utf-8"), skill_content)


if __name__ == "__main__":
    unittest.main()
