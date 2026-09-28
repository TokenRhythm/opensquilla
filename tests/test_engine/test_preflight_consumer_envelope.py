"""Offline preflight acceptance with a real consumer projection and SQLite history."""

from unittest.mock import Mock

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import TurnRunner
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.session.compaction import estimate_entry_model_replay_tokens
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.helpers.compaction import synthetic_compaction_config


async def test_persisted_active_prompt_consumes_consumer_capacity_once(tmp_path):
    storage = SessionStorage(str(tmp_path / "sessions.sqlite"))
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False,
                                 checkpoint_workspace_dir=tmp_path)
        node = await manager.create("agent:main:webchat:preflight-input-accounting")
        for index in range(8):
            await manager.append_message(
                node.session_key, "user" if index % 2 == 0 else "assistant",
                "Archived ordinary discussion completed. " * 500,
            )
        prompt = "a b c d e f g h " * 800
        active = await manager.append_message(node.session_key, "user", prompt)
        before = await manager.get_canonical_transcript(node.session_key)
        provider = OpenAIProvider(api_key="synthetic-offline", model="synthetic-model")
        agent = Agent(provider=provider, config=AgentConfig(
            context_window_tokens=16_000, max_tokens=1024,
        ))
        budget = agent.resolve_compaction_budget(
            consumer_provider=provider, active_user_message=prompt,
            active_user_in_history=True, bound_user_message_id=active.message_id,
            attachment_messages=None, context_window_tokens=16_000, max_output_tokens=1024,
        )
        active_entry = active.model_dump(mode="json")
        assert budget.consumer_admission("Archived discussion complete.", [active_entry])
        summary_config = synthetic_compaction_config()
        summary_provider = summary_config.llm_plan.primary.provider
        runner = TurnRunner(provider_selector=Mock(), session_manager=manager)
        await runner._maybe_preflight_compact(
            node.session_key, 16_000,
            compaction_plan=summary_config.llm_plan,
            compaction_budget=budget,
            history_has_persisted_user=True, bound_user_message_id=active.message_id,
        )

        assert summary_provider.calls
        assert await manager.get_summaries(node.session_key)
        after = await manager.get_transcript(node.session_key)
        assert after[-1].message_id == active.message_id
        assert after[-1].content == prompt
        assert estimate_entry_model_replay_tokens(active_entry) < budget.history_capacity_tokens
        assert await manager.get_canonical_transcript(node.session_key) == before
    finally:
        await storage.close()
