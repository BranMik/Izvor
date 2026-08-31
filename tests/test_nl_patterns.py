import pytest
from nl_patterns import (
    HISTORY_COMMAND_PATTERN,
    HISTORY_NL_PATTERNS,
    MAX_WORDS_COMMAND_PATTERN,
    MAX_WORDS_NL_PATTERNS,
    MIN_WORDS_COMMAND_PATTERN,
    MIN_WORDS_NL_PATTERNS,
    FILE_COMMAND_PATTERN,
    FILE_NL_PATTERNS,
    STOP_COMMAND_PATTERN,
    extract_history_limit,
    extract_max_words_limit,
    extract_min_words_limit,
    extract_file_instruction,
    is_stop_command,
)


def test_nl_history_extraction():
    limit, text = extract_history_limit("uzmi u obzir zadnjih 5 poruka u raspravi")
    assert limit == 5
    assert text == "u raspravi"

    limit, text = extract_history_limit("last 3 messages please")
    assert limit == 3
    assert text == "please"

    limit, text = extract_history_limit("povijest od 4 poruke")
    assert limit == 4
    assert text == ""

    # Slash command takes precedence if both present
    limit, text = extract_history_limit("/history 2 uzmi u obzir zadnjih 5 poruka")
    assert limit == 2


def test_nl_max_words_extraction():
    limit, text = extract_max_words_limit("odgovori u maksimalno 100 riječi na pitanje")
    assert limit == 100
    assert text == "odgovori na pitanje"

    limit, text = extract_max_words_limit("u najviše 50 riječi objasni")
    assert limit == 50
    assert text == "objasni"

    limit, text = extract_max_words_limit("in max 120 words please")
    assert limit == 120
    assert text == "please"

    limit, text = extract_max_words_limit("up to 80 words summarize")
    assert limit == 80
    assert text == "summarize"


def test_nl_min_words_extraction():
    limit, text = extract_min_words_limit("odgovori u minimalno 120 riječi o bazi")
    assert limit == 120
    assert text == "odgovori o bazi"

    limit, text = extract_min_words_limit("barem 150 riječi detaljno opiši")
    assert limit == 150
    assert text == "detaljno opiši"

    limit, text = extract_min_words_limit("at least 100 words detailed analysis")
    assert limit == 100
    assert text == "detailed analysis"


def test_nl_file_instruction_extraction():
    # Natural language triggers
    is_req, target, text = extract_file_instruction("molim te pogledaj privitak i komentiraj")
    assert is_req is True
    assert text == "molim te pogledaj privitak i komentiraj"

    is_req, target, text = extract_file_instruction("u priloženom fajlu je definicija arhitekture")
    assert is_req is True

    is_req, target, text = extract_file_instruction("please read the uploaded file and analyze")
    assert is_req is True

    # Slash command with clean group extraction
    is_req, target, text = extract_file_instruction("/file: schema.sql Provjeri indekse")
    assert is_req is True
    assert target == "schema.sql"
    assert text == "Provjeri indekse"


def test_stop_command_detection():
    assert is_stop_command("/stop") is True
    assert is_stop_command("Please /stop the discussion") is True
    assert is_stop_command("Don't stop now") is False
    assert is_stop_command("") is False
