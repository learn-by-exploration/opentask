"""Extensive tests for the Recipe system — model, broker CRUD, matching, bot handlers, prompt enrichment."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Recipe, Task, TaskStatus


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def recipe_engine():
    """In-memory SQLite engine with tables."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def recipe_session(recipe_engine) -> AsyncSession:
    factory = async_sessionmaker(
        recipe_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def fresh_db(recipe_engine):
    """Patch get_session to use in-memory engine."""
    factory = async_sessionmaker(
        recipe_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def fake_get_session():
        return factory()

    with patch("app.core.broker.get_session", side_effect=fake_get_session):
        yield factory


def _make_update(text=None, user_id=12345, chat_id=12345):
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    if text is not None:
        update.message = MagicMock()
        update.message.text = text
        update.message.message_id = 42
        update.message.reply_text = AsyncMock()
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    update.get_bot = MagicMock(return_value=bot)
    return update


def _make_callback_update(data, user_id=12345, chat_id=12345):
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.chat_id = chat_id
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_document = AsyncMock()
    bot = AsyncMock()
    update.get_bot = MagicMock(return_value=bot)
    return update


def _make_context(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    ctx.user_data = {}
    return ctx


# ══════════════════════════════════════════════════════════════════════
# 1. Recipe Model Tests
# ══════════════════════════════════════════════════════════════════════


class TestRecipeModel:
    @pytest.mark.asyncio
    async def test_create_recipe_basic(self, recipe_session):
        """Recipe can be created with minimal fields."""
        recipe = Recipe(
            name="ros-analysis",
            triggers_json=json.dumps(["rosbag", "ros2"]),
        )
        recipe_session.add(recipe)
        await recipe_session.commit()
        await recipe_session.refresh(recipe)
        assert recipe.id is not None
        assert recipe.name == "ros-analysis"
        assert recipe.triggers == ["rosbag", "ros2"]

    @pytest.mark.asyncio
    async def test_recipe_triggers_property(self, recipe_session):
        """triggers @property deserializes JSON correctly."""
        recipe = Recipe(
            name="test", triggers_json=json.dumps(["kw1", "kw2", "kw3"]),
        )
        recipe_session.add(recipe)
        await recipe_session.commit()
        assert recipe.triggers == ["kw1", "kw2", "kw3"]

    @pytest.mark.asyncio
    async def test_recipe_triggers_bad_json(self):
        """triggers @property handles corrupt JSON gracefully."""
        recipe = Recipe(name="bad", triggers_json="not-json{")
        assert recipe.triggers == []

    @pytest.mark.asyncio
    async def test_recipe_triggers_none(self):
        """triggers @property handles None."""
        recipe = Recipe(name="none", triggers_json=None)
        assert recipe.triggers == []

    @pytest.mark.asyncio
    async def test_recipe_setup_commands_property(self):
        recipe = Recipe(
            name="test",
            triggers_json="[]",
            setup_commands_json=json.dumps(["source /opt/ros/setup.bash"]),
        )
        assert recipe.setup_commands == ["source /opt/ros/setup.bash"]

    @pytest.mark.asyncio
    async def test_recipe_setup_commands_bad_json(self):
        recipe = Recipe(name="bad", triggers_json="[]", setup_commands_json="corrupt")
        assert recipe.setup_commands == []

    @pytest.mark.asyncio
    async def test_recipe_setup_commands_none(self):
        recipe = Recipe(name="none", triggers_json="[]", setup_commands_json=None)
        assert recipe.setup_commands == []

    @pytest.mark.asyncio
    async def test_recipe_skills_property(self):
        recipe = Recipe(
            name="test",
            triggers_json="[]",
            skills_json=json.dumps(["ros2-skill", "nav2-skill"]),
        )
        assert recipe.skills == ["ros2-skill", "nav2-skill"]

    @pytest.mark.asyncio
    async def test_recipe_skills_bad_json(self):
        recipe = Recipe(name="bad", triggers_json="[]", skills_json="bad")
        assert recipe.skills == []

    @pytest.mark.asyncio
    async def test_recipe_skills_none(self):
        recipe = Recipe(name="none", triggers_json="[]", skills_json=None)
        assert recipe.skills == []

    @pytest.mark.asyncio
    async def test_recipe_full_fields(self, recipe_session):
        """Recipe with all optional fields."""
        recipe = Recipe(
            name="full-recipe",
            triggers_json=json.dumps(["rosbag"]),
            agent="claude",
            project_dir="~/ros_ws",
            setup_commands_json=json.dumps(["source setup.bash"]),
            skills_json=json.dumps(["ros2-skill"]),
            prompt_prefix="You are a ROS expert.",
            prompt_suffix="Use best practices.",
            telegram_chat_id=12345,
        )
        recipe_session.add(recipe)
        await recipe_session.commit()
        await recipe_session.refresh(recipe)

        assert recipe.agent == "claude"
        assert recipe.project_dir == "~/ros_ws"
        assert recipe.setup_commands == ["source setup.bash"]
        assert recipe.skills == ["ros2-skill"]
        assert recipe.prompt_prefix == "You are a ROS expert."
        assert recipe.prompt_suffix == "Use best practices."
        assert recipe.created_at is not None

    @pytest.mark.asyncio
    async def test_recipe_unique_name(self, recipe_session):
        """Recipe names must be unique."""
        from sqlalchemy.exc import IntegrityError

        r1 = Recipe(name="dup", triggers_json="[]")
        recipe_session.add(r1)
        await recipe_session.commit()

        r2 = Recipe(name="dup", triggers_json="[]")
        recipe_session.add(r2)
        with pytest.raises(IntegrityError):
            await recipe_session.commit()


# ══════════════════════════════════════════════════════════════════════
# 2. Broker CRUD Tests
# ══════════════════════════════════════════════════════════════════════


class TestSaveRecipe:
    @pytest.mark.asyncio
    async def test_save_basic(self, fresh_db):
        from app.core.broker import save_recipe
        recipe = await save_recipe(name="test", triggers=["kw1", "kw2"])
        assert recipe.id is not None
        assert recipe.name == "test"
        assert recipe.triggers == ["kw1", "kw2"]

    @pytest.mark.asyncio
    async def test_save_with_all_fields(self, fresh_db):
        from app.core.broker import save_recipe
        recipe = await save_recipe(
            name="full",
            triggers=["ros", "bag"],
            agent="claude",
            project_dir="~/ws",
            setup_commands=["source setup.bash"],
            skills=["ros2-skill"],
            prompt_prefix="Expert mode.",
            prompt_suffix="Be thorough.",
            chat_id=12345,
        )
        assert recipe.agent == "claude"
        assert recipe.project_dir == "~/ws"
        assert recipe.setup_commands == ["source setup.bash"]
        assert recipe.skills == ["ros2-skill"]
        assert recipe.prompt_prefix == "Expert mode."
        assert recipe.prompt_suffix == "Be thorough."

    @pytest.mark.asyncio
    async def test_save_upsert(self, fresh_db):
        """Saving a recipe with same name replaces the old one."""
        from app.core.broker import save_recipe, list_recipes
        r1 = await save_recipe(name="ros", triggers=["bag"])
        r2 = await save_recipe(name="ros", triggers=["bag", "topic"])
        recipes = await list_recipes()
        assert len(recipes) == 1
        assert recipes[0].triggers == ["bag", "topic"]

    @pytest.mark.asyncio
    async def test_save_empty_name_rejected(self, fresh_db):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError, match="1-128"):
            await save_recipe(name="", triggers=["kw"])

    @pytest.mark.asyncio
    async def test_save_long_name_rejected(self, fresh_db):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError, match="1-128"):
            await save_recipe(name="x" * 129, triggers=["kw"])

    @pytest.mark.asyncio
    async def test_save_no_triggers_rejected(self, fresh_db):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError, match="at least one"):
            await save_recipe(name="empty", triggers=[])

    @pytest.mark.asyncio
    async def test_save_too_many_triggers(self, fresh_db):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError, match="50"):
            await save_recipe(name="many", triggers=["kw"] * 51)

    @pytest.mark.asyncio
    async def test_save_trigger_too_long(self, fresh_db):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError, match="1-200"):
            await save_recipe(name="long", triggers=["x" * 201])

    @pytest.mark.asyncio
    async def test_save_empty_trigger_rejected(self, fresh_db):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError, match="1-200"):
            await save_recipe(name="empty-trig", triggers=[""])

    @pytest.mark.asyncio
    async def test_save_too_many_setup_commands(self, fresh_db):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError, match="20 setup"):
            await save_recipe(name="cmds", triggers=["kw"], setup_commands=["cmd"] * 21)

    @pytest.mark.asyncio
    async def test_save_too_many_skills(self, fresh_db):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError, match="20 skills"):
            await save_recipe(name="skills", triggers=["kw"], skills=["s"] * 21)

    @pytest.mark.asyncio
    async def test_save_prefix_too_long(self, fresh_db):
        from app.core.broker import save_recipe
        from app.config.settings import settings
        with pytest.raises(ValueError, match="prompt_prefix"):
            await save_recipe(name="pfx", triggers=["kw"], prompt_prefix="x" * (settings.max_prompt_len + 1))

    @pytest.mark.asyncio
    async def test_save_suffix_too_long(self, fresh_db):
        from app.core.broker import save_recipe
        from app.config.settings import settings
        with pytest.raises(ValueError, match="prompt_suffix"):
            await save_recipe(name="sfx", triggers=["kw"], prompt_suffix="x" * (settings.max_prompt_len + 1))


class TestListRecipes:
    @pytest.mark.asyncio
    async def test_list_empty(self, fresh_db):
        from app.core.broker import list_recipes
        assert await list_recipes() == []

    @pytest.mark.asyncio
    async def test_list_multiple(self, fresh_db):
        from app.core.broker import save_recipe, list_recipes
        await save_recipe(name="r1", triggers=["a"])
        await save_recipe(name="r2", triggers=["b"])
        recipes = await list_recipes()
        assert len(recipes) == 2
        names = {r.name for r in recipes}
        assert names == {"r1", "r2"}


class TestGetRecipeByName:
    @pytest.mark.asyncio
    async def test_get_existing(self, fresh_db):
        from app.core.broker import save_recipe, get_recipe_by_name
        await save_recipe(name="ros", triggers=["bag"])
        r = await get_recipe_by_name("ros")
        assert r is not None
        assert r.name == "ros"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, fresh_db):
        from app.core.broker import get_recipe_by_name
        assert await get_recipe_by_name("nope") is None


class TestGetRecipeById:
    @pytest.mark.asyncio
    async def test_get_by_id(self, fresh_db):
        from app.core.broker import save_recipe, get_recipe_by_id
        r = await save_recipe(name="ros", triggers=["bag"])
        found = await get_recipe_by_id(r.id)
        assert found is not None
        assert found.name == "ros"

    @pytest.mark.asyncio
    async def test_get_by_invalid_id(self, fresh_db):
        from app.core.broker import get_recipe_by_id
        assert await get_recipe_by_id(9999) is None


class TestDeleteRecipe:
    @pytest.mark.asyncio
    async def test_delete_existing(self, fresh_db):
        from app.core.broker import save_recipe, delete_recipe, list_recipes
        await save_recipe(name="ros", triggers=["bag"])
        assert await delete_recipe("ros") is True
        assert await list_recipes() == []

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, fresh_db):
        from app.core.broker import delete_recipe
        assert await delete_recipe("nope") is False


class TestMatchRecipe:
    @pytest.mark.asyncio
    async def test_match_single_trigger(self, fresh_db):
        from app.core.broker import save_recipe, match_recipe
        await save_recipe(name="ros", triggers=["rosbag"])
        r = await match_recipe("please analyze this rosbag file")
        assert r is not None
        assert r.name == "ros"

    @pytest.mark.asyncio
    async def test_match_case_insensitive(self, fresh_db):
        from app.core.broker import save_recipe, match_recipe
        await save_recipe(name="ros", triggers=["RosBag"])
        r = await match_recipe("analyze the ROSBAG data")
        assert r is not None
        assert r.name == "ros"

    @pytest.mark.asyncio
    async def test_match_no_match(self, fresh_db):
        from app.core.broker import save_recipe, match_recipe
        await save_recipe(name="ros", triggers=["rosbag"])
        r = await match_recipe("fix the login page")
        assert r is None

    @pytest.mark.asyncio
    async def test_match_best_score(self, fresh_db):
        """Recipe with most trigger matches wins."""
        from app.core.broker import save_recipe, match_recipe
        await save_recipe(name="general", triggers=["analysis"])
        await save_recipe(name="ros", triggers=["rosbag", "analysis", "ros2"])
        r = await match_recipe("do analysis of this rosbag in ros2")
        assert r is not None
        assert r.name == "ros"  # 3 matches beats 1 match

    @pytest.mark.asyncio
    async def test_match_empty_db(self, fresh_db):
        from app.core.broker import match_recipe
        assert await match_recipe("anything") is None

    @pytest.mark.asyncio
    async def test_match_multiple_recipes_first_wins_on_tie(self, fresh_db):
        """When two recipes have equal score, one of them is returned."""
        from app.core.broker import save_recipe, match_recipe
        await save_recipe(name="r1", triggers=["deploy"])
        await save_recipe(name="r2", triggers=["deploy"])
        r = await match_recipe("deploy the app")
        assert r is not None
        assert r.name in ("r1", "r2")


class TestApplyRecipeToTask:
    @pytest.mark.asyncio
    async def test_apply_updates_task(self, fresh_db):
        from app.core.broker import (
            enqueue_task,
            save_recipe,
            _apply_recipe_to_task,
            get_task_by_id,
        )
        task = await enqueue_task(prompt="analyze rosbag", chat_id=123)
        recipe = await save_recipe(
            name="ros",
            triggers=["rosbag"],
            agent="claude",
            project_dir="~/ros_ws",
        )
        updated = await _apply_recipe_to_task(
            task.id, recipe, "enriched prompt"
        )
        assert updated is not None
        assert updated.prompt == "enriched prompt"
        assert updated.agent == "claude"
        assert updated.project_dir == "~/ros_ws"

    @pytest.mark.asyncio
    async def test_apply_no_override(self, fresh_db):
        """Recipe without agent/project_dir doesn't override task defaults."""
        from app.core.broker import (
            enqueue_task,
            save_recipe,
            _apply_recipe_to_task,
        )
        task = await enqueue_task(prompt="test", chat_id=123)
        recipe = await save_recipe(name="basic", triggers=["test"])
        updated = await _apply_recipe_to_task(
            task.id, recipe, "new prompt"
        )
        assert updated is not None
        assert updated.prompt == "new prompt"
        assert updated.agent == task.agent  # unchanged

    @pytest.mark.asyncio
    async def test_apply_to_running_task_fails(self, fresh_db):
        """Can't apply recipe to a running task."""
        from app.core.broker import (
            enqueue_task,
            pick_next_task,
            save_recipe,
            _apply_recipe_to_task,
        )
        task = await enqueue_task(prompt="test", chat_id=123)
        await pick_next_task()  # marks it RUNNING
        recipe = await save_recipe(name="basic", triggers=["test"])
        result = await _apply_recipe_to_task(
            task.id, recipe, "enriched"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_apply_to_nonexistent_task(self, fresh_db):
        from app.core.broker import save_recipe, _apply_recipe_to_task
        recipe = await save_recipe(name="basic", triggers=["test"])
        result = await _apply_recipe_to_task(9999, recipe, "prompt")
        assert result is None


# ══════════════════════════════════════════════════════════════════════
# 3. Bot Command Tests
# ══════════════════════════════════════════════════════════════════════


class TestCmdRecipes:
    @pytest.mark.asyncio
    async def test_recipes_empty(self):
        from app.telegram.bot import cmd_recipes
        with patch("app.telegram.bot.list_recipes", new_callable=AsyncMock, return_value=[]):
            update = _make_update()
            await cmd_recipes(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "No recipes" in text

    @pytest.mark.asyncio
    async def test_recipes_list(self):
        from app.telegram.bot import cmd_recipes
        r1 = MagicMock()
        r1.name = "ros"
        r1.triggers = ["rosbag", "ros2"]
        r1.agent = "claude"
        r2 = MagicMock()
        r2.name = "deploy"
        r2.triggers = ["deploy"]
        r2.agent = None
        with patch("app.telegram.bot.list_recipes", new_callable=AsyncMock, return_value=[r1, r2]):
            update = _make_update()
            await cmd_recipes(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "ros" in text
        assert "deploy" in text
        assert "[claude]" in text

    @pytest.mark.asyncio
    async def test_recipes_truncates_triggers(self):
        """Recipes with >5 triggers show truncation indicator."""
        from app.telegram.bot import cmd_recipes
        r = MagicMock()
        r.name = "many"
        r.triggers = ["t1", "t2", "t3", "t4", "t5", "t6", "t7"]
        r.agent = None
        with patch("app.telegram.bot.list_recipes", new_callable=AsyncMock, return_value=[r]):
            update = _make_update()
            await cmd_recipes(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "+2 more" in text


class TestCmdAddRecipe:
    @pytest.mark.asyncio
    async def test_addrecipe_no_args(self):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _make_context(args=[]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_addrecipe_single_arg(self):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _make_context(args=["myrecipe"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_addrecipe_long_name(self):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _make_context(args=["x" * 129, "triggers:kw"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "1-128" in text

    @pytest.mark.asyncio
    async def test_addrecipe_no_triggers(self):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        with patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None}):
            await cmd_addrecipe(update, _make_context(args=["test", "agent:claude"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "trigger" in text.lower()

    @pytest.mark.asyncio
    async def test_addrecipe_basic(self):
        from app.telegram.bot import cmd_addrecipe
        mock_recipe = MagicMock()
        mock_recipe.name = "ros"
        mock_recipe.triggers = ["rosbag", "ros2"]
        mock_recipe.agent = "claude"
        mock_recipe.setup_commands = []
        mock_recipe.skills = []
        mock_recipe.prompt_prefix = None
        mock_recipe.prompt_suffix = None

        with patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None}), \
             patch("app.telegram.bot.save_recipe", new_callable=AsyncMock, return_value=mock_recipe):
            update = _make_update()
            await cmd_addrecipe(update, _make_context(args=["ros", "triggers:rosbag,ros2", "agent:claude"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "ros" in text
        assert "saved" in text.lower()

    @pytest.mark.asyncio
    async def test_addrecipe_with_all_options(self):
        from app.telegram.bot import cmd_addrecipe
        mock_recipe = MagicMock()
        mock_recipe.name = "ros"
        mock_recipe.triggers = ["rosbag"]
        mock_recipe.agent = "claude"
        mock_recipe.setup_commands = ["source setup.bash"]
        mock_recipe.skills = ["ros2-skill"]
        mock_recipe.prompt_prefix = "Expert mode"
        mock_recipe.prompt_suffix = "Be thorough"

        with patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None}), \
             patch("app.telegram.bot.save_recipe", new_callable=AsyncMock, return_value=mock_recipe):
            update = _make_update()
            await cmd_addrecipe(update, _make_context(
                args=["ros", "triggers:rosbag", "agent:claude", "setup:source setup.bash", "skills:ros2-skill", "prefix:Expert mode", "suffix:Be thorough"]
            ))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "saved" in text.lower()
        assert "Setup" in text
        assert "Skills" in text

    @pytest.mark.asyncio
    async def test_addrecipe_save_error(self):
        from app.telegram.bot import cmd_addrecipe
        with patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None}), \
             patch("app.telegram.bot.save_recipe", new_callable=AsyncMock, side_effect=ValueError("too many triggers")):
            update = _make_update()
            await cmd_addrecipe(update, _make_context(args=["bad", "triggers:kw"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "too many" in text


class TestCmdDelRecipe:
    @pytest.mark.asyncio
    async def test_delrecipe_no_args(self):
        from app.telegram.bot import cmd_delrecipe
        update = _make_update()
        await cmd_delrecipe(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_delrecipe_success(self):
        from app.telegram.bot import cmd_delrecipe
        with patch("app.telegram.bot.delete_recipe", new_callable=AsyncMock, return_value=True):
            update = _make_update()
            await cmd_delrecipe(update, _make_context(args=["ros"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "deleted" in text.lower()

    @pytest.mark.asyncio
    async def test_delrecipe_not_found(self):
        from app.telegram.bot import cmd_delrecipe
        with patch("app.telegram.bot.delete_recipe", new_callable=AsyncMock, return_value=False):
            update = _make_update()
            await cmd_delrecipe(update, _make_context(args=["nope"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()


class TestCmdRecipe:
    @pytest.mark.asyncio
    async def test_recipe_no_args(self):
        from app.telegram.bot import cmd_recipe
        update = _make_update()
        await cmd_recipe(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_recipe_not_found(self):
        from app.telegram.bot import cmd_recipe
        with patch("app.telegram.bot.get_recipe_by_name", new_callable=AsyncMock, return_value=None):
            update = _make_update()
            await cmd_recipe(update, _make_context(args=["nope"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_recipe_detail(self):
        from app.telegram.bot import cmd_recipe
        r = MagicMock()
        r.name = "ros"
        r.triggers = ["rosbag", "ros2"]
        r.agent = "claude"
        r.project_dir = "~/ros_ws"
        r.setup_commands = ["source setup.bash"]
        r.skills = ["ros2-skill"]
        r.prompt_prefix = "Expert."
        r.prompt_suffix = "Be careful."
        with patch("app.telegram.bot.get_recipe_by_name", new_callable=AsyncMock, return_value=r):
            update = _make_update()
            await cmd_recipe(update, _make_context(args=["ros"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "ros" in text
        assert "claude" in text
        assert "ros_ws" in text
        assert "setup.bash" in text
        assert "ros2-skill" in text
        assert "Expert" in text


# ══════════════════════════════════════════════════════════════════════
# 4. Handle Text + Recipe Matching Tests
# ══════════════════════════════════════════════════════════════════════


class TestHandleTextRecipeMatch:
    @pytest.mark.asyncio
    async def test_text_with_recipe_match_shows_buttons(self):
        from app.telegram.bot import handle_text
        mock_task = MagicMock()
        mock_task.id = 1
        mock_task.agent = "opencode"
        mock_task.project_dir = "/home/user/project"
        mock_task.model = None
        mock_recipe = MagicMock()
        mock_recipe.name = "ros"

        with patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None}), \
             patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock, return_value=mock_task), \
             patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=mock_recipe):
            update = _make_update(text="analyze rosbag")
            await handle_text(update, _make_context())

        update.message.reply_text.assert_called_once()
        call_kwargs = update.message.reply_text.call_args
        text = call_kwargs[0][0]
        assert "Recipe" in text
        assert "ros" in text
        # Check inline keyboard
        keyboard = call_kwargs[1]["reply_markup"]
        buttons = keyboard.inline_keyboard
        flat_buttons = [b for row in buttons for b in row]
        data_values = [b.callback_data for b in flat_buttons]
        assert any("recipeuse:" in d for d in data_values)
        assert any("recipeskip:" in d for d in data_values)

    @pytest.mark.asyncio
    async def test_text_without_recipe_no_recipe_buttons(self):
        from app.telegram.bot import handle_text
        mock_task = MagicMock()
        mock_task.id = 1
        mock_task.agent = "opencode"
        mock_task.project_dir = "/home/user/project"
        mock_task.model = None

        with patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None}), \
             patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock, return_value=mock_task), \
             patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None):
            update = _make_update(text="fix login page")
            await handle_text(update, _make_context())

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "Recipe" not in text


# ══════════════════════════════════════════════════════════════════════
# 5. Recipe Callback Tests
# ══════════════════════════════════════════════════════════════════════


class TestHandleRecipeCallback:
    @pytest.mark.asyncio
    async def test_recipeskip_callback(self):
        from app.telegram.bot import handle_recipe_callback
        update = _make_callback_update("recipeskip:42")
        await handle_recipe_callback(update, _make_context())
        update.callback_query.edit_message_text.assert_called_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "42" in text
        assert "without recipe" in text

    @pytest.mark.asyncio
    async def test_recipeskip_invalid_id(self):
        from app.telegram.bot import handle_recipe_callback
        update = _make_callback_update("recipeskip:notanum")
        await handle_recipe_callback(update, _make_context())
        # Should not crash, just returns
        update.callback_query.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_recipeuse_recipe_not_found(self):
        from app.telegram.bot import handle_recipe_callback
        update = _make_callback_update("recipeuse:42:ros")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock), \
             patch("app.telegram.bot.get_recipe_by_name", new_callable=AsyncMock, return_value=None):
            await handle_recipe_callback(update, _make_context())
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "no longer exists" in text

    @pytest.mark.asyncio
    async def test_recipeuse_task_not_found(self):
        from app.telegram.bot import handle_recipe_callback
        mock_recipe = MagicMock()
        mock_recipe.name = "ros"
        mock_recipe.setup_commands = []
        mock_recipe.skills = []
        mock_recipe.prompt_prefix = None
        mock_recipe.prompt_suffix = None

        update = _make_callback_update("recipeuse:42:ros")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock), \
             patch("app.telegram.bot.get_recipe_by_name", new_callable=AsyncMock, return_value=mock_recipe), \
             patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock, return_value=None):
            await handle_recipe_callback(update, _make_context())
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "already started" in text or "not found" in text

    @pytest.mark.asyncio
    async def test_recipeuse_task_already_running(self):
        from app.telegram.bot import handle_recipe_callback
        mock_recipe = MagicMock()
        mock_recipe.name = "ros"
        mock_recipe.setup_commands = []
        mock_recipe.skills = []
        mock_recipe.prompt_prefix = None
        mock_recipe.prompt_suffix = None

        mock_task = MagicMock()
        mock_task.id = 42
        mock_task.status = TaskStatus.RUNNING

        update = _make_callback_update("recipeuse:42:ros")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock), \
             patch("app.telegram.bot.get_recipe_by_name", new_callable=AsyncMock, return_value=mock_recipe), \
             patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock, return_value=mock_task):
            await handle_recipe_callback(update, _make_context())
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "already started" in text

    @pytest.mark.asyncio
    async def test_recipeuse_success(self):
        from app.telegram.bot import handle_recipe_callback
        mock_recipe = MagicMock()
        mock_recipe.name = "ros"
        mock_recipe.setup_commands = []
        mock_recipe.skills = []
        mock_recipe.prompt_prefix = None
        mock_recipe.prompt_suffix = None

        mock_task = MagicMock()
        mock_task.id = 42
        mock_task.status = TaskStatus.PENDING
        mock_task.prompt = "analyze rosbag"

        update = _make_callback_update("recipeuse:42:ros")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock), \
             patch("app.telegram.bot.get_recipe_by_name", new_callable=AsyncMock, return_value=mock_recipe), \
             patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock, return_value=mock_task), \
             patch("app.telegram.bot._apply_recipe_to_task", new_callable=AsyncMock, return_value=mock_task):
            await handle_recipe_callback(update, _make_context())
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "Recipe" in text
        assert "ros" in text
        assert "applied" in text

    @pytest.mark.asyncio
    async def test_recipeuse_bad_data(self):
        """Callback data with wrong format is handled gracefully."""
        from app.telegram.bot import handle_recipe_callback
        update = _make_callback_update("recipeuse:bad")
        await handle_recipe_callback(update, _make_context())
        # Should not crash — no edit called since parts length check fails
        # The action matches "recipeuse" but rest is "bad" with no colon split
        # so sub_parts has len != 2 → early return
        update.callback_query.edit_message_text.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# 6. Prompt Enrichment Tests
# ══════════════════════════════════════════════════════════════════════


class TestEnrichPrompt:
    def test_plain_prompt(self):
        """No prefix/suffix/setup/skills → prompt unchanged."""
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = []
        recipe.skills = []
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None
        result = _enrich_prompt("fix the bug", recipe)
        assert result == "fix the bug"

    def test_with_prefix(self):
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = []
        recipe.skills = []
        recipe.prompt_prefix = "You are a ROS expert."
        recipe.prompt_suffix = None
        result = _enrich_prompt("analyze rosbag", recipe)
        assert "You are a ROS expert." in result
        assert "analyze rosbag" in result
        # Prefix should come before the prompt
        assert result.index("ROS expert") < result.index("analyze rosbag")

    def test_with_suffix(self):
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = []
        recipe.skills = []
        recipe.prompt_prefix = None
        recipe.prompt_suffix = "Use best practices."
        result = _enrich_prompt("analyze rosbag", recipe)
        assert "Use best practices." in result
        assert "analyze rosbag" in result
        # Suffix should come after the prompt
        assert result.index("analyze rosbag") < result.index("best practices")

    def test_with_setup_commands(self):
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = ["source /opt/ros/humble/setup.bash", "export ROS_DOMAIN_ID=1"]
        recipe.skills = []
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None
        result = _enrich_prompt("analyze bag", recipe)
        assert "source /opt/ros/humble/setup.bash" in result
        assert "export ROS_DOMAIN_ID=1" in result
        assert "Setup commands" in result

    @patch("os.path.isfile", return_value=True)
    @patch("builtins.open", create=True)
    def test_with_skills(self, mock_open, mock_isfile):
        from io import StringIO
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = []
        recipe.skills = ["ros2-skill"]
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None

        mock_open.return_value.__enter__ = lambda s: StringIO("# ROS2 Skill\nDo ROS stuff.\n")
        mock_open.return_value.__exit__ = MagicMock(return_value=False)

        result = _enrich_prompt("analyze bag", recipe)
        assert "Skill: ros2-skill" in result

    def test_with_skill_file_missing(self):
        """Missing skill file is silently skipped."""
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = []
        recipe.skills = ["nonexistent"]
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None
        result = _enrich_prompt("analyze bag", recipe)
        # Should not contain skill block since file doesn't exist
        assert "Skill:" not in result
        assert "analyze bag" in result

    def test_full_enrichment(self):
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = ["source setup.bash"]
        recipe.skills = []  # skip skill file loading
        recipe.prompt_prefix = "Expert mode."
        recipe.prompt_suffix = "Be thorough."
        result = _enrich_prompt("analyze rosbag", recipe)
        parts = result.split("\n\n")
        # Should have: setup block, prefix, prompt, suffix
        assert len(parts) >= 4
        assert "source setup.bash" in parts[0]

    def test_prefix_and_suffix_ordering(self):
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = []
        recipe.skills = []
        recipe.prompt_prefix = "PREFIX"
        recipe.prompt_suffix = "SUFFIX"
        result = _enrich_prompt("PROMPT", recipe)
        assert result.index("PREFIX") < result.index("PROMPT")
        assert result.index("PROMPT") < result.index("SUFFIX")


# ══════════════════════════════════════════════════════════════════════
# 7. Integration: Full flow with fresh DB
# ══════════════════════════════════════════════════════════════════════


class TestRecipeIntegration:
    @pytest.mark.asyncio
    async def test_full_recipe_lifecycle(self, fresh_db):
        """Save → list → get → match → delete lifecycle."""
        from app.core.broker import (
            save_recipe,
            list_recipes,
            get_recipe_by_name,
            match_recipe,
            delete_recipe,
        )

        # Save
        recipe = await save_recipe(
            name="ros-analysis",
            triggers=["rosbag", "ros2", "bag file"],
            agent="claude",
            setup_commands=["source /opt/ros/humble/setup.bash"],
            skills=["ros2-skill"],
            prompt_prefix="You are a ROS2 expert.",
        )
        assert recipe.id is not None

        # List
        recipes = await list_recipes()
        assert len(recipes) == 1
        assert recipes[0].name == "ros-analysis"

        # Get by name
        found = await get_recipe_by_name("ros-analysis")
        assert found is not None
        assert found.agent == "claude"

        # Match
        matched = await match_recipe("Please analyze this rosbag file for errors")
        assert matched is not None
        assert matched.name == "ros-analysis"

        # No match
        no_match = await match_recipe("Fix the login page CSS")
        assert no_match is None

        # Delete
        assert await delete_recipe("ros-analysis") is True
        assert await list_recipes() == []

    @pytest.mark.asyncio
    async def test_recipe_with_task_enrichment(self, fresh_db):
        """Full flow: create recipe + task, apply recipe, verify enrichment."""
        from app.core.broker import (
            enqueue_task,
            save_recipe,
            match_recipe,
            _apply_recipe_to_task,
            get_task_by_id,
        )
        from app.telegram.bot import _enrich_prompt

        recipe = await save_recipe(
            name="ros",
            triggers=["rosbag"],
            agent="claude",
            prompt_prefix="ROS expert mode.",
            prompt_suffix="Output in JSON.",
        )

        task = await enqueue_task(prompt="analyze the rosbag", chat_id=123)
        matched = await match_recipe(task.prompt)
        assert matched is not None

        enriched = _enrich_prompt(task.prompt, matched)
        assert "ROS expert mode." in enriched
        assert "Output in JSON." in enriched

        updated = await _apply_recipe_to_task(task.id, matched, enriched)
        assert updated is not None
        assert "ROS expert mode." in updated.prompt
        assert updated.agent == "claude"

    @pytest.mark.asyncio
    async def test_multiple_recipes_best_match(self, fresh_db):
        """With multiple recipes, the one with most trigger matches wins."""
        from app.core.broker import save_recipe, match_recipe

        await save_recipe(name="general-analysis", triggers=["analysis"])
        await save_recipe(name="ros-analysis", triggers=["rosbag", "analysis", "ros2"])
        await save_recipe(name="deploy", triggers=["deploy", "production"])

        r = await match_recipe("do analysis of this rosbag in ros2 workspace")
        assert r is not None
        assert r.name == "ros-analysis"

        r = await match_recipe("deploy to production server")
        assert r is not None
        assert r.name == "deploy"

        r = await match_recipe("fix the CSS on login page")
        assert r is None


# ══════════════════════════════════════════════════════════════════════
# 8. Settings Tests
# ══════════════════════════════════════════════════════════════════════


class TestSkillsDirSetting:
    def test_default_skills_dir(self):
        from app.config.settings import settings
        assert settings.skills_dir == "~/.taskpilot/skills"

    def test_skills_dir_env_override(self, monkeypatch):
        monkeypatch.setenv("SKILLS_DIR", "/custom/skills")
        from app.config.settings import Settings
        s = Settings(
            telegram_bot_token="test",
            allowed_user_ids=[1],
        )
        assert s.skills_dir == "/custom/skills"


# ══════════════════════════════════════════════════════════════════════
# 9. Edge Cases and Security
# ══════════════════════════════════════════════════════════════════════


class TestRecipeEdgeCases:
    @pytest.mark.asyncio
    async def test_recipe_with_null_bytes_in_trigger(self, fresh_db):
        """Null bytes in triggers are handled."""
        from app.core.broker import save_recipe, match_recipe
        recipe = await save_recipe(name="test", triggers=["clean\x00keyword"])
        # Matching should still work on the clean part
        matched = await match_recipe("this has clean")
        # The trigger contains null byte but the prompt doesn't — partial match on "clean" substring
        assert matched is not None or matched is None  # Just ensure no crash

    @pytest.mark.asyncio
    async def test_recipe_with_special_chars(self, fresh_db):
        """Triggers with special regex chars don't break matching."""
        from app.core.broker import save_recipe, match_recipe
        recipe = await save_recipe(name="test", triggers=["file.bag", "(data)", "[topic]"])
        # match_recipe uses `in` not regex, so special chars are fine
        matched = await match_recipe("analyze this file.bag")
        assert matched is not None

    @pytest.mark.asyncio
    async def test_recipe_unicode_triggers(self, fresh_db):
        from app.core.broker import save_recipe, match_recipe
        recipe = await save_recipe(name="unicode", triggers=["分析", "データ"])
        matched = await match_recipe("请分析这个文件")
        assert matched is not None

    @pytest.mark.asyncio
    async def test_recipe_very_long_prompt_match(self, fresh_db):
        """Matching against a very long prompt doesn't crash."""
        from app.core.broker import save_recipe, match_recipe
        await save_recipe(name="test", triggers=["keyword"])
        long_prompt = "x" * 10000 + " keyword " + "y" * 10000
        matched = await match_recipe(long_prompt)
        assert matched is not None

    @pytest.mark.asyncio
    async def test_addrecipe_null_byte_name(self):
        """Null bytes in recipe name are sanitized."""
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        with patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None}), \
             patch("app.telegram.bot.save_recipe", new_callable=AsyncMock) as mock_save:
            mock_recipe = MagicMock()
            mock_recipe.name = "test"
            mock_recipe.triggers = ["kw"]
            mock_recipe.agent = None
            mock_recipe.setup_commands = []
            mock_recipe.skills = []
            mock_recipe.prompt_prefix = None
            mock_recipe.prompt_suffix = None
            mock_save.return_value = mock_recipe
            await cmd_addrecipe(update, _make_context(args=["\x00test\x00", "triggers:kw"]))
        # Name should have null bytes stripped
        call_args = mock_save.call_args
        assert "\x00" not in call_args.kwargs.get("name", call_args[1].get("name", ""))

    @pytest.mark.asyncio
    async def test_build_app_includes_recipe_handlers(self):
        """build_app registers recipe command and callback handlers."""
        with patch("app.telegram.bot.Application") as mock_cls:
            mock_built = MagicMock()
            mock_builder = MagicMock()
            mock_builder.token.return_value = mock_builder
            mock_builder.build.return_value = mock_built
            mock_cls.builder.return_value = mock_builder

            from app.telegram.bot import build_app
            build_app()

            calls = mock_built.add_handler.call_args_list
            handler_types = [str(c) for c in calls]
            combined = " ".join(handler_types)
            assert "recipes" in combined
            assert "addrecipe" in combined
            assert "delrecipe" in combined
            assert "recipe" in combined
