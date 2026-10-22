import json
from unittest.mock import AsyncMock, MagicMock
import pytest

from orchestration.app_settings import (
    get_workspace_context,
    set_workspace_context,
    AppSettingsManager,
)
from prompts.builder import build_agent_prompt
from services.slack_home_views import (
    build_app_home_view,
    build_edit_workspace_context_modal,
)


@pytest.fixture(autouse=True)
def clean_workspace_context(tmp_path, monkeypatch):
    test_settings_file = tmp_path / "app_settings.json"
    test_settings_file.write_text(json.dumps({"workspace_context": ""}), encoding="utf-8")
    monkeypatch.setattr("orchestration.app_settings.APP_SETTINGS_FILE", test_settings_file)
    mgr = AppSettingsManager(settings_file=test_settings_file, auto_load=True)
    monkeypatch.setattr("orchestration.app_settings.app_settings_manager", mgr)
    yield


def test_get_and_set_workspace_context(tmp_path):
    assert get_workspace_context() == ""

    test_ctx = "Acme Corp enterprise guidelines: prioritize reliability and security."
    set_workspace_context(test_ctx)
    assert get_workspace_context() == test_ctx

    # Verify 1000 characters cap
    long_ctx = "A" * 1500
    set_workspace_context(long_ctx)
    saved = get_workspace_context()
    assert len(saved) == 1000
    assert saved == "A" * 1000


def test_workspace_context_injected_into_agent_prompt():
    set_workspace_context("")
    prompt_empty = build_agent_prompt("agent-10001")
    assert "### WORKSPACE BASELINE CONTEXT" not in prompt_empty

    guideline = "We build clinical decision tools. Ensure strict HIPAA compliance."
    set_workspace_context(guideline)
    prompt_with_ctx = build_agent_prompt("agent-10001")
    assert "### WORKSPACE BASELINE CONTEXT" in prompt_with_ctx
    assert guideline in prompt_with_ctx


def test_build_app_home_view_admin_vs_non_admin(monkeypatch):
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_ADMIN_ONLY")
    set_workspace_context("Enterprise global rules")

    # 1. Non-admin user view
    view_non_admin = build_app_home_view(user_id="U_REGULAR_USER")
    blocks_non_admin = view_non_admin["blocks"]
    ctx_section_non_admin = next(
        (b for b in blocks_non_admin if b.get("block_id") == "workspace_context_display_section"),
        None,
    )
    assert ctx_section_non_admin is not None
    assert "Enterprise global rules" in ctx_section_non_admin["text"]["text"]
    assert "View-only" in ctx_section_non_admin["text"]["text"]
    assert "accessory" not in ctx_section_non_admin  # No edit button for non-admin

    # 2. Admin user view
    view_admin = build_app_home_view(user_id="U_ADMIN_ONLY")
    blocks_admin = view_admin["blocks"]
    ctx_section_admin = next(
        (b for b in blocks_admin if b.get("block_id") == "workspace_context_display_section"),
        None,
    )
    assert ctx_section_admin is not None
    assert "accessory" in ctx_section_admin
    assert ctx_section_admin["accessory"]["action_id"] == "open_edit_workspace_context_modal"


def test_build_edit_workspace_context_modal():
    modal = build_edit_workspace_context_modal(current_context="Existing rules")
    assert modal["type"] == "modal"
    assert modal["callback_id"] == "submit_workspace_context_modal"

    input_block = next(
        (b for b in modal["blocks"] if b.get("block_id") == "workspace_context_input_block"),
        None,
    )
    assert input_block is not None
    element = input_block["element"]
    assert element["type"] == "plain_text_input"
    assert element["action_id"] == "workspace_context_input"
    assert element["max_length"] == 1000
    assert element["initial_value"] == "Existing rules"


@pytest.mark.anyio
async def test_interactivity_open_modal_and_submit(monkeypatch):
    from adapters.slack.interactivity import handle_slack_interactivity
    from fastapi import BackgroundTasks, Request

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_ADMIN_123")
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda *args, **kwargs: True)

    mock_views_open = AsyncMock()
    monkeypatch.setattr("services.slack_client.slack_async_client.views_open", mock_views_open)
    monkeypatch.setattr("adapters.slack.interactivity.slack_async_client.views_open", mock_views_open)

    # 1. Non-admin tries to click open_edit_workspace_context_modal
    non_admin_payload = {
        "type": "block_actions",
        "user": {"id": "U_NON_ADMIN"},
        "trigger_id": "trig_111",
        "actions": [{"action_id": "open_edit_workspace_context_modal"}],
    }
    req = MagicMock(spec=Request)
    req.body = AsyncMock(return_value=json.dumps(non_admin_payload).encode("utf-8"))
    req.headers = {}
    bg = BackgroundTasks()
    res = await handle_slack_interactivity(req, bg)
    assert res.status_code == 403
    mock_views_open.assert_not_called()

    # 2. Admin clicks open_edit_workspace_context_modal
    admin_payload = {
        "type": "block_actions",
        "user": {"id": "U_ADMIN_123"},
        "trigger_id": "trig_222",
        "actions": [{"action_id": "open_edit_workspace_context_modal"}],
    }
    req = MagicMock(spec=Request)
    req.body = AsyncMock(return_value=json.dumps(admin_payload).encode("utf-8"))
    req.headers = {}
    bg = BackgroundTasks()
    res = await handle_slack_interactivity(req, bg)
    assert res.status_code == 200
    await bg()
    mock_views_open.assert_called_once()
    call_kwargs = mock_views_open.call_args.kwargs
    assert call_kwargs["trigger_id"] == "trig_222"
    assert call_kwargs["view"]["callback_id"] == "submit_workspace_context_modal"

    # 3. Non-admin tries to submit modal
    submit_non_admin = {
        "type": "view_submission",
        "user": {"id": "U_NON_ADMIN"},
        "view": {
            "callback_id": "submit_workspace_context_modal",
            "state": {
                "values": {
                    "workspace_context_input_block": {
                        "workspace_context_input": {"value": "Malicious override"}
                    }
                }
            },
        },
    }
    req = MagicMock(spec=Request)
    req.body = AsyncMock(return_value=json.dumps(submit_non_admin).encode("utf-8"))
    req.headers = {}
    bg = BackgroundTasks()
    res = await handle_slack_interactivity(req, bg)
    assert res.status_code == 403
    assert get_workspace_context() == ""

    # 4. Admin submits valid modal
    submit_admin = {
        "type": "view_submission",
        "user": {"id": "U_ADMIN_123"},
        "view": {
            "callback_id": "submit_workspace_context_modal",
            "state": {
                "values": {
                    "workspace_context_input_block": {
                        "workspace_context_input": {"value": "Valid company rules."}
                    }
                }
            },
        },
    }
    req = MagicMock(spec=Request)
    req.body = AsyncMock(return_value=json.dumps(submit_admin).encode("utf-8"))
    req.headers = {}
    bg = BackgroundTasks()
    res = await handle_slack_interactivity(req, bg)
    assert res.status_code == 200
    assert get_workspace_context() == "Valid company rules."

    # 5. Admin submits exceeding 1000 characters
    submit_admin_too_long = {
        "type": "view_submission",
        "user": {"id": "U_ADMIN_123"},
        "view": {
            "callback_id": "submit_workspace_context_modal",
            "state": {
                "values": {
                    "workspace_context_input_block": {
                        "workspace_context_input": {"value": "X" * 1050}
                    }
                }
            },
        },
    }
    req = MagicMock(spec=Request)
    req.body = AsyncMock(return_value=json.dumps(submit_admin_too_long).encode("utf-8"))
    req.headers = {}
    bg = BackgroundTasks()
    res = await handle_slack_interactivity(req, bg)
    assert res.status_code == 200
    resp_data = json.loads(res.body.decode("utf-8"))
    assert resp_data["response_action"] == "errors"
    assert "workspace_context_input_block" in resp_data["errors"]
