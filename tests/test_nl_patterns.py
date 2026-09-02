import pytest
from nlp.nl_patterns import (
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

    # Additional English history patterns
    limit, text = extract_history_limit("consider the previous 5 messages in this discussion")
    assert limit == 5
    assert text == "in this discussion"

    limit, text = extract_history_limit("limit history to 6 messages")
    assert limit == 6

    limit, text = extract_history_limit("context limit: 4 messages")
    assert limit == 4

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

    # Additional English max_words patterns
    limit, text = extract_max_words_limit("reply in under 50 words please")
    assert limit == 50
    assert text == "please"

    limit, text = extract_max_words_limit("word limit: 90 words")
    assert limit == 90

    limit, text = extract_max_words_limit("keep it below 40 words")
    assert limit == 40

    limit, text = extract_max_words_limit("no more than 75 words")
    assert limit == 75


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

    # Additional English min_words patterns
    limit, text = extract_min_words_limit("write at least 200 words about the topic")
    assert limit == 200
    assert text == "write about the topic"

    limit, text = extract_min_words_limit("minimum length: 150 words")
    assert limit == 150

    limit, text = extract_min_words_limit("reply with more than 80 words")
    assert limit == 80

    limit, text = extract_min_words_limit("no fewer than 110 words")
    assert limit == 110


def test_nl_file_instruction_extraction():
    # Natural language triggers
    is_req, target, text = extract_file_instruction("molim te pogledaj privitak i komentiraj")
    assert is_req is True
    assert text == "molim te pogledaj privitak i komentiraj"

    is_req, target, text = extract_file_instruction("u priloženom fajlu je definicija arhitekture")
    assert is_req is True

    is_req, target, text = extract_file_instruction("please read the uploaded file and analyze")
    assert is_req is True

    # Additional English file triggers
    is_req, target, text = extract_file_instruction("summarize the attached file")
    assert is_req is True

    is_req, target, text = extract_file_instruction("I attached the code for you to inspect")
    assert is_req is True

    is_req, target, text = extract_file_instruction("refer to the attachment for more details")
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


def test_resolve_agent_name():
    from nlp.nl_patterns import resolve_agent_name

    assert resolve_agent_name("[MistLar_TechLead]") == "MistLar_TechLead"
    assert resolve_agent_name("MistLar_TechLead") == "MistLar_TechLead"
    assert resolve_agent_name("mistlar_techlead") == "MistLar_TechLead"
    assert resolve_agent_name("MistLar TechLead") == "MistLar_TechLead"
    assert resolve_agent_name("TechLead") == "MistLar_TechLead"
    assert resolve_agent_name("[AnthrOpus_Innovator]") == "AnthrOpus_Innovator"
    assert resolve_agent_name("Innovator") == "AnthrOpus_Innovator"
    assert resolve_agent_name("AnthrOpus Sceptic") == "AnthrOpus_Sceptic"
    assert resolve_agent_name("Gemini_Visionary") == "Gemini_Visionary"
    assert resolve_agent_name("NonExistentAgent") is None
    assert resolve_agent_name("") is None


def test_exclude_slash_command_single_and_multi():
    from nlp.nl_patterns import extract_excluded_agents

    # Single agent slash command
    exc, text = extract_excluded_agents("/exclude [MistLar_TechLead] Debate this feature")
    assert exc == ["MistLar_TechLead"]
    assert text == "Debate this feature"

    # Multi-agent syntax: /exclude [Agent1] [Agent2] [Agent3]
    exc, text = extract_excluded_agents(
        "/exclude [MistLar_TechLead] [AnthrOpus_Innovator] [Gemini_ScopeOptimizer] Plan the sprint"
    )
    assert exc == [
        "MistLar_TechLead",
        "AnthrOpus_Innovator",
        "Gemini_ScopeOptimizer",
    ]
    assert text == "Plan the sprint"

    # Comma-separated syntax
    exc, text = extract_excluded_agents(
        "/exclude [MistLar_TechLead], [AnthrOpus_Innovator] Plan the sprint"
    )
    assert exc == ["MistLar_TechLead", "AnthrOpus_Innovator"]
    assert text == "Plan the sprint"

    # Colon syntax
    exc, text = extract_excluded_agents("/exclude: [MistLar_TechLead] test")
    assert exc == ["MistLar_TechLead"]
    assert text == "test"

    # Unbracketed names
    exc, text = extract_excluded_agents("/exclude MistLar_TechLead AnthrOpus_Innovator test")
    assert exc == ["MistLar_TechLead", "AnthrOpus_Innovator"]
    assert text == "test"

    # Multiple separate /exclude commands
    exc, text = extract_excluded_agents(
        "/exclude [MistLar_TechLead] Some text /exclude [AnthrOpus_Innovator]"
    )
    assert exc == ["MistLar_TechLead", "AnthrOpus_Innovator"]
    assert text == "Some text"


def test_exclude_natural_language():
    from nlp.nl_patterns import extract_excluded_agents

    # English NL
    exc, text = extract_excluded_agents("Debate this feature, but exclude MistLar_TechLead from debate")
    assert exc == ["MistLar_TechLead"]
    assert "exclude" not in text

    exc, text = extract_excluded_agents("Leave out [AnthrOpus_Innovator] and discuss architecture")
    assert exc == ["AnthrOpus_Innovator"]
    assert "Leave out" not in text

    exc, text = extract_excluded_agents("Discuss without [Gemini_ScopeOptimizer]")
    assert exc == ["Gemini_ScopeOptimizer"]

    # Croatian NL
    exc, text = extract_excluded_agents("Raspravite o temi bez agenta MistLar_TechLead")
    assert exc == ["MistLar_TechLead"]
    assert "bez agenta" not in text

    exc, text = extract_excluded_agents("Isključi [Gemini_ScopeOptimizer] iz rasprave o bazi")
    assert exc == ["Gemini_ScopeOptimizer"]
    assert "Isključi" not in text

    exc, text = extract_excluded_agents("Nemoj uključiti agenta MistLar_QALead")
    assert exc == ["MistLar_QALead"]

    # Regular prompt without exclusions
    exc, text = extract_excluded_agents("Just a standard user prompt with no directives")
    assert exc == []
    assert text == "Just a standard user prompt with no directives"
