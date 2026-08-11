"""CodeTask tool deny policy.

Deterministic: expand the declared deny selectors against an explicit tool
universe so the assertions do not depend on which tools happen to be registered
in the test process.
"""

import tomllib
from pathlib import Path

from opensquilla.tools.policy_config import expand_selectors

_ROOT = Path(__file__).resolve().parents[3]
CODETASK_CONFIG = _ROOT / "src/opensquilla/contrib/codetask/data/agent_config/config.toml"

_AVAIL = frozenset(
    {
        "http_request", "web_fetch", "web_search",
        "exec_command", "edit_file", "apply_patch", "git_diff", "read_file",
        "process", "background_process",
        "tts", "music_generate", "song_generate", "voice_clone", "dubbing_generate",
        "image", "image_generate",
        "memory_save", "memory_search", "agents_list", "subagents",
        "sessions_send", "cron", "message",
    }
)

_NETWORK = {"http_request", "web_fetch", "web_search"}
_MEDIA = {"tts", "music_generate", "voice_clone", "dubbing_generate", "image_generate"}
_CODING = {"exec_command", "edit_file", "apply_patch", "git_diff", "process"}


def _denied(path):
    deny = tomllib.load(open(path, "rb"))["tools"]["deny"]
    return expand_selectors(frozenset(deny), _AVAIL)


def test_codetask_keeps_network():
    # code-task legitimately needs the network (docs, deps).
    assert _denied(CODETASK_CONFIG).isdisjoint(_NETWORK)


def test_codetask_blocks_noncoding_tools():
    d = _denied(CODETASK_CONFIG)
    assert _MEDIA <= d
    assert {"memory_save", "agents_list", "cron", "message"} <= d
    assert d.isdisjoint(_CODING)
