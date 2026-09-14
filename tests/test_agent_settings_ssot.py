import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import (
    BASE_AVATAR_URL,
    resolve_avatar_url,
)
from orchestration.agent_settings import AgentSettingsManager, settings_manager
from services.slack_client import build_app_home_view


def test_relative_avatar_url_resolution():
    # Relative path resolution
    res_rel = resolve_avatar_url("static/avatar_25.png")
    assert res_rel == f"{BASE_AVATAR_URL}static/avatar_25.png"

    res_slash = resolve_avatar_url("/static/avatar_33.png")
    assert res_slash == f"{BASE_AVATAR_URL}static/avatar_33.png"

    # Absolute URL preservation
    res_abs = resolve_avatar_url("https://example.com/my_avatar.png")
    assert res_abs == "https://example.com/my_avatar.png"

    # None / Empty handling
    assert resolve_avatar_url(None) is None
    assert resolve_avatar_url("") is None


def test_agent_settings_ssot_loading():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "agent_settings.json"
        backup_file = Path(tmp_dir) / "agent_settings_backup.json"

        data = {
            "circuit_breaker_enabled": True,
            "circuit_breaker_threshold_usd": 2.50,
            "enabled_agents": ["Visionary", "Architect"],
            "agents": {
                "Visionary": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "Visionary",
                    "icon_url": "static/avatar_25.png",
                },
                "Architect": {
                    "model": "openai/gpt-5.6-terra",
                    "username": "Architect",
                    "icon_url": "static/avatar_33.png",
                },
            },
            "system_agents": {
                "thread_notes": {"model": "mistral/mistral-small-latest"},
                "usage_agent": {"model": "groq/llama-3.3-70b-versatile"},
            },
        }
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        mgr = AgentSettingsManager(
            settings_file=settings_file,
            backup_file=backup_file,
            auto_load=True,
        )

        assert mgr.is_running_from_backup is False
        assert mgr.is_circuit_breaker_enabled() is True
        assert mgr.get_circuit_breaker_threshold() == 2.50
        assert "Visionary" in mgr.agents_config
        assert mgr.agents_config["Visionary"]["model"] == "gemini/gemini-3.8-flash"
        assert mgr.agents_config["Visionary"]["icon_url"] == "static/avatar_25.png"
        assert mgr.system_agents_config["usage_agent"]["model"] == "groq/llama-3.3-70b-versatile"


def test_agent_validation_skips_invalid():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "agent_settings.json"
        backup_file = Path(tmp_dir) / "agent_settings_backup.json"

        data = {
            "circuit_breaker_enabled": False,
            "circuit_breaker_threshold_usd": 1.0,
            "enabled_agents": ["Visionary", "Bad_Agent_No_Model", "Bad_Agent_No_Persona"],
            "agents": {
                "Visionary": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "Visionary",
                    "icon_url": "static/avatar_25.png",
                },
                "Bad_Agent_No_Model": {
                    "model": "",
                    "username": "Missing Model",
                    "icon_url": "static/avatar_1.png",
                },
                "Bad_Agent_No_Username": {
                    "model": "gemini/gemini-pro",
                    "username": "",
                    "icon_url": "static/avatar_1.png",
                },
                "Bad_Agent_No_Persona": {
                    "model": "openai/gpt-4o",
                    "username": "Missing Persona MD",
                    "icon_url": "static/avatar_1.png",
                },
            },
        }
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        mgr = AgentSettingsManager(
            settings_file=settings_file,
            backup_file=backup_file,
            auto_load=True,
        )

        assert "Visionary" in mgr.agents_config
        assert "Bad_Agent_No_Model" not in mgr.agents_config
        assert "Bad_Agent_No_Username" not in mgr.agents_config
        assert "Bad_Agent_No_Persona" not in mgr.agents_config
        assert "Bad_Agent_No_Model" not in mgr.get_enabled_agents()


def test_emergency_backup_failover_non_destructive():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "agent_settings.json"
        backup_file = Path(tmp_dir) / "agent_settings_backup.json"

        # Valid backup file
        backup_data = {
            "circuit_breaker_enabled": True,
            "circuit_breaker_threshold_usd": 5.0,
            "enabled_agents": ["Visionary"],
            "agents": {
                "Visionary": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "Visionary",
                    "icon_url": "static/avatar_25.png",
                }
            },
        }
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2)

        # Corrupted active file
        corrupted_content = "{ this is completely malformed json !!! "
        with open(settings_file, "w", encoding="utf-8") as f:
            f.write(corrupted_content)

        mgr = AgentSettingsManager(
            settings_file=settings_file,
            backup_file=backup_file,
            auto_load=True,
        )

        # Emergency backup loaded in memory
        assert mgr.is_running_from_backup is True
        assert mgr.backup_error_reason is not None
        assert "Unreadable/corrupted JSON" in mgr.backup_error_reason
        assert "Visionary" in mgr.agents_config
        assert mgr.is_circuit_breaker_enabled() is True

        # Non-destructive check: active file MUST still contain corrupted content untouched
        with open(settings_file, "r", encoding="utf-8") as f:
            active_content = f.read()
        assert active_content == corrupted_content


def test_restore_from_backup():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "agent_settings.json"
        backup_file = Path(tmp_dir) / "agent_settings_backup.json"

        backup_data = {
            "circuit_breaker_enabled": False,
            "circuit_breaker_threshold_usd": 1.0,
            "enabled_agents": ["Visionary", "Architect"],
            "agents": {
                "Visionary": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "Visionary",
                    "icon_url": "static/avatar_25.png",
                },
                "Architect": {
                    "model": "openai/gpt-5.6-terra",
                    "username": "Architect",
                    "icon_url": "static/avatar_33.png",
                },
            },
        }
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2)

        # Corrupted active file
        with open(settings_file, "w", encoding="utf-8") as f:
            f.write("corrupted json")

        mgr = AgentSettingsManager(
            settings_file=settings_file,
            backup_file=backup_file,
            auto_load=True,
        )
        assert mgr.is_running_from_backup is True

        # Execute restore
        success = mgr.restore_from_backup()
        assert success is True
        assert mgr.is_running_from_backup is False
        assert mgr.backup_error_reason is None

        # Verify active file on disk has been replaced with backup data
        with open(settings_file, "r", encoding="utf-8") as f:
            restored = json.load(f)
        assert "Visionary" in restored["agents"]
        assert "Architect" in restored["agents"]


def test_7_day_backup_refresh():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "agent_settings.json"
        backup_file = Path(tmp_dir) / "agent_settings_backup.json"

        data = {
            "circuit_breaker_enabled": False,
            "circuit_breaker_threshold_usd": 1.0,
            "enabled_agents": ["Visionary"],
            "agents": {
                "Visionary": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "Visionary",
                    "icon_url": "static/avatar_25.png",
                }
            },
        }
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        mgr = AgentSettingsManager(
            settings_file=settings_file,
            backup_file=backup_file,
            backup_interval_seconds=600,  # 10 minutes for testing
            auto_load=True,
        )
        assert backup_file.exists()

        # Simulate old backup mtime (20 minutes ago)
        old_time = time.time() - 1200
        os.utime(backup_file, (old_time, old_time))
        assert backup_file.stat().st_mtime < time.time() - 600

        # Run check
        refreshed = mgr.check_and_update_backup()
        assert refreshed is True
        assert backup_file.stat().st_mtime >= time.time() - 5


def test_slack_app_home_emergency_alert():
    # Test alert presence when running in backup mode
    with patch("orchestration.agent_settings.is_running_from_backup", return_value=True), \
         patch("orchestration.agent_settings.get_backup_error_reason", return_value="Test file missing"):
        view = build_app_home_view(enabled_agents={"Visionary"})
        blocks = view.get("blocks", [])

        # Check for emergency alert section
        has_alert = any(
            b.get("type") == "section" and "EMERGENCY BACKUP" in b.get("text", {}).get("text", "")
            for b in blocks
        )
        assert has_alert is True

        # Check for restore button
        has_restore_btn = any(
            b.get("type") == "section"
            and b.get("accessory", {}).get("action_id") == "restore_from_backup"
            for b in blocks
        )
        assert has_restore_btn is True

    # Test no alert when normal
    with patch("orchestration.agent_settings.is_running_from_backup", return_value=False):
        view = build_app_home_view(enabled_agents={"Visionary"})
        blocks = view.get("blocks", [])
        has_alert = any(
            b.get("type") == "section" and "EMERGENCY BACKUP" in b.get("text", {}).get("text", "")
            for b in blocks
        )
        assert has_alert is False


def test_dynamic_config_getattr_resolution():
    import core.config

    # Attribute error on unknown attribute
    with pytest.raises(AttributeError):
        _ = core.config.NON_EXISTENT_ATTRIBUTE

    # Test dynamic resolution through settings_manager
    original_system_agents = dict(settings_manager.system_agents_config)
    try:
        # Without override, returns default from SSOT file
        settings_manager.system_agents_config = {}
        default_model = core.config.THREAD_NOTES_MODEL
        assert default_model == "mistral/mistral-small-latest"
        assert core.config.LANGUAGE_DETECTOR_MODEL == "gemini/gemini-3.8-flash"
        assert core.config.INTENT_EXTRACTOR_MODEL == "mistral/mistral-small-latest"

        # With dynamic override, __getattr__ dynamically fetches the updated model
        settings_manager.system_agents_config = {
            "thread_notes": {"model": "anthropic/claude-3-7-sonnet-dynamic"},
            "usage_agent": {"model": "openai/gpt-5-turbo-dynamic"},
            "language_detector": {"model": "groq/llama-3.3-70b-versatile-dynamic"},
            "intent_extractor": {"model": "mistral/mistral-large-latest-dynamic"},
        }
        assert core.config.THREAD_NOTES_MODEL == "anthropic/claude-3-7-sonnet-dynamic"
        assert core.config.USAGE_AGENT_MODEL == "openai/gpt-5-turbo-dynamic"
        assert core.config.LANGUAGE_DETECTOR_MODEL == "groq/llama-3.3-70b-versatile-dynamic"
        assert core.config.INTENT_EXTRACTOR_MODEL == "mistral/mistral-large-latest-dynamic"
    finally:
        settings_manager.system_agents_config = original_system_agents


def test_configurable_pacing_defaults():
    import core.config

    assert isinstance(core.config.DEBATE_TURN_PACING_SECONDS, float)
    assert isinstance(core.config.SLACK_POST_PAUSE_SECONDS, float)
    assert isinstance(core.config.MAX_DISCUSSION_TURNS, int)
    assert core.config.MAX_DISCUSSION_TURNS == 20


@pytest.mark.anyio
async def test_turn_pipeline_propagates_agent_avatar_icon():
    """Verifies that TurnPipeline populates ctx.icon_url from agent_cfg and passes it to Slack post."""
    from unittest.mock import AsyncMock

    from orchestration.turn_pipeline import TurnContext, TurnPipeline

    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="12345.6789",
        raw_text="[Visionary] Hello test",
        icon_url=None,
    )
    ctx.my_generation = 0

    # Stage 1: parse directives & agent
    res1 = await pipeline.stage_validate_and_parse(ctx)
    assert res1 is None
    assert ctx.agent_key == "Visionary"

    # Stage 2: resolve agent cfg and populate icon_url
    res2 = await pipeline.stage_resolve_agent_and_governance(ctx)
    assert res2 is None
    expected_icon = ctx.agent_cfg.get("icon_url")
    assert expected_icon is not None
    assert ctx.icon_url == expected_icon

    # Stage 5: post to Slack and verify icon_url passed
    mock_post = AsyncMock(return_value={"ok": True, "ts": "12345.9999"})
    with patch("orchestration.agent_router.post_agent_response", mock_post), \
         patch("orchestration.agent_router.trigger_thread_notes_update"), \
         patch("orchestration.agent_router.trigger_usage_update"), \
         patch("orchestration.agent_router.record_thread_tokens"):
        ctx.final_text = "Visionary response."
        res5 = await pipeline.stage_post_and_record(ctx)

        assert mock_post.called
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["username"] == "Visionary"
        assert call_kwargs["icon_url"] == expected_icon
        assert res5["icon_url"] == expected_icon


def test_scribe_settings_toggle_and_app_home_view():
    from orchestration.agent_settings import is_scribe_enabled, set_scribe_enabled
    from services.slack_client import build_app_home_view

    # Test toggling scribe in settings manager
    set_scribe_enabled(True)
    assert is_scribe_enabled() is True

    # View with scribe enabled
    view_on = build_app_home_view()
    blocks_on = view_on.get("blocks", [])
    scribe_block_on = next(
        (b for b in blocks_on if b.get("block_id") == "scribe_toggle_section_on"),
        None,
    )
    assert scribe_block_on is not None
    assert scribe_block_on["accessory"]["action_id"] == "toggle_scribe_radio"
    assert scribe_block_on["accessory"]["initial_option"]["value"] == "scribe:on"

    # Toggle to False
    set_scribe_enabled(False)
    assert is_scribe_enabled() is False

    # View with scribe disabled
    view_off = build_app_home_view()
    blocks_off = view_off.get("blocks", [])
    scribe_block_off = next(
        (b for b in blocks_off if b.get("block_id") == "scribe_toggle_section_off"),
        None,
    )
    assert scribe_block_off is not None
    assert scribe_block_off["accessory"]["action_id"] == "toggle_scribe_radio"
    assert scribe_block_off["accessory"]["initial_option"]["value"] == "scribe:off"

    # Reset back to True for clean test state
    set_scribe_enabled(True)
    assert is_scribe_enabled() is True



