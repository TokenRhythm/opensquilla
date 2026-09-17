"""Unit tests for opensquilla.contrib.codetask.config."""

import sys

import pytest

from opensquilla.contrib.codetask import config


class TestSlugify:
    def test_basic(self):
        assert config.slugify("Fix CSV export bug") == "fix-csv-export-bug"

    def test_strips_punctuation_and_unicode(self):
        assert config.slugify("修复 CSV 导出 bug!!!") == "csv-bug"

    def test_truncates(self):
        s = config.slugify("a" * 100, max_len=10)
        assert len(s) <= 10

    def test_empty_fallback(self):
        assert config.slugify("!!!") == "task"


class TestPaths:
    @pytest.mark.parametrize("kind", ["desktop-primary", "desktop-recovery", ""])
    def test_desktop_outputs_are_outside_live_profile(self, monkeypatch, tmp_path, kind):
        home = tmp_path / "Application Data" / "测试 profile"
        monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
        monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
        monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", kind)
        monkeypatch.delenv("OPENSQUILLA_CODETASK_RUNS_DIR", raising=False)
        monkeypatch.delenv("OPENSQUILLA_CODETASK_WORKSPACE_DIR", raising=False)

        expected = home.with_name("测试 profile-code-task")
        assert config.storage_root() == expected
        assert config.runs_root() == expected / "code-task"
        assert config.build_workspace_dir() == expected / "workspace"
        assert not home.exists()
        assert not expected.exists()

    def test_explicit_cli_keeps_existing_output_paths(self, monkeypatch, tmp_path):
        home = tmp_path / "existing-profile"
        monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
        monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
        monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "cli")
        monkeypatch.delenv("OPENSQUILLA_CODETASK_RUNS_DIR", raising=False)
        monkeypatch.delenv("OPENSQUILLA_CODETASK_WORKSPACE_DIR", raising=False)

        assert config.storage_root() == home
        assert config.runs_root() == home / "code-task"
        assert config.build_workspace_dir() == home / "workspace"

    def test_runs_root_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path))
        assert config.runs_root() == tmp_path
        assert config.run_dir("r1") == tmp_path / "r1"
        assert config.repo_dir("r1") == tmp_path / "r1" / "repo"
        assert config.artifact_path("r1", "result.json") == tmp_path / "r1" / "result.json"

    def test_scratch_is_sandbox_writable_not_under_runs_root(self, monkeypatch, tmp_path):
        # The scratch dir MUST NOT live under the runs root (which defaults to
        # ~/.opensquilla, i.e. /root/... — hard-blocked by the agent sandbox).
        # It must default under the system temp dir.
        import tempfile

        monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path))
        monkeypatch.delenv("OPENSQUILLA_CODETASK_SCRATCH_DIR", raising=False)
        scratch = config.scratch_dir("r1")
        assert tempfile.gettempdir() in str(scratch)
        assert str(tmp_path) not in str(scratch)
        assert scratch.name == "scratch"

    def test_scratch_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENSQUILLA_CODETASK_SCRATCH_DIR", str(tmp_path / "s"))
        assert config.scratch_dir("r1") == tmp_path / "s" / "r1" / "scratch"

    def test_agent_python_default(self, monkeypatch):
        monkeypatch.delenv("OPENSQUILLA_CODETASK_AGENT_PYTHON", raising=False)
        assert config.agent_python() == sys.executable

    def test_agent_python_override(self, monkeypatch):
        monkeypatch.setenv("OPENSQUILLA_CODETASK_AGENT_PYTHON", "/opt/py/bin/python")
        assert config.agent_python() == "/opt/py/bin/python"

    def test_prompt_template_shipped(self, monkeypatch):
        monkeypatch.delenv("OPENSQUILLA_CODETASK_PROMPT_TEMPLATE", raising=False)
        path = config.prompt_template_path()
        assert path.exists()
        body = path.read_text(encoding="utf-8")
        for slot in ("{task}", "{env_hints}", "{scratch_dir}", "{manifest_name}"):
            assert slot in body


class TestAgentConfigPath:
    def test_default_exists(self):
        p = config.agent_config_path()
        assert p.name == "config.toml"
        assert p.exists()

    def test_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom.toml"
        custom.write_text("")
        monkeypatch.setenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", str(custom))
        assert config.agent_config_path() == custom
