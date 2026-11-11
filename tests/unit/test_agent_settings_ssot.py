import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import (
    get_base_avatar_url,
    resolve_avatar_url,
)
from orchestration.agent_settings import AgentSettingsManager, settings_manager
from services.slack_home import build_app_home_view


def test_relative_avatar_url_resolution():
    # Relative path resolution
    res_rel = resolve_avatar_url("static/avatar_25.png")
    assert res_rel == f"{get_base_avatar_url()}static/avatar_25.png"

    res_slash = resolve_avatar_url("/static/avatar_33.png")
    assert res_slash == f"{get_base_avatar_url()}static/avatar_33.png"

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
            "enabled_agents": ["agent-10001", "agent-10008"],
            "agents": {
                "agent-10001": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "agent-10001",
                    "displayname": "Agent 10001",
                    "icon_url": "static/avatar_25.png",
                },
                "agent-10008": {
                    "model": "openai/gpt-5.6-terra",
                    "username": "agent-10008",
                    "displayname": "Agent 10008",
                    "icon_url": "static/avatar_33.png",
                },
            },
            "orchestrator": {
                "fast_model": "gemini/gemini-3.8-flash",
                "reasoning_model": "mistral/mistral-large-latest",
                "username": "Orchestrator",
                "icon_url": "static/image_99.png",
            },
        }
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        app_file = Path(tmp_dir) / "app_settings.json"
        app_data = {
            "circuit_breaker_enabled": True,
            "circuit_breaker_threshold_usd": 2.50,
        }
        with open(app_file, "w", encoding="utf-8") as f:
            json.dump(app_data, f, indent=2)

        mgr = AgentSettingsManager(
            settings_file=settings_file,
            backup_file=backup_file,
            auto_load=True,
        )

        assert mgr.is_running_from_backup is False
        assert mgr.app_settings.is_circuit_breaker_enabled() is True
        assert mgr.app_settings.get_circuit_breaker_threshold() == 2.50
        assert "agent-10001" in mgr.agents_config
        assert mgr.agents_config["agent-10001"]["model"] == "gemini/gemini-3.8-flash"
        assert mgr.agents_config["agent-10001"]["icon_url"] == "static/avatar_25.png"
        assert mgr.orchestrator_config["fast_model"] == "gemini/gemini-3.8-flash"
        assert mgr.orchestrator_config["reasoning_model"] == "mistral/mistral-large-latest"
        assert mgr.get_orchestrator_fast_model() == "gemini/gemini-3.8-flash"
        assert mgr.get_orchestrator_reasoning_model() == "mistral/mistral-large-latest"


def test_agent_validation_skips_invalid():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "agent_settings.json"
        backup_file = Path(tmp_dir) / "agent_settings_backup.json"

        data = {
            "circuit_breaker_enabled": False,
            "circuit_breaker_threshold_usd": 1.0,
            "enabled_agents": ["agent-10001", "Bad_Agent_No_Model", "Bad_Agent_No_Persona"],
            "agents": {
                "agent-10001": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "agent-10001",
                    "displayname": "Agent 10001",
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

        assert "agent-10001" in mgr.agents_config
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
            "enabled_agents": ["agent-10001"],
            "agents": {
                "agent-10001": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "agent-10001",
                    "displayname": "Agent 10001",
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
        assert "agent-10001" in mgr.agents_config
        assert mgr.app_settings.is_circuit_breaker_enabled() is True

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
            "enabled_agents": ["agent-10001", "agent-10008"],
            "agents": {
                "agent-10001": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "agent-10001",
                    "displayname": "Agent 10001",
                    "icon_url": "static/avatar_25.png",
                },
                "agent-10008": {
                    "model": "openai/gpt-5.6-terra",
                    "username": "agent-10008",
                    "displayname": "Agent 10008",
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
        assert "agent-10001" in restored["agents"]
        assert "agent-10008" in restored["agents"]


def test_7_day_backup_refresh():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "agent_settings.json"
        backup_file = Path(tmp_dir) / "agent_settings_backup.json"

        data = {
            "circuit_breaker_enabled": False,
            "circuit_breaker_threshold_usd": 1.0,
            "enabled_agents": ["agent-10001"],
            "agents": {
                "agent-10001": {
                    "model": "gemini/gemini-3.8-flash",
                    "username": "agent-10001",
                    "displayname": "Agent 10001",
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
        view = build_app_home_view(enabled_agents={"agent-10001"})
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
        view = build_app_home_view(enabled_agents={"agent-10001"})
        blocks = view.get("blocks", [])
        has_alert = any(
            b.get("type") == "section" and "EMERGENCY BACKUP" in b.get("text", {}).get("text", "")
            for b in blocks
        )
        assert has_alert is False


def test_dynamic_config_getter_resolution():
    import core.config

    # Attribute error on unknown attribute
    with pytest.raises(AttributeError):
        _ = core.config.NON_EXISTENT_ATTRIBUTE

    # Test dynamic resolution through settings_manager
    original_orchestrator = dict(settings_manager.orchestrator_config)
    try:
        settings_manager.orchestrator_config = {
            "fast_model": "gemini/gemini-3.8-flash",
            "reasoning_model": "mistral/mistral-large-latest",
            "username": "Orchestrator",
            "icon_url": "static/image_99.png",
        }
        assert core.config.get_orchestrator_fast_model() == "gemini/gemini-3.8-flash"
        assert core.config.get_orchestrator_reasoning_model() == "mistral/mistral-large-latest"

        settings_manager.set_orchestrator_models(
            fast_model="groq/llama-3.3-70b-versatile-dynamic",
            reasoning_model="anthropic/claude-3-7-sonnet-dynamic",
            save=False,
        )
        assert core.config.get_orchestrator_fast_model() == "groq/llama-3.3-70b-versatile-dynamic"
        assert core.config.get_orchestrator_reasoning_model() == "anthropic/claude-3-7-sonnet-dynamic"
    finally:
        settings_manager.orchestrator_config = original_orchestrator


def test_configurable_pacing_defaults():
    import core.config
    from orchestration.system_settings import (
        get_debate_turn_pacing_seconds,
        get_max_discussion_turns,
        get_slack_post_pause_seconds,
        get_tool_call_loop_timeout_seconds,
    )

    assert isinstance(get_debate_turn_pacing_seconds(), float)
    assert isinstance(get_slack_post_pause_seconds(), float)
    assert isinstance(get_tool_call_loop_timeout_seconds(), float)
    assert isinstance(get_max_discussion_turns(), int)
    assert get_max_discussion_turns() == 20


@pytest.mark.anyio
async def test_turn_pipeline_propagates_agent_avatar_icon():
    """Verifies that TurnPipeline populates ctx.icon_url from agent_cfg and passes it to Slack post."""
    from unittest.mock import AsyncMock

    from orchestration.turn_pipeline import TurnContext, TurnPipeline

    mock_post = AsyncMock(return_value={"ok": True, "ts": "12345.9999"})
    pipeline = TurnPipeline(
        slack_poster=mock_post,
        notes_updater=lambda *a, **k: None,
        usage_updater=lambda *a, **k: None,
        token_recorder=lambda *a, **k: None,
    )
    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="12345.6789",
        raw_text="/[agent-10001] Hello test",
        icon_url=None,
    )
    ctx.my_generation = 0

    # Stage 1: parse directives & agent
    res1 = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res1 is None
    assert ctx.agent_key == "agent-10001"

    # Stage 2: resolve agent cfg and populate icon_url
    res2 = await pipeline.resolution_stage.stage_resolve_agent_and_governance(ctx)
    assert res2 is None
    expected_icon = ctx.agent_cfg.get("icon_url")
    assert expected_icon is not None
    assert ctx.icon_url == expected_icon

    # Stage 5: post to Slack and verify icon_url passed
    ctx.output.final_text = "Visionary response."
    res5 = await pipeline.post_record_stage.stage_post_and_record(ctx)

    assert mock_post.called
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["username"] == "Visionary"
    assert call_kwargs["icon_url"] == expected_icon
    assert res5["icon_url"] == expected_icon


def test_scribe_settings_toggle_and_app_home_view():
    from orchestration.app_settings import is_scribe_enabled, set_scribe_enabled
    from services.slack_home import build_app_home_view

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


def test_weekly_pricing_settings_toggle_and_app_home_view():
    from orchestration.app_settings import (
        is_weekly_pricing_update_enabled,
        set_weekly_pricing_update_enabled,
    )
    from services.slack_home import build_app_home_view

    try:
        set_weekly_pricing_update_enabled(True)
        assert is_weekly_pricing_update_enabled() is True

        view_on = build_app_home_view()
        blocks_on = view_on.get("blocks", [])
        pricing_block_on = next(
            (b for b in blocks_on if b.get("block_id") == "weekly_pricing_toggle_section_on"),
            None,
        )
        assert pricing_block_on is not None
        assert pricing_block_on["accessory"]["action_id"] == "toggle_weekly_pricing_update_radio"
        assert pricing_block_on["accessory"]["initial_option"]["value"] == "pricing_update:on"

        set_weekly_pricing_update_enabled(False)
        assert is_weekly_pricing_update_enabled() is False

        view_off = build_app_home_view()
        blocks_off = view_off.get("blocks", [])
        pricing_block_off = next(
            (b for b in blocks_off if b.get("block_id") == "weekly_pricing_toggle_section_off"),
            None,
        )
        assert pricing_block_off is not None
        assert pricing_block_off["accessory"]["action_id"] == "toggle_weekly_pricing_update_radio"
        assert pricing_block_off["accessory"]["initial_option"]["value"] == "pricing_update:off"
    finally:
        set_weekly_pricing_update_enabled(True)
        assert is_weekly_pricing_update_enabled() is True


def test_single_challenger_and_arbitrator_system_agent_ssot():
    from orchestration.agent_settings import (
        get_challenger_agent,
        get_arbitrator_agent,
        settings_manager,
    )
    from services.slack_home import build_app_home_view

    # SSOT singular getters default to system agent keys
    assert get_challenger_agent() == "Challenger"
    assert get_arbitrator_agent() == "Arbitrator"

    # Debate panel agents are strictly 16
    assert len(settings_manager.agents_config) == 16
    for agent_key, cfg in settings_manager.agents_config.items():
        assert "special_role" not in cfg
        assert "challenger_priority" not in cfg

    # Dialectical system agents exist in system_agents_config
    assert "Challenger" in settings_manager.system_agents_config
    assert "Arbitrator" in settings_manager.system_agents_config

    # Dialectical system agents are excluded from the Slack App Home agent panel list
    home_view = build_app_home_view()
    agent_toggle_block_ids = [
        b.get("block_id")
        for b in home_view.get("blocks", [])
        if str(b.get("block_id", "")).startswith("agent_toggle_")
    ]
    assert "agent_toggle_Challenger" not in agent_toggle_block_ids
    assert "agent_toggle_Arbitrator" not in agent_toggle_block_ids
    assert len(agent_toggle_block_ids) == 16







def test_all_ssot_icon_urls_resolve_to_served_avatar_files():
    """Every icon_url in agent/system-agent SSOTs must point at a PNG served under /avatars or /static."""
    from core.paths import (
        AGENT_SETTINGS_FILE,
        AVATARS_DIR,
        DEFAULT_SYSTEM_AGENT_SETTINGS_FILE,
        ENGINE_AVATARS_DIR,
    )

    served_prefixes = ("avatars/", "static/")
    search_dirs = (AVATARS_DIR, ENGINE_AVATARS_DIR)

    def iter_icon_urls(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "icon_url":
                    yield v
                else:
                    yield from iter_icon_urls(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from iter_icon_urls(v)

    checked = 0
    for settings_file in (AGENT_SETTINGS_FILE, DEFAULT_SYSTEM_AGENT_SETTINGS_FILE):
        data = json.loads(Path(settings_file).read_text(encoding="utf-8"))
        for icon_url in iter_icon_urls(data):
            assert isinstance(icon_url, str) and icon_url, f"{settings_file}: bad icon_url {icon_url!r}"
            assert icon_url.startswith(served_prefixes), (
                f"{settings_file}: icon_url {icon_url!r} is not under a served mount (/avatars, /static)"
            )
            filename = icon_url.rsplit("/", 1)[-1]
            assert any((d / filename).exists() for d in search_dirs), (
                f"{settings_file}: avatar file {filename!r} not found in {search_dirs}"
            )
            assert resolve_avatar_url(icon_url) == f"{get_base_avatar_url()}{icon_url}"
            checked += 1
    assert checked >= 20  # 16 panel agents + 5 system/orchestrator entries


def test_base_avatar_url_env_override_and_resolve():
    """BASE_AVATAR_URL must be read from env at call time; resolve_avatar_url must use it (Slack fetches icon_url server-side)."""
    from core.config import DEFAULT_BASE_AVATAR_URL, get_base_avatar_url, resolve_avatar_url

    custom_env = {"BASE_AVATAR_URL": "https://agents-slack.cailogics.com/"}
    assert get_base_avatar_url(env=custom_env) == "https://agents-slack.cailogics.com/"
    assert (
        resolve_avatar_url("avatars/image_43.png", env=custom_env)
        == "https://agents-slack.cailogics.com/avatars/image_43.png"
    )
    assert (
        resolve_avatar_url("/avatars/image_17.png", env=custom_env)
        == "https://agents-slack.cailogics.com/avatars/image_17.png"
    )
    # absolute URLs pass through untouched
    assert (
        resolve_avatar_url("https://cdn.example.com/a.png", env=custom_env)
        == "https://cdn.example.com/a.png"
    )

    empty_env: dict[str, str] = {}
    assert get_base_avatar_url(env=empty_env) == DEFAULT_BASE_AVATAR_URL
    assert (
        resolve_avatar_url("avatars/image_43.png", env=empty_env)
        == f"{DEFAULT_BASE_AVATAR_URL}avatars/image_43.png"
    )
