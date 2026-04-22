"""Extensive tests for model selection feature across Task, ChatPrefs, Recipe, Runner, and Bot."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, ChatPrefs, Recipe, Task, TaskStatus


# ── Fixtures ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()


def _patch_broker_session(engine):
    """Patch get_session to use our in-memory engine."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get():
        return factory()

    return patch("app.core.broker.get_session", side_effect=_get)


# ── Model: Task.model field ────────────────────────────────────────

class TestTaskModelField:
    @pytest.mark.asyncio
    async def test_task_model_default_none(self, session):
        task = Task(prompt="test", project_dir="/tmp", agent="opencode")
        session.add(task)
        await session.flush()
        await session.refresh(task)
        assert task.model is None

    @pytest.mark.asyncio
    async def test_task_model_set(self, session):
        task = Task(prompt="test", project_dir="/tmp", agent="opencode", model="sonnet")
        session.add(task)
        await session.flush()
        await session.refresh(task)
        assert task.model == "sonnet"

    @pytest.mark.asyncio
    async def test_task_model_update(self, session):
        task = Task(prompt="test", project_dir="/tmp", agent="claude", model="haiku")
        session.add(task)
        await session.flush()
        task.model = "opus"
        await session.flush()
        await session.refresh(task)
        assert task.model == "opus"

    @pytest.mark.asyncio
    async def test_task_model_clear(self, session):
        task = Task(prompt="test", project_dir="/tmp", agent="claude", model="sonnet")
        session.add(task)
        await session.flush()
        task.model = None
        await session.flush()
        await session.refresh(task)
        assert task.model is None

    @pytest.mark.asyncio
    async def test_task_model_long_value(self, session):
        model = "anthropic/claude-sonnet-4-20260514"
        task = Task(prompt="test", project_dir="/tmp", agent="claude", model=model)
        session.add(task)
        await session.flush()
        await session.refresh(task)
        assert task.model == model


# ── Model: ChatPrefs.model field ───────────────────────────────────

class TestChatPrefsModelField:
    @pytest.mark.asyncio
    async def test_prefs_model_default_none(self, session):
        prefs = ChatPrefs(chat_id=100)
        session.add(prefs)
        await session.flush()
        await session.refresh(prefs)
        assert prefs.model is None

    @pytest.mark.asyncio
    async def test_prefs_model_set(self, session):
        prefs = ChatPrefs(chat_id=101, model="opus")
        session.add(prefs)
        await session.flush()
        await session.refresh(prefs)
        assert prefs.model == "opus"


# ── Model: Recipe.model field ──────────────────────────────────────

class TestRecipeModelField:
    @pytest.mark.asyncio
    async def test_recipe_model_default_none(self, session):
        recipe = Recipe(name="test", triggers_json='["kw"]')
        session.add(recipe)
        await session.flush()
        await session.refresh(recipe)
        assert recipe.model is None

    @pytest.mark.asyncio
    async def test_recipe_model_set(self, session):
        recipe = Recipe(name="test2", triggers_json='["kw"]', model="haiku")
        session.add(recipe)
        await session.flush()
        await session.refresh(recipe)
        assert recipe.model == "haiku"


# ── Broker: enqueue_task with model ────────────────────────────────

class TestEnqueueTaskModel:
    @pytest.mark.asyncio
    async def test_enqueue_no_model(self, engine):
        from app.core.broker import enqueue_task
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None):
            task = await enqueue_task(prompt="test", project_dir="/tmp", agent="opencode")
        assert task.model is None

    @pytest.mark.asyncio
    async def test_enqueue_with_model(self, engine):
        from app.core.broker import enqueue_task
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None):
            task = await enqueue_task(prompt="test", project_dir="/tmp", agent="claude", model="sonnet")
        assert task.model == "anthropic/claude-sonnet-4"

    @pytest.mark.asyncio
    async def test_enqueue_empty_model_becomes_none(self, engine):
        from app.core.broker import enqueue_task
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None):
            task = await enqueue_task(prompt="test", project_dir="/tmp", agent="opencode", model="")
        assert task.model is None

    @pytest.mark.asyncio
    async def test_enqueue_default_model_from_settings(self, engine):
        from app.core.broker import enqueue_task
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None), \
             patch("app.core.broker.settings") as mock_settings:
            mock_settings.max_queue_size = 20
            mock_settings.default_project_dir = "/tmp"
            mock_settings.default_agent = "opencode"
            mock_settings.default_model = "opus"
            mock_settings.cost_budget_daily = 0
            mock_settings.resolve_model.return_value = "anthropic/claude-opus-4"
            task = await enqueue_task(prompt="test")
        assert task.model == "anthropic/claude-opus-4"


# ── Broker: switch_task_model ──────────────────────────────────────

class TestSwitchTaskModel:
    @pytest.mark.asyncio
    async def test_switch_model_success(self, engine):
        from app.core.broker import enqueue_task, switch_task_model
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None):
            task = await enqueue_task(prompt="test", project_dir="/tmp", agent="claude")
            updated = await switch_task_model(task.id, "opus")
        assert updated is not None
        assert updated.model == "anthropic/claude-opus-4"

    @pytest.mark.asyncio
    async def test_switch_model_clear(self, engine):
        from app.core.broker import enqueue_task, switch_task_model
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None):
            task = await enqueue_task(prompt="test", project_dir="/tmp", agent="claude", model="sonnet")
            updated = await switch_task_model(task.id, "")
        assert updated is not None
        assert updated.model is None

    @pytest.mark.asyncio
    async def test_switch_model_nonexistent_task(self, engine):
        from app.core.broker import switch_task_model
        with _patch_broker_session(engine):
            result = await switch_task_model(9999, "opus")
        assert result is None

    @pytest.mark.asyncio
    async def test_switch_model_running_task(self, engine, session):
        from app.core.broker import switch_task_model
        task = Task(prompt="test", project_dir="/tmp", agent="claude", status=TaskStatus.RUNNING, model="sonnet")
        session.add(task)
        await session.flush()
        await session.refresh(task)
        with _patch_broker_session(engine):
            result = await switch_task_model(task.id, "opus")
        assert result is None


# ── Broker: get/set_chat_pref with model ───────────────────────────

class TestChatPrefsModel:
    @pytest.mark.asyncio
    async def test_get_prefs_includes_model(self, engine):
        from app.core.broker import get_chat_prefs
        with _patch_broker_session(engine):
            prefs = await get_chat_prefs(999)
        assert "model" in prefs
        assert prefs["model"] is None

    @pytest.mark.asyncio
    async def test_set_and_get_model_pref(self, engine):
        from app.core.broker import get_chat_prefs, set_chat_pref
        with _patch_broker_session(engine):
            await set_chat_pref(888, model="sonnet")
            prefs = await get_chat_prefs(888)
        assert prefs["model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_set_model_pref_preserves_agent(self, engine):
        from app.core.broker import get_chat_prefs, set_chat_pref
        with _patch_broker_session(engine):
            await set_chat_pref(777, agent="claude")
            await set_chat_pref(777, model="opus")
            prefs = await get_chat_prefs(777)
        assert prefs["agent"] == "claude"
        assert prefs["model"] == "opus"


# ── Broker: save_recipe with model ─────────────────────────────────

class TestSaveRecipeModel:
    @pytest.mark.asyncio
    async def test_save_recipe_with_model(self, engine):
        from app.core.broker import save_recipe
        with _patch_broker_session(engine):
            recipe = await save_recipe(name="r1", triggers=["kw"], model="opus")
        assert recipe.model == "opus"

    @pytest.mark.asyncio
    async def test_save_recipe_no_model(self, engine):
        from app.core.broker import save_recipe
        with _patch_broker_session(engine):
            recipe = await save_recipe(name="r2", triggers=["kw"])
        assert recipe.model is None


# ── Broker: _apply_recipe_to_task with model ───────────────────────

class TestApplyRecipeModel:
    @pytest.mark.asyncio
    async def test_apply_recipe_sets_model(self, engine):
        from app.core.broker import _apply_recipe_to_task, enqueue_task
        recipe = Recipe(name="r1", triggers_json='["kw"]', agent="claude", model="opus")
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None):
            task = await enqueue_task(prompt="test", project_dir="/tmp", agent="opencode")
            updated = await _apply_recipe_to_task(task.id, recipe, "enriched")
        assert updated is not None
        assert updated.model == "opus"
        assert updated.agent == "claude"

    @pytest.mark.asyncio
    async def test_apply_recipe_no_model_no_override(self, engine):
        from app.core.broker import _apply_recipe_to_task, enqueue_task
        recipe = Recipe(name="r2", triggers_json='["kw"]')
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None):
            task = await enqueue_task(prompt="test", project_dir="/tmp", agent="opencode", model="haiku")
            updated = await _apply_recipe_to_task(task.id, recipe, "enriched")
        assert updated is not None
        assert updated.model == "anthropic/claude-haiku-4"  # original model preserved


# ── Runner: _build_command with model ──────────────────────────────

class TestBuildCommandModel:
    def test_build_command_no_model(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.agent = "opencode"
        task.model = None
        task.prompt = "fix bug"
        task.project_dir = "/tmp/proj"
        task.parent_task_id = None
        argv = runner._build_command(task)
        assert "--model" not in " ".join(argv)
        assert "opencode" == argv[0]
        assert "run" == argv[1]

    def test_build_command_with_model(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.agent = "opencode"
        task.model = "anthropic/claude-sonnet-4"
        task.prompt = "fix bug"
        task.project_dir = "/tmp/proj"
        task.parent_task_id = None
        argv = runner._build_command(task)
        assert argv[0] == "opencode"
        assert "--model" in argv
        model_idx = argv.index("--model")
        assert argv[model_idx + 1] == "anthropic/claude-sonnet-4"

    def test_build_command_claude_with_model(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.agent = "claude"
        task.model = "opus"
        task.prompt = "test"
        task.project_dir = "/tmp"
        task.parent_task_id = None
        argv = runner._build_command(task)
        assert argv[0] == "claude"
        assert "--model" in argv
        model_idx = argv.index("--model")
        assert argv[model_idx + 1] == "opus"

    def test_build_command_model_before_prompt(self):
        """Model flag must come after subcommand, before prompt.

        Regression: previously --model was at argv[1], producing:
            opencode --model haiku run "hello"  (BROKEN)
        Must be:
            opencode run --model haiku "hello"  (CORRECT)
        """
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.agent = "opencode"
        task.model = "haiku"
        task.prompt = "hello"
        task.project_dir = "/tmp"
        task.parent_task_id = None
        argv = runner._build_command(task)
        assert argv[0] == "opencode"
        assert argv[1] == "run"  # subcommand stays in position
        idx_model = argv.index("--model")
        idx_prompt = argv.index("hello")
        assert idx_model < idx_prompt, f"--model must come before prompt: {argv}"

    def test_build_command_empty_model_no_flag(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.agent = "opencode"
        task.model = ""
        task.prompt = "test"
        task.project_dir = "/tmp"
        task.parent_task_id = None
        argv = runner._build_command(task)
        assert "--model" not in argv

    def test_build_command_unknown_agent_no_model_flag(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.agent = "unknown"
        task.model = "sonnet"
        task.prompt = "test"
        task.project_dir = "/tmp"
        task.parent_task_id = None
        argv = runner._build_command(task)
        assert argv[0] == "echo"

    def test_build_command_model_with_slash(self):
        """Model names with / (provider/model format) should stay intact."""
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.agent = "opencode"
        task.model = "openai/gpt-5-nano"
        task.prompt = "test"
        task.project_dir = "/tmp"
        task.parent_task_id = None
        argv = runner._build_command(task)
        model_idx = argv.index("--model")
        assert argv[model_idx + 1] == "openai/gpt-5-nano"

    def test_build_command_model_no_agent_model_flag_template(self):
        """If agent has no model flag template, model is silently ignored."""
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.agent = "opencode"
        task.model = "sonnet"
        task.prompt = "test"
        task.project_dir = "/tmp"
        task.parent_task_id = None
        with patch("app.core.runner.settings") as mock_settings:
            mock_settings.agent_commands = {"opencode": "opencode run {prompt}"}
            mock_settings.agent_model_flags = {}  # no model flag for opencode
            mock_settings.task_timeout_seconds = 1800
            argv = runner._build_command(task)
        assert "--model" not in argv


# ── Bot: cmd_model ─────────────────────────────────────────────────

def _make_update(chat_id=12345, user_id=12345, text="", args=None):
    update = MagicMock(spec=["effective_chat", "effective_user", "message", "callback_query", "get_bot"])
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    update.message.message_id = 1
    update.message.text = text
    update.callback_query = None
    bot = AsyncMock()
    update.get_bot.return_value = bot
    ctx = MagicMock()
    ctx.args = args or []
    return update, ctx


def _get_reply(update) -> str:
    """Extract the reply text from either _send (get_bot().send_message) or reply_text."""
    bot = update.get_bot()
    if bot.send_message.called:
        return bot.send_message.call_args.kwargs.get("text", "")
    if update.message.reply_text.called:
        args = update.message.reply_text.call_args
        return args[0][0] if args[0] else args.kwargs.get("text", "")
    return ""


class TestCmdModel:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_set_model(self, mock_prefs, mock_set, *_):
        from app.telegram.bot import cmd_model, _chat_model
        update, ctx = _make_update(args=["sonnet"])
        _chat_model.pop(12345, None)
        await cmd_model(update, ctx)
        assert _chat_model[12345] == "sonnet"
        mock_set.assert_called_once_with(12345, model="sonnet")
        assert "sonnet" in _get_reply(update)

    @pytest.mark.asyncio
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_clear_model_none(self, mock_prefs, mock_set, *_):
        from app.telegram.bot import cmd_model, _chat_model
        _chat_model[12345] = "sonnet"
        update, ctx = _make_update(args=["none"])
        await cmd_model(update, ctx)
        assert 12345 not in _chat_model
        mock_set.assert_called_once_with(12345, model="")

    @pytest.mark.asyncio
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_clear_model_default(self, mock_prefs, mock_set, *_):
        from app.telegram.bot import cmd_model, _chat_model
        _chat_model[12345] = "opus"
        update, ctx = _make_update(args=["default"])
        await cmd_model(update, ctx)
        assert 12345 not in _chat_model

    @pytest.mark.asyncio
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_clear_model_reset(self, mock_prefs, mock_set, *_):
        from app.telegram.bot import cmd_model, _chat_model
        _chat_model[12345] = "opus"
        update, ctx = _make_update(args=["reset"])
        await cmd_model(update, ctx)
        assert 12345 not in _chat_model

    @pytest.mark.asyncio
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_show_current_model_set(self, mock_prefs, mock_set, *_):
        from app.telegram.bot import cmd_model, _chat_model
        _chat_model[12345] = "opus"
        update, ctx = _make_update(args=[])
        await cmd_model(update, ctx)
        reply = _get_reply(update)
        assert "opus" in reply

    @pytest.mark.asyncio
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_show_current_model_default(self, mock_prefs, mock_set, *_):
        from app.telegram.bot import cmd_model, _chat_model
        _chat_model.pop(12345, None)
        update, ctx = _make_update(args=[])
        await cmd_model(update, ctx)
        reply = _get_reply(update)
        assert "default" in reply.lower()


# ── Bot: cmd_setmodel ──────────────────────────────────────────────

class TestCmdSetmodel:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.switch_task_model", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_setmodel_success(self, mock_prefs, mock_switch, *_):
        updated = MagicMock()
        updated.model = "opus"
        mock_switch.return_value = updated
        update, ctx = _make_update(args=["42", "opus"])
        from app.telegram.bot import cmd_setmodel
        await cmd_setmodel(update, ctx)
        mock_switch.assert_called_once_with(42, "opus")
        assert "opus" in _get_reply(update)

    @pytest.mark.asyncio
    @patch("app.telegram.bot.switch_task_model", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_setmodel_clear(self, mock_prefs, mock_switch, *_):
        updated = MagicMock()
        updated.model = None
        mock_switch.return_value = updated
        update, ctx = _make_update(args=["42", "none"])
        from app.telegram.bot import cmd_setmodel
        await cmd_setmodel(update, ctx)
        mock_switch.assert_called_once_with(42, "")

    @pytest.mark.asyncio
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_setmodel_no_args(self, mock_prefs, *_):
        update, ctx = _make_update(args=[])
        from app.telegram.bot import cmd_setmodel
        await cmd_setmodel(update, ctx)
        assert "Usage" in _get_reply(update)

    @pytest.mark.asyncio
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_setmodel_invalid_id(self, mock_prefs, *_):
        update, ctx = _make_update(args=["abc", "sonnet"])
        from app.telegram.bot import cmd_setmodel
        await cmd_setmodel(update, ctx)
        assert "Invalid" in _get_reply(update)

    @pytest.mark.asyncio
    @patch("app.telegram.bot.switch_task_model", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_setmodel_task_not_found(self, mock_prefs, mock_switch, *_):
        update, ctx = _make_update(args=["999", "opus"])
        from app.telegram.bot import cmd_setmodel
        await cmd_setmodel(update, ctx)
        assert "already started" in _get_reply(update)


# ── Bot: handle_text passes model ──────────────────────────────────

class TestHandleTextModel:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_enqueue_passes_model(self, mock_prefs, mock_enqueue, mock_match, *_):
        from app.telegram.bot import handle_text, _chat_model
        _chat_model[12345] = "opus"
        task = MagicMock()
        task.id = 1
        task.agent = "opencode"
        task.model = "opus"
        task.project_dir = "/tmp"
        mock_enqueue.return_value = task
        update, ctx = _make_update(text="fix it")
        update.message.text = "fix it"
        await handle_text(update, ctx)
        call_kwargs = mock_enqueue.call_args
        assert call_kwargs[1].get("model") == "opus" or call_kwargs.kwargs.get("model") == "opus"

    @pytest.mark.asyncio
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_enqueue_no_model(self, mock_prefs, mock_enqueue, mock_match, *_):
        from app.telegram.bot import handle_text, _chat_model
        _chat_model.pop(12345, None)
        task = MagicMock()
        task.id = 1
        task.agent = "opencode"
        task.model = None
        task.project_dir = "/tmp"
        mock_enqueue.return_value = task
        update, ctx = _make_update(text="fix it")
        update.message.text = "fix it"
        await handle_text(update, ctx)
        call_kwargs = mock_enqueue.call_args
        assert call_kwargs[1].get("model") is None or call_kwargs.kwargs.get("model") is None

    @pytest.mark.asyncio
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_model_button_shown(self, mock_prefs, mock_enqueue, mock_match, *_):
        from app.telegram.bot import handle_text, _chat_model
        _chat_model.pop(12345, None)
        task = MagicMock()
        task.id = 1
        task.agent = "opencode"
        task.model = None
        task.project_dir = "/tmp"
        mock_enqueue.return_value = task
        update, ctx = _make_update(text="fix it")
        update.message.text = "fix it"
        await handle_text(update, ctx)
        reply_call = update.message.reply_text
        assert reply_call.called
        # Check InlineKeyboardMarkup has model button
        kwargs = reply_call.call_args[1] if reply_call.call_args[1] else {}
        markup = kwargs.get("reply_markup")
        assert markup is not None
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        model_btns = [b for b in all_buttons if "modelswitch:" in (b.callback_data or "")]
        assert len(model_btns) >= 3  # sonnet, opus, haiku shown directly


# ── Bot: handle_model_set_callback ─────────────────────────────────

class TestHandleModelSetCallback:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_shows_model_choices(self, mock_prefs, mock_get, *_):
        task = MagicMock()
        task.id = 42
        task.status = TaskStatus.PENDING
        task.agent = "claude"
        task.model = "sonnet"
        task.project_dir = "/tmp"
        mock_get.return_value = task
        from app.telegram.bot import handle_model_set_callback
        update = MagicMock()
        query = MagicMock()
        query.answer = AsyncMock()
        query.data = "modelset:42"
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        update.effective_user.id = 12345
        ctx = MagicMock()
        await handle_model_set_callback(update, ctx)
        query.edit_message_text.assert_called_once()
        kwargs = query.edit_message_text.call_args[1] if query.edit_message_text.call_args[1] else {}
        markup = kwargs.get("reply_markup")
        assert markup is not None
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        labels = [b.text for b in all_buttons]
        assert any("sonnet" in l.lower() for l in labels)
        assert any("opus" in l.lower() for l in labels)
        assert any("Default" in l for l in labels)

    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_task_not_found(self, mock_prefs, mock_get, *_):
        from app.telegram.bot import handle_model_set_callback
        update = MagicMock()
        query = MagicMock()
        query.answer = AsyncMock()
        query.data = "modelset:999"
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        update.effective_user.id = 12345
        ctx = MagicMock()
        await handle_model_set_callback(update, ctx)
        assert "already started" in query.edit_message_text.call_args[0][0]

    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_task_already_running(self, mock_prefs, mock_get, *_):
        task = MagicMock()
        task.status = TaskStatus.RUNNING
        mock_get.return_value = task
        from app.telegram.bot import handle_model_set_callback
        update = MagicMock()
        query = MagicMock()
        query.answer = AsyncMock()
        query.data = "modelset:42"
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        update.effective_user.id = 12345
        ctx = MagicMock()
        await handle_model_set_callback(update, ctx)
        assert "already started" in query.edit_message_text.call_args[0][0]


# ── Bot: handle_model_switch_callback ──────────────────────────────

class TestHandleModelSwitchCallback:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.switch_task_model", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_switch_model_success(self, mock_prefs, mock_switch, *_):
        updated = MagicMock()
        updated.id = 42
        updated.agent = "claude"
        updated.model = "opus"
        updated.project_dir = "/tmp"
        mock_switch.return_value = updated
        from app.telegram.bot import handle_model_switch_callback
        update = MagicMock()
        query = MagicMock()
        query.answer = AsyncMock()
        query.data = "modelswitch:42:opus"
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        update.effective_user.id = 12345
        ctx = MagicMock()
        await handle_model_switch_callback(update, ctx)
        mock_switch.assert_called_once_with(42, "opus")
        assert "opus" in query.edit_message_text.call_args[0][0]

    @pytest.mark.asyncio
    @patch("app.telegram.bot.switch_task_model", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_switch_to_default(self, mock_prefs, mock_switch, *_):
        updated = MagicMock()
        updated.id = 42
        updated.agent = "claude"
        updated.model = None
        updated.project_dir = "/tmp"
        mock_switch.return_value = updated
        from app.telegram.bot import handle_model_switch_callback
        update = MagicMock()
        query = MagicMock()
        query.answer = AsyncMock()
        query.data = "modelswitch:42:__default__"
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        update.effective_user.id = 12345
        ctx = MagicMock()
        await handle_model_switch_callback(update, ctx)
        mock_switch.assert_called_once_with(42, "")
        assert "default" in query.edit_message_text.call_args[0][0].lower()

    @pytest.mark.asyncio
    @patch("app.telegram.bot.switch_task_model", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_switch_model_task_gone(self, mock_prefs, mock_switch, *_):
        from app.telegram.bot import handle_model_switch_callback
        update = MagicMock()
        query = MagicMock()
        query.answer = AsyncMock()
        query.data = "modelswitch:42:opus"
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        update.effective_user.id = 12345
        ctx = MagicMock()
        await handle_model_switch_callback(update, ctx)
        assert "already started" in query.edit_message_text.call_args[0][0]


# ── Bot: _load_prefs loads model ───────────────────────────────────

class TestLoadPrefsModel:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_load_prefs_caches_model(self, mock_get):
        mock_get.return_value = {"project_dir": None, "agent": None, "model": "opus"}
        from app.telegram.bot import _load_prefs, _chat_model
        _chat_model.pop(55555, None)
        from app.telegram.bot import _chat_project_dir, _chat_agent
        _chat_project_dir.pop(55555, None)
        _chat_agent.pop(55555, None)
        await _load_prefs(55555)
        assert _chat_model[55555] == "opus"

    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_load_prefs_no_model(self, mock_get):
        mock_get.return_value = {"project_dir": None, "agent": None, "model": None}
        from app.telegram.bot import _load_prefs, _chat_model
        _chat_model.pop(55556, None)
        from app.telegram.bot import _chat_project_dir, _chat_agent
        _chat_project_dir.pop(55556, None)
        _chat_agent.pop(55556, None)
        await _load_prefs(55556)
        assert 55556 not in _chat_model


# ── Bot: _model helper ────────────────────────────────────────────

class TestModelHelper:
    def test_model_from_cache(self):
        from app.telegram.bot import _model, _chat_model
        _chat_model[77777] = "haiku"
        assert _model(77777) == "haiku"
        _chat_model.pop(77777, None)

    def test_model_default_empty(self):
        from app.telegram.bot import _model, _chat_model
        _chat_model.pop(77778, None)
        result = _model(77778)
        # Should be empty or settings.default_model
        assert isinstance(result, str)


# ── Settings: agent_model_flags ────────────────────────────────────

class TestSettingsModelFlags:
    def test_default_model_empty(self):
        from app.config.settings import Settings
        s = Settings(telegram_bot_token="test", allowed_user_ids=[1])
        assert s.default_model == ""

    def test_agent_model_flags_present(self):
        from app.config.settings import Settings
        s = Settings(telegram_bot_token="test", allowed_user_ids=[1])
        assert "opencode" in s.agent_model_flags
        assert "claude" in s.agent_model_flags
        assert "{model}" in s.agent_model_flags["opencode"]

    def test_default_model_override(self):
        from app.config.settings import Settings
        s = Settings(
            telegram_bot_token="test",
            allowed_user_ids=[1],
            default_model="sonnet",
        )
        assert s.default_model == "sonnet"


# ── Bot: build_app includes model handlers ─────────────────────────

class TestBuildAppModel:
    def test_build_app_has_model_handlers(self):
        from app.telegram.bot import build_app
        app = build_app()
        handler_patterns = []
        for group in app.handlers.values():
            for h in group:
                if hasattr(h, "pattern"):
                    handler_patterns.append(h.pattern.pattern if hasattr(h.pattern, "pattern") else str(h.pattern))
                if hasattr(h, "commands"):
                    handler_patterns.extend(h.commands)
        assert "model" in handler_patterns
        assert "setmodel" in handler_patterns

    def test_build_app_has_model_callback_handlers(self):
        from app.telegram.bot import build_app
        app = build_app()
        patterns = []
        for group in app.handlers.values():
            for h in group:
                if hasattr(h, "pattern"):
                    p = h.pattern.pattern if hasattr(h.pattern, "pattern") else str(h.pattern)
                    patterns.append(p)
        assert any("modelset" in p for p in patterns)
        assert any("modelswitch" in p for p in patterns)
        assert any("workerswitch" in p for p in patterns)


# ── Bot: recipe addrecipe with model ───────────────────────────────

class TestAddRecipeWithModel:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.save_recipe", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_addrecipe_with_model(self, mock_prefs, mock_save, *_):
        recipe = MagicMock()
        recipe.name = "r1"
        recipe.triggers = ["kw"]
        recipe.agent = "claude"
        recipe.model = "opus"
        recipe.setup_commands = []
        recipe.skills = []
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None
        mock_save.return_value = recipe
        update, ctx = _make_update(args=["r1", "triggers:kw", "agent:claude", "model:opus"])
        from app.telegram.bot import cmd_addrecipe
        await cmd_addrecipe(update, ctx)
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["model"] == "opus"
        reply = _get_reply(update)
        assert "Model: opus" in reply

    @pytest.mark.asyncio
    @patch("app.telegram.bot.save_recipe", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_addrecipe_without_model(self, mock_prefs, mock_save, *_):
        recipe = MagicMock()
        recipe.name = "r2"
        recipe.triggers = ["kw"]
        recipe.agent = None
        recipe.model = None
        recipe.setup_commands = []
        recipe.skills = []
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None
        mock_save.return_value = recipe
        update, ctx = _make_update(args=["r2", "triggers:kw"])
        from app.telegram.bot import cmd_addrecipe
        await cmd_addrecipe(update, ctx)
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["model"] is None


# ── Bot: recipe detail shows model ─────────────────────────────────

class TestRecipeDetailModel:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_recipe_by_name", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_recipe_detail_shows_model(self, mock_prefs, mock_get, *_):
        recipe = MagicMock()
        recipe.name = "r1"
        recipe.triggers = ["kw"]
        recipe.agent = "claude"
        recipe.model = "opus"
        recipe.project_dir = None
        recipe.setup_commands = []
        recipe.skills = []
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None
        mock_get.return_value = recipe
        update, ctx = _make_update(args=["r1"])
        from app.telegram.bot import cmd_recipe
        await cmd_recipe(update, ctx)
        reply = _get_reply(update)
        assert "opus" in reply


# ── Integration: full model lifecycle ──────────────────────────────

class TestModelIntegration:
    @pytest.mark.asyncio
    async def test_task_with_model_lifecycle(self, engine, session):
        """Create task with model, verify it persists and can be switched."""
        from app.core.broker import enqueue_task, switch_task_model, get_task_by_id
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None):
            task = await enqueue_task(prompt="test", project_dir="/tmp", agent="claude", model="sonnet")
            assert task.model == "anthropic/claude-sonnet-4"

            updated = await switch_task_model(task.id, "opus")
            assert updated.model == "anthropic/claude-opus-4"

            switched = await switch_task_model(task.id, "")
            assert switched.model is None

    @pytest.mark.asyncio
    async def test_recipe_with_model_applies(self, engine):
        """Recipe with model overrides task model via _apply_recipe_to_task."""
        from app.core.broker import enqueue_task, _apply_recipe_to_task, save_recipe
        with _patch_broker_session(engine), \
             patch("app.core.broker._runner_wake", None):
            recipe = await save_recipe(name="model-recipe", triggers=["test"], model="haiku", agent="claude")
            task = await enqueue_task(prompt="hello", project_dir="/tmp", agent="opencode")
            updated = await _apply_recipe_to_task(task.id, recipe, "enriched prompt")
            assert updated.model == "haiku"
            assert updated.agent == "claude"

    @pytest.mark.asyncio
    async def test_prefs_model_roundtrip(self, engine):
        """Set model pref, retrieve it, verify round-trip."""
        from app.core.broker import set_chat_pref, get_chat_prefs
        with _patch_broker_session(engine):
            await set_chat_pref(123, model="opus")
            prefs = await get_chat_prefs(123)
            assert prefs["model"] == "opus"

            await set_chat_pref(123, model="haiku")
            prefs = await get_chat_prefs(123)
            assert prefs["model"] == "haiku"


# ── Edge cases ─────────────────────────────────────────────────────

class TestModelEdgeCases:
    @pytest.mark.asyncio
    async def test_model_with_special_chars(self, session):
        task = Task(prompt="t", project_dir="/tmp", agent="claude", model="provider/model-name.v2")
        session.add(task)
        await session.flush()
        await session.refresh(task)
        assert task.model == "provider/model-name.v2"

    def test_build_command_model_with_spaces_in_prompt(self):
        """Prompt with spaces shouldn't affect model flag insertion."""
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.agent = "opencode"
        task.model = "sonnet"
        task.prompt = "fix the big bad bug in auth.py"
        task.project_dir = "/tmp/my project"
        task.parent_task_id = None
        argv = runner._build_command(task)
        assert "--model" in argv
        assert "sonnet" in argv
        assert task.prompt in argv

    @pytest.mark.asyncio
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_setmodel_missing_model_arg(self, mock_prefs, *_):
        update, ctx = _make_update(args=["42"])
        from app.telegram.bot import cmd_setmodel
        await cmd_setmodel(update, ctx)
        assert "Usage" in _get_reply(update)

    def test_model_helper_returns_settings_default(self):
        from app.telegram.bot import _model, _chat_model
        _chat_model.pop(99999, None)
        with patch("app.telegram.bot.settings") as mock_settings:
            mock_settings.default_model = "haiku"
            result = _model(99999)
        assert result == "haiku"
