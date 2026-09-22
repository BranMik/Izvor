"""
Tests for Control Hub Thread Skill Expiration Time section and Interactivity.
"""

from orchestration.app_settings import (
    get_thread_skill_expiration_time,
    set_thread_skill_expiration_time,
)
from services.slack_client import build_app_home_view


def test_control_hub_blocks_contain_thread_skill_expiry():
    set_thread_skill_expiration_time(12.0)
    view = build_app_home_view()
    blocks = view["blocks"]

    # Verify section text
    expiry_section = next((b for b in blocks if "Thread Global Skills Expiration Time" in str(b)), None)
    assert expiry_section is not None
    assert "12.0 hours" in expiry_section["text"]["text"]

    # Verify preset buttons (1h, 4h, 6h, 12h, 24h)
    action_block = next((b for b in blocks if b.get("block_id", "").startswith("thread_skill_expiry_actions_")), None)
    assert action_block is not None
    action_ids = [el["action_id"] for el in action_block["elements"]]
    assert "set_thread_skill_expiry_preset:1.0" in action_ids
    assert "set_thread_skill_expiry_preset:4.0" in action_ids
    assert "set_thread_skill_expiry_preset:6.0" in action_ids
    assert "set_thread_skill_expiry_preset:12.0" in action_ids
    assert "set_thread_skill_expiry_preset:24.0" in action_ids

    # 12h should be active style
    btn_12h = next(el for el in action_block["elements"] if el["action_id"] == "set_thread_skill_expiry_preset:12.0")
    assert btn_12h.get("style") == "primary"
    assert "(active)" in btn_12h["text"]["text"]


def test_change_thread_skill_expiry_reflects_in_control_hub():
    set_thread_skill_expiration_time(6.0)
    assert get_thread_skill_expiration_time() == 6.0

    view = build_app_home_view()
    blocks = view["blocks"]
    action_block = next(b for b in blocks if b.get("block_id", "").startswith("thread_skill_expiry_actions_"))

    btn_6h = next(el for el in action_block["elements"] if el["action_id"] == "set_thread_skill_expiry_preset:6.0")
    assert btn_6h.get("style") == "primary"
    assert "(active)" in btn_6h["text"]["text"]

    # Reset back to default 12h
    set_thread_skill_expiration_time(12.0)
