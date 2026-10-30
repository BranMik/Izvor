# Directives & Slash Command Syntax User Guide

This guide details both the slash command and bracketed directive syntax, parameter clamping boundaries, Slack Admin privilege gates, and natural language controls supported by **Izvor** for steering multi-agent debates in Slack.

---

## 🔤 Dual Syntax: Slash Commands & Bracketed Directives

Users can control debates using either slash commands or bracketed directives embedded within messages. Multiple directives can be combined in a single turn.

### 1. Word Count Constraints
Control verbosity and prevent runaway token expenditure. `max_words` is strictly clamped to $\le 2000$ words.

| Slash Command | Bracket Directive Syntax | Clamping Limits | Privilege Level | Description |
|---|---|---|---|---|
| `/max_words 150` (or `/max 150`) | `[max_words: 150]` | $10 \le N \le 2000$ | Standard (`off` = **Slack Admin**) | Enforces an upper limit of words per persona turn. |
| `/min_words 75` (or `/min 75`) | `[min_words: 75]` | $1 \le N \le 1000$ | Standard (`off` = **Slack Admin**) | Requires personas to elaborate with at least $N$ words. |
| `/scribe_max_words 400` | `[scribe_max_words: 400]` | $10 \le N \le 2000$ | Standard (`off` = **Slack Admin**) | Restricts only the final Scribe synthesis word count. |
| `/scribe_min_words 200` | `[scribe_min_words: 200]` | $1 \le N \le 1000$ | Standard (`off` = **Slack Admin**) | Ensures the final Scribe synthesis has at least $N$ words. |

> [!NOTE]
> **Word Limit Grace Buffer**: To prevent cutting off concluding thoughts mid-sentence, the post-generation truncation filter applies a configurable `word_limit_grace_buffer` (default: 50 words, managed in system settings). Truncation only occurs if the response exceeds `max_words + word_limit_grace_buffer`.

*Examples:*
```
/max 120 Evaluate using Stripe vs. Adyen for international multi-currency processing.
```
```
@Izvor [max_words: 120] [min_words: 60] What are the tradeoffs between CockroachDB and Spanner?
```

---

### 2. Discussion Rounds & History Slicing
Control how many debate cycles take place before synthesis, and slice conversational history.

| Slash Command | Bracket Directive Syntax | Limits & Bounds | Privilege Level |
|---|---|---|---|
| `/rounds 2` | `[rounds: 2]` | Clamped $1 \le N \le 5$ (Global cap: 20 turns) | **Slash Origin Only** |
| `/history 15` | `[history: 15]` | Clamped $1 \le N \le 50$ | Standard |

> [!IMPORTANT]
> To prevent adversarial prompt injection from escalating debate costs or locking the thread in an infinite loop, `/rounds` directives require explicit slash or bracketed syntax (`origin: "slash"`). When `/history N` slices earlier messages, Izvor automatically injects the Scribe's `<episodic_thread_summary>` (Tier 2 memory) so historical context is preserved.

---

### 3. Agent Inclusion, Exclusion & Speaker Selection
Selectively invite personas, dismiss personas, designate the opening speaker, or trigger an open dynamic debate:

| Slash Command | Bracket Directive Syntax | Privilege Level | Effect |
|---|---|---|---|
| `/include SecurityArchitect, LegalAdvisor` | `[include: SecurityArchitect, LegalAdvisor]` | Standard (`off` = **Slack Admin**) | Adds specific personas to the active debate pool. |
| `/exclude Sceptic` | `[exclude: Sceptic]` | Standard (`off` = **Slack Admin**) | Excludes a persona from participating in this thread. |
| `/first Visionary` | `[first: Visionary]` | Standard | Designates the opening speaker, continuing in normal workflow order. |
| `/open_debate Visionary` | `[open_debate: Visionary]` | Standard | Designates opening speaker and runs an open debate without fixed start/end chains. |

---

### 4. Dynamic Tool & Skill Assignment (Slack Admin Gated)
Workspace administrators (`SLACK_ADMIN_USER_IDS`) can grant or revoke tool capabilities dynamically in an active thread, or clear active thread constraints:

| Slash Command | Bracket Directive Syntax | Privilege Required |
|---|---|---|
| `/grant_skill web_research to Visionary` | `[grant: web_research to Visionary]` | **Slack Admin + Slash Origin** |
| `/grant_skill_for_all web_research` (or `/grant_for_all`) | `[grant_all: web_research]` | **Slack Admin + Slash Origin** |
| `/revoke_skill_for_all web_research` (or `/revoke_for_all`) | `[revoke_all: web_research]` | **Slack Admin + Slash Origin** |
| `/revoke_constraint max_words` (or `/revoke max_words`) | `[revoke_constraint: max_words]` | **Slack Admin + Slash Origin** |

> [!CAUTION]
> Enforced by `require_directive_admin` in [`orchestration/stages/directive_handlers.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/stages/directive_handlers.py), `/grant_skill`, `/grant_skill_for_all`, `/revoke_skill_for_all`, `/revoke_constraint`, and disabling constraints via `=off` require **Slack Workspace Admin** privileges (`is_slack_admin(user_id)`) AND explicit slash invocation (`origin: "slash"`). Unauthorized attempts are rejected and emit an `UNAUTHORIZED_DIRECTIVE_ATTEMPT` security audit log; authorized executions emit `ADMIN_DIRECTIVE_EXECUTED`.

---

### 5. Control & Utility Directives

| Slash Command | Bracket Directive Syntax | Effect |
|---|---|---|
| `/stop` | `[stop]` or `[stop_discussion]` | Immediately halts automated agent debate turns in the thread. |
| `/list_skills` (or `/skills`) | `[list_skills]` | Posts an in-thread summary of all 21 registered skills and tool schemas. |

---

## 🗣️ Unified Pre-Flight Intent & Language Extraction

Izvor's [`nlp/intent_extractor.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/nlp/intent_extractor.py) extracts both safe conversational directives and the user's `detected_language` in a single pre-flight LLM pass:

- *"Please keep all answers under 100 words"* ➔ Automatically parses as `max_words = 100` (`origin: "inferred"`).
- *"Stop the debate"* ➔ Automatically triggers `stop_discussion`.
- *"Have the security architect look at this"* ➔ Automatically includes and routes to `SecurityArchitect`.

**Privilege Boundary Safeguard**: Directives inferred from natural language are tagged with `origin: "inferred"`. If an inferred directive attempts to invoke privileged actions (e.g., granting tools or setting round limits), the pipeline fails closed, ignores the privileged command, and logs an audit warning.
