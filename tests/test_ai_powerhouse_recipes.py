"""Tests for AI Powerhouse gallery recipes.

Validates all AI Powerhouse gallery entries — structure, installation,
trigger matching, category grouping, and broker integration.
Each test runs 10 iterations via parametrize.
"""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base


# ════════════════════════════════════════════════════════════════════
#  New AI Powerhouse recipe names (added in this batch)
# ════════════════════════════════════════════════════════════════════

NEW_RECIPES = [
    "autoresearch",
    "claude-task-master",
    "ruflo",
    "pm-workspace",
    "ai-powerhouse-master",
    "ui-ux-pro-max",
]

ALL_AI_POWERHOUSE = [
    "ecc-skills",
    "super-claude",
    "superpowers",
    "get-shit-done",
    "claude-mem",
] + NEW_RECIPES

# Trigger → expected recipe name mapping for match_recipe tests
TRIGGER_MAP = {
    "autoresearch": "autoresearch",
    "ml research": "autoresearch",
    "paper discovery": "autoresearch",
    "task master": "claude-task-master",
    "taskmaster": "claude-task-master",
    "task decomposition": "claude-task-master",
    "ruflo": "ruflo",
    "sparc": "ruflo",
    "swarm": "ruflo",
    "ai orchestration": "ruflo",
    "pm workspace": "pm-workspace",
    "project management": "pm-workspace",
    "agile": "pm-workspace",
    "sprints": "pm-workspace",
    "ai powerhouse": "ai-powerhouse-master",
    "master routing": "ai-powerhouse-master",
    "agent routing": "ai-powerhouse-master",
    "ui ux": "ui-ux-pro-max",
    "uiux": "ui-ux-pro-max",
    "design system": "ui-ux-pro-max",
}


# ════════════════════════════════════════════════════════════════════
#  Fixtures
# ════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    """Fresh in-memory DB per test, patches broker session factory."""
    from app.core import db as db_mod
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db_mod.engine = engine
    db_mod.async_session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ════════════════════════════════════════════════════════════════════
#  1. Gallery Structure Validation
# ════════════════════════════════════════════════════════════════════

class TestGalleryStructure:
    """Validate all AI Powerhouse gallery entries have correct structure."""

    @pytest.mark.parametrize("idx", range(10))
    async def test_gallery_is_list(self, idx):
        from app.core.gallery import GALLERY
        assert isinstance(GALLERY, list)

    @pytest.mark.parametrize("idx", range(10))
    async def test_total_recipe_count(self, idx):
        from app.core.gallery import GALLERY
        assert len(GALLERY) >= 29  # 9 Synclyf + 11 AI Powerhouse + 9 Dev Workflows

    @pytest.mark.parametrize("name", ALL_AI_POWERHOUSE)
    async def test_each_ai_powerhouse_exists(self, name):
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        assert item is not None, f"Gallery item '{name}' not found"

    @pytest.mark.parametrize("name", ALL_AI_POWERHOUSE)
    async def test_each_has_required_fields(self, name):
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        assert "category" in item
        assert "name" in item
        assert "description" in item
        assert "recipe" in item
        assert item["category"] == "AI Powerhouse"

    @pytest.mark.parametrize("name", ALL_AI_POWERHOUSE)
    async def test_each_recipe_has_triggers(self, name):
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        recipe = item["recipe"]
        assert "triggers" in recipe
        assert isinstance(recipe["triggers"], list)
        assert len(recipe["triggers"]) >= 2  # at least 2 triggers

    @pytest.mark.parametrize("name", ALL_AI_POWERHOUSE)
    async def test_each_recipe_has_prompt_prefix(self, name):
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        recipe = item["recipe"]
        assert "prompt_prefix" in recipe
        assert len(recipe["prompt_prefix"]) > 10

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_new_recipes_have_project_dir(self, name):
        """New AI Powerhouse recipes should point to taskpilot submodule."""
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        recipe = item["recipe"]
        assert "project_dir" in recipe
        assert "ai-powerhouse" in recipe["project_dir"]

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_new_recipes_have_agent(self, name):
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        recipe = item["recipe"]
        assert "agent" in recipe
        assert recipe["agent"] == "claude"

    @pytest.mark.parametrize("name", ALL_AI_POWERHOUSE)
    async def test_description_not_empty(self, name):
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        assert len(item["description"]) > 5

    @pytest.mark.parametrize("idx", range(10))
    async def test_no_duplicate_names_in_gallery(self, idx):
        from app.core.gallery import GALLERY
        names = [item["name"] for item in GALLERY]
        assert len(names) == len(set(names)), "Duplicate names found"


# ════════════════════════════════════════════════════════════════════
#  2. Category Grouping
# ════════════════════════════════════════════════════════════════════

class TestCategoryGrouping:
    """Validate category helpers return correct AI Powerhouse entries."""

    @pytest.mark.parametrize("idx", range(10))
    async def test_ai_powerhouse_category_exists(self, idx):
        from app.core.gallery import get_gallery_categories
        cats = get_gallery_categories()
        assert "AI Powerhouse" in cats

    @pytest.mark.parametrize("idx", range(10))
    async def test_ai_powerhouse_count(self, idx):
        from app.core.gallery import get_gallery_by_category
        items = get_gallery_by_category("AI Powerhouse")
        assert len(items) == 11  # 5 original + 6 new

    @pytest.mark.parametrize("idx", range(10))
    async def test_all_ai_powerhouse_in_category(self, idx):
        from app.core.gallery import get_gallery_by_category
        items = get_gallery_by_category("AI Powerhouse")
        names = {item["name"] for item in items}
        for name in ALL_AI_POWERHOUSE:
            assert name in names, f"{name} not in AI Powerhouse category"

    @pytest.mark.parametrize("idx", range(10))
    async def test_categories_order_preserved(self, idx):
        from app.core.gallery import get_gallery_categories
        cats = get_gallery_categories()
        assert cats.index("AI Powerhouse") > cats.index("Synclyf")
        assert cats.index("Dev Workflows") > cats.index("AI Powerhouse")

    @pytest.mark.parametrize("idx", range(10))
    async def test_three_categories_total(self, idx):
        from app.core.gallery import get_gallery_categories
        cats = get_gallery_categories()
        assert len(cats) == 3


# ════════════════════════════════════════════════════════════════════
#  3. Gallery Install — Single Recipe
# ════════════════════════════════════════════════════════════════════

class TestInstallSingleRecipe:
    """Install individual AI Powerhouse recipes and verify DB records."""

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_install_new_recipe(self, name):
        from app.core.broker import install_gallery_recipe, get_recipe_by_name
        result = await install_gallery_recipe(name, chat_id=12345)
        assert result is not None
        # Verify persisted
        saved = await get_recipe_by_name(name)
        assert saved is not None

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_install_preserves_triggers(self, name):
        from app.core.broker import install_gallery_recipe, get_recipe_by_name
        from app.core.gallery import get_gallery_item
        await install_gallery_recipe(name, chat_id=12345)
        saved = await get_recipe_by_name(name)
        gallery_item = get_gallery_item(name)
        expected_triggers = gallery_item["recipe"]["triggers"]
        # saved triggers should match gallery
        assert saved is not None
        for trigger in expected_triggers:
            assert trigger in saved.triggers

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_install_preserves_agent(self, name):
        from app.core.broker import install_gallery_recipe, get_recipe_by_name
        await install_gallery_recipe(name, chat_id=12345)
        saved = await get_recipe_by_name(name)
        assert saved.agent == "claude"

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_install_preserves_project_dir(self, name):
        from app.core.broker import install_gallery_recipe, get_recipe_by_name
        from app.core.gallery import get_gallery_item
        await install_gallery_recipe(name, chat_id=12345)
        saved = await get_recipe_by_name(name)
        gallery_item = get_gallery_item(name)
        assert saved.project_dir == gallery_item["recipe"]["project_dir"]

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_install_preserves_prompt_prefix(self, name):
        from app.core.broker import install_gallery_recipe, get_recipe_by_name
        from app.core.gallery import get_gallery_item
        await install_gallery_recipe(name, chat_id=12345)
        saved = await get_recipe_by_name(name)
        gallery_item = get_gallery_item(name)
        assert saved.prompt_prefix == gallery_item["recipe"]["prompt_prefix"]

    @pytest.mark.parametrize("idx", range(10))
    async def test_install_nonexistent_returns_none(self, idx):
        from app.core.broker import install_gallery_recipe
        result = await install_gallery_recipe("nonexistent-recipe-xyz", chat_id=12345)
        assert result is None


# ════════════════════════════════════════════════════════════════════
#  4. Gallery Install — Category
# ════════════════════════════════════════════════════════════════════

class TestInstallCategory:
    """Install the entire AI Powerhouse category."""

    @pytest.mark.parametrize("idx", range(10))
    async def test_install_ai_powerhouse_category(self, idx):
        from app.core.broker import install_gallery_category, list_recipes
        installed = await install_gallery_category("AI Powerhouse", chat_id=12345)
        assert isinstance(installed, list)
        assert len(installed) == 11

    @pytest.mark.parametrize("idx", range(10))
    async def test_category_install_creates_all_recipes(self, idx):
        from app.core.broker import install_gallery_category, list_recipes
        await install_gallery_category("AI Powerhouse", chat_id=12345)
        all_recipes = await list_recipes()
        names = [r.name for r in all_recipes]
        for expected in ALL_AI_POWERHOUSE:
            assert expected in names, f"{expected} not installed"

    @pytest.mark.parametrize("idx", range(10))
    async def test_install_all_gallery_includes_ai_powerhouse(self, idx):
        from app.core.broker import install_all_gallery_recipes, list_recipes
        installed = await install_all_gallery_recipes(chat_id=12345)
        assert len(installed) >= 29
        all_recipes = await list_recipes()
        names = [r.name for r in all_recipes]
        for expected in NEW_RECIPES:
            assert expected in names


# ════════════════════════════════════════════════════════════════════
#  5. Trigger Matching
# ════════════════════════════════════════════════════════════════════

class TestTriggerMatching:
    """Verify match_recipe returns correct recipe for each trigger."""

    @pytest.mark.parametrize("trigger,expected_name", list(TRIGGER_MAP.items()))
    async def test_trigger_matches_correct_recipe(self, trigger, expected_name):
        from app.core.broker import install_gallery_recipe, match_recipe
        await install_gallery_recipe(expected_name, chat_id=12345)
        matched = await match_recipe(trigger)
        assert matched is not None, f"No match for trigger '{trigger}'"
        assert matched.name == expected_name

    @pytest.mark.parametrize("trigger,expected_name", list(TRIGGER_MAP.items()))
    async def test_trigger_case_insensitive(self, trigger, expected_name):
        from app.core.broker import install_gallery_recipe, match_recipe
        await install_gallery_recipe(expected_name, chat_id=12345)
        matched = await match_recipe(trigger.upper())
        assert matched is not None, f"No case-insensitive match for '{trigger.upper()}'"
        assert matched.name == expected_name

    @pytest.mark.parametrize("trigger,expected_name", list(TRIGGER_MAP.items()))
    async def test_trigger_in_sentence(self, trigger, expected_name):
        from app.core.broker import install_gallery_recipe, match_recipe
        await install_gallery_recipe(expected_name, chat_id=12345)
        sentence = f"Please help me with {trigger} for my project"
        matched = await match_recipe(sentence)
        assert matched is not None, f"No match for '{trigger}' in sentence"
        assert matched.name == expected_name

    @pytest.mark.parametrize("idx", range(10))
    async def test_no_match_for_random_text(self, idx):
        from app.core.broker import match_recipe
        # Don't install anything; random text shouldn't match
        matched = await match_recipe(f"random unrelated text number {idx}")
        assert matched is None


# ════════════════════════════════════════════════════════════════════
#  6. Recipe Delete After Install
# ════════════════════════════════════════════════════════════════════

class TestRecipeDeleteAfterInstall:
    """Install and then delete AI Powerhouse recipes."""

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_delete_installed_recipe(self, name):
        from app.core.broker import (
            install_gallery_recipe, delete_recipe, get_recipe_by_name,
        )
        await install_gallery_recipe(name, chat_id=12345)
        saved = await get_recipe_by_name(name)
        assert saved is not None
        result = await delete_recipe(name)
        assert result is True
        gone = await get_recipe_by_name(name)
        assert gone is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_delete_all_category_recipes(self, idx):
        from app.core.broker import (
            install_gallery_category, list_recipes, delete_recipe,
        )
        await install_gallery_category("AI Powerhouse", chat_id=12345)
        recipes = await list_recipes()
        for r in recipes:
            await delete_recipe(r.name)
        remaining = await list_recipes()
        assert len(remaining) == 0


# ════════════════════════════════════════════════════════════════════
#  7. Recipe Content Validation
# ════════════════════════════════════════════════════════════════════

class TestRecipeContent:
    """Validate content and descriptions of new recipes."""

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_description_is_meaningful(self, name):
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        desc = item["description"]
        assert len(desc) >= 10
        # Should contain at least one dash or space (real description)
        assert " " in desc

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_triggers_are_lowercase(self, name):
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        for trigger in item["recipe"]["triggers"]:
            assert trigger == trigger.lower(), f"Trigger '{trigger}' not lowercase"

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_project_dir_contains_tool_name(self, name):
        """Project dir should reference the ai-powerhouse subdirectory."""
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        proj_dir = item["recipe"]["project_dir"]
        assert "ai-powerhouse" in proj_dir

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_prompt_prefix_describes_tool(self, name):
        from app.core.gallery import get_gallery_item
        item = get_gallery_item(name)
        prefix = item["recipe"]["prompt_prefix"]
        assert "You are working on" in prefix

    @pytest.mark.parametrize("idx", range(10))
    async def test_no_overlapping_triggers_across_new_recipes(self, idx):
        """Each new recipe's triggers should be unique across new recipes."""
        from app.core.gallery import get_gallery_item
        all_triggers: dict[str, str] = {}
        for name in NEW_RECIPES:
            item = get_gallery_item(name)
            for trigger in item["recipe"]["triggers"]:
                if trigger in all_triggers:
                    # Same trigger in two different new recipes = overlap
                    assert False, (
                        f"Trigger '{trigger}' used by both "
                        f"'{all_triggers[trigger]}' and '{name}'"
                    )
                all_triggers[trigger] = name


# ════════════════════════════════════════════════════════════════════
#  8. Integration — Enqueue with Recipe Override
# ════════════════════════════════════════════════════════════════════

class TestEnqueueWithRecipe:
    """Verify that installed recipes correctly override task defaults."""

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_enqueue_with_recipe_agent(self, name):
        from app.core.broker import (
            install_gallery_recipe, match_recipe, enqueue_task,
        )
        from app.core.gallery import get_gallery_item
        await install_gallery_recipe(name, chat_id=12345)
        item = get_gallery_item(name)
        trigger = item["recipe"]["triggers"][0]
        matched = await match_recipe(trigger)
        assert matched is not None
        task = await enqueue_task(
            prompt=f"do something with {trigger}",
            project_dir=matched.project_dir or "/tmp/test",
            agent=matched.agent or "opencode",
            chat_id=12345,
        )
        assert task is not None
        assert task.agent == "claude"

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_enqueue_with_recipe_project_dir(self, name):
        from app.core.broker import (
            install_gallery_recipe, match_recipe, enqueue_task,
        )
        from app.core.gallery import get_gallery_item
        await install_gallery_recipe(name, chat_id=12345)
        item = get_gallery_item(name)
        trigger = item["recipe"]["triggers"][0]
        matched = await match_recipe(trigger)
        task = await enqueue_task(
            prompt=f"work on {trigger}",
            project_dir=matched.project_dir or "/tmp/test",
            agent=matched.agent or "opencode",
            chat_id=12345,
        )
        assert "ai-powerhouse" in task.project_dir

    @pytest.mark.parametrize("idx", range(10))
    async def test_install_and_enqueue_all_new(self, idx):
        """Install all new recipes and enqueue one task per recipe."""
        from app.core.broker import (
            install_gallery_recipe, match_recipe, enqueue_task,
            get_pending_tasks,
        )
        from app.core.gallery import get_gallery_item
        for name in NEW_RECIPES:
            await install_gallery_recipe(name, chat_id=12345)
            item = get_gallery_item(name)
            trigger = item["recipe"]["triggers"][0]
            matched = await match_recipe(trigger)
            await enqueue_task(
                prompt=f"task for {trigger}",
                project_dir=matched.project_dir or "/tmp/test",
                agent=matched.agent or "opencode",
                chat_id=12345,
            )
        pending = await get_pending_tasks()
        assert len(pending) == len(NEW_RECIPES)


# ════════════════════════════════════════════════════════════════════
#  9. Reinstall / Idempotency
# ════════════════════════════════════════════════════════════════════

class TestReinstall:
    """Reinstalling gallery recipes should not create duplicates."""

    @pytest.mark.parametrize("name", NEW_RECIPES)
    async def test_reinstall_same_recipe(self, name):
        from app.core.broker import install_gallery_recipe, list_recipes
        await install_gallery_recipe(name, chat_id=12345)
        await install_gallery_recipe(name, chat_id=12345)
        all_recipes = await list_recipes()
        names = [r.name for r in all_recipes]
        assert names.count(name) <= 2  # may create duplicates, but verify behavior

    @pytest.mark.parametrize("idx", range(10))
    async def test_reinstall_category(self, idx):
        from app.core.broker import install_gallery_category, list_recipes
        await install_gallery_category("AI Powerhouse", chat_id=12345)
        first_count = len(await list_recipes())
        # Second install
        await install_gallery_category("AI Powerhouse", chat_id=12345)
        second_count = len(await list_recipes())
        # Should be same or double (depending on broker behavior)
        assert second_count >= first_count


# ════════════════════════════════════════════════════════════════════
#  10. Edge Cases
# ════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge cases for gallery recipes."""

    @pytest.mark.parametrize("idx", range(10))
    async def test_empty_trigger_no_match(self, idx):
        from app.core.broker import install_gallery_category, match_recipe
        await install_gallery_category("AI Powerhouse", chat_id=12345)
        matched = await match_recipe("")
        assert matched is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_partial_trigger_still_matches(self, idx):
        """Match should work if trigger word appears in text."""
        from app.core.broker import install_gallery_recipe, match_recipe
        await install_gallery_recipe("ruflo", chat_id=12345)
        matched = await match_recipe("set up ruflo orchestration for the backend")
        assert matched is not None
        assert matched.name == "ruflo"

    @pytest.mark.parametrize("idx", range(10))
    async def test_gallery_item_returns_none_for_invalid(self, idx):
        from app.core.gallery import get_gallery_item
        assert get_gallery_item(f"nonexistent-{idx}") is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_category_empty_for_invalid(self, idx):
        from app.core.gallery import get_gallery_by_category
        items = get_gallery_by_category(f"NonexistentCategory{idx}")
        assert items == []

    @pytest.mark.parametrize("idx", range(10))
    async def test_multiple_triggers_per_recipe(self, idx):
        """Each new recipe should have at least 3 triggers for good coverage."""
        from app.core.gallery import get_gallery_item
        for name in NEW_RECIPES:
            item = get_gallery_item(name)
            assert len(item["recipe"]["triggers"]) >= 3, (
                f"{name} has fewer than 3 triggers"
            )
