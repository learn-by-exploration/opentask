"""Tests for recipe gallery — gallery module, broker install functions, bot /gallery command."""

from __future__ import annotations

import os
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

from app.core.gallery import (
    GALLERY,
    get_gallery_by_category,
    get_gallery_categories,
    get_gallery_item,
)

# ── A. Gallery module unit tests ────────────────────────────────────


class TestGalleryModule:
    def test_gallery_not_empty(self):
        assert len(GALLERY) > 0

    def test_every_item_has_required_fields(self):
        for item in GALLERY:
            assert "category" in item, f"Missing category in {item.get('name')}"
            assert "name" in item, f"Missing name"
            assert "description" in item, f"Missing description in {item.get('name')}"
            assert "recipe" in item, f"Missing recipe in {item.get('name')}"

    def test_every_recipe_has_triggers(self):
        for item in GALLERY:
            recipe = item["recipe"]
            assert "triggers" in recipe, f"Missing triggers in {item['name']}"
            assert len(recipe["triggers"]) > 0, f"Empty triggers in {item['name']}"

    def test_no_duplicate_names(self):
        names = [item["name"] for item in GALLERY]
        assert len(names) == len(set(names)), f"Duplicate names: {[n for n in names if names.count(n) > 1]}"

    def test_get_gallery_categories(self):
        cats = get_gallery_categories()
        assert len(cats) >= 3  # Synclyf, AI Powerhouse, Dev Workflows
        assert "Synclyf" in cats
        assert "AI Powerhouse" in cats
        assert "Dev Workflows" in cats

    def test_get_gallery_categories_preserves_order(self):
        cats = get_gallery_categories()
        # Should match insertion order of first occurrence in GALLERY
        first_cat = GALLERY[0]["category"]
        assert cats[0] == first_cat

    def test_get_gallery_by_category(self):
        items = get_gallery_by_category("Synclyf")
        assert len(items) > 0
        for item in items:
            assert item["category"] == "Synclyf"

    def test_get_gallery_by_category_unknown(self):
        items = get_gallery_by_category("Nonexistent")
        assert items == []

    def test_get_gallery_item(self):
        item = get_gallery_item("lifeflow")
        assert item is not None
        assert item["name"] == "lifeflow"
        assert item["category"] == "Synclyf"

    def test_get_gallery_item_not_found(self):
        item = get_gallery_item("nonexistent-recipe-xyz")
        assert item is None

    def test_synclyf_recipes_have_project_dirs(self):
        items = get_gallery_by_category("Synclyf")
        for item in items:
            recipe = item["recipe"]
            assert "project_dir" in recipe, f"Synclyf recipe {item['name']} should have project_dir"
            assert recipe["project_dir"].startswith("/home/shyam/synclyf")

    def test_ai_powerhouse_recipes_have_project_dirs(self):
        items = get_gallery_by_category("AI Powerhouse")
        for item in items:
            recipe = item["recipe"]
            assert "project_dir" in recipe, f"AI Powerhouse recipe {item['name']} should have project_dir"
            assert "ai-powerhouse" in recipe["project_dir"]

    def test_dev_workflow_recipes_exist(self):
        items = get_gallery_by_category("Dev Workflows")
        names = [item["name"] for item in items]
        assert "bug-fix" in names
        assert "code-review" in names
        assert "refactor" in names
        assert "new-feature" in names
        assert "security-audit" in names

    def test_trigger_lengths_valid(self):
        for item in GALLERY:
            for t in item["recipe"]["triggers"]:
                assert 0 < len(t) <= 200, f"Bad trigger '{t}' in {item['name']}"

    def test_name_lengths_valid(self):
        for item in GALLERY:
            assert 0 < len(item["name"]) <= 128, f"Bad name length: {item['name']}"

    def test_prefix_suffix_within_limits(self):
        for item in GALLERY:
            recipe = item["recipe"]
            if "prompt_prefix" in recipe and recipe["prompt_prefix"]:
                assert len(recipe["prompt_prefix"]) <= 2000, f"Prefix too long in {item['name']}"
            if "prompt_suffix" in recipe and recipe["prompt_suffix"]:
                assert len(recipe["prompt_suffix"]) <= 2000, f"Suffix too long in {item['name']}"


# ── B. Broker gallery install tests ─────────────────────────────────

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from app.core.models import Base, Recipe


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def _patch_broker_session(engine):
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async def _fake_session():
        return Session()
    with patch("app.core.broker.get_session", _fake_session):
        yield


@pytest.mark.asyncio
class TestBrokerGalleryInstall:
    async def test_install_single_recipe(self, _patch_broker_session):
        from app.core.broker import install_gallery_recipe
        recipe = await install_gallery_recipe("lifeflow", chat_id=12345)
        assert recipe is not None
        assert recipe.name == "lifeflow"
        assert "lifeflow" in recipe.triggers

    async def test_install_unknown_recipe(self, _patch_broker_session):
        from app.core.broker import install_gallery_recipe
        result = await install_gallery_recipe("nonexistent-xyz")
        assert result is None

    async def test_install_category(self, _patch_broker_session):
        from app.core.broker import install_gallery_category
        installed = await install_gallery_category("Synclyf", chat_id=12345)
        synclyf_count = len(get_gallery_by_category("Synclyf"))
        assert len(installed) == synclyf_count
        assert all(isinstance(r, Recipe) for r in installed)

    async def test_install_unknown_category(self, _patch_broker_session):
        from app.core.broker import install_gallery_category
        installed = await install_gallery_category("Nonexistent")
        assert installed == []

    async def test_install_all(self, _patch_broker_session):
        from app.core.broker import install_all_gallery_recipes
        installed = await install_all_gallery_recipes(chat_id=12345)
        assert len(installed) == len(GALLERY)

    async def test_install_is_idempotent(self, _patch_broker_session):
        from app.core.broker import install_gallery_recipe, list_recipes
        await install_gallery_recipe("bug-fix")
        await install_gallery_recipe("bug-fix")  # second time = upsert
        recipes = await list_recipes()
        names = [r.name for r in recipes]
        assert names.count("bug-fix") == 1

    async def test_installed_recipe_has_correct_fields(self, _patch_broker_session):
        from app.core.broker import install_gallery_recipe
        item = get_gallery_item("financeflow")
        recipe = await install_gallery_recipe("financeflow")
        assert recipe.agent == item["recipe"].get("agent")
        assert recipe.project_dir == item["recipe"].get("project_dir")
        if item["recipe"].get("prompt_prefix"):
            assert recipe.prompt_prefix == item["recipe"]["prompt_prefix"]

    async def test_dev_workflow_recipe_no_project_dir(self, _patch_broker_session):
        from app.core.broker import install_gallery_recipe
        recipe = await install_gallery_recipe("code-review")
        assert recipe is not None
        # Dev workflow recipes may or may not have project_dir
        assert recipe.name == "code-review"


# ── C. Bot /gallery command tests ───────────────────────────────────


def _make_update(chat_id=12345, user_id=12345, text="/gallery"):
    user = MagicMock()
    user.id = user_id
    user.first_name = "Test"

    chat = MagicMock()
    chat.id = chat_id
    chat.type = "private"

    message = MagicMock()
    message.chat = chat
    message.chat_id = chat_id
    message.from_user = user
    message.text = text
    message.reply_text = AsyncMock()

    bot = MagicMock()
    bot.send_message = AsyncMock()

    update = MagicMock(spec=["message", "effective_user", "effective_chat", "callback_query", "get_bot"])
    update.message = message
    update.effective_user = user
    update.effective_chat = chat
    update.callback_query = None
    update.get_bot = MagicMock(return_value=bot)

    return update


def _ctx(*args):
    context = MagicMock()
    context.args = list(args) if args else []
    return context


def _make_callback_query(chat_id=12345, user_id=12345, data=""):
    user = MagicMock()
    user.id = user_id
    user.first_name = "Test"

    chat = MagicMock()
    chat.id = chat_id
    chat.type = "private"

    message = MagicMock()
    message.chat = chat
    message.chat_id = chat_id

    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = message
    query.edit_message_text = AsyncMock()
    query.from_user = user

    update = MagicMock(spec=["message", "effective_user", "effective_chat", "callback_query", "get_bot"])
    update.message = message
    update.effective_user = user
    update.effective_chat = chat
    update.callback_query = query

    bot = MagicMock()
    bot.send_message = AsyncMock()
    update.get_bot = MagicMock(return_value=bot)

    return update


@pytest.fixture
def _clear_bot_caches():
    from app.telegram.bot import _chat_agent, _chat_model, _chat_project_dir, _load_prefs
    _chat_agent.clear()
    _chat_model.clear()
    _chat_project_dir.clear()
    loaded = getattr(_load_prefs, "_loaded", None)
    if loaded is not None:
        loaded.clear()
    yield
    _chat_agent.clear()
    _chat_model.clear()
    _chat_project_dir.clear()
    loaded = getattr(_load_prefs, "_loaded", None)
    if loaded is not None:
        loaded.clear()


@pytest.mark.asyncio
class TestGalleryCommand:
    async def test_gallery_shows_categories(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import cmd_gallery
        update = _make_update()
        ctx = _ctx()
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await cmd_gallery(update, ctx)

        bot = update.get_bot()
        assert bot.send_message.called
        text = bot.send_message.call_args[1]["text"]
        assert "Recipe Gallery" in text
        assert "Synclyf" in text
        assert "AI Powerhouse" in text
        assert "Dev Workflows" in text

    async def test_gallery_shows_inline_buttons(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import cmd_gallery
        update = _make_update()
        ctx = _ctx()
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await cmd_gallery(update, ctx)

        bot = update.get_bot()
        kwargs = bot.send_message.call_args[1]
        assert "reply_markup" in kwargs
        markup = kwargs["reply_markup"]
        # Should have category buttons + install all
        assert len(markup.inline_keyboard) >= 3

    async def test_gallery_category_filter(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import cmd_gallery
        update = _make_update()
        ctx = _ctx("Synclyf")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await cmd_gallery(update, ctx)

        bot = update.get_bot()
        text = bot.send_message.call_args[1]["text"]
        assert "Synclyf" in text
        assert "lifeflow" in text

    async def test_gallery_unknown_category(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import cmd_gallery
        update = _make_update()
        ctx = _ctx("Nonexistent")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await cmd_gallery(update, ctx)

        bot = update.get_bot()
        text = bot.send_message.call_args[1]["text"]
        assert "not found" in text

    async def test_gallery_install_single(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import cmd_gallery
        update = _make_update()
        ctx = _ctx("install", "lifeflow")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await cmd_gallery(update, ctx)

        bot = update.get_bot()
        text = bot.send_message.call_args[1]["text"]
        assert "Installed" in text
        assert "lifeflow" in text

    async def test_gallery_install_unknown(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import cmd_gallery
        update = _make_update()
        ctx = _ctx("install", "nonexistent")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await cmd_gallery(update, ctx)

        bot = update.get_bot()
        text = bot.send_message.call_args[1]["text"]
        assert "not found" in text

    async def test_gallery_install_all(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import cmd_gallery
        update = _make_update()
        ctx = _ctx("install-all")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await cmd_gallery(update, ctx)

        bot = update.get_bot()
        text = bot.send_message.call_args[1]["text"]
        assert "Installed" in text
        assert str(len(GALLERY)) in text

    async def test_gallery_install_category(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import cmd_gallery
        update = _make_update()
        ctx = _ctx("install-category", "Synclyf")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await cmd_gallery(update, ctx)

        bot = update.get_bot()
        text = bot.send_message.call_args[1]["text"]
        assert "Installed" in text
        assert "Synclyf" in text

    async def test_gallery_install_category_unknown(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import cmd_gallery
        update = _make_update()
        ctx = _ctx("install-category", "Nonexistent")
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await cmd_gallery(update, ctx)

        bot = update.get_bot()
        text = bot.send_message.call_args[1]["text"]
        assert "not found" in text


@pytest.mark.asyncio
class TestGalleryCallback:
    async def test_gallery_cat_callback(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import handle_gallery_callback
        update = _make_callback_query(data="gallerycat:Synclyf")
        ctx = _ctx()
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await handle_gallery_callback(update, ctx)

        query = update.callback_query
        assert query.edit_message_text.called
        text = query.edit_message_text.call_args[0][0]
        assert "Installed" in text
        assert "Synclyf" in text

    async def test_gallery_all_callback(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import handle_gallery_callback
        update = _make_callback_query(data="galleryall:")
        ctx = _ctx()
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await handle_gallery_callback(update, ctx)

        query = update.callback_query
        assert query.edit_message_text.called
        text = query.edit_message_text.call_args[0][0]
        assert "Installed" in text
        assert str(len(GALLERY)) in text

    async def test_gallery_cat_unknown_callback(self, _patch_broker_session, _clear_bot_caches):
        from app.telegram.bot import handle_gallery_callback
        update = _make_callback_query(data="gallerycat:Nonexistent")
        ctx = _ctx()
        with patch("app.telegram.bot._load_prefs", new_callable=AsyncMock):
            await handle_gallery_callback(update, ctx)

        query = update.callback_query
        assert query.edit_message_text.called
        text = query.edit_message_text.call_args[0][0]
        assert "not found" in text


# ── D. Integration test — install and match ─────────────────────────

@pytest.mark.asyncio
class TestGalleryIntegration:
    async def test_installed_recipe_matches_prompt(self, _patch_broker_session):
        from app.core.broker import install_gallery_recipe, match_recipe
        await install_gallery_recipe("lifeflow")
        matched = await match_recipe("please fix the lifeflow tasks api")
        assert matched is not None
        assert matched.name == "lifeflow"

    async def test_installed_recipe_no_match_unrelated(self, _patch_broker_session):
        from app.core.broker import install_gallery_recipe, match_recipe
        await install_gallery_recipe("lifeflow")
        matched = await match_recipe("deploy kubernetes pods")
        assert matched is None

    async def test_install_all_then_match_finance(self, _patch_broker_session):
        from app.core.broker import install_all_gallery_recipes, match_recipe
        await install_all_gallery_recipes()
        matched = await match_recipe("update the budget feature in financeflow")
        assert matched is not None
        assert matched.name == "financeflow"

    async def test_install_all_then_match_security(self, _patch_broker_session):
        from app.core.broker import install_all_gallery_recipes, match_recipe
        await install_all_gallery_recipes()
        matched = await match_recipe("run owasp security audit on the api")
        assert matched is not None
        assert matched.name == "security-audit"

    async def test_install_all_then_match_bug(self, _patch_broker_session):
        from app.core.broker import install_all_gallery_recipes, match_recipe
        await install_all_gallery_recipes()
        matched = await match_recipe("fix this crash bug in login")
        assert matched is not None
        assert matched.name == "bug-fix"

    async def test_gallery_recipe_enrichment(self, _patch_broker_session):
        from app.core.broker import install_gallery_recipe
        from app.telegram.bot import _enrich_prompt
        recipe = await install_gallery_recipe("financeflow")
        enriched = _enrich_prompt("add budget categories", recipe)
        assert "FinanceFlow" in enriched
        assert "add budget categories" in enriched
