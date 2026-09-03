---
name: sandwich-orchestrator
description: Multi-agent debate workflow topology engine supporting sandwich mode (start/middle/end), fixed pipelines, and cognitive moderator turn selection.
---

# Sandwich Orchestrator Skill

## Purpose
Coordinates multi-turn multi-agent discussions across complex architectural, operational, and domain review panels.

## Architecture
The skill implements a **Hybrid Execution Model**:

### 1. Deterministic Fast-Path (0ms, $0 cost)
* **Start Chain**: Executes deterministic framing specialists (e.g. `Architect` -> `ScopeOptimizer` -> `Visionary`).
* **End Chain**: Executes deterministic synthesis specialists (e.g. `Architect`).
* **Fixed Mode**: Strictly follows the configured chain sequence without LLM overhead.

### 2. Cognitive Dynamic-Path (~500ms, ~$0.0002 cost)
* **Middle Pool**: When open specialists (`SecurityArchitect`, `TechLead`, `ROIStrategist`, `UserAdvocate`, etc.) remain, invokes `mistral-small-latest` as the moderator to contextually pick the next best specialist.
* **Consensus Detection**: Periodically evaluates whether the team has reached consensus to skip remaining debate turns and jump directly to closing synthesis.
