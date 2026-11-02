# Security Architecture & Data Protection Policy

## Data Isolation & Sandboxing
- Documents placed in agent-specific directories are only retrievable by that agent persona.
- The shared directory is accessible across all active panel members.
- Credentials and API keys must never be committed to repository code or stored unencrypted in knowledge files.

## Incident Reporting
- Report suspected token leaks immediately to security leads.
- Invalidate compromised tokens within 15 minutes of detection.
