# Slack App Home Administration & Settings SSOT

This document details the administrative interface provided via the **Slack App Home** tab, how real-time configuration is managed, how server-side authorization is enforced, and how state persistence and backup failovers operate.

---

## 🏠 The App Home Interface

Izvor provides an interactive configuration dashboard embedded directly within Slack’s App Home tab. Administrators can modify the deliberation engine's behavior in real time without restarting the server or editing config files manually.

```mermaid
graph TD
    SlackUser([Slack User]) -->|Opens App Home / Interacts| SlackAPI[Slack Platform]
    SlackAPI -->|POST /slack/interactivity| InteractivityHandler["adapters/slack/interactivity.py"]
    InteractivityHandler --> AdminGate{is_slack_admin(user_id)?}
    AdminGate -->|No| RejectBlock[Ignore / Post Ephemeral Access Denied]
    AdminGate -->|Yes| SettingsMgr["orchestration/agent_settings.py (SSOT)"]
    
    subgraph Persistence & Durability
        SettingsMgr --> AtomicWrite["Atomic File Write to data/settings/agent_settings.json"]
        AtomicWrite --> EmergencyBackup["Rolling Emergency Backup (data/backups/)"]
    end
    
    SettingsMgr --> RePublish["Re-render & Publish Updated App Home View"]
```

---

## 🛡️ Strict Server-Side Access Control

To prevent unauthorized workspace members from tampering with agent settings, model selections, or budget circuit breakers:
- **`is_slack_admin(user_id)` Gate**: Defined in [`core/config.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/core/config.py), resolving authorized admin IDs from `ADMIN_SLACK_USER_IDS` or `ADMIN_SLACK_USER_ID` in `.env`.
- **Zero Client Trust**: All interactive actions dispatched to [`adapters/slack/interactivity.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/adapters/slack/interactivity.py) (toggling personas, switching workflows, updating budget limits, approving HITL requests, or triggering restores) verify `is_slack_admin(user_id)` server-side.
- **Fail-Closed**: Non-admin users are strictly blocked from triggering administrative mutations, and all unauthorized attempts trigger `AUTH_FAILURE` audit logs.

---

## 🎛️ Configurable Controls

The App Home view ([`services/slack_home_views.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/services/slack_home_views.py)) organizes controls into structured blocks (strictly capped at <100 blocks to comply with Slack Block Kit limits):

### 1. Active Personas Roster
- **Multi-Select Checkboxes**: Enable or disable any of the 16 debate personas.
- **Model Overrides**: Assign custom LLM models per persona.
- **Concise Output Toggles**: Toggle `is_short_output` for personas to keep their arguments brief.

### 2. Discussion Topology & Workflows
- **Topology Selection**: Switch between **Sandwich Mode** and **Fixed Mode**.
- **Start & End Chains**: Specify which personas open and close debates in Sandwich mode.
- **Preset Selection**: Load curated workflow presets (e.g., Architecture Review, Product Strategy, Quick Brainstorm).

### 3. Debate Limits & Pacing
- **Maximum Discussion Rounds**: Limit total turns before closing synthesis begins.
- **Word Count Caps**: Set global default minimum and maximum word counts.

### 4. Spend Governance & Circuit Breakers
- **Circuit Breaker Toggle**: Turn budget limits on or off (enabled by default).
- **Per-Thread USD Spend Limit**: Define the dollar threshold (`circuit_breaker_threshold_usd`) that triggers automatic discussion pause.
- **Daily Workspace Budget**: Configure `daily_budget_usd` across all workspace threads.

---

## 💾 Single Source of Truth (SSOT) & Atomic Persistence

All configuration changes are handled through [`AgentSettingsManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/agent_settings.py):

### 1. Atomic Disk Writes
To prevent file corruption during concurrent updates:
- Settings are written to a temporary file (`agent_settings.json.tmp`).
- The temporary file is atomically replaced using `os.replace`.

### 2. Rolling Backups in `data/backups/`
Every modification triggers emergency backup checks in `data/backups/`:
- `data/backups/agent_settings_backup.json`
- `data/backups/app_settings_backup.json`
- `data/backups/system_settings_backup.json`
Backups are updated on periodic intervals (7-day emergency cycle) or upon critical administrative mutations.

### 3. Non-Destructive Failover
If `data/settings/agent_settings.json` is missing or corrupted on startup, Izvor automatically recovers by inspecting the latest valid snapshot in `data/backups/` before falling back to default factory presets.
