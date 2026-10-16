# Directives & Slash Command Syntax User Guide

This guide details both the slash command and bracketed directive syntax, parameter clamping boundaries, and natural language controls supported by **Izvor** for steering multi-agent debates in Slack.

---

## 🔤 Dual Syntax: Slash Commands & Bracketed Directives

Users can control debates using either slash commands or bracketed directives embedded within messages. Multiple directives can be combined in a single turn.

### 1. Word Count Constraints
Control verbosity and prevent runaway token expenditure. `max_words` is strictly clamped to $\le 2000$ words.

| Slash Command | Bracket Directive Syntax | Clamping Limits | Description |
|---|---|---|---|
| `/max_words 150` (or `/max 150`) | `[max_words: 150]` | $10 \le N \le 2000$ | Enforces an upper limit of words per persona turn. |
| `/min_words 75` (or `/min 75`) | `[min_words: 75]` | $1 \le N \le 1000$ | Requires personas to elaborate with at least $N$ words. |
| `/scribe_max_words 400` | `[scribe_max_words: 400]` | $10 \le N \le 2000$ | Restricts only the final Scribe synthesis word count. |
| `/scribe_min_words 200` | `[scribe_min_words: 200]` | $1 \le N \le 1000$ | Ensures the final Scribe synthesis has at least $N$ words. |

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
> To prevent adversarial prompt injection from escalating debate costs or locking the thread in an infinite loop, `/rounds` directives require explicit slash or bracketed syntax (`origin: "slash"`). They cannot be activated through inferred natural language chatter.

---

### 3. Agent Inclusion & Exclusion
Selectively invite or dismiss personas from a thread:

| Slash Command | Bracket Directive Syntax | Effect |
|---|---|---|
| `/include SecurityArchitect, LegalAdvisor` | `[include: SecurityArchitect, LegalAdvisor]` | Adds specific personas to the active debate pool. |
| `/exclude Sceptic` | `[exclude: Sceptic]` | Excludes a persona from participating in this thread. |

---

### 4. Dynamic Tool & Skill Assignment
Grant or revoke tool capabilities dynamically in an active thread:

| Slash Command | Bracket Directive Syntax | Privilege Required |
|---|---|---|
| `/grant_skill web_research to Visionary` | `[grant: web_research to Visionary]` | Standard |
| `/grant_skill_for_all web_research` (or `/grant_for_all`) | `[grant_all: web_research]` | **Slash Origin Only** |
| `/revoke_skill web_research from Visionary` | `[revoke: web_research from Visionary]` | Standard |
| `/revoke_skill_for_all web_research` (or `/revoke_for_all`) | `[revoke_all: web_research]` | **Slash Origin Only** |
| `/revoke_constraint max_words` (or `/revoke max_words`) | `[revoke_constraint: max_words]` | Standard |

> [!CAUTION]
> Global grants (`/grant_skill_for_all`) and revocations (`/revoke_skill_for_all`) are high-privilege operations persisted to SQLite `thread_skills` and audited. Inferred directives (`origin: "inferred"`) are strictly blocked from triggering them.

---

### 5. Control & Utility Directives

| Slash Command | Bracket Directive Syntax | Effect |
|---|---|---|
| `/stop` | `[stop]` or `[stop_discussion]` | Immediately halts automated agent debate turns in the thread. |
| `/list_skills` (or `/skills`) | `[list_skills]` | Posts an in-thread summary of all registered skills and tool schemas. |

---

## 🗣️ Natural Language Intent Extraction & Privilege Safeguards

Izvor's [`nlp/intent_extractor.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/nlp/intent_extractor.py) extracts safe directives automatically from conversational English:

- *"Please keep all answers under 100 words"* ➔ Automatically parses as `max_words = 100` (`origin: "inferred"`).
- *"Stop the debate"* ➔ Automatically triggers `stop_discussion`.
- *"Have the security architect look at this"* ➔ Automatically includes and routes to `SecurityArchitect`.

**Privilege Boundary Safeguard**: Directives inferred from natural language are tagged with `origin: "inferred"`. If an inferred directive attempts to invoke privileged actions (e.g., granting tools to all agents or setting round limits), the pipeline fails closed, ignores the privileged command, and logs an audit warning.
