"""Tests for the setup read workflow application seam."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from opensquilla.application.setup_workflow import SetupWorkflow


class SetupPort:
    async def load_setup_catalog(self) -> Mapping[str, Any]:
        return {"providers": [{"providerId": "openai"}]}

    async def load_setup_status(self) -> Mapping[str, Any]:
        return {"llmConfigured": True}


@pytest.mark.asyncio
async def test_setup_reads_use_explicit_catalog_and_status_ports() -> None:
    port = SetupPort()
    workflow = SetupWorkflow(port, port)

    assert await workflow.catalog() == {"providers": [{"providerId": "openai"}]}
    assert await workflow.status() == {"llmConfigured": True}
