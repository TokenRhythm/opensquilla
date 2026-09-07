from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.skill_catalog import (
    SkillCatalog,
    SkillIdentity,
    SkillSearchPage,
    SkillSearchQuery,
)


@pytest.mark.asyncio
async def test_skill_catalog_expresses_reads_without_wire_details() -> None:
    reader = AsyncMock()
    reader.list.return_value = ({"name": "demo"},)
    reader.detail.return_value = {"name": "demo", "content": "# Demo"}
    reader.search.return_value = SkillSearchPage(results=({"name": "remote"},))
    catalog = SkillCatalog(reader)

    assert await catalog.list(include_lifecycle=True) == ({"name": "demo"},)
    assert await catalog.detail(SkillIdentity(name="demo")) == {
        "name": "demo",
        "content": "# Demo",
    }
    assert (await catalog.search(SkillSearchQuery("demo", limit=500))).results == (
        {"name": "remote"},
    )

    reader.list.assert_awaited_once_with(include_lifecycle=True)
    assert reader.search.await_args.args[0].limit == 100


@pytest.mark.asyncio
async def test_skill_catalog_rejects_identity_mismatch() -> None:
    reader = AsyncMock()
    reader.detail.return_value = {"name": "other"}
    catalog = SkillCatalog(reader)

    with pytest.raises(KeyError, match="does not match"):
        await catalog.detail(SkillIdentity(name="demo", instance_id="managed:1"))


def test_skill_identity_requires_at_least_one_business_identity() -> None:
    with pytest.raises(ValueError, match="identity is required"):
        SkillIdentity()
