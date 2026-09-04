import pytest

from unittest.mock import MagicMock, patch

from nlp.language_detector import (
    detect_language,
    detect_language_llm,
    normalize_language_name,
)
from nlp.message_builder import build_llm_messages
from orchestration.thread_state import (
    clear_thread_language,
    clear_thread_state,
    get_thread_language,
    set_thread_language,
    thread_state_manager,
)


def test_detect_language_english():
    text = "What are the architectural trade-offs of microservices vs modular monoliths?"
    assert detect_language(text) == "English"

    short_eng = "Can you please check this error in the log and tell me why it failed?"
    assert detect_language(short_eng) == "English"


def test_detect_language_croatian_with_diacritics():
    text = "Kako funkcionira ovaj sustav i što preporučaš za rješavanje problema?"
    assert detect_language(text) == "Croatian"

    text2 = "Provjeri je li baza podataka dobro optimizirana za veće opterećenje."
    assert detect_language(text2) == "Croatian"


def test_detect_language_croatian_without_diacritics():
    text = "moze li netko reci kako ovo radi i da li je sve spremno za produkciju"
    assert detect_language(text) == "Croatian"

    text2 = "daj napravi analizu koda i reci nam sto treba popraviti prije deploya"
    assert detect_language(text2) == "Croatian"


def test_detect_language_short_2_3_word_prompts():
    # Croatian 2-3 word prompts with stems and suffixes (-ura, -cija, etc.)
    croatian_short_prompts = [
        "arhitektura sustava",
        "analiza koda",
        "usporedi rjesenja",
        "optimizacija baze",
        "struktura projekta",
        "faktura za klijenta",
        "daj analizu",
        "sto mislis",
        "provjeri ovo",
    ]
    for p in croatian_short_prompts:
        assert detect_language(p) == "Croatian", f"Expected Croatian for '{p}', got {detect_language(p)}"

    # English 2-3 word prompts
    english_short_prompts = [
        "show me code",
        "what is this",
        "explain architecture",
        "system design tradeoffs",
        "database optimization",
        "code architecture",
    ]
    for p in english_short_prompts:
        assert detect_language(p) == "English", f"Expected English for '{p}', got {detect_language(p)}"


def test_detect_language_other_languages():
    german = "Wie funktioniert dieses System und was sind die Hauptvorteile für uns?"
    assert detect_language(german) == "German"

    spanish = "¿Cómo funciona este sistema y cuáles son las principales ventajas para el proyecto?"
    assert detect_language(spanish) == "Spanish"

    french = "Comment fonctionne ce système et quels sont les avantages pour notre équipe?"
    assert detect_language(french) == "French"


def test_detect_language_fallbacks():
    assert detect_language("") == "English"
    assert detect_language("   ") == "English"
    assert detect_language("12345 67890") == "English"
    assert detect_language("SELECT * FROM users WHERE id = 1;") == "English"


def test_thread_state_language_management():
    channel_id = "C_LANG_TEST"
    thread_ts = "1700000000.123456"

    # Initially None
    assert get_thread_language(channel_id, thread_ts) is None

    # Set and lock language
    set_thread_language(channel_id, thread_ts, "Croatian")
    assert get_thread_language(channel_id, thread_ts) == "Croatian"

    # Clear language
    clear_thread_language(channel_id, thread_ts)
    assert get_thread_language(channel_id, thread_ts) is None

    # Test cleanup_thread
    set_thread_language(channel_id, thread_ts, "English")
    thread_state_manager.cleanup_thread(channel_id, thread_ts)
    assert get_thread_language(channel_id, thread_ts) is None


def test_build_llm_messages_with_thread_language():
    system_prompt = "You are an assistant."
    effective_history = []
    prompt = "Analyze system architecture."
    agent_cfg = {"username": "TechLead"}

    # 1. Bot trigger with English
    msgs_eng = build_llm_messages(
        system_prompt=system_prompt,
        effective_history=[{"role": "user", "content": "Hello"}],
        prompt=prompt,
        is_bot_trigger=True,
        agent_key="TechLead",
        agent_cfg=agent_cfg,
        thread_language="English",
    )
    user_msg_eng = msgs_eng[-1]["content"]
    assert "strictly in English" in user_msg_eng
    assert "German" not in user_msg_eng
    assert "Croatian" not in user_msg_eng

    # 2. Bot trigger with Croatian
    msgs_cro = build_llm_messages(
        system_prompt=system_prompt,
        effective_history=[{"role": "user", "content": "Bok"}],
        prompt="",
        is_bot_trigger=True,
        agent_key="TechLead",
        agent_cfg=agent_cfg,
        thread_language="Croatian",
    )
    user_msg_cro = msgs_cro[-1]["content"]
    assert "strictly in Croatian" in user_msg_cro
    assert "German" not in user_msg_cro


def test_normalize_language_name():
    assert normalize_language_name("croatian") == "Croatian"
    assert normalize_language_name("hrvatski") == "Croatian"
    assert normalize_language_name("ENGLISH") == "English"
    assert normalize_language_name("deutsch") == "German"
    assert normalize_language_name("español") == "Spanish"
    assert normalize_language_name("francais") == "French"
    assert normalize_language_name("`German`.") == "German"
    assert normalize_language_name("japanese") == "Japanese"


@pytest.mark.anyio
async def test_detect_language_llm_fast_path_diacritics():
    # Croatian diacritics fast path should return Croatian without calling litellm
    text = "Kako funkcionira ovaj sustav i što preporučaš za rješavanje problema?"
    with patch("litellm.completion") as mock_comp:
        res = await detect_language_llm(text)
        assert res == "Croatian"
        mock_comp.assert_not_called()


@pytest.mark.anyio
async def test_detect_language_llm_mocked_success():
    text = "analiza sustava"
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Croatian"
    mock_resp.choices = [mock_choice]

    with patch("litellm.completion", return_value=mock_resp):
        res = await detect_language_llm(text)
        assert res == "Croatian"


@pytest.mark.anyio
async def test_detect_language_llm_fallback_on_error():
    # When litellm fails/times out, cleanly fall back to heuristic detector
    text = "wie geht es dir heute mein freund"
    with patch("litellm.completion", side_effect=Exception("API connection timeout")):
        res = await detect_language_llm(text)
        assert res == "German"

