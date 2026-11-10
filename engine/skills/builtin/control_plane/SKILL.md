---
name: control_plane
display_name: Discussion Control Plane
description: Explicit structured tools for speaker nomination, user clarification requests, and consensus convergence signaling.
scope: orchestrator
global: true
triggers:
  auto_detect: false
tools:
  - nominate_next_speaker
  - request_user_clarification
  - signal_consensus_convergence
requires_human_ITL: false
requires_admin: false
---

# Discussion Control Plane Skill

## Purpose & When to Use
Use these control plane tools to explicitly steer the multi-agent discussion flow rather than embedding unstructured regex text cues:
- `nominate_next_speaker`: Directly nominate a peer specialist agent with a specific rationale or question.
- `request_user_clarification`: Pause the autonomous agent chain to request clarification or decisions from the human user in Slack.
- `signal_consensus_convergence`: Signal that the core problem has been resolved and consensus has emerged, transitioning to final summary.

## Tool Signatures & Guidelines
1. `nominate_next_speaker(agent_name: str, rationale: str)`
   - Call this when you want to pass the floor to a specific peer specialist agent who should answer or build upon your points.
2. `request_user_clarification(question: str)`
   - Call this when critical information, requirements, or approvals from the human user are missing. Your turn will conclude, the question will be posted to the user, and the agent chain will wait for user reply.
3. `signal_consensus_convergence(summary_of_agreement: str, dissenting_points: str | None = None)`
   - Call this when participating agents have reached a firm conclusion or agreement, indicating the discussion is ready for conclusion synthesis.
