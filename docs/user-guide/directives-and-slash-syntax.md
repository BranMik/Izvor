# Directives & Syntax User Guide

This guide details the bracketed directive syntax and natural language controls supported by **Izvor** for steering multi-agent debates in Slack.

---

## 🔤 Bracket Directive Syntax

Users can include bracketed directives anywhere within their message text. Multiple directives can be combined in a single message.

### 1. Word Count Constraints
Control verbosity and prevent overly long responses:

| Directive Syntax | Effect |
|---|---|
| `[max_words: 150]` | Enforces an upper limit of 150 words per persona turn. |
| `[min_words: 75]` | Requires personas to elaborate with at least 75 words. |
| `[scribe_max_words: 400]` | Restricts only the final Scribe synthesis to 400 words. |
| `[scribe_min_words: 200]` | Ensures the final Scribe synthesis has at least 200 words. |

*Example:*
```
@Izvor [max_words: 120] Evaluate using Stripe vs. Adyen for international multi-currency processing.
```

---

### 2. Discussion Rounds
Control how many debate cycles take place before synthesis:

| Directive Syntax | Effect |
|---|---|
| `[rounds: 2]` | Limits the debate to 2 rounds before triggering the closing chain. |
| `[rounds: 1]` | Each participating persona speaks once, followed by immediate closure. |

---

### 3. Agent Inclusion & Exclusion
Selectively invite or dismiss personas from a thread:

| Directive Syntax | Effect |
|---|---|
| `[include: SecurityArchitect, LegalAdvisor]` | Adds specific personas to the thread roster. |
| `[exclude: Sceptic]` | Excludes a persona from participating in this thread. |

*Example:*
```
@Izvor [include: LegalAdvisor, CompliancePrivacyAndGovernanceOfficer] [exclude: Innovator]
Review our GDPR data retention and deletion proposal.
```

---

### 4. Dynamic Tool & Skill Assignment
Grant or revoke tool capabilities on the fly:

| Directive Syntax | Effect |
|---|---|
| `[grant: web_research to Visionary]` | Grants internet research tool exclusively to `Visionary`. |
| `[grant_all: web_research]` | Grants internet research tool to all participating personas. |
| `[revoke: web_research from Visionary]` | Removes tool access from `Visionary`. |
| `[revoke_all: web_research]` | Revokes tool access from all personas in the thread. |
| `[revoke_constraint: max_words]` | Clears previously set word count caps. |

---

### 5. Control & Utility Directives

| Directive Syntax | Effect |
|---|---|
| `[stop_discussion]` or `[stop]` | Immediately halts automated agent debate turns in the thread. |
| `[list_skills]` | Posts an in-thread summary of all registered skills and tool schemas. |

---

## 🗣️ Natural Language Intent Extraction

If you prefer natural phrasing over bracketed syntax, Izvor's [`nlp/intent_extractor.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/nlp/intent_extractor.py) extracts directives automatically from conversational English:

- *"Please keep all answers under 100 words"* ➔ Automatically parses as `[max_words: 100]`.
- *"Limit the discussion to 2 rounds"* ➔ Automatically parses as `[rounds: 2]`.
- *"Stop the debate"* ➔ Automatically triggers `[stop_discussion]`.
- *"Have the security architect look at this"* ➔ Automatically includes and routes to `SecurityArchitect`.
