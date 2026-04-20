"""Tests for the Settings pydantic model and field validators."""

from __future__ import annotations

import pytest


class TestParseUserIds:
    """Tests for the allowed_user_ids field_validator."""

    def test_comma_separated_string(self):
        from app.config.settings import Settings

        result = Settings.parse_user_ids("111,222,333")
        assert result == [111, 222, 333]

    def test_single_int(self):
        from app.config.settings import Settings

        result = Settings.parse_user_ids(42)
        assert result == [42]

    def test_list_of_ints_passthrough(self):
        from app.config.settings import Settings

        result = Settings.parse_user_ids([1, 2, 3])
        assert result == [1, 2, 3]

    def test_string_with_spaces(self):
        from app.config.settings import Settings

        result = Settings.parse_user_ids(" 100 , 200 , 300 ")
        assert result == [100, 200, 300]

    def test_empty_string_returns_empty_list(self):
        from app.config.settings import Settings

        result = Settings.parse_user_ids("")
        assert result == []

    def test_string_with_trailing_comma(self):
        from app.config.settings import Settings

        result = Settings.parse_user_ids("111,222,")
        assert result == [111, 222]

    def test_single_value_string(self):
        from app.config.settings import Settings

        result = Settings.parse_user_ids("99999")
        assert result == [99999]


class TestSettingsProperties:
    def test_db_url_format(self):
        from app.config.settings import settings

        assert settings.db_url.startswith("sqlite+aiosqlite:///")
        assert settings.db_path in settings.db_url

    def test_default_agent(self):
        from app.config.settings import settings

        assert settings.default_agent in settings.agent_commands

    def test_agent_commands_has_templates(self):
        from app.config.settings import settings

        for name, template in settings.agent_commands.items():
            assert "{prompt}" in template, f"{name} template missing {{prompt}}"

    def test_max_queue_size_positive(self):
        from app.config.settings import settings

        assert settings.max_queue_size > 0

    def test_task_timeout_positive(self):
        from app.config.settings import settings

        assert settings.task_timeout_seconds > 0

    def test_output_summary_max_chars_positive(self):
        from app.config.settings import settings

        assert settings.output_summary_max_chars > 0
